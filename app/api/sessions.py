import logging
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import HydraSession, SessionEvent, SessionMetrics, Span, TriageResult
from app.schemas import (
    AssistRequest,
    CreateSessionRequest,
    DayCount,
    EventResponse,
    FailureReasonCount,
    MetricsSummary,
    SessionMetricsResponse,
    SessionResponse,
    SpanResponse,
    TimeBreakdown,
    TraceResponse,
    TriageResultResponse,
)
from app import workflow_registry

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Helper to query live state from Mistral Temporal ---

async def _query_live_state(workflow_run_id: str) -> dict | None:
    """Query workflow state from Mistral's Temporal server."""
    try:
        result = await workflow_registry.query_workflow(
            workflow_run_id, "get_status"
        )
        if result and hasattr(result, 'output'):
            return result.output
        return result
    except Exception as e:
        logger.debug("Query workflow %s failed: %s", workflow_run_id, e)
        return None


# --- Sessions ---

@router.post("/sessions", response_model=SessionResponse)
async def create_session(req: CreateSessionRequest, db: AsyncSession = Depends(get_db)):
    session = HydraSession(
        task_description=req.task_description,
        repo_url=req.repo_url,
        max_iterations=req.max_iterations,
        issue_number=req.issue_number,
        issue_type=req.issue_type,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    # Start the session workflow on Mistral's Temporal server
    try:
        from app.workflows.hydra_session import HydraSessionWorkflow, SessionInput

        parts = req.repo_url.rstrip("/").split("/")
        owner, repo = parts[-2], parts[-1]
        execution_id = f"session-{session.id}"

        await workflow_registry.start_workflow(
            HydraSessionWorkflow,
            SessionInput(
                session_id=session.id,
                repo_url=req.repo_url,
                task_description=req.task_description,
                max_iterations=req.max_iterations or 3,
                issue_number=req.issue_number,
                issue_type=req.issue_type,
                owner=owner,
                repo=repo,
                github_token=settings.github_token,
                mistral_api_key=settings.mistral_api_key,
            ),
            execution_id=execution_id,
        )
        session.workflow_run_id = execution_id
        await db.commit()
    except Exception as e:
        logger.error("Failed to start workflow: %s", e, exc_info=True)

    return session


_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(HydraSession).order_by(HydraSession.created_at.desc())
    if status:
        stmt = stmt.where(HydraSession.status == status)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    enrich: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Enrich with live workflow state from Temporal (skip when caller uses /live separately)
    if enrich and session.workflow_run_id and session.status not in _TERMINAL_STATUSES:
        state = await _query_live_state(session.workflow_run_id)
        if state and isinstance(state, dict):
            session.status = state.get("status", session.status)
            session.branch_name = state.get("branch_name", session.branch_name)
            session.pr_url = state.get("pr_url", session.pr_url)
            session.pr_merged = state.get("pr_merged", session.pr_merged)
            session.iteration_count = state.get("iteration", session.iteration_count)
            session.error_summary = state.get("error_summary", session.error_summary)
            session.issue_title = state.get("issue_title", session.issue_title)
            session.issue_type = state.get("issue_type", session.issue_type)
            # Persist terminal state to DB
            if session.status in _TERMINAL_STATUSES:
                await db.commit()

    return session


@router.get("/sessions/{session_id}/live")
async def get_session_live(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get live workflow state including test results, confidence, PR info."""
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.workflow_run_id and session.status not in _TERMINAL_STATUSES:
        state = await _query_live_state(session.workflow_run_id)
        if state and isinstance(state, dict):
            return state

    return {"status": session.status, "iteration": session.iteration_count}


@router.post("/sessions/{session_id}/assist")
async def assist_session(session_id: str, req: AssistRequest, db: AsyncSession = Depends(get_db)):
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "needs_human":
        raise HTTPException(status_code=400, detail="Session is not awaiting human assistance")

    if session.workflow_run_id:
        await workflow_registry.signal_workflow(
            session.workflow_run_id,
            "human_assist",
            {"guidance": req.guidance},
        )

    return {"status": "signal_sent", "session_id": session_id}


@router.post("/sessions/{session_id}/retry")
async def retry_session(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=400, detail="Only failed or cancelled sessions can be retried")

    # Reset session state
    session.status = "pending"
    session.error_summary = None
    session.iteration_count = 0
    await db.commit()

    # Start a new workflow on Temporal
    try:
        from app.workflows.hydra_session import HydraSessionWorkflow, SessionInput

        parts = session.repo_url.rstrip("/").split("/")
        owner, repo = parts[-2], parts[-1]
        execution_id = f"session-{session.id}-retry"

        await workflow_registry.start_workflow(
            HydraSessionWorkflow,
            SessionInput(
                session_id=session.id,
                repo_url=session.repo_url,
                task_description=session.task_description,
                max_iterations=session.max_iterations or 3,
                issue_number=session.issue_number,
                issue_type=session.issue_type,
                owner=owner,
                repo=repo,
                github_token=settings.github_token,
                mistral_api_key=settings.mistral_api_key,
            ),
            execution_id=execution_id,
        )
        session.workflow_run_id = execution_id
        await db.commit()
    except Exception as e:
        logger.error("Failed to retry workflow: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "retried", "session_id": session_id}


@router.post("/sessions/{session_id}/signal/ci")
async def signal_ci(session_id: str, conclusion: str = "success", db: AsyncSession = Depends(get_db)):
    """Send a CI result signal to a running workflow on Temporal."""
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.workflow_run_id:
        raise HTTPException(status_code=400, detail="No workflow running")

    await workflow_registry.signal_workflow(
        session.workflow_run_id,
        "ci_result",
        {"payload": {"check_name": "manual", "status": "completed", "conclusion": conclusion}},
    )
    return {"status": "signal_sent", "conclusion": conclusion}


@router.post("/sessions/{session_id}/signal/review")
async def signal_review(session_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """Send a PR review signal to a running workflow on Temporal."""
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.workflow_run_id:
        raise HTTPException(status_code=400, detail="No workflow running")

    await workflow_registry.signal_workflow(
        session.workflow_run_id,
        "review_feedback",
        {"action": body.get("action", "commented"), "comments": body.get("comments", [])},
    )
    return {"status": "signal_sent", "action": body.get("action")}


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.workflow_run_id:
        await workflow_registry.signal_workflow(
            session.workflow_run_id,
            "cancel",
        )

    return {"status": "cancel_sent", "session_id": session_id}


# --- Issue preview ---

@router.get("/issues/preview")
async def preview_issue(repo_url: str, issue_number: int):
    import httpx
    parts = repo_url.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1]
    headers = {"Accept": "application/vnd.github.v3+json"}
    if settings.github_token:
        headers["Authorization"] = f"token {settings.github_token}"
    try:
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            timeout=10.0,
        ) as client:
            resp = await client.get(f"/repos/{owner}/{repo}/issues/{issue_number}")
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "repo_url": repo_url, "issue_number": issue_number,
                    "title": data.get("title", ""), "labels": [l["name"] for l in data.get("labels", [])],
                }
            logger.warning("GitHub API returned %s for issue preview: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Issue preview failed: %s", e)
    return {"repo_url": repo_url, "issue_number": issue_number, "title": "", "labels": []}


# --- Triage ---

@router.get("/triage", response_model=list[TriageResultResponse])
async def list_triage(
    eligibility: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TriageResult).order_by(TriageResult.created_at.desc())
    if eligibility:
        stmt = stmt.where(TriageResult.eligibility == eligibility)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/triage/{triage_id}", response_model=TriageResultResponse)
async def get_triage(triage_id: str, db: AsyncSession = Depends(get_db)):
    triage = await db.get(TriageResult, triage_id)
    if not triage:
        raise HTTPException(status_code=404, detail="Triage result not found")
    return triage


@router.post("/triage/{triage_id}/approve")
async def approve_triage(triage_id: str, db: AsyncSession = Depends(get_db)):
    triage = await db.get(TriageResult, triage_id)
    if not triage:
        raise HTTPException(status_code=404, detail="Triage result not found")
    if triage.eligibility != "needs_review":
        raise HTTPException(status_code=400, detail="Only needs_review items can be approved")

    session = HydraSession(
        task_description=triage.suggested_approach or f"Fix issue #{triage.issue_number}",
        repo_url=triage.repo_url,
        issue_number=triage.issue_number,
        issue_type=triage.issue_type,
        issue_title=triage.issue_title,
        channel="triage",
        triage_id=triage.id,
    )
    db.add(session)
    triage.session_id = session.id
    triage.eligibility = "auto_assign"
    await db.commit()
    await db.refresh(session)

    try:
        from app.workflows.hydra_session import HydraSessionWorkflow, SessionInput

        parts = triage.repo_url.rstrip("/").split("/")
        owner, repo = parts[-2], parts[-1]
        execution_id = f"session-{session.id}"

        await workflow_registry.start_workflow(
            HydraSessionWorkflow,
            SessionInput(
                session_id=session.id,
                repo_url=triage.repo_url,
                task_description=session.task_description,
                issue_number=triage.issue_number,
                issue_type=triage.issue_type,
                issue_title=triage.issue_title,
                triage_suggested_approach=triage.suggested_approach,
                triage_relevant_files=triage.relevant_files or [],
                owner=owner, repo=repo,
                github_token=settings.github_token,
                mistral_api_key=settings.mistral_api_key,
            ),
            execution_id=execution_id,
        )
        session.workflow_run_id = execution_id
        await db.commit()
    except Exception as e:
        logger.error("Failed to start workflow from triage: %s", e, exc_info=True)

    return {"status": "approved", "session_id": session.id}


@router.post("/triage/{triage_id}/reject")
async def reject_triage(triage_id: str, db: AsyncSession = Depends(get_db)):
    triage = await db.get(TriageResult, triage_id)
    if not triage:
        raise HTTPException(status_code=404, detail="Triage result not found")
    triage.eligibility = "not_eligible"
    await db.commit()
    return {"status": "rejected", "triage_id": triage_id}


# --- Trace / Observability ---

def _build_span_tree(spans: list[Span]) -> list[SpanResponse]:
    span_map: dict[str, SpanResponse] = {}
    for s in spans:
        span_map[s.id] = SpanResponse.model_validate(s)

    roots = []
    for s in spans:
        resp = span_map[s.id]
        if s.parent_span_id and s.parent_span_id in span_map:
            span_map[s.parent_span_id].children.append(resp)
        else:
            roots.append(resp)
    return roots


@router.get("/sessions/{session_id}/trace", response_model=TraceResponse)
async def get_trace(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = select(Span).where(Span.session_id == session_id).order_by(Span.started_at)
    result = await db.execute(stmt)
    spans = list(result.scalars().all())

    trace_id = session.workflow_run_id or session.id
    root_spans = _build_span_tree(spans)

    total_duration = 0
    if spans:
        first = min(s.started_at for s in spans)
        last_ended = [s.ended_at for s in spans if s.ended_at]
        if last_ended:
            total_duration = int((max(last_ended) - first).total_seconds() * 1000)

    metrics = await db.get(SessionMetrics, session_id)
    summary = None
    if metrics:
        summary = _build_metrics_response(metrics)

    return TraceResponse(
        trace_id=trace_id,
        session_id=session_id,
        root_spans=root_spans,
        total_duration_ms=total_duration,
        summary=summary,
    )


def _build_metrics_response(m: SessionMetrics) -> SessionMetricsResponse:
    total = m.total_duration_ms or 1
    breakdown = TimeBreakdown(
        coding_pct=round(m.coding_duration_ms / total * 100, 1),
        testing_pct=round(m.testing_duration_ms / total * 100, 1),
        ci_wait_pct=round(m.ci_wait_duration_ms / total * 100, 1),
        review_wait_pct=round(m.review_wait_duration_ms / total * 100, 1),
    )
    return SessionMetricsResponse(
        session_id=m.session_id,
        total_duration_ms=m.total_duration_ms,
        coding_duration_ms=m.coding_duration_ms,
        testing_duration_ms=m.testing_duration_ms,
        ci_wait_duration_ms=m.ci_wait_duration_ms,
        review_wait_duration_ms=m.review_wait_duration_ms,
        provision_duration_ms=m.provision_duration_ms,
        iterations=m.iterations,
        test_runs=m.test_runs,
        total_tests_executed=m.total_tests_executed,
        total_tokens_used=m.total_tokens_used,
        vibe_turns=m.vibe_turns,
        tool_calls_count=m.tool_calls_count,
        files_modified_count=m.files_modified_count,
        outcome=m.outcome,
        failure_reason=m.failure_reason,
        time_breakdown=breakdown,
    )


@router.get("/sessions/{session_id}/metrics", response_model=SessionMetricsResponse)
async def get_session_metrics(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(HydraSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    metrics = await db.get(SessionMetrics, session_id)
    if not metrics:
        raise HTTPException(status_code=404, detail="Metrics not found")
    return _build_metrics_response(metrics)


@router.get("/metrics/summary", response_model=MetricsSummary)
async def get_metrics_summary(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SessionMetrics))
    all_metrics = list(result.scalars().all())

    if not all_metrics:
        return MetricsSummary()

    completed = sum(1 for m in all_metrics if m.outcome == "completed")
    failed = sum(1 for m in all_metrics if m.outcome == "failed")
    cancelled = sum(1 for m in all_metrics if m.outcome == "cancelled")
    total = len(all_metrics)

    failure_reasons = Counter(m.failure_reason for m in all_metrics if m.failure_reason)

    session_result = await db.execute(select(HydraSession))
    sessions = list(session_result.scalars().all())
    day_counts: dict[str, int] = {}
    for s in sessions:
        day = s.created_at.strftime("%Y-%m-%d")
        day_counts[day] = day_counts.get(day, 0) + 1

    return MetricsSummary(
        total_sessions=total,
        completed=completed,
        failed=failed,
        cancelled=cancelled,
        success_rate=round(completed / total * 100, 1) if total else 0.0,
        avg_iterations=round(sum(m.iterations for m in all_metrics) / total, 1),
        avg_duration_ms=round(sum(m.total_duration_ms for m in all_metrics) / total, 1),
        avg_coding_ms=round(sum(m.coding_duration_ms for m in all_metrics) / total, 1),
        avg_ci_wait_ms=round(sum(m.ci_wait_duration_ms for m in all_metrics) / total, 1),
        common_failure_reasons=[
            FailureReasonCount(reason=r, count=c) for r, c in failure_reasons.most_common()
        ],
        tokens_total=sum(m.total_tokens_used for m in all_metrics),
        sessions_per_day=[DayCount(date=d, count=c) for d, c in sorted(day_counts.items())],
    )

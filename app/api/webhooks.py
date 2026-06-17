"""GitHub webhook handler — signal relay to running workflows."""

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import HydraSession
from app import workflow_registry

logger = logging.getLogger(__name__)
router = APIRouter()


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub HMAC-SHA256 webhook signature."""
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: str = Header(None, alias="X-Hub-Signature-256"),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()

    if settings.github_webhook_secret:
        if not verify_signature(body, x_hub_signature_256 or "", settings.github_webhook_secret):
            raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()

    if x_github_event == "issues" and payload.get("action") == "opened":
        return await _handle_issue_opened(payload, db)

    elif x_github_event in ("check_suite", "check_run"):
        status = payload.get("check_suite", payload.get("check_run", {})).get("status")
        if status == "completed":
            return await _handle_ci_completed(payload, db)

    elif x_github_event == "pull_request_review":
        return await _handle_pr_review(payload, db)

    return {"status": "ignored", "event": x_github_event}


async def _handle_issue_opened(payload: dict, db: AsyncSession) -> dict:
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    repo_url = repo.get("html_url", "")

    try:
        from app.workflows.triage import TriageWorkflow, TriageInput

        parts = repo_url.rstrip("/").split("/")
        owner, repo_name = parts[-2], parts[-1]
        execution_id = f"triage-{owner}-{repo_name}-{issue.get('number')}"

        await workflow_registry.start_workflow(
            TriageWorkflow,
            TriageInput(
                repo_url=repo_url,
                issue_number=issue.get("number"),
                owner=owner,
                repo=repo_name,
                github_token=settings.github_token,
                mistral_api_key=settings.mistral_api_key,
            ),
            execution_id=execution_id,
        )
    except Exception as e:
        logger.error("Failed to start triage workflow: %s", e, exc_info=True)

    return {
        "status": "triage_queued",
        "issue_number": issue.get("number"),
        "repo_url": repo_url,
    }


async def _handle_ci_completed(payload: dict, db: AsyncSession) -> dict:
    """Look up session by branch and send ci_result signal."""
    check = payload.get("check_suite") or payload.get("check_run", {})
    head_branch = check.get("head_branch", "")

    stmt = select(HydraSession).where(
        HydraSession.branch_name == head_branch,
        HydraSession.status.in_(["ci_monitoring", "running"]),
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    if not session:
        return {"status": "no_matching_session", "branch": head_branch}

    if session.workflow_run_id:
        wf = workflow_registry.get_workflow(session.workflow_run_id)
        if wf:
            from app.workflows.hydra_session import CISignalData
            from app.schemas import CIResultPayload
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name=check.get("name", check.get("app", {}).get("name", "CI")),
                status="completed",
                conclusion=check.get("conclusion", ""),
            )))

    return {
        "status": "ci_signal_queued",
        "session_id": session.id,
        "conclusion": check.get("conclusion"),
    }


async def _handle_pr_review(payload: dict, db: AsyncSession) -> dict:
    review = payload.get("review", {})
    pr = payload.get("pull_request", {})
    pr_url = pr.get("html_url", "")
    action = review.get("state", "")

    stmt = select(HydraSession).where(
        HydraSession.pr_url == pr_url,
        HydraSession.status.in_(["pr_review", "ci_monitoring", "running"]),
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    if not session:
        return {"status": "no_matching_session", "pr_url": pr_url}

    comments = []
    if action == "changes_requested":
        comments = [{"author": review.get("user", {}).get("login", ""), "body": review.get("body", "")}]

    if session.workflow_run_id:
        wf = workflow_registry.get_workflow(session.workflow_run_id)
        if wf:
            from app.workflows.hydra_session import ReviewFeedbackData
            await wf.signal_review_feedback(ReviewFeedbackData(action=action, comments=comments))

    return {
        "status": "review_signal_queued",
        "session_id": session.id,
        "action": action,
        "comments_count": len(comments),
    }

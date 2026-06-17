import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HydraSession, SessionMetrics, Span, TriageResult


# --- Session CRUD ---

async def test_create_session(client):
    resp = await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "Fix the login bug",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_description"] == "Fix the login bug"
    assert data["status"] == "pending"
    assert data["id"] is not None


async def test_list_sessions(client):
    await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "Task 1",
    })
    await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "Task 2",
    })
    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_list_sessions_filter_by_status(client):
    await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "Task 1",
    })
    resp = await client.get("/api/sessions?status=running")
    assert resp.status_code == 200
    assert len(resp.json()) == 0

    resp = await client.get("/api/sessions?status=pending")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_get_session(client):
    create_resp = await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "Test get",
    })
    session_id = create_resp.json()["id"]

    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == session_id


async def test_get_session_not_found(client):
    resp = await client.get("/api/sessions/nonexistent")
    assert resp.status_code == 404


async def test_assist_session_wrong_status(client):
    create_resp = await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "Test assist",
    })
    session_id = create_resp.json()["id"]

    resp = await client.post(f"/api/sessions/{session_id}/assist", json={
        "guidance": "Try checking the auth module",
    })
    assert resp.status_code == 400


async def test_cancel_session(client):
    create_resp = await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "Test cancel",
    })
    session_id = create_resp.json()["id"]

    resp = await client.post(f"/api/sessions/{session_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancel_sent"


# --- Issue preview ---

async def test_issue_preview(client):
    resp = await client.get("/api/issues/preview?repo_url=https://github.com/t/r&issue_number=1")
    assert resp.status_code == 200
    assert resp.json()["issue_number"] == 1


# --- Triage ---

async def test_triage_crud(client, db_session):
    triage = TriageResult(
        repo_url="https://github.com/test/repo",
        issue_number=42,
        issue_title="Bug fix",
        eligibility="needs_review",
        complexity="medium",
        issue_type="bug",
    )
    db_session.add(triage)
    await db_session.commit()
    await db_session.refresh(triage)

    resp = await client.get("/api/triage")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = await client.get(f"/api/triage/{triage.id}")
    assert resp.status_code == 200
    assert resp.json()["issue_number"] == 42


async def test_triage_filter_by_eligibility(client, db_session):
    for elig in ["auto_assign", "needs_review", "not_eligible"]:
        db_session.add(TriageResult(
            repo_url="https://github.com/test/repo",
            issue_number=1,
            issue_title="Test",
            eligibility=elig,
            complexity="simple",
            issue_type="bug",
        ))
    await db_session.commit()

    resp = await client.get("/api/triage?eligibility=needs_review")
    assert len(resp.json()) == 1


async def test_approve_triage(client, db_session):
    triage = TriageResult(
        repo_url="https://github.com/test/repo",
        issue_number=42,
        issue_title="Bug fix",
        eligibility="needs_review",
        complexity="medium",
        issue_type="bug",
        suggested_approach="Check auth module",
    )
    db_session.add(triage)
    await db_session.commit()
    await db_session.refresh(triage)

    resp = await client.post(f"/api/triage/{triage.id}/approve")
    assert resp.status_code == 200
    assert "session_id" in resp.json()


async def test_approve_triage_wrong_eligibility(client, db_session):
    triage = TriageResult(
        repo_url="https://github.com/test/repo",
        issue_number=42,
        issue_title="Bug fix",
        eligibility="auto_assign",
        complexity="simple",
        issue_type="bug",
    )
    db_session.add(triage)
    await db_session.commit()
    await db_session.refresh(triage)

    resp = await client.post(f"/api/triage/{triage.id}/approve")
    assert resp.status_code == 400


async def test_reject_triage(client, db_session):
    triage = TriageResult(
        repo_url="https://github.com/test/repo",
        issue_number=42,
        issue_title="Bug fix",
        eligibility="needs_review",
        complexity="medium",
        issue_type="bug",
    )
    db_session.add(triage)
    await db_session.commit()
    await db_session.refresh(triage)

    resp = await client.post(f"/api/triage/{triage.id}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


# --- Trace / Metrics ---

async def test_get_trace_empty(client):
    create_resp = await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "Test trace",
    })
    session_id = create_resp.json()["id"]

    resp = await client.get(f"/api/sessions/{session_id}/trace")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session_id
    assert data["root_spans"] == []


async def test_get_trace_with_spans(client, db_session):
    session = HydraSession(
        task_description="Trace test",
        repo_url="https://github.com/test/repo",
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    parent = Span(
        trace_id="trace-1",
        session_id=session.id,
        name="coding_phase",
        kind="phase",
        status="completed",
        started_at=now,
        ended_at=now + timedelta(seconds=10),
        duration_ms=10000,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    child = Span(
        trace_id="trace-1",
        session_id=session.id,
        parent_span_id=parent.id,
        name="agent_turn_1",
        kind="agent_turn",
        status="completed",
        started_at=now + timedelta(seconds=1),
        ended_at=now + timedelta(seconds=5),
        duration_ms=4000,
    )
    db_session.add(child)
    await db_session.commit()

    resp = await client.get(f"/api/sessions/{session.id}/trace")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["root_spans"]) == 1
    assert len(data["root_spans"][0]["children"]) == 1
    assert data["root_spans"][0]["name"] == "coding_phase"


async def test_get_session_metrics(client, db_session):
    session = HydraSession(
        task_description="Metrics test",
        repo_url="https://github.com/test/repo",
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    metrics = SessionMetrics(
        session_id=session.id,
        total_duration_ms=100000,
        coding_duration_ms=50000,
        testing_duration_ms=20000,
        ci_wait_duration_ms=10000,
        iterations=3,
        outcome="completed",
    )
    db_session.add(metrics)
    await db_session.commit()

    resp = await client.get(f"/api/sessions/{session.id}/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_duration_ms"] == 100000
    assert data["time_breakdown"]["coding_pct"] == 50.0


async def test_get_session_metrics_not_found(client):
    create_resp = await client.post("/api/sessions", json={
        "repo_url": "https://github.com/test/repo",
        "task_description": "No metrics",
    })
    session_id = create_resp.json()["id"]
    resp = await client.get(f"/api/sessions/{session_id}/metrics")
    assert resp.status_code == 404


async def test_metrics_summary_empty(client):
    resp = await client.get("/api/metrics/summary")
    assert resp.status_code == 200
    assert resp.json()["total_sessions"] == 0


async def test_metrics_summary(client, db_session):
    session = HydraSession(
        task_description="Summary test",
        repo_url="https://github.com/test/repo",
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    metrics = SessionMetrics(
        session_id=session.id,
        total_duration_ms=60000,
        coding_duration_ms=30000,
        iterations=2,
        outcome="completed",
        total_tokens_used=5000,
    )
    db_session.add(metrics)
    await db_session.commit()

    resp = await client.get("/api/metrics/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_sessions"] == 1
    assert data["completed"] == 1
    assert data["success_rate"] == 100.0
    assert data["tokens_total"] == 5000

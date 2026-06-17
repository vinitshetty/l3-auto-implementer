from sqlalchemy import select

from app.models import HydraSession, SessionEvent, SessionMetrics, Span, TriageResult


async def test_create_session(db_session):
    session = HydraSession(
        task_description="Fix the bug",
        repo_url="https://github.com/test/repo",
    )
    db_session.add(session)
    await db_session.commit()

    result = await db_session.execute(select(HydraSession))
    saved = result.scalar_one()
    assert saved.task_description == "Fix the bug"
    assert saved.status == "pending"
    assert saved.id is not None
    assert saved.created_at is not None


async def test_create_event(db_session):
    session = HydraSession(
        task_description="Test event",
        repo_url="https://github.com/test/repo",
    )
    db_session.add(session)
    await db_session.commit()

    event = SessionEvent(
        session_id=session.id,
        event_type="status_change",
        payload={"from": "pending", "to": "running"},
    )
    db_session.add(event)
    await db_session.commit()

    result = await db_session.execute(
        select(SessionEvent).where(SessionEvent.session_id == session.id)
    )
    saved = result.scalar_one()
    assert saved.event_type == "status_change"
    assert saved.payload["to"] == "running"


async def test_create_span(db_session):
    session = HydraSession(
        task_description="Test spans",
        repo_url="https://github.com/test/repo",
    )
    db_session.add(session)
    await db_session.commit()

    span = Span(
        trace_id="trace-123",
        session_id=session.id,
        name="provision_sandbox",
        kind="activity",
    )
    db_session.add(span)
    await db_session.commit()

    result = await db_session.execute(
        select(Span).where(Span.session_id == session.id)
    )
    saved = result.scalar_one()
    assert saved.name == "provision_sandbox"
    assert saved.kind == "activity"
    assert saved.status == "running"


async def test_create_session_metrics(db_session):
    session = HydraSession(
        task_description="Test metrics",
        repo_url="https://github.com/test/repo",
    )
    db_session.add(session)
    await db_session.commit()

    metrics = SessionMetrics(
        session_id=session.id,
        total_duration_ms=60000,
        coding_duration_ms=30000,
        testing_duration_ms=10000,
        iterations=2,
        outcome="completed",
    )
    db_session.add(metrics)
    await db_session.commit()

    result = await db_session.execute(
        select(SessionMetrics).where(SessionMetrics.session_id == session.id)
    )
    saved = result.scalar_one()
    assert saved.total_duration_ms == 60000
    assert saved.outcome == "completed"


async def test_create_triage_result(db_session):
    triage = TriageResult(
        repo_url="https://github.com/test/repo",
        issue_number=42,
        issue_title="Fix login bug",
        eligibility="auto_assign",
        complexity="simple",
        issue_type="bug",
    )
    db_session.add(triage)
    await db_session.commit()

    result = await db_session.execute(select(TriageResult))
    saved = result.scalar_one()
    assert saved.issue_number == 42
    assert saved.eligibility == "auto_assign"

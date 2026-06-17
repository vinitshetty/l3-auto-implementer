import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Float
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class TriageResult(Base):
    __tablename__ = "triage_results"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    repo_url: Mapped[str] = mapped_column(String, nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_title: Mapped[str] = mapped_column(String, nullable=False)
    issue_body: Mapped[str] = mapped_column(Text, default="")
    issue_labels: Mapped[dict] = mapped_column(JSON, default=list)
    eligibility: Mapped[str] = mapped_column(String, nullable=False)  # auto_assign|needs_review|not_eligible
    complexity: Mapped[str] = mapped_column(String, nullable=False)  # simple|medium|complex
    issue_type: Mapped[str] = mapped_column(String, nullable=False)  # bug|feature
    relevant_files: Mapped[list] = mapped_column(JSON, default=list)
    suggested_approach: Mapped[str] = mapped_column(Text, default="")
    triage_workflow_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, ForeignKey("hydra_sessions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class HydraSession(Base):
    __tablename__ = "hydra_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    repo_url: Mapped[str] = mapped_column(String, nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    pr_url: Mapped[str | None] = mapped_column(String, nullable=True)
    pr_merged: Mapped[bool] = mapped_column(Boolean, default=False)
    iteration_count: Mapped[int] = mapped_column(Integer, default=0)
    max_iterations: Mapped[int] = mapped_column(Integer, default=3)
    channel: Mapped[str] = mapped_column(String, default="web")  # web|slack|triage
    workflow_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    triage_id: Mapped[str | None] = mapped_column(String, ForeignKey("triage_results.id"), nullable=True)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_type: Mapped[str | None] = mapped_column(String, nullable=True)  # bug|feature|null
    issue_title: Mapped[str | None] = mapped_column(String, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Span(Base):
    __tablename__ = "spans"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    trace_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str] = mapped_column(String, ForeignKey("hydra_sessions.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # phase|activity|agent_turn|tool_call|container_op|http|wait
    status: Mapped[str] = mapped_column(String, default="running")  # running|completed|failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SessionEvent(Base):
    __tablename__ = "session_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    session_id: Mapped[str] = mapped_column(String, ForeignKey("hydra_sessions.id"), nullable=False, index=True)
    span_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SessionMetrics(Base):
    __tablename__ = "session_metrics"

    session_id: Mapped[str] = mapped_column(String, ForeignKey("hydra_sessions.id"), primary_key=True)
    total_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    coding_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    testing_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ci_wait_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    review_wait_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    provision_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    test_runs: Mapped[int] = mapped_column(Integer, default=0)
    total_tests_executed: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    vibe_turns: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls_count: Mapped[int] = mapped_column(Integer, default=0)
    files_modified_count: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(String, default="pending")  # completed|failed|cancelled
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)

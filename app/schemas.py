from datetime import datetime
from pydantic import BaseModel, Field


# --- Request schemas ---

class CreateSessionRequest(BaseModel):
    repo_url: str
    task_description: str
    max_iterations: int = 3
    issue_number: int | None = None
    issue_type: str | None = None  # bug|feature


class AssistRequest(BaseModel):
    guidance: str


# --- Payload schemas ---

class TestFailure(BaseModel):
    name: str
    error: str
    traceback: str | None = None


class TestResultPayload(BaseModel):
    total: int
    passed: int
    failed: int
    skipped: int
    failures: list[TestFailure] = []


class TestCaseResult(BaseModel):
    name: str
    outcome: str  # passed|failed|skipped
    duration_ms: int = 0
    error: str | None = None
    traceback: str | None = None


class CIResultPayload(BaseModel):
    check_name: str
    status: str
    conclusion: str | None = None
    details_url: str | None = None
    test_results: TestResultPayload | None = None


class ReviewComment(BaseModel):
    author: str
    body: str


class PRUpdatePayload(BaseModel):
    pr_url: str
    state: str
    merged: bool = False
    mergeable: bool | None = None
    review_comments: list[ReviewComment] = []


class ConfidenceSummary(BaseModel):
    files_changed: int
    lines_added: int
    lines_removed: int
    changed_files: list[str] = []
    new_dependencies: list[str] = []
    test_results: TestResultPayload | None = None
    risk_flags: list[str] = []
    confidence_score: int = 0  # 0-100
    summary: str = ""


class VibeSummary(BaseModel):
    turns: int
    tool_calls: int
    files_modified: list[str] = []
    tokens_used: int = 0


# --- Response schemas ---

class EventResponse(BaseModel):
    id: str
    session_id: str
    span_id: str | None = None
    trace_id: str | None = None
    event_type: str
    payload: dict = {}
    timestamp: datetime


class SessionResponse(BaseModel):
    id: str
    task_description: str
    repo_url: str
    branch_name: str | None = None
    status: str
    pr_url: str | None = None
    pr_merged: bool = False
    iteration_count: int = 0
    max_iterations: int = 3
    channel: str = "web"
    workflow_run_id: str | None = None
    triage_id: str | None = None
    issue_number: int | None = None
    issue_type: str | None = None
    issue_title: str | None = None
    issue_url: str | None = None
    error_summary: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @property
    def computed_issue_url(self) -> str | None:
        if self.issue_number and self.repo_url:
            base = self.repo_url.rstrip("/").removesuffix(".git")
            return f"{base}/issues/{self.issue_number}"
        return None


class TriageResultResponse(BaseModel):
    id: str
    repo_url: str
    issue_number: int
    issue_title: str
    issue_body: str = ""
    issue_labels: list = []
    eligibility: str
    complexity: str
    issue_type: str
    relevant_files: list = []
    suggested_approach: str = ""
    triage_workflow_run_id: str | None = None
    session_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SpanResponse(BaseModel):
    id: str
    trace_id: str
    parent_span_id: str | None = None
    session_id: str
    name: str
    kind: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None
    span_metadata: dict | None = None
    error: str | None = None
    children: list["SpanResponse"] = []

    model_config = {"from_attributes": True}


class TimeBreakdown(BaseModel):
    coding_pct: float = 0.0
    testing_pct: float = 0.0
    ci_wait_pct: float = 0.0
    review_wait_pct: float = 0.0


class SessionMetricsResponse(BaseModel):
    session_id: str
    total_duration_ms: int = 0
    coding_duration_ms: int = 0
    testing_duration_ms: int = 0
    ci_wait_duration_ms: int = 0
    review_wait_duration_ms: int = 0
    provision_duration_ms: int = 0
    iterations: int = 0
    test_runs: int = 0
    total_tests_executed: int = 0
    total_tokens_used: int = 0
    vibe_turns: int = 0
    tool_calls_count: int = 0
    files_modified_count: int = 0
    outcome: str = "pending"
    failure_reason: str | None = None
    time_breakdown: TimeBreakdown = Field(default_factory=TimeBreakdown)

    model_config = {"from_attributes": True}


class TraceResponse(BaseModel):
    trace_id: str
    session_id: str
    root_spans: list[SpanResponse] = []
    total_duration_ms: int = 0
    summary: SessionMetricsResponse | None = None


class FailureReasonCount(BaseModel):
    reason: str
    count: int


class DayCount(BaseModel):
    date: str
    count: int


class MetricsSummary(BaseModel):
    total_sessions: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    success_rate: float = 0.0
    avg_iterations: float = 0.0
    avg_duration_ms: float = 0.0
    avg_coding_ms: float = 0.0
    avg_ci_wait_ms: float = 0.0
    common_failure_reasons: list[FailureReasonCount] = []
    tokens_total: int = 0
    sessions_per_day: list[DayCount] = []

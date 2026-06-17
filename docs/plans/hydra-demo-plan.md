# Hydra Demo — Implementation Plan

> **Goal:** Build a Hydra async coding agent demo showcasing Mistral Vibe CLI, Workflows, Medium 3.5, Agents API, and Function Calling — end-to-end from GitHub issue triage to PR with CI monitoring and human-in-the-loop intervention.

## Core Design: Two-Workflow Architecture

**Two Mistral Workflows form the brain.** Everything else is a thin shell around them.

```
  GitHub Issue Created (#42)
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│              TRIAGE WORKFLOW (the gatekeeper)                │
│                                                              │
│  ┌──────────┐   ┌──────────────┐   ┌─────────────────────┐  │
│  │ fetch     │──▶│ analyze with │──▶│ decision:           │  │
│  │ issue +   │   │ Mistral      │   │ • auto-assign →     │──┼──▶ starts Session Workflow
│  │ repo ctx  │   │ Agents API   │   │ • needs-triage →    │──┼──▶ label + notify team
│  └──────────┘   └──────────────┘   │ • not-eligible →    │──┼──▶ label + skip
│                                     └─────────────────────┘  │
│  Uses: function calling to read repo structure, search code, │
│  check similar issues, estimate complexity                    │
└──────────────────────────────────────────────────────────────┘
         │ (auto-assign)
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    SESSION WORKFLOW (the coder)                      │
│                                                                     │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌───────────────┐   │
│  │ provision │──▶│ code +   │──▶│ test +   │──▶│ push + PR     │   │
│  │ sandbox   │   │ review   │   │ validate │   │               │   │
│  └──────────┘   └──────────┘   └──────────┘   └───────┬───────┘   │
│                                                        │           │
│                                              ┌────────▼────────┐  │
│                                              │  CI wait loop    │  │
│          ┌──────────────┐                    │  (signal-driven) │  │
│          │ human_assist  │◀── needs_human ◀──┤                  │  │
│          │ signal        │──▶ resume ──────▶ │  ci_result       │  │
│          └──────────────┘                    │  signal          │  │
│                                              └────────┬────────┘  │
│          ┌──────────────┐                             │           │
│          │ review_feedback│◀── PR review ◀────────────┘           │
│          │ signal         │──▶ re-code ──▶ push ──▶ CI           │
│          └──────────────┘                                         │
│                                              ┌─────────────────┐  │
│                                              │ cleanup +       │  │
│                                              │ complete/fail   │  │
│                                              └─────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
         ▲               ▲               ▲               │
         │               │               │               │ (activities publish events)
         │               │               │               ▼
    ┌────┴────┐   ┌──────┴──────┐   ┌────┴────┐   ┌──────────┐
    │ FastAPI  │   │ GitHub      │   │ Slack   │   │ Event    │
    │ API      │   │ Webhooks    │   │ Bot     │   │ Bus      │
    │ (thin)   │   │ (signals)   │   │(signals)│   │ → SSE    │
    └─────────┘   └─────────────┘   └─────────┘   └──────────┘
```

**Triage Workflow (the gatekeeper):**
- Triggered by GitHub `issues.opened` webhook (or manual via UI/Slack)
- Uses Mistral Agents API + function calling (NOT Vibe CLI) to analyze the issue
- Functions: read repo file tree, search code for relevant files, check recent commits, find similar closed issues
- Outputs: eligibility decision, complexity estimate (simple/medium/complex), type (bug/feature), suggested approach, relevant files
- Actions: label the issue, post triage summary as comment, auto-start session workflow if eligible

**Session Workflow (the coder):**
- ALL state transitions (pending → running → ci_monitoring → needs_human → completed/failed)
- ALL business logic (when to code, when to test, when to ask for help, when to give up)
- ALL side effects via activities (Docker, GitHub, events)
- Durable execution — survives process restarts
- Signal-driven coordination — CI results, human input, PR review feedback, cancellation

**What FastAPI does (thin API shell):**
- Start workflows (`POST /api/sessions` → `workflow.start()`)
- Query workflow state (`GET /api/sessions/{id}` → `workflow.query()`)
- Forward signals (`POST /assist` → `workflow.signal("human_assist")`)
- Forward signals (`POST /cancel` → `workflow.signal("cancel")`)
- Stream events (`GET /api/sessions/{id}/events` → SSE from event bus)
- Triage dashboard (`GET /api/triage` → list triage results)

**What webhooks do (signal relay):**
- `issues.opened` → start Triage Workflow
- `check_suite.completed` → `session_workflow.signal("ci_result")`
- `pull_request_review.submitted` → `session_workflow.signal("review_feedback")`

**What Slack does (signal relay):**
- `/hydra` command → `workflow.start()`, thread updates from event bus

---

**Tech Stack:** Python 3.12, FastAPI, SQLite (aiosqlite + SQLAlchemy), Mistral Workflows SDK, Docker SDK, httpx, sse-starlette, Slack Bolt, vanilla HTML/JS frontend

**Principles:**
- Workflow-first: all logic lives in the workflow, API/UI are just windows into it
- Keep infra minimal — SQLite, no migrations, no starter-app template
- Workflow signals for all external input (CI, human, cancel)
- Workflow queries for all state reads (UI polls workflow, not just DB)
- Rich UI showing test results, CI status, PR state, agent reasoning, HITL
- TDD: write failing test → implement → verify → commit
- Wave/task dispatch: `implement wave N` or `implement task N`

---

## Issue Lifecycle (triage workflow → session workflow)

```
GitHub Issue #42 opened
        │
        ▼
┌── Triage Workflow ──┐
│                     │
│  analyzing          │
│      │              │
│      ├─→ eligible (simple) ──→ auto-start Session Workflow
│      ├─→ eligible (medium) ──→ label "hydra-eligible" + notify team for approval
│      ├─→ needs-triage ───────→ label "needs-triage" + post analysis for human decision
│      └─→ not-eligible ──────→ label "not-automatable" + explain why
│                     │
└─────────────────────┘

Triage output (posted as issue comment):
  - Type: 🐛 Bug / ✨ Feature
  - Complexity: Simple / Medium / Complex
  - Relevant files: [list]
  - Suggested approach: "..."
  - Eligibility: ✅ Auto-assignable / ⚠️ Needs human review / ❌ Not automatable
```

## Session Lifecycle (owned by session workflow)

```
pending → running → ci_monitoring ──→ pr_review ──→ completed
             │            │               │
             │            │               └─→ changes_requested ──→ running (re-code)
             │            │
             │            ├─→ needs_human ──→ running (resumed with hint)
             │            │        │
             │            │        └─→ cancelled (human gave up)
             │            │
             │            └─→ failed (max iterations)
             │
             └─→ failed (agent error)
```

**Session workflow signals (external → workflow):**
- `ci_result` — GitHub webhook delivers CI pass/fail + test details
- `human_assist` — Human provides guidance text via UI/Slack
- `review_feedback` — PR review submitted (approved / changes_requested + comments)
- `cancel` — Human cancels session, workflow cleans up

**Session workflow queries (workflow → external):**
- `get_status` — current state, iteration count, error context
- `get_test_results` — latest test pass/fail/skip details
- `get_pr_info` — PR url, merge state, review comments

**Triage workflow queries:**
- `get_triage_result` — eligibility, complexity, type, relevant files, suggested approach

---

## File Structure

```
l3-auto-implementer/
├── pyproject.toml
├── .env.example
├── Dockerfile.sandbox
├── docs/plans/
│
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app + lifespan + workflow worker start
│   ├── config.py                # pydantic-settings
│   ├── database.py              # async SQLite engine + session
│   ├── models.py                # HydraSession + SessionEvent (SQLAlchemy)
│   ├── schemas.py               # Pydantic request/response schemas
│   ├── event_bus.py             # In-memory asyncio.Queue per session → SSE
│   ├── observability.py         # Tracer: spans + events, writes to DB + event bus
│   ├── api/
│   │   ├── __init__.py
│   │   ├── sessions.py          # Thin: start/query/signal workflows
│   │   ├── events.py            # SSE streaming endpoint
│   │   └── webhooks.py          # GitHub webhook → workflow.signal("ci_result")
│   ├── sandbox/
│   │   ├── __init__.py
│   │   └── manager.py           # Docker container lifecycle
│   ├── workflows/
│   │   ├── __init__.py
│   │   ├── triage.py            # Triage workflow — issue analysis + eligibility
│   │   ├── triage_functions.py  # Function calling tools for triage agent
│   │   ├── hydra_session.py     # Session workflow — coding + CI + HITL
│   │   └── activities.py        # Shared workflow activities
│   └── integrations/
│       ├── __init__.py
│       ├── github_client.py     # GitHub REST API (httpx)
│       └── slack_bot.py         # Slack Bolt → workflow.start/signal
│
├── static/
│   ├── index.html               # Dashboard: session list + create form
│   ├── triage.html              # Triage dashboard: issue queue + triage results
│   ├── session.html             # Session detail: events, tests, PR, HITL
│   ├── app.js                   # Vanilla JS (fetch + EventSource)
│   └── style.css                # Dark theme CSS
│
└── tests/
    ├── conftest.py
    ├── test_models.py
    ├── test_api_sessions.py
    ├── test_sandbox.py
    ├── test_workflow.py
    ├── test_triage.py
    ├── test_github_client.py
    ├── test_webhooks.py
    ├── test_events_sse.py
    └── test_slack_bot.py
```

---

## Wave 1: Foundation (Config + Models + CRUD API)

### Task 1: Project Setup + Config

**Files:** `pyproject.toml`, `.env.example`, `app/__init__.py`, `app/config.py`, `app/main.py`

- [ ] **Step 1:** Create `pyproject.toml` with dependencies: fastapi, uvicorn, sqlalchemy[asyncio], aiosqlite, pydantic-settings, httpx, docker, sse-starlette, slack-bolt, mistral-workflows, pytest, pytest-asyncio, httpx (test client)
- [ ] **Step 2:** Create `.env.example` with: `MISTRAL_API_KEY`, `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `DATABASE_URL=sqlite+aiosqlite:///./hydra.db`, `MAX_CI_ITERATIONS=3`, `VIBE_MAX_TURNS=50`, `VIBE_MAX_PRICE=5.0`
- [ ] **Step 3:** Implement `config.py` — pydantic-settings `Settings` class loading from `.env`
- [ ] **Step 4:** Implement `main.py` — FastAPI app with lifespan (create tables on startup, start workflow worker), mount `/static`
- [ ] **Step 5:** Write test for config loading, run — verify PASS
- [ ] **Step 6:** Commit

### Task 2: Database + Models

**Files:** `app/database.py`, `app/models.py`, `app/schemas.py`, `tests/conftest.py`, `tests/test_models.py`

- [ ] **Step 1:** Implement `database.py` — async engine, `async_session` factory, `Base` declarative base
- [ ] **Step 2:** Implement `models.py`:
  - `TriageResult`: id (UUID), repo_url, issue_number, issue_title, issue_body, issue_labels (JSON), eligibility (auto_assign|needs_review|not_eligible), complexity (simple|medium|complex), issue_type (bug|feature), relevant_files (JSON list), suggested_approach (text), triage_workflow_run_id, session_id (FK, nullable — set when session created), created_at
  - `HydraSession`: id (UUID), task_description, repo_url, branch_name, status, pr_url, pr_merged (bool), iteration_count, max_iterations, channel (web|slack|triage), workflow_run_id (links to Mistral Workflow), triage_id (FK to TriageResult, nullable), issue_number (optional int), issue_type (bug|feature|null), issue_title (cached from GitHub), error_summary, created_at, updated_at
  - `Span`: id (UUID), trace_id (UUID — same as workflow_run_id, 1 trace per workflow run), parent_span_id (UUID, nullable — for nesting), session_id (FK), name (str — e.g. "provision_sandbox", "agent_turn_1"), kind (str — "phase"|"activity"|"agent_turn"|"tool_call"|"container_op"|"http"|"wait"), status (str — "running"|"completed"|"failed"), started_at (datetime), ended_at (datetime, nullable), duration_ms (int, nullable), metadata (JSON — activity-specific context: prompt_length, cmd, exit_code, tool name, args, etc.), error (text, nullable)
  - `SessionEvent`: id (UUID), session_id (FK), span_id (UUID, nullable — links event to producing span), trace_id (UUID — top-level trace correlation), event_type, payload (JSON), timestamp
  - `SessionMetrics`: session_id (FK, unique), total_duration_ms, coding_duration_ms (sum of run_vibe_code spans), testing_duration_ms (sum of run_tests spans), ci_wait_duration_ms (sum of wait_ci spans), review_wait_duration_ms (sum of wait_pr_review spans), provision_duration_ms, iterations (int), test_runs (int), total_tests_executed (int), total_tokens_used (int), vibe_turns (int), tool_calls_count (int), files_modified_count (int), outcome (str — completed|failed|cancelled), failure_reason (str, nullable — "max_iterations"|"agent_error"|"cancelled")
  - Event types (original): `status_change`, `agent_message`, `tool_call`, `error`, `ci_result`, `test_results`, `pr_update`, `review_feedback`, `human_assist_request`, `human_assist_response`
  - Event types (observability — new): `span_start`, `span_end`, `agent_thinking`, `llm_call`, `vibe_summary`, `triage_summary`, `container_exec`, `test_case`, `test_summary`, `signal_received`
- [ ] **Step 3:** Implement `schemas.py`:
  - `CreateSessionRequest` (repo_url, task_description, max_iterations?, issue_number?: int, issue_type?: "bug"|"feature")
  - `SessionResponse` (full session — populated from workflow query + DB, includes issue_number, issue_type, issue_title, issue_url)
  - `AssistRequest` (guidance: str)
  - `EventResponse` (typed event payload)
  - `TestResultPayload` (total, passed, failed, skipped, failures: [{name, error, traceback}])
  - `TestCaseResult` (name: str, outcome: "passed"|"failed"|"skipped", duration_ms: int, error: str|None, traceback: str|None)
  - `CIResultPayload` (check_name, status, conclusion, details_url, test_results?)
  - `PRUpdatePayload` (pr_url, state, merged, mergeable, review_comments: [{author, body}])
  - `ConfidenceSummary` (files_changed: int, lines_added: int, lines_removed: int, new_dependencies: list[str], test_results: TestResultPayload, risk_flags: list[str], summary: str)
  - `VibeSummary` (turns: int, tool_calls: int, files_modified: list[str], tokens_used: int)
  - `SpanResponse` (id, trace_id, parent_span_id, name, kind, status, started_at, ended_at, duration_ms, metadata, error, children: list[SpanResponse])
  - `SessionMetricsResponse` (all SessionMetrics fields + computed: success_rate, avg_iterations, time_breakdown: {coding_pct, testing_pct, ci_wait_pct, review_wait_pct})
  - `TraceResponse` (trace_id, session_id, root_spans: list[SpanResponse], total_duration_ms, summary: SessionMetricsResponse)
- [ ] **Step 4:** Create `tests/conftest.py` — in-memory SQLite fixtures
- [ ] **Step 5:** Write + run `test_models.py` — CRUD for session and event
- [ ] **Step 6:** Commit

### Task 3: Session API (Thin Workflow Gateway)

**Files:** `app/api/sessions.py`, `tests/test_api_sessions.py`

The API is a thin shell — it starts, queries, and signals workflows. No business logic here.

- [ ] **Step 1:** Write failing tests for:
  - `POST /api/sessions` — creates DB record + starts workflow → returns session with workflow_run_id
  - `GET /api/sessions` — list sessions (from DB), with status filter
  - `GET /api/sessions/{id}` — session detail: DB record enriched with live workflow query (current state, test results, PR info)
  - `GET /api/issues/preview?repo_url=...&issue_number=N` — fetches issue title + labels from GitHub (for create form preview, no workflow needed)
  - `POST /api/sessions/{id}/assist` — validates status=needs_human, sends `human_assist` signal to workflow
  - `POST /api/sessions/{id}/cancel` — sends `cancel` signal to workflow
  - `GET /api/triage` — list triage results with eligibility filter
  - `GET /api/triage/{id}` — triage detail (from workflow query)
  - `POST /api/triage/{id}/approve` — for "needs_review" items: starts session workflow with triage context
  - `POST /api/triage/{id}/reject` — mark as rejected, no session created
  - `GET /api/sessions/{id}/trace` — full span tree for a session (nested SpanResponse with children)
  - `GET /api/sessions/{id}/metrics` — per-session metrics (time breakdown, token usage, iterations)
  - `GET /api/metrics/summary` — aggregate metrics across all sessions: {total_sessions, completed, failed, cancelled, success_rate, avg_iterations, avg_duration_ms, avg_coding_ms, avg_ci_wait_ms, common_failure_reasons: [{reason, count}], tokens_total, sessions_per_day: [{date, count}]}
- [ ] **Step 2:** Implement `sessions.py`:
  - POST: `db.add(session)` → `workflow_client.start(HydraSessionWorkflow, input)` → save workflow_run_id. If issue_number provided, pass it in workflow input (issue context fetched by workflow activity)
  - GET detail: `db.get(session)` + `workflow_client.query(workflow_run_id, "get_status")` → merge
  - POST assist: `workflow_client.signal(workflow_run_id, "human_assist", guidance)`
  - POST cancel: `workflow_client.signal(workflow_run_id, "cancel")`
- [ ] **Step 3:** Register router in `main.py`
- [ ] **Step 4:** Run tests — verify PASS
- [ ] **Step 5:** Commit

---

## Wave 2: Docker Sandbox + Workflow

### Task 4: Dockerfile + Sandbox Manager

**Files:** `Dockerfile.sandbox`, `app/sandbox/manager.py`, `tests/test_sandbox.py`

- [ ] **Step 1:** Write `Dockerfile.sandbox` — python:3.12-slim, install git + curl + mistral-vibe-code + pytest-json-report (for per-test structured output)
- [ ] **Step 2:** Build and verify image
- [ ] **Step 3:** Write failing tests for SandboxManager: create(), run_vibe(), run_tests(), run_git(), destroy()
- [ ] **Step 4:** Implement `manager.py` — all methods accept `tracer: Tracer` + `parent_span: Span` for observability:
  - `exec_in_container(container_id, cmd, tracer, parent_span)` → ExecResult — **single instrumentation point** for all container operations. Creates a `container_op` span with cmd, exit_code, stdout_lines, stderr (truncated). Every other method calls this.
  - `create(session_id, repo_url, token, api_key, tracer, parent_span)` → container_id. Emits child spans for docker.create + docker.start.
  - `run_vibe(container_id, prompt, max_turns, max_price, tracer, parent_span)` → VibeSummary. Parses Vibe CLI NDJSON stream into trace spans:
    - Creates a `vibe_session` span (kind="agent") as parent
    - Each `turn_start`/`turn_end` → `agent_turn_N` span (kind="agent_turn") nested under vibe_session
    - Each `thinking` event → `agent_thinking` event tied to the turn span
    - Each `tool_call` → child span (kind="tool_call") under the turn span, with tool name + args in metadata
    - Each `tool_result` → closes the tool_call span, stores result_preview (truncated to 500 chars). Tracks files_modified for file_write/file_edit tools.
    - Each `message` → `agent_message` event tied to the turn span
    - Each `error` → `agent_error` event on the vibe_session span
    - On stream end → emits `vibe_summary` event: {turns, tool_calls, files_modified, tokens_used}
  - `run_tests(container_id, tracer, parent_span)` → TestResultPayload. Runs `pytest --json-report --json-report-file=/tmp/report.json -v` inside container, then:
    - Creates a `run_tests_suite` span (kind="container_op")
    - Reads `/tmp/report.json` from container
    - Emits a `test_case` event for each individual test: {name (nodeid), outcome (passed/failed/skipped), duration_ms, error (longrepr for failures)}
    - Emits a `test_summary` event: {total, passed, failed, skipped, duration_ms}
  - `run_git(container_id, *args, tracer, parent_span)` → stdout. Wraps via exec_in_container, auto-instrumented.
  - `get_diff_stats(container_id, tracer, parent_span)` → {files_changed, lines_added, lines_removed, changed_files: list}
  - `destroy(container_id, tracer, parent_span)` — emits container.remove span
- [ ] **Step 5:** Run tests — verify PASS
- [ ] **Step 6:** Commit

### Task 5: Mistral Workflow (The Brain)

**Files:** `app/workflows/hydra_session.py`, `app/workflows/activities.py`, `app/event_bus.py`, `app/observability.py`, `tests/test_workflow.py`

This is the core of the application. The workflow owns all state, all decisions, all transitions.

- [ ] **Step 1:** Implement `event_bus.py` — publish/subscribe per session_id, persists events to DB

- [ ] **Step 1.5:** Implement `app/observability.py` — lightweight Tracer (no external deps, writes to DB + event bus):
  ```python
  class Tracer:
      """Every workflow run = 1 trace. Every activity/phase/wait = 1 span.
      Spans nest. Events attach to spans. Everything flows to UI via event bus."""

      def __init__(self, session_id: str, trace_id: str, event_bus: EventBus, db: AsyncSession):
          self.session_id = session_id
          self.trace_id = trace_id   # = workflow_run_id
          self.event_bus = event_bus
          self.db = db

      @asynccontextmanager
      async def span(self, name: str, kind: str = "activity",
                     parent: Span | None = None, **metadata):
          """Context manager — auto-tracks start/end/error/duration.
          Publishes span_start and span_end events to UI via event bus."""
          s = Span(trace_id=self.trace_id, parent_span_id=parent.id if parent else None,
                   session_id=self.session_id, name=name, kind=kind,
                   status="running", started_at=utcnow(), metadata=metadata)
          await self._persist_span(s)
          await self.event_bus.publish(self.session_id, "span_start", {
              "span_id": str(s.id), "name": name, "kind": kind, **metadata})
          try:
              yield s
              s.status = "completed"
          except Exception as e:
              s.status = "failed"
              s.error = str(e)
              raise
          finally:
              s.ended_at = utcnow()
              s.duration_ms = int((s.ended_at - s.started_at).total_seconds() * 1000)
              await self._persist_span(s)
              await self.event_bus.publish(self.session_id, "span_end", {
                  "span_id": str(s.id), "name": name, "status": s.status,
                  "duration_ms": s.duration_ms, "error": s.error})

      async def start_span(self, name: str, kind: str = "activity",
                           parent: Span | None = None, **metadata) -> Span:
          """Manual start — for streaming where spans cross async boundaries (e.g. Vibe turns).
          Must call end_span() later."""
          s = Span(...)
          await self._persist_span(s)
          await self.event_bus.publish(self.session_id, "span_start", {...})
          return s

      async def end_span(self, span: Span, status: str = "completed", error: str | None = None):
          """Close a manually-started span."""
          span.status = status
          span.error = error
          span.ended_at = utcnow()
          span.duration_ms = int((span.ended_at - span.started_at).total_seconds() * 1000)
          await self._persist_span(span)
          await self.event_bus.publish(self.session_id, "span_end", {...})

      async def event(self, span: Span, event_type: str, payload: dict):
          """Emit an event tied to a span — flows to UI via event bus."""
          evt = SessionEvent(session_id=self.session_id, span_id=span.id,
                             trace_id=self.trace_id, event_type=event_type,
                             payload=payload)
          await self._persist_event(evt)
          await self.event_bus.publish(self.session_id, event_type, {
              **payload, "span_id": str(span.id), "trace_id": str(self.trace_id)})

      async def compute_session_metrics(self) -> SessionMetrics:
          """Aggregate all spans into SessionMetrics — called on workflow completion."""
          spans = await self._get_all_spans()
          return SessionMetrics(
              session_id=self.session_id,
              total_duration_ms=...,           # root span duration
              coding_duration_ms=...,          # sum of kind="agent" spans
              testing_duration_ms=...,         # sum of name="run_tests_suite" spans
              ci_wait_duration_ms=...,         # sum of name="wait_ci" spans
              review_wait_duration_ms=...,     # sum of name="wait_review" spans
              provision_duration_ms=...,       # sum of name="provision_sandbox" spans
              iterations=...,                  # count of "coding_iteration_*" phase spans
              test_runs=...,                   # count of "run_tests_suite" spans
              total_tests_executed=...,        # sum of test_summary.total from events
              total_tokens_used=...,           # sum of vibe_summary.tokens + llm_call tokens
              vibe_turns=...,                  # sum of vibe_summary.turns
              tool_calls_count=...,            # count of kind="tool_call" spans
              files_modified_count=...,        # union of vibe_summary.files_modified
              outcome=..., failure_reason=...)
  ```

- [ ] **Step 2:** Define workflow state + queries in `hydra_session.py`:
  ```python
  @dataclass
  class WorkflowState:
      status: str = "pending"
      iteration: int = 0
      max_iterations: int = 3
      container_id: str | None = None
      branch_name: str | None = None
      pr_url: str | None = None
      pr_merged: bool = False
      test_results: TestResultPayload | None = None
      ci_results: list[CIResultPayload] = field(default_factory=list)
      error_summary: str | None = None
      human_assists: list[str] = field(default_factory=list)
      review_comments: list[dict] = field(default_factory=list)  # [{author, body}]
      # Confidence summary (generated before PR, updated after each iteration)
      confidence: ConfidenceSummary | None = None
      # Triage context (if started from triage)
      triage_suggested_approach: str | None = None
      triage_relevant_files: list[str] = field(default_factory=list)
      # Issue context
      issue_number: int | None = None
      issue_type: str | None = None  # "bug" | "feature"
      issue_title: str | None = None
      issue_body: str | None = None
      issue_labels: list[str] = field(default_factory=list)

  @workflow.query
  def get_status(self) -> WorkflowState:
      return self.state

  @workflow.query
  def get_test_results(self) -> TestResultPayload | None:
      return self.state.test_results

  @workflow.query
  def get_pr_info(self) -> dict:
      return {"pr_url": self.state.pr_url, "merged": self.state.pr_merged, ...}

  @workflow.query
  def get_confidence(self) -> ConfidenceSummary | None:
      return self.state.confidence
  ```

- [ ] **Step 3:** Define signal handlers:
  ```python
  @workflow.signal
  async def ci_result(self, payload: CIResultPayload):
      self.state.ci_results.append(payload)
      self._ci_event.set()

  @workflow.signal
  async def human_assist(self, guidance: str):
      self.state.human_assists.append(guidance)
      self._human_event.set()

  @workflow.signal
  async def review_feedback(self, payload: ReviewFeedbackPayload):
      self.state.review_comments.extend(payload.comments)
      self._review_event.set()

  @workflow.signal
  async def cancel(self):
      self._cancelled = True
      self._ci_event.set()  # unblock any wait
      self._human_event.set()
      self._review_event.set()
  ```

- [ ] **Step 4:** Implement main workflow run — the decision engine. **Every phase is a span, every activity call is nested inside its phase span, the tracer is threaded through to activities so container operations get child spans too.**
  ```python
  @workflow.run
  async def run(self, input: SessionInput):
      self.state.status = "running"
      self.tracer = Tracer(session_id=input.session_id,
                           trace_id=self.workflow_id, ...)

      # ── Phase 0: Setup ──────────────────────────────────
      async with self.tracer.span("setup", kind="phase") as setup:

          # Fetch issue context (if issue# provided)
          if input.issue_number:
              async with self.tracer.span("fetch_issue", kind="activity", parent=setup) as s:
                  issue = await workflow.execute_activity(
                      fetch_issue, owner=input.owner, repo=input.repo,
                      issue_number=input.issue_number, tracer=self.tracer, parent_span=s)
                  self.state.issue_number = input.issue_number
                  self.state.issue_type = input.issue_type or self._infer_type(issue.labels)
                  self.state.issue_title = issue.title
                  self.state.issue_body = issue.body
                  self.state.issue_labels = issue.labels

          # Build agent prompt from task + issue context
          prompt = self._build_prompt(input.task, self.state)

          # Provision sandbox
          async with self.tracer.span("provision_sandbox", kind="activity", parent=setup) as s:
              self.state.container_id = await workflow.execute_activity(
                  provision_sandbox, session_id=input.session_id, ...,
                  tracer=self.tracer, parent_span=s)

          # Clone repo
          async with self.tracer.span("clone_repo", kind="activity", parent=setup) as s:
              await workflow.execute_activity(clone_repo, ...,
                  tracer=self.tracer, parent_span=s)

      # ── Phase 1: Coding iteration 1 ─────────────────────
      async with self.tracer.span("coding_iteration_1", kind="phase", iteration=1) as iter_phase:

          # Code with Vibe
          async with self.tracer.span("run_vibe_code", kind="activity", parent=iter_phase,
                                      purpose="initial_coding") as s:
              await workflow.execute_activity(run_vibe_code, ..., prompt=prompt,
                  tracer=self.tracer, parent_span=s)
              # run_vibe_code internally creates agent_turn + tool_call child spans
              # and emits agent_thinking, agent_message, vibe_summary events

          # Test locally before pushing
          async with self.tracer.span("run_tests", kind="activity", parent=iter_phase,
                                      purpose="initial_validation") as s:
              self.state.test_results = await workflow.execute_activity(run_tests, ...,
                  tracer=self.tracer, parent_span=s)
              # run_tests internally emits test_case events + test_summary event

          if not self.state.test_results.all_passed:
              # Auto-fix attempt
              async with self.tracer.span("run_vibe_code", kind="activity", parent=iter_phase,
                                          purpose="auto_fix_tests") as s:
                  await workflow.execute_activity(run_vibe_code, ...,
                      prompt=f"Fix failing tests:\n{self.state.test_results.failure_summary}",
                      tracer=self.tracer, parent_span=s)

              async with self.tracer.span("run_tests", kind="activity", parent=iter_phase,
                                          purpose="verify_fix") as s:
                  self.state.test_results = await workflow.execute_activity(run_tests, ...,
                      tracer=self.tracer, parent_span=s)

      # ── Phase 2: PR creation ─────────────────────────────
      async with self.tracer.span("pr_creation", kind="phase") as pr_phase:
          async with self.tracer.span("run_vibe_review", kind="activity", parent=pr_phase) as s:
              await workflow.execute_activity(run_vibe_review, ...,
                  tracer=self.tracer, parent_span=s)

          async with self.tracer.span("generate_confidence", kind="activity", parent=pr_phase) as s:
              self.state.confidence = await workflow.execute_activity(
                  generate_confidence_summary, ..., tracer=self.tracer, parent_span=s)

          async with self.tracer.span("commit_and_push", kind="activity", parent=pr_phase) as s:
              await workflow.execute_activity(commit_and_push, ...,
                  tracer=self.tracer, parent_span=s)
              # internally emits container_op spans for git add, git commit, git push

          async with self.tracer.span("open_pr", kind="activity", parent=pr_phase) as s:
              self.state.pr_url = await workflow.execute_activity(
                  open_pr, ..., confidence=self.state.confidence,
                  tracer=self.tracer, parent_span=s)
              # internally emits http span for GitHub API call

      # ── Phase 3: CI monitoring loop ──────────────────────
      self.state.status = "ci_monitoring"
      for i in range(self.state.max_iterations):
          self.state.iteration = i + 1
          async with self.tracer.span(f"ci_iteration_{i+1}", kind="phase",
                                      iteration=i+1) as ci_phase:

              # Wait for CI result signal
              async with self.tracer.span("wait_ci", kind="wait", parent=ci_phase) as s:
                  await self._ci_event.wait()
                  self._ci_event.clear()
                  # Signal handler already emitted signal_received event

              if self._cancelled:
                  return await self._cleanup("cancelled")

              latest_ci = self.state.ci_results[-1]
              if latest_ci.passed:
                  # ── Phase 4: PR review ───────────────────
                  self.state.status = "pr_review"
                  async with self.tracer.span("pr_review", kind="phase") as review_phase:
                      async with self.tracer.span("wait_review", kind="wait",
                                                  parent=review_phase) as s:
                          await self._review_event.wait()
                          self._review_event.clear()

                      if self._cancelled:
                          return await self._cleanup("cancelled")

                      if self.state.review_comments:
                          # Address review feedback
                          feedback = "\n".join(c["body"] for c in self.state.review_comments)
                          self.state.status = "running"

                          async with self.tracer.span("run_vibe_code", kind="activity",
                                                      parent=review_phase,
                                                      purpose="address_review") as s:
                              await workflow.execute_activity(run_vibe_code, ...,
                                  prompt=f"PR review feedback:\n{feedback}\nAddress these comments.",
                                  tracer=self.tracer, parent_span=s)

                          async with self.tracer.span("run_tests", kind="activity",
                                                      parent=review_phase) as s:
                              self.state.test_results = await workflow.execute_activity(
                                  run_tests, ..., tracer=self.tracer, parent_span=s)

                          async with self.tracer.span("commit_and_push", kind="activity",
                                                      parent=review_phase) as s:
                              await workflow.execute_activity(commit_and_push, ...,
                                  tracer=self.tracer, parent_span=s)

                          self.state.review_comments = []
                          continue  # back to CI wait

                      return await self._cleanup("completed")

              # CI failed — try auto-fix
              async with self.tracer.span("run_vibe_code", kind="activity", parent=ci_phase,
                                          purpose="ci_fix") as s:
                  await workflow.execute_activity(run_vibe_code, ...,
                      prompt=f"CI failed:\n{latest_ci.error_details}",
                      tracer=self.tracer, parent_span=s)

              async with self.tracer.span("run_tests", kind="activity", parent=ci_phase) as s:
                  self.state.test_results = await workflow.execute_activity(run_tests, ...,
                      tracer=self.tracer, parent_span=s)

              if not self.state.test_results.all_passed:
                  # Agent stuck — ask human
                  self.state.status = "needs_human"
                  self.state.error_summary = self._build_error_context()

                  async with self.tracer.span("human_assist", kind="phase",
                                              parent=ci_phase) as hitl_phase:
                      await self.tracer.event(hitl_phase, "human_assist_request", {
                          "error": self.state.error_summary,
                          "iteration": self.state.iteration,
                          "test_results": self.state.test_results})

                      async with self.tracer.span("wait_human", kind="wait",
                                                  parent=hitl_phase) as s:
                          await self._human_event.wait()
                          self._human_event.clear()

                      if self._cancelled:
                          return await self._cleanup("cancelled")

                      # Resume with human guidance
                      hint = self.state.human_assists[-1]
                      self.state.status = "running"

                      async with self.tracer.span("run_vibe_code", kind="activity",
                                                  parent=hitl_phase,
                                                  purpose="human_guided_fix") as s:
                          await workflow.execute_activity(run_vibe_code, ...,
                              prompt=f"Human guidance: {hint}\nFix the issues.",
                              tracer=self.tracer, parent_span=s)

                      async with self.tracer.span("run_tests", kind="activity",
                                                  parent=hitl_phase) as s:
                          self.state.test_results = await workflow.execute_activity(
                              run_tests, ..., tracer=self.tracer, parent_span=s)

              async with self.tracer.span("commit_and_push", kind="activity",
                                          parent=ci_phase) as s:
                  await workflow.execute_activity(commit_and_push, ...,
                      tracer=self.tracer, parent_span=s)

      return await self._cleanup("failed")  # max iterations exceeded

  async def _cleanup(self, final_status):
      self.state.status = final_status
      async with self.tracer.span("cleanup", kind="activity") as s:
          if self.state.container_id:
              await workflow.execute_activity(destroy_sandbox, ...,
                  tracer=self.tracer, parent_span=s)
          await self.tracer.event(s, "status_change", {"status": final_status})

      # Compute and persist aggregate metrics
      await self.tracer.compute_session_metrics()
      return self.state
  ```

  **Signal handlers also emit trace events:**
  ```python
  @workflow.signal
  async def ci_result(self, payload: CIResultPayload):
      self.state.ci_results.append(payload)
      if self.tracer and self._current_wait_span:
          await self.tracer.event(self._current_wait_span, "signal_received", {
              "signal": "ci_result", "conclusion": payload.conclusion,
              "check_name": payload.check_name})
      self._ci_event.set()

  @workflow.signal
  async def human_assist(self, guidance: str):
      self.state.human_assists.append(guidance)
      if self.tracer and self._current_wait_span:
          await self.tracer.event(self._current_wait_span, "signal_received", {
              "signal": "human_assist", "guidance_length": len(guidance)})
      self._human_event.set()

  @workflow.signal
  async def review_feedback(self, payload: ReviewFeedbackPayload):
      self.state.review_comments.extend(payload.comments)
      if self.tracer and self._current_wait_span:
          await self.tracer.event(self._current_wait_span, "signal_received", {
              "signal": "review_feedback", "action": payload.action,
              "comments_count": len(payload.comments)})
      self._review_event.set()

  @workflow.signal
  async def cancel(self):
      self._cancelled = True
      if self.tracer and self._current_wait_span:
          await self.tracer.event(self._current_wait_span, "signal_received", {
              "signal": "cancel"})
      self._ci_event.set()
      self._human_event.set()
      self._review_event.set()
  ```

- [ ] **Step 5:** Implement `activities.py` — each activity receives `tracer: Tracer` + `parent_span: Span` and delegates to SandboxManager / GitHubClient which create child spans internally:
  - `fetch_issue(tracer, parent_span)` → GitHubClient.get_issue() — internally emits http span for the API call. Returns title, body, labels for prompt context.
  - `provision_sandbox(tracer, parent_span)` → SandboxManager.create() — internally emits container_op spans for docker.create + docker.start
  - `clone_repo(tracer, parent_span)` → SandboxManager.run_git("clone", ...) — internally emits container_op span via exec_in_container
  - `run_vibe_code(tracer, parent_span)` → SandboxManager.run_vibe() — internally creates the full agent trace:
    - `vibe_session` span (kind="agent") as parent
    - `agent_turn_N` spans (kind="agent_turn") per turn
    - `tool_call` spans (kind="tool_call") per tool invocation, nested under turns
    - Events: `agent_thinking`, `agent_message`, `agent_error`, `vibe_summary`
  - `run_vibe_review(tracer, parent_span)` → same as run_vibe_code with review prompt
  - `run_tests(tracer, parent_span)` → SandboxManager.run_tests() — internally:
    - Emits `container_op` span for the pytest execution
    - Emits `test_case` event per individual test (name, outcome, duration_ms, error)
    - Emits `test_summary` event (total, passed, failed, skipped, duration_ms)
  - `generate_confidence_summary(tracer, parent_span)` → runs `git diff --stat` (container_op span), counts files/lines changed, detects new dependencies, flags risks via Mistral agent call (emits `llm_call` event with token counts) → returns ConfidenceSummary
  - `commit_and_push(tracer, parent_span)` → SandboxManager.run_git() series — each git command (add, commit, push) gets its own container_op span via exec_in_container
  - `open_pr(tracer, parent_span)` → GitHubClient.create_pull_request() — emits http span. PR body includes:
    - `Fixes #N` (bug) or `Implements #N` (feature) when issue linked
    - Confidence summary block: files changed, lines +/-, test results, risk flags
    - Triage context (if from triage): suggested approach, relevant files
  - `destroy_sandbox(tracer, parent_span)` → SandboxManager.destroy() — emits container.remove span

  Prompt builder (`_build_prompt`):
  - No issue: just the task description
  - With issue: structured prompt combining task + issue title + issue body + labels + issue type (bug→"fix this bug", feature→"implement this feature")
  - With triage context: adds suggested approach + relevant files list from triage agent analysis

- [ ] **Step 6:** Write failing test — happy path (CI passes first time)
- [ ] **Step 7:** Write failing test — CI fails, agent auto-fixes, CI passes
- [ ] **Step 8:** Write failing test — CI fails, agent can't fix, requests human help, human responds, agent fixes
- [ ] **Step 9:** Write failing test — max iterations reached, workflow fails with error summary
- [ ] **Step 10:** Write failing test — cancel signal at any point triggers cleanup
- [ ] **Step 11:** Write failing test — CI passes, PR review requests changes, agent re-codes, CI passes again, PR approved → completed
- [ ] **Step 12:** Write failing test — workflow queries return correct state at each phase
- [ ] **Step 13:** Write failing test — **trace completeness**: after happy path, verify:
  - Span tree has correct nesting: trace → phases → activities → container_ops/agent_turns → tool_calls
  - Every span has started_at, ended_at, duration_ms, status
  - Every activity span has at least one child span (container_op or agent_turn)
  - `vibe_summary` events exist with turns, tool_calls, tokens_used
  - `test_case` events exist for each individual test
  - `test_summary` events match the TestResultPayload
  - `signal_received` events logged for ci_result, review_feedback
  - No orphan events (every event has a valid span_id)
  - No orphan spans (every span except root has a valid parent_span_id)
- [ ] **Step 14:** Write failing test — **SessionMetrics computed correctly** after workflow completion:
  - coding_duration_ms = sum of run_vibe_code span durations
  - testing_duration_ms = sum of run_tests spans
  - ci_wait_duration_ms = sum of wait_ci spans
  - total_tokens_used > 0
  - outcome matches final workflow status
- [ ] **Step 15:** Run all tests — verify PASS
- [ ] **Step 16:** Commit

---

## Wave 3: Triage Agent Workflow

### Task 6: Triage Workflow + Agent Functions

**Files:** `app/workflows/triage.py`, `app/workflows/triage_functions.py`, `tests/test_triage.py`

The triage workflow uses Mistral Agents API directly (NOT Vibe CLI) with function calling to analyze issues. This showcases Agents API + function calling as a standalone capability outside of Vibe.

- [ ] **Step 1:** Implement `triage_functions.py` — function calling tools the triage agent can use:
  ```python
  # Functions registered with Mistral Agents API
  tools = [
      {
          "name": "get_repo_file_tree",
          "description": "List files/dirs in the repo to understand structure",
          "parameters": {"path": "optional subdirectory"}
      },
      {
          "name": "read_file",
          "description": "Read a file from the repo to understand code",
          "parameters": {"path": "file path"}
      },
      {
          "name": "search_code",
          "description": "Search for a pattern across the codebase",
          "parameters": {"query": "search string or regex"}
      },
      {
          "name": "get_recent_commits",
          "description": "Get recent commits to understand recent changes",
          "parameters": {"path": "optional file path filter", "limit": "number"}
      },
      {
          "name": "find_similar_issues",
          "description": "Search closed issues for similar problems",
          "parameters": {"query": "search terms"}
      }
  ]
  ```
  Each function calls GitHub API (file tree, contents, search, commits, issues search) via httpx.

- [ ] **Step 2:** Implement `triage.py` — Mistral Workflow:
  ```python
  @workflow.define
  class TriageWorkflow:
      @dataclass
      class TriageState:
          status: str = "analyzing"  # analyzing | triaged
          issue_number: int = 0
          issue_title: str = ""
          issue_body: str = ""
          issue_labels: list[str] = field(default_factory=list)
          eligibility: str = ""     # auto_assign | needs_review | not_eligible
          complexity: str = ""      # simple | medium | complex
          issue_type: str = ""      # bug | feature
          relevant_files: list[str] = field(default_factory=list)
          suggested_approach: str = ""
          reasoning: str = ""
          session_id: str | None = None  # set if auto-assigned

      @workflow.query
      def get_triage_result(self) -> TriageState:
          return self.state

      @workflow.run
      async def run(self, input: TriageInput):
          # 1. Fetch issue details
          issue = await workflow.execute_activity(fetch_issue, ...)
          self.state.issue_title = issue.title
          self.state.issue_body = issue.body
          self.state.issue_labels = issue.labels

          # 2. Run Mistral agent with function calling to analyze
          analysis = await workflow.execute_activity(
              run_triage_agent,
              issue=issue,
              repo_url=input.repo_url,
              system_prompt=TRIAGE_SYSTEM_PROMPT)
          # Agent uses tools to explore repo, then returns structured decision

          # 3. Apply results
          self.state.eligibility = analysis.eligibility
          self.state.complexity = analysis.complexity
          self.state.issue_type = analysis.issue_type
          self.state.relevant_files = analysis.relevant_files
          self.state.suggested_approach = analysis.suggested_approach
          self.state.reasoning = analysis.reasoning

          # 4. Take action based on eligibility
          await workflow.execute_activity(label_issue, ...,
              labels=self._compute_labels())
          await workflow.execute_activity(comment_on_issue, ...,
              body=self._format_triage_comment())

          if self.state.eligibility == "auto_assign":
              # Auto-start session workflow
              session_id = await workflow.execute_activity(
                  create_and_start_session,
                  repo_url=input.repo_url,
                  issue_number=input.issue_number,
                  task=self.state.suggested_approach,
                  triage_context=self.state)
              self.state.session_id = session_id

          self.state.status = "triaged"
          return self.state
  ```

  `TRIAGE_SYSTEM_PROMPT`:
  ```
  You are a triage agent for a software project. Analyze the GitHub issue and determine:
  1. Type: Is this a bug fix or a new feature?
  2. Complexity: Simple (< 50 lines, single file), Medium (multi-file, tests needed), Complex (architectural changes)
  3. Eligibility: Can an AI coding agent handle this autonomously?
     - auto_assign: Simple bugs with clear repro, small features with clear spec
     - needs_review: Medium complexity, team should approve before agent starts
     - not_eligible: Complex, ambiguous, security-sensitive, or requires human judgment
  4. Relevant files: Which files in the codebase are likely involved?
  5. Suggested approach: Brief plan for how to fix/implement this

  Use the available tools to explore the codebase before making your decision.
  ```

- [ ] **Step 3:** Implement `run_triage_agent` activity — fully instrumented with tracer:
  - Creates Mistral agent (Agents API) with tools from `triage_functions.py`
  - Creates a `triage_agent` span (kind="agent") as parent
  - Runs agent loop, emitting trace data at each step:
    - Each agent turn → `agent_turn_N` span (kind="agent_turn") nested under triage_agent
    - Each LLM call → `llm_call` event with model name, input_tokens, output_tokens
    - Each function call → `tool_call` span (kind="tool_call") nested under turn span, with tool name + args in metadata
    - Each function result → closes tool_call span, stores result_preview (truncated)
    - Agent thinking/reasoning → `agent_thinking` event tied to turn span
    - Final answer → `agent_message` event with structured analysis
  - On completion → emits `triage_summary` event: {turns, tools_used, decision, total_tokens}
  - Uses Mistral Medium 3.5 as the model
  - Parses agent's final response into structured TriageResult

- [ ] **Step 4:** Implement helper activities — all receive `tracer` + `parent_span`:
  - `label_issue(tracer, parent_span)` → GitHubClient: add labels — emits http span
  - `comment_on_issue(tracer, parent_span)` → GitHubClient: post formatted triage summary — emits http span
  - `create_and_start_session(tracer, parent_span)` → creates HydraSession in DB + starts Session Workflow, passing triage context

- [ ] **Step 5:** Write failing test — simple bug issue → analyzed → auto_assign → session started
- [ ] **Step 6:** Write failing test — complex issue → analyzed → not_eligible → labeled + comment posted
- [ ] **Step 7:** Write failing test — medium feature → needs_review → labeled, no auto-start
- [ ] **Step 8:** Write failing test — triage agent uses function calling tools correctly (reads files, searches code)
- [ ] **Step 9:** Write failing test — triage query returns correct state
- [ ] **Step 10:** Write failing test — **triage trace completeness**: verify triage_agent span contains agent_turn spans, each turn has llm_call event with token counts, tool_call spans have result_preview, triage_summary event has final decision
- [ ] **Step 11:** Run all tests — verify PASS
- [ ] **Step 12:** Commit

---

## Wave 4: GitHub Integration + Webhook Signals

### Task 7: GitHub Client

**Files:** `app/integrations/github_client.py`, `tests/test_github_client.py`

- [ ] **Step 1:** Write failing tests for:
  - `get_issue(owner, repo, issue_number)` → {title, body, labels, state, url}
  - `get_repo_tree(owner, repo, path?)` → file/dir listing (used by triage agent)
  - `get_file_content(owner, repo, path)` → file contents (used by triage agent)
  - `search_code(owner, repo, query)` → matching files + snippets (used by triage agent)
  - `get_recent_commits(owner, repo, path?, limit?)` → commit list
  - `search_issues(owner, repo, query, state?)` → similar issues
  - `add_labels(owner, repo, issue_number, labels)` → adds labels
  - `create_issue_comment(owner, repo, issue_number, body)` → posts comment
  - `create_pull_request(owner, repo, head, base, title, body)` → {pr_url, pr_number} — body includes `Fixes #N` or `Implements #N` when issue linked
  - `get_check_runs(owner, repo, ref)` → list of {name, status, conclusion, details_url}
  - `get_pr_status(owner, repo, pr_number)` → {state, merged, mergeable, review_comments}
- [ ] **Step 2:** Implement with httpx async client. Add a `_request` wrapper that accepts optional `tracer: Tracer` + `parent_span: Span` — when provided, wraps each HTTP call in an `http` span with: url, method, status_code, response_bytes. This means every GitHub API call made from activities or triage functions automatically appears in the trace tree.
- [ ] **Step 3:** Run tests — verify PASS
- [ ] **Step 4:** Commit

### Task 8: Webhook Handler (Signal Relay to Workflows)

**Files:** `app/api/webhooks.py`, `tests/test_webhooks.py`

The webhook handler is a pure signal relay — it does zero business logic. It verifies, parses, and forwards to the correct workflow.

- [ ] **Step 1:** Write failing test — `issues.opened` webhook → starts Triage Workflow
- [ ] **Step 2:** Write failing test — `check_suite.completed` webhook → sends `ci_result` signal to Session Workflow
- [ ] **Step 3:** Write failing test — `pull_request_review.submitted` with "changes_requested" → sends `review_feedback` signal to Session Workflow
- [ ] **Step 4:** Write failing test — `pull_request_review.submitted` with "approved" → sends `review_feedback` signal (empty comments) to Session Workflow
- [ ] **Step 5:** Write failing test — invalid HMAC signature returns 403
- [ ] **Step 6:** Implement `webhooks.py`:
  - HMAC-SHA256 signature verification
  - Route by event type:
    - `issues.opened` → start Triage Workflow with repo_url + issue_number
    - `check_suite` / `check_run` completed → look up session by branch → `session_workflow.signal("ci_result", payload)`
    - `pull_request_review.submitted` → look up session by PR → `session_workflow.signal("review_feedback", payload)`
- [ ] **Step 7:** Run tests — verify PASS
- [ ] **Step 8:** Commit

---

## Wave 5: UI + Slack + Polish

### Task 9: SSE Endpoint + Rich Web UI

**Files:** `app/api/events.py`, `static/*`, `tests/test_events_sse.py`

- [ ] **Step 1:** Write failing test for SSE endpoint — subscribe, receive events
- [ ] **Step 2:** Implement `events.py` — replay past events from DB, then stream live from event bus

- [ ] **Step 3:** Build `index.html` — Dashboard with nav tabs: **Sessions** | **Triage Queue** | **Metrics**

  **Sessions tab:**
  - Session list with status badges (color-coded)
  - Status filter tabs: All | Running | Needs Human | PR Review | Completed | Failed
  - "Needs Human" count badge + "PR Review" count badge in nav (attention indicators)
  - Create session form:
    - Repo URL (required)
    - Task description (required, textarea)
    - Issue # (optional, number input) — when provided, fetches issue title preview via API
    - Type toggle: Bug Fix | New Feature (auto-detected from issue labels if issue# given, manual override available)
    - Max iterations (optional, default 3)
  - Each card: repo, task excerpt, status, iteration N/max, elapsed time, issue badge (🐛 #42 or ✨ #15) if linked
  - Cards from triage show "Triaged" origin badge

  **Triage Queue tab** (`triage.html`):
  - List of triaged issues with eligibility indicators
  - Filter: All | Auto-Assigned | Needs Review | Not Eligible
  - Each triage card shows:
    - Issue title + number (clickable to GitHub)
    - Type badge (🐛 Bug / ✨ Feature)
    - Complexity badge (Simple=green / Medium=amber / Complex=red)
    - Eligibility badge (✅ Auto-assigned / ⚠️ Needs review / ❌ Not eligible)
    - Relevant files list (collapsed)
    - Suggested approach (collapsed)
    - Agent reasoning (collapsed)
    - For "needs_review": "Approve & Start" button → starts session workflow with triage context
    - Link to session if already auto-assigned

- [ ] **Step 4:** Build `session.html` — Session Detail (all data from workflow queries + events):

  **Header:**
  - Status badge (live-updating via SSE)
  - Repo link, branch name
  - Issue badge if linked: "🐛 Bug Fix #42: Login page crashes" or "✨ Feature #15: Add dark mode" — clickable link to GitHub issue
  - Iteration progress: "Iteration 2 of 3"
  - Elapsed time

  **Event Stream Panel** (live via SSE):
  - `status_change` → colored status transition badge with timestamp
  - `agent_message` → chat bubble (dark bg, monospace for code)
  - `tool_call` → collapsible block: tool name + args + output
  - `error` → red alert banner
  - `human_assist_request` → orange alert with full error context

  **Test Results Panel** (updates each iteration):
  - Summary bar: ✓ 12 passed · ✗ 2 failed · ○ 1 skipped
  - Expandable failure list: test name + assertion error + traceback
  - History toggle: show results per iteration

  **CI Checks Panel** (updates each ci_result event):
  - Per-check row: name | status icon (✓/✗/⏳) | conclusion | link
  - Iteration history: "Iter 1: ✗ 2 failed → Iter 2: ✗ 1 failed → Iter 3: ✓ all passed"

  **Confidence Summary Panel** (visible after agent finishes coding, before/during PR review):
  - Change stats: "Changed 3 files · +47 lines · -12 lines"
  - Changed files list (clickable to GitHub diff)
  - Test results summary: "✓ 47 passed · 0 failed · 0 skipped"
  - New dependencies: list (or "None added")
  - Risk flags (if any): amber warnings like "⚠ Modified authentication logic", "⚠ New dependency: pyjwt", "⚠ Changed database schema"
  - Overall confidence indicator: green "Low risk" / amber "Review carefully" / red "High risk — manual review recommended"
  - This helps reviewers quickly assess: is this a safe 3-line fix or a risky refactor?

  **Triage Context Panel** (visible if session was triaged):
  - Triage agent's analysis: type, complexity, relevant files, suggested approach
  - Agent reasoning (collapsible)
  - Link to original issue

  **PR Panel** (updates on pr_update + review_feedback events):
  - PR link (clickable), state badge (open/merged/closed)
  - Mergeable indicator (✓ no conflicts / ✗ conflicts)
  - Review status: Pending / Changes Requested / Approved
  - Review comments list (author + body)
  - If changes_requested: shows "Agent is addressing review feedback..." status

  **Human Assist Panel** (visible when status=needs_human):
  - "Agent is stuck" header
  - What was attempted (iteration history summary)
  - Current error context (test failures, CI errors)
  - Text area for human guidance
  - "Resume with Guidance" button → POST /api/sessions/{id}/assist
  - "Cancel Session" button → POST /api/sessions/{id}/cancel

  **Trace Timeline Panel** (waterfall view — fetched from `GET /api/sessions/{id}/trace`):
  - Gantt-style horizontal bar chart showing every span, nested by parent
  - Each bar: span name | duration | status color (green=completed, red=failed, gray=running, hollow=wait)
  - Nested indentation: phase → activity → container_op/agent_turn → tool_call
  - Click a span to expand and show:
    - Child spans (nested bars)
    - Events emitted during this span (agent_thinking, tool_call results, test_case results)
    - Metadata (cmd, exit_code, prompt_length, tool args, etc.)
    - Error details if failed
  - Wait spans (kind="wait") shown as hollow/dashed bars to distinguish wall-clock time from compute time
  - Live-updating: new span_start/span_end SSE events append to the timeline as the workflow runs
  - Example rendering:
    ```
    setup                    ██████░░░░░░░░░░░░░░░░░░░░░░░░░  3.4s
      fetch_issue              ██░░░░░░░░░░░░░░░░░░░░░░░░░░░  0.3s
        http GET /issues/42      █░░░░░░░░░░░░░░░░░░░░░░░░░░  0.3s → 200
      provision_sandbox          ████░░░░░░░░░░░░░░░░░░░░░░░  2.0s
        docker.create              ███░░░░░░░░░░░░░░░░░░░░░░  1.4s
        docker.start                  █░░░░░░░░░░░░░░░░░░░░░  0.6s
      clone_repo                        ██░░░░░░░░░░░░░░░░░░  1.1s
    coding_iteration_1                    █████████████████░░ 61.7s
      run_vibe_code                       ████████████████░░░ 45.2s
        agent_turn_1                        ████░░░░░░░░░░░░░  8.3s
          tool: file_read(auth.py)            █░░░░░░░░░░░░░░  0.1s
          tool: file_read(test_auth.py)        █░░░░░░░░░░░░░  0.1s
        agent_turn_2                              █████░░░░░░ 12.1s
          tool: file_write(auth.py)                 █░░░░░░░░  0.1s
          tool: file_write(test_auth.py)             █░░░░░░░  0.1s
        ...
      run_tests                                          ████  3.4s
    wait_ci                  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 128.0s
    ```

  **Agent Replay Panel** (step-by-step agent execution — built from agent_turn spans + events):
  - For each Vibe invocation (run_vibe_code span):
    - Header: purpose ("initial_coding" / "auto_fix_tests" / "ci_fix" / "address_review"), duration, turn count
    - For each agent turn:
      - Thinking bubbles (from `agent_thinking` events) — dark background, italic
      - Tool call blocks (from `tool_call` spans): tool name, args, result preview, duration
      - Agent messages (from `agent_message` events) — chat bubble style
    - Summary footer: turns used, tools called, files modified, tokens consumed
  - Collapsible by default — expand each Vibe invocation to see turns
  - Useful for understanding *why* the agent made certain decisions

  **Test Drilldown Panel** (built from `test_case` + `test_summary` events across iterations):
  - Per-iteration test results table:
    - Columns: Test Name | Iter 1 | Iter 2 | Iter 3 | ...
    - Cells: pass/fail/skip icon + duration_ms
    - Failed cells expandable to show error + traceback
  - Highlights tests that flipped: "test_token_expiry: FAIL → FAIL → PASS"
  - Summary row per iteration: "11/12 passed" → "12/12 passed"
  - Helps identify which specific tests the agent struggled with

  **Metrics tab** (aggregate dashboard — fetched from `GET /api/metrics/summary`):
  - Summary cards row:
    - Total Sessions | Success Rate % | Avg Iterations | Avg Time to PR | Total Tokens Used
  - Outcome donut chart: completed (green) / failed (red) / cancelled (gray)
  - Time breakdown stacked bar: where time is spent across all sessions:
    - Coding (blue) | Testing (green) | CI Wait (amber) | Review Wait (purple) | Provisioning (gray)
    - Shows both absolute (avg seconds) and percentage
  - Failure reasons table: ranked list — "max_iterations" (5), "agent_error" (2), "cancelled" (1)
  - Sessions over time: line chart, sessions per day, with success/fail coloring
  - Agent efficiency stats: avg turns per coding session, avg tool calls per turn, avg tests per run

- [ ] **Step 5:** `app.js`:
  - API client for all endpoints (including `/trace`, `/metrics/summary`)
  - EventSource with auto-reconnect
  - Panel updaters triggered by SSE event types:
    - `span_start` / `span_end` → update trace timeline (append bar, update duration/color on end)
    - `agent_thinking` / `agent_message` / `tool_call` → update agent replay panel (append to current turn)
    - `test_case` / `test_summary` → update test drilldown panel (append row, update iteration column)
    - `signal_received` → flash indicator on trace timeline wait bar
    - Existing events: `status_change`, `ci_result`, `pr_update`, etc. → update respective panels
  - Trace timeline renderer: fetches full span tree from `/trace` on load, then live-updates via SSE
  - Metrics dashboard: fetches from `/metrics/summary`, renders charts (use simple CSS bars / SVG — no chart library needed for demo)
  - Periodic workflow query polling (test results, PR info) as fallback
  - Human assist form submission + UI state update
  - Auto-scroll event stream, collapsible sections

- [ ] **Step 6:** `style.css`:
  - Dark theme (charcoal grays, not pure black)
  - Status colors: pending=gray, running=blue, ci_monitoring=amber, pr_review=purple, needs_human=orange, completed=green, failed=red, cancelled=gray
  - Test results: pass=green, fail=red, skip=gray
  - CI iteration timeline visualization
  - Chat bubble styling, monospace code blocks
  - Responsive: sidebar session list + main detail area
  - **Trace timeline styles:**
    - Gantt bars with nesting indentation (left-padding per depth level)
    - Span kind colors: phase=blue-gray, activity=blue, agent_turn=purple, tool_call=teal, container_op=amber, http=green, wait=hollow/dashed-border
    - Failed spans: red bar with error icon
    - Running spans: animated pulse/stripe pattern
    - Hover tooltip: span name, duration, metadata summary
    - Clickable expand/collapse for nested spans
  - **Agent replay styles:**
    - Thinking bubbles: dark bg, italic, muted text
    - Tool call blocks: bordered, monospace args + result, collapsible
    - Agent messages: chat bubble style (distinct from thinking)
    - Vibe summary footer: token usage, files modified count
  - **Test drilldown styles:**
    - Matrix grid: test names (rows) x iterations (columns)
    - Cell icons: green checkmark / red X / gray dash
    - Flipped tests highlighted: amber border on cells that changed outcome
    - Expandable traceback: monospace, red background
  - **Metrics dashboard styles:**
    - Summary cards: large number + label, accent color border
    - Donut chart: CSS conic-gradient (no JS library needed)
    - Stacked bar: CSS flexbox with colored segments + percentage labels
    - Table: striped rows, sortable headers

- [ ] **Step 7:** Verify in browser — create session, watch full flow
- [ ] **Step 8:** Commit

### Task 10: Slack Bot (Signal Relay)

**Files:** `app/integrations/slack_bot.py`, `tests/test_slack_bot.py`

Slack is another signal relay — same pattern as the API.

- [ ] **Step 1:** Write failing test — `/hydra <repo> <task>` and `/hydra <repo> #42 <task>` both start workflows
- [ ] **Step 2:** Implement slack_bot.py:
  - `/hydra` command → parse `<repo> [#issue] <task>` → create session in DB → `workflow_client.start()` → reply in thread
  - If issue# provided, reply includes issue title + type badge
  - Subscribe to event bus → post updates to Slack thread:
    - Status changes: 🔵 Running → 🟡 CI Monitoring → 🟢 Completed
    - Test results: "✓ 12 passed · ✗ 2 failed"
    - `needs_human`: "⚠️ Agent needs help — [View in UI](link)"
    - PR opened: "📋 PR opened: <url>" + confidence summary (files changed, test results, risk flags)
    - PR review: "🔍 Changes requested" / "✅ Approved"
    - PR merged: "✅ PR merged!"
- [ ] **Step 3:** Thread reply for human assist (Slack user replies in thread → signal to workflow)
- [ ] **Step 4:** Mount at `/slack/` in main.py
- [ ] **Step 5:** Run tests — verify PASS
- [ ] **Step 6:** Commit

### Task 11: E2E Test + Polish

- [ ] **Step 1:** Write integration test — full pipeline with mocked Vibe CLI:
  - Happy path: code → test pass → PR → CI pass → review approved → completed
  - Auto-fix path: code → test fail → fix → test pass → PR → CI pass
  - HITL path: code → CI fail → auto-fix fails → needs_human → human assists → fix → CI pass
  - Review path: CI pass → review changes_requested → agent re-codes → CI pass → approved → completed
  - Cancel path: running → cancel signal → cleanup → cancelled
  - Triage path: issue opened → triage agent analyzes → auto-assigns → session workflow runs → completed
  - Triage needs-review path: issue → triage → needs_review → human approves via UI → session starts
- [ ] **Step 2:** Verify workflow queries return correct data at every stage
- [ ] **Step 3:** Verify trace completeness for each E2E path:
  - Happy path trace: setup → coding_iteration_1 → pr_creation → wait_ci → pr_review → cleanup (all spans completed, no errors)
  - Auto-fix trace: coding_iteration_1 contains two run_vibe_code spans + two run_tests spans (initial + fix)
  - HITL trace: ci_iteration contains human_assist phase with wait_human span, signal_received event logged
  - Review trace: pr_review phase contains run_vibe_code(purpose="address_review") + run_tests + commit_and_push
  - Cancel trace: whatever phase was active has its span ended with status="failed" or "cancelled"
  - Triage trace: triage_agent span contains agent_turn spans with tool_call children, triage_summary event present
  - All traces: every event has valid span_id, every span has valid parent (except root), SessionMetrics computed
- [ ] **Step 4:** Verify metrics aggregation:
  - After running multiple E2E tests, `GET /api/metrics/summary` returns correct totals, success_rate, avg_iterations
  - Time breakdown percentages add up (coding + testing + ci_wait + review_wait + other = 100%)
  - Common failure reasons correctly ranked
- [ ] **Step 5:** Add container cleanup on activity failure/timeout
- [ ] **Step 6:** Add structured logging — use Tracer as the single source of truth. Structured log lines emitted on span_start/span_end with: trace_id, span_id, session_id, name, kind, duration_ms, status. No separate logging system — the trace IS the log.
- [ ] **Step 7:** Add graceful shutdown (signal cancel to running workflows, cleanup containers)
- [ ] **Step 8:** Update `.env.example`
- [ ] **Step 9:** Commit

---

## Verification Checklist

- [ ] `pytest tests/ -v` — all pass
- [ ] `docker build -f Dockerfile.sandbox -t hydra-sandbox .` — builds
- [ ] `uvicorn app.main:app` — starts at :8000, workflow worker starts
- [ ] `POST /api/sessions` — starts workflow, returns workflow_run_id
- [ ] `GET /api/sessions/{id}` — returns live state from workflow query
- [ ] Browser at `http://localhost:8000`:
  - [ ] Dashboard: session list + triage queue + metrics tabs
  - [ ] Triage queue: issues with eligibility badges, approve/reject for needs_review
  - [ ] Session detail: live event stream via SSE
  - [ ] Test results panel: pass/fail counts + expandable failures
  - [ ] CI checks panel: iteration history with status icons
  - [ ] Confidence summary: files changed, test results, risk flags, overall risk level
  - [ ] PR panel: merge status + review feedback + review comments
  - [ ] Triage context panel: agent analysis, relevant files, suggested approach
  - [ ] Human assist: error context + guidance input + resume/cancel
  - [ ] **Trace timeline: waterfall view of spans, nested, clickable, live-updating via SSE**
  - [ ] **Agent replay: step-by-step thinking + tool calls + messages, per Vibe invocation**
  - [ ] **Test drilldown: individual test results across iterations, flipped tests highlighted**
  - [ ] **Metrics dashboard: success rate, time breakdown, failure reasons, sessions over time**
- [ ] Triage E2E: issue opened → triage workflow → analyzed → auto-assigned → session runs
- [ ] Triage review: issue → triage → needs_review → human approves in UI → session starts
- [ ] E2E: session → workflow → Docker → Vibe → tests → PR → CI → review → completed
- [ ] E2E with issue: create session with #42 → agent prompt includes issue context → PR body has "Fixes #42"
- [ ] HITL: agent stuck → needs_human → human assists via UI → resumed → completed
- [ ] Review loop: CI passes → PR review requests changes → agent re-codes → CI passes → approved
- [ ] Cancel: session cancelled → workflow signals cancel → cleanup
- [ ] Slack: `/hydra` → workflow starts → thread updates → "needs help" alert
- [ ] **Trace E2E: every completed session has a full span tree with no orphan events/spans**
- [ ] **Metrics E2E: after 3+ sessions, metrics dashboard shows correct aggregates**
- [ ] **Agent replay E2E: can step through agent's thinking and tool calls for any session**

---

## Mistral Capabilities Showcased

| Capability | Where | How |
|---|---|---|
| **Mistral Workflows** | `workflows/triage.py`, `workflows/hydra_session.py` | TWO workflows: triage (gatekeeper) + session (coder). Durable, signal-driven, queryable. Workflow composition: triage auto-starts session workflow. |
| **Workflow Signals** | `webhooks.py`, `sessions.py`, `slack_bot.py` | CI results, human guidance, PR review feedback, cancellation, issue events — all as signals |
| **Workflow Queries** | `sessions.py GET`, `triage GET` | Live state from running workflows: triage analysis, session status, test results, PR info |
| **Workflow Activities** | `workflows/activities.py` | Each side effect (Docker, GitHub, events, triage labeling) as a retriable activity |
| **Agents API** | `workflows/triage.py` (triage agent) | Standalone agent with function calling to analyze issues — NOT inside Vibe CLI |
| **Function Calling** | `workflows/triage_functions.py` | Triage agent tools: read repo tree, read files, search code, find similar issues, get recent commits |
| **Vibe CLI** | `Dockerfile.sandbox`, `sandbox/manager.py` | `vibe --prompt "..." --max-turns N --max-price M --output streaming` in Docker |
| **Mistral Medium 3.5** | Triage agent + Vibe in container | Same model for triage analysis, coding, review, test fixing, CI fixing, review feedback fixing |
| **Agents API (Vibe)** | Inside Vibe CLI | Multi-step tool-calling agentic loop for coding |
| **Function Calling (Vibe)** | Inside Vibe CLI | File read/write, shell exec, git operations |
| **Observability** | `observability.py`, UI trace/metrics panels | Full trace tree: workflow → phase → activity → agent turn → tool call. Every container op, HTTP call, LLM call, test case tracked. Metrics dashboard for aggregate insights. |

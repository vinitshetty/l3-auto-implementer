# Hydra Demo — Architecture Context

> This file is the shared context loaded every session. It defines the vocabulary, models, APIs, and contracts that all waves reference.

## Goal

Build a Hydra async coding agent demo showcasing Mistral Vibe CLI, Workflows, Medium 3.5, Agents API, and Function Calling — end-to-end from GitHub issue triage to PR with CI monitoring and human-in-the-loop intervention.

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
  - Type: bug / feature
  - Complexity: Simple / Medium / Complex
  - Relevant files: [list]
  - Suggested approach: "..."
  - Eligibility: Auto-assignable / Needs human review / Not automatable
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
│   ├── models.py                # HydraSession + SessionEvent + Span + SessionMetrics
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

## Models (SQLAlchemy)

### TriageResult
id (UUID), repo_url, issue_number, issue_title, issue_body, issue_labels (JSON), eligibility (auto_assign|needs_review|not_eligible), complexity (simple|medium|complex), issue_type (bug|feature), relevant_files (JSON list), suggested_approach (text), triage_workflow_run_id, session_id (FK, nullable — set when session created), created_at

### HydraSession
id (UUID), task_description, repo_url, branch_name, status, pr_url, pr_merged (bool), iteration_count, max_iterations, channel (web|slack|triage), workflow_run_id (links to Mistral Workflow), triage_id (FK to TriageResult, nullable), issue_number (optional int), issue_type (bug|feature|null), issue_title (cached from GitHub), error_summary, created_at, updated_at

### Span
id (UUID), trace_id (UUID — same as workflow_run_id, 1 trace per workflow run), parent_span_id (UUID, nullable — for nesting), session_id (FK), name (str — e.g. "provision_sandbox", "agent_turn_1"), kind (str — "phase"|"activity"|"agent_turn"|"tool_call"|"container_op"|"http"|"wait"), status (str — "running"|"completed"|"failed"), started_at (datetime), ended_at (datetime, nullable), duration_ms (int, nullable), span_metadata (Python attr, mapped to DB column "metadata" — JSON, activity-specific context: prompt_length, cmd, exit_code, tool name, args, etc.), error (text, nullable)
**Note:** Python attribute is `span_metadata` (not `metadata`) to avoid conflict with SQLAlchemy's reserved `metadata` attribute on DeclarativeBase. DB column name remains `metadata`.

### SessionEvent
id (UUID), session_id (FK), span_id (UUID, nullable — links event to producing span), trace_id (UUID — top-level trace correlation), event_type, payload (JSON), timestamp

### SessionMetrics
session_id (FK, unique), total_duration_ms, coding_duration_ms (sum of run_vibe_code spans), testing_duration_ms (sum of run_tests spans), ci_wait_duration_ms (sum of wait_ci spans), review_wait_duration_ms (sum of wait_pr_review spans), provision_duration_ms, iterations (int), test_runs (int), total_tests_executed (int), total_tokens_used (int), vibe_turns (int), tool_calls_count (int), files_modified_count (int), outcome (str — completed|failed|cancelled), failure_reason (str, nullable — "max_iterations"|"agent_error"|"cancelled")

---

## Event Types

**Original:**
`status_change`, `agent_message`, `tool_call`, `error`, `ci_result`, `test_results`, `pr_update`, `review_feedback`, `human_assist_request`, `human_assist_response`

**Observability (new):**
`span_start`, `span_end`, `agent_thinking`, `llm_call`, `vibe_summary`, `triage_summary`, `container_exec`, `test_case`, `test_summary`, `signal_received`

---

## Schemas (Pydantic)

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
- `SpanResponse` (id, trace_id, parent_span_id, name, kind, status, started_at, ended_at, duration_ms, span_metadata, error, children: list[SpanResponse])
- `SessionMetricsResponse` (all SessionMetrics fields + computed: success_rate, avg_iterations, time_breakdown: {coding_pct, testing_pct, ci_wait_pct, review_wait_pct})
- `TraceResponse` (trace_id, session_id, root_spans: list[SpanResponse], total_duration_ms, summary: SessionMetricsResponse)

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/sessions` | Create session + start workflow |
| GET | `/api/sessions` | List sessions (status filter) |
| GET | `/api/sessions/{id}` | Session detail (DB + workflow query) |
| GET | `/api/issues/preview` | Fetch issue title + labels from GitHub |
| POST | `/api/sessions/{id}/assist` | Send human_assist signal |
| POST | `/api/sessions/{id}/cancel` | Send cancel signal |
| GET | `/api/sessions/{id}/events` | SSE stream |
| GET | `/api/sessions/{id}/trace` | Full span tree (nested) |
| GET | `/api/sessions/{id}/metrics` | Per-session metrics |
| GET | `/api/triage` | List triage results (eligibility filter) |
| GET | `/api/triage/{id}` | Triage detail |
| POST | `/api/triage/{id}/approve` | Approve needs_review → start session |
| POST | `/api/triage/{id}/reject` | Reject triage item |
| GET | `/api/metrics/summary` | Aggregate metrics dashboard |

---

## Observability: Tracer Contract

```python
class Tracer:
    """Every workflow run = 1 trace. Every activity/phase/wait = 1 span.
    Spans nest. Events attach to spans. Everything flows to UI via event bus."""

    def __init__(self, session_id: str, trace_id: str, event_bus: EventBus, db: AsyncSession): ...

    @asynccontextmanager
    async def span(self, name: str, kind: str = "activity",
                   parent: Span | None = None, **metadata):
        """Context manager — auto-tracks start/end/error/duration.
        Publishes span_start and span_end events to UI via event bus."""

    async def start_span(self, name: str, kind: str = "activity",
                         parent: Span | None = None, **metadata) -> Span:
        """Manual start — for streaming where spans cross async boundaries.
        Must call end_span() later."""

    async def end_span(self, span: Span, status: str = "completed", error: str | None = None):
        """Close a manually-started span."""

    async def event(self, span: Span, event_type: str, payload: dict):
        """Emit an event tied to a span — flows to UI via event bus."""

    async def compute_session_metrics(self) -> SessionMetrics:
        """Aggregate all spans into SessionMetrics — called on workflow completion."""
```

**Span kinds:** `phase`, `activity`, `agent_turn`, `tool_call`, `container_op`, `http`, `wait`

---

## Wave Overview

| Wave | Tasks | What Gets Built |
|---|---|---|
| **Wave 1** (Foundation) | Tasks 1-3 | Config, DB models + Span + SessionMetrics, thin CRUD API + trace/metrics endpoints |
| **Wave 2** (Docker + Workflow) | Tasks 4-5 | Instrumented sandbox manager, observability.py Tracer, session workflow with full span tree |
| **Wave 3** (Triage) | Task 6 | Triage workflow + agent functions, instrumented with tracer |
| **Wave 4** (GitHub + Webhooks) | Tasks 7-8 | GitHub client with HTTP span tracing, webhook signal relay |
| **Wave 5** (UI + Slack + Polish) | Tasks 9-11 | Rich UI (trace timeline, agent replay, test drilldown, metrics dashboard), Slack bot, E2E tests |

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

# Wave 1: Foundation (Config + Models + CRUD API)

> **Prerequisites:** None — this is the starting wave.
> **Architecture context:** See `hydra-architecture.md` for models, schemas, and API contracts.

---

## Task 1: Project Setup + Config

**Files:** `pyproject.toml`, `.env.example`, `app/__init__.py`, `app/config.py`, `app/main.py`

- [ ] **Step 1:** Create `pyproject.toml` with dependencies: fastapi, uvicorn, sqlalchemy[asyncio], aiosqlite, pydantic-settings, httpx, docker, sse-starlette, slack-bolt, mistral-workflows, pytest, pytest-asyncio, httpx (test client)
- [ ] **Step 2:** Create `.env.example` with: `MISTRAL_API_KEY`, `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `DATABASE_URL=sqlite+aiosqlite:///./hydra.db`, `MAX_CI_ITERATIONS=3`, `VIBE_MAX_TURNS=50`, `VIBE_MAX_PRICE=5.0`
- [ ] **Step 3:** Implement `config.py` — pydantic-settings `Settings` class loading from `.env`
- [ ] **Step 4:** Implement `main.py` — FastAPI app with lifespan (create tables on startup, start workflow worker), mount `/static`
- [ ] **Step 5:** Write test for config loading, run — verify PASS
- [ ] **Step 6:** Commit

---

## Task 2: Database + Models

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

---

## Task 3: Session API (Thin Workflow Gateway)

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

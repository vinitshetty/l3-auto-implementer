# Wave 5: UI + Slack + Polish

> **Prerequisites:** Wave 4 complete (GitHub client, webhooks, all workflows functional).
> **Architecture context:** See `hydra-architecture.md` for event types, API endpoints, Span kinds.

---

## Task 9: SSE Endpoint + Rich Web UI

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
  - Each card: repo, task excerpt, status, iteration N/max, elapsed time, issue badge if linked
  - Cards from triage show "Triaged" origin badge

  **Triage Queue tab** (`triage.html`):
  - List of triaged issues with eligibility indicators
  - Filter: All | Auto-Assigned | Needs Review | Not Eligible
  - Each triage card shows:
    - Issue title + number (clickable to GitHub)
    - Type badge (Bug / Feature)
    - Complexity badge (Simple=green / Medium=amber / Complex=red)
    - Eligibility badge (Auto-assigned / Needs review / Not eligible)
    - Relevant files list (collapsed)
    - Suggested approach (collapsed)
    - Agent reasoning (collapsed)
    - For "needs_review": "Approve & Start" button → starts session workflow with triage context
    - Link to session if already auto-assigned

- [ ] **Step 4:** Build `session.html` — Session Detail (all data from workflow queries + events):

  **Header:**
  - Status badge (live-updating via SSE)
  - Repo link, branch name
  - Issue badge if linked — clickable link to GitHub issue
  - Iteration progress: "Iteration 2 of 3"
  - Elapsed time

  **Event Stream Panel** (live via SSE):
  - `status_change` → colored status transition badge with timestamp
  - `agent_message` → chat bubble (dark bg, monospace for code)
  - `tool_call` → collapsible block: tool name + args + output
  - `error` → red alert banner
  - `human_assist_request` → orange alert with full error context

  **Test Results Panel** (updates each iteration):
  - Summary bar: N passed / N failed / N skipped
  - Expandable failure list: test name + assertion error + traceback
  - History toggle: show results per iteration

  **CI Checks Panel** (updates each ci_result event):
  - Per-check row: name | status icon | conclusion | link
  - Iteration history: "Iter 1: 2 failed → Iter 2: 1 failed → Iter 3: all passed"

  **Confidence Summary Panel** (visible after agent finishes coding, before/during PR review):
  - Change stats: "Changed 3 files / +47 lines / -12 lines"
  - Changed files list (clickable to GitHub diff)
  - Test results summary
  - New dependencies: list (or "None added")
  - Risk flags (if any): amber warnings like "Modified authentication logic", "New dependency: pyjwt", "Changed database schema"
  - Overall confidence indicator: green "Low risk" / amber "Review carefully" / red "High risk"
  - This helps reviewers quickly assess: is this a safe 3-line fix or a risky refactor?

  **Triage Context Panel** (visible if session was triaged):
  - Triage agent's analysis: type, complexity, relevant files, suggested approach
  - Agent reasoning (collapsible)
  - Link to original issue

  **PR Panel** (updates on pr_update + review_feedback events):
  - PR link (clickable), state badge (open/merged/closed)
  - Mergeable indicator (no conflicts / conflicts)
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
    setup                    ██████                              3.4s
      fetch_issue              ██                                0.3s
        http GET /issues/42      █                               0.3s  200
      provision_sandbox          ████                            2.0s
        docker.create              ███                           1.4s
        docker.start                  █                          0.6s
      clone_repo                        ██                       1.1s
    coding_iteration_1                    █████████████████     61.7s
      run_vibe_code                       ████████████████      45.2s
        agent_turn_1                        ████                 8.3s
          tool: file_read(auth.py)            █                  0.1s
          tool: file_read(test_auth.py)        █                 0.1s
        agent_turn_2                              █████         12.1s
          tool: file_write(auth.py)                 █            0.1s
          tool: file_write(test_auth.py)             █           0.1s
        ...
      run_tests                                          ████    3.4s
    wait_ci                  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  128.0s
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

---

## Task 10: Slack Bot (Signal Relay)

**Files:** `app/integrations/slack_bot.py`, `tests/test_slack_bot.py`

Slack is another signal relay — same pattern as the API.

- [ ] **Step 1:** Write failing test — `/hydra <repo> <task>` and `/hydra <repo> #42 <task>` both start workflows
- [ ] **Step 2:** Implement slack_bot.py:
  - `/hydra` command → parse `<repo> [#issue] <task>` → create session in DB → `workflow_client.start()` → reply in thread
  - If issue# provided, reply includes issue title + type badge
  - Subscribe to event bus → post updates to Slack thread:
    - Status changes: Running → CI Monitoring → Completed
    - Test results: "N passed / N failed"
    - `needs_human`: "Agent needs help — [View in UI](link)"
    - PR opened: "PR opened: <url>" + confidence summary (files changed, test results, risk flags)
    - PR review: "Changes requested" / "Approved"
    - PR merged: "PR merged!"
- [ ] **Step 3:** Thread reply for human assist (Slack user replies in thread → signal to workflow)
- [ ] **Step 4:** Mount at `/slack/` in main.py
- [ ] **Step 5:** Run tests — verify PASS
- [ ] **Step 6:** Commit

---

## Task 11: E2E Test + Polish

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

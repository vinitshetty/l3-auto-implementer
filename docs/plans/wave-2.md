# Wave 2: Docker Sandbox + Workflow

> **Prerequisites:** Wave 1 complete (models, schemas, API stubs exist).
> **Architecture context:** See `hydra-architecture.md` for Tracer contract, Span model, event types.

---

## Task 4: Dockerfile + Sandbox Manager

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

---

## Task 5: Mistral Workflow (The Brain)

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

      # -- Phase 0: Setup --
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

      # -- Phase 1: Coding iteration 1 --
      async with self.tracer.span("coding_iteration_1", kind="phase", iteration=1) as iter_phase:

          # Code with Vibe
          async with self.tracer.span("run_vibe_code", kind="activity", parent=iter_phase,
                                      purpose="initial_coding") as s:
              await workflow.execute_activity(run_vibe_code, ..., prompt=prompt,
                  tracer=self.tracer, parent_span=s)

          # Test locally before pushing
          async with self.tracer.span("run_tests", kind="activity", parent=iter_phase,
                                      purpose="initial_validation") as s:
              self.state.test_results = await workflow.execute_activity(run_tests, ...,
                  tracer=self.tracer, parent_span=s)

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

      # -- Phase 2: PR creation --
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

          async with self.tracer.span("open_pr", kind="activity", parent=pr_phase) as s:
              self.state.pr_url = await workflow.execute_activity(
                  open_pr, ..., confidence=self.state.confidence,
                  tracer=self.tracer, parent_span=s)

      # -- Phase 3: CI monitoring loop --
      self.state.status = "ci_monitoring"
      for i in range(self.state.max_iterations):
          self.state.iteration = i + 1
          async with self.tracer.span(f"ci_iteration_{i+1}", kind="phase",
                                      iteration=i+1) as ci_phase:

              # Wait for CI result signal
              async with self.tracer.span("wait_ci", kind="wait", parent=ci_phase) as s:
                  await self._ci_event.wait()
                  self._ci_event.clear()

              if self._cancelled:
                  return await self._cleanup("cancelled")

              latest_ci = self.state.ci_results[-1]
              if latest_ci.passed:
                  # -- Phase 4: PR review --
                  self.state.status = "pr_review"
                  async with self.tracer.span("pr_review", kind="phase") as review_phase:
                      async with self.tracer.span("wait_review", kind="wait",
                                                  parent=review_phase) as s:
                          await self._review_event.wait()
                          self._review_event.clear()

                      if self._cancelled:
                          return await self._cleanup("cancelled")

                      if self.state.review_comments:
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

              # CI failed -- try auto-fix
              async with self.tracer.span("run_vibe_code", kind="activity", parent=ci_phase,
                                          purpose="ci_fix") as s:
                  await workflow.execute_activity(run_vibe_code, ...,
                      prompt=f"CI failed:\n{latest_ci.error_details}",
                      tracer=self.tracer, parent_span=s)

              async with self.tracer.span("run_tests", kind="activity", parent=ci_phase) as s:
                  self.state.test_results = await workflow.execute_activity(run_tests, ...,
                      tracer=self.tracer, parent_span=s)

              if not self.state.test_results.all_passed:
                  # Agent stuck -- ask human
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

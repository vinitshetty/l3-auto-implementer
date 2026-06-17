# Wave 3: Triage Agent Workflow

> **Prerequisites:** Wave 2 complete (Tracer, activities, session workflow exist).
> **Architecture context:** See `hydra-architecture.md` for Tracer contract, triage lifecycle, event types.

---

## Task 6: Triage Workflow + Agent Functions

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

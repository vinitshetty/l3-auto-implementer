"""Triage Workflow — analyzes GitHub issues using Mistral Agents API
with function calling to determine eligibility for automated coding.

Uses Mistral Workflows SDK (@workflow.define) for durability and
child workflow composition (auto-starts HydraSessionWorkflow)."""

import json
import uuid
from datetime import timedelta

from pydantic import BaseModel, Field

from mistralai.workflows import workflow, activity, Depends

from app import workflow_registry
from app.workflows.triage_functions import TOOLS_SCHEMA, TriageTools


TRIAGE_SYSTEM_PROMPT = """You are a triage agent for a software project. Analyze the GitHub issue and determine:
1. Type: Is this a bug fix or a new feature?
2. Complexity: Simple (< 50 lines, single file), Medium (multi-file, tests needed), Complex (architectural changes)
3. Eligibility: Can an AI coding agent handle this autonomously?
   - auto_assign: Simple bugs with clear repro, small features with clear spec
   - needs_review: Medium complexity, team should approve before agent starts
   - not_eligible: Complex, ambiguous, security-sensitive, or requires human judgment
4. Relevant files: Which files in the codebase are likely involved?
5. Suggested approach: Brief plan for how to fix/implement this

Use the available tools to explore the codebase before making your decision.

After your analysis, respond with a JSON block:
```json
{
  "issue_type": "bug" or "feature",
  "complexity": "simple", "medium", or "complex",
  "eligibility": "auto_assign", "needs_review", or "not_eligible",
  "relevant_files": ["file1.py", "file2.py"],
  "suggested_approach": "Brief description of how to fix/implement",
  "reasoning": "Why you made this decision"
}
```"""


# --- Models ---

class TriageInput(BaseModel):
    repo_url: str
    issue_number: int
    owner: str
    repo: str
    github_token: str
    mistral_api_key: str


class TriageState(BaseModel):
    status: str = "analyzing"
    issue_number: int = 0
    issue_title: str = ""
    issue_body: str = ""
    issue_labels: list[str] = Field(default_factory=list)
    eligibility: str = ""
    complexity: str = ""
    issue_type: str = ""
    relevant_files: list[str] = Field(default_factory=list)
    suggested_approach: str = ""
    reasoning: str = ""
    session_id: str | None = None


class TriageAnalysis(BaseModel):
    eligibility: str
    complexity: str
    issue_type: str
    relevant_files: list[str] = Field(default_factory=list)
    suggested_approach: str = ""
    reasoning: str = ""


# --- Activities for triage side effects ---

class FetchIssueParams(BaseModel):
    owner: str
    repo: str
    issue_number: int
    github_token: str


class LabelIssueParams(BaseModel):
    owner: str
    repo: str
    issue_number: int
    labels: list[str]
    github_token: str


class CommentIssueParams(BaseModel):
    owner: str
    repo: str
    issue_number: int
    body: str
    github_token: str


class RunTriageAgentParams(BaseModel):
    owner: str
    repo: str
    github_token: str
    mistral_api_key: str
    issue_prompt: str


@activity(start_to_close_timeout=timedelta(minutes=1), name="triage_fetch_issue")
async def triage_fetch_issue(params: FetchIssueParams) -> dict:
    """Fetch issue details from GitHub."""
    import httpx
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        headers={"Authorization": f"token {params.github_token}", "Accept": "application/vnd.github.v3+json"},
    ) as client:
        resp = await client.get(f"/repos/{params.owner}/{params.repo}/issues/{params.issue_number}")
        resp.raise_for_status()
        return resp.json()


@activity(start_to_close_timeout=timedelta(minutes=1), name="triage_label_issue")
async def triage_label_issue(params: LabelIssueParams) -> None:
    """Add labels to an issue."""
    import httpx
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        headers={"Authorization": f"token {params.github_token}", "Accept": "application/vnd.github.v3+json"},
    ) as client:
        await client.post(
            f"/repos/{params.owner}/{params.repo}/issues/{params.issue_number}/labels",
            json={"labels": params.labels},
        )


@activity(start_to_close_timeout=timedelta(minutes=1), name="triage_comment_issue")
async def triage_comment_issue(params: CommentIssueParams) -> None:
    """Post a comment on an issue."""
    import httpx
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        headers={"Authorization": f"token {params.github_token}", "Accept": "application/vnd.github.v3+json"},
    ) as client:
        await client.post(
            f"/repos/{params.owner}/{params.repo}/issues/{params.issue_number}/comments",
            json={"body": params.body},
        )


@activity(start_to_close_timeout=timedelta(minutes=5), name="triage_run_agent")
async def triage_run_agent(params: RunTriageAgentParams) -> TriageAnalysis:
    """Run the Mistral agent loop with function calling for triage."""
    try:
        from mistralai.client import Mistral
        mistral_client = Mistral(api_key=params.mistral_api_key)
    except Exception:
        return TriageAnalysis(
            eligibility="needs_review", complexity="medium", issue_type="bug",
            relevant_files=[], suggested_approach=f"No Mistral client available",
            reasoning="Mistral client could not be initialized",
        )

    tools = TriageTools(params.owner, params.repo, params.github_token)

    messages = [
        {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
        {"role": "user", "content": params.issue_prompt},
    ]

    turn = 0
    while turn < 10:
        turn += 1
        response = await mistral_client.chat.complete_async(
            model="mistral-medium-latest",
            messages=messages,
            tools=TOOLS_SCHEMA,
        )

        choice = response.choices[0]
        message = choice.message

        if message.tool_calls:
            messages.append(message)
            for tc in message.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
                result = await tools.execute(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result[:2000],
                })
            continue

        # No tool calls — final answer
        content = message.content or ""
        return _parse_analysis(content)

    return TriageAnalysis(
        eligibility="needs_review", complexity="medium", issue_type="bug",
        relevant_files=[], suggested_approach="Analysis incomplete",
        reasoning="Agent did not produce a final answer",
    )


def _parse_analysis(content: str) -> TriageAnalysis:
    """Parse the agent's response into structured analysis."""
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(content[start:end])
            return TriageAnalysis(
                eligibility=data.get("eligibility", "needs_review"),
                complexity=data.get("complexity", "medium"),
                issue_type=data.get("issue_type", "bug"),
                relevant_files=data.get("relevant_files", []),
                suggested_approach=data.get("suggested_approach", ""),
                reasoning=data.get("reasoning", ""),
            )
    except (json.JSONDecodeError, KeyError):
        pass

    return TriageAnalysis(
        eligibility="needs_review", complexity="medium", issue_type="bug",
        relevant_files=[], suggested_approach="Could not parse analysis",
        reasoning=content[:500],
    )


# --- The Workflow ---

@workflow.define(name="hydra_triage", execution_timeout=timedelta(hours=1))
class TriageWorkflow:
    """Analyzes GitHub issues using Mistral Agents API with function calling."""

    def __init__(self):
        self.state = TriageState()

    @workflow.query(name="get_triage_result", description="Get triage analysis result")
    def get_triage_result(self) -> TriageState:
        return self.state

    @workflow.entrypoint
    async def run(self, input: TriageInput) -> TriageState:
        self.state.issue_number = input.issue_number

        # 1. Fetch issue details (activity)
        issue_data = await triage_fetch_issue(FetchIssueParams(
            owner=input.owner, repo=input.repo,
            issue_number=input.issue_number,
            github_token=input.github_token,
        ))
        self.state.issue_title = issue_data.get("title", "")
        self.state.issue_body = issue_data.get("body", "")
        self.state.issue_labels = [l.get("name", "") for l in issue_data.get("labels", [])]

        # 2. Run triage agent (activity)
        issue_prompt = self._format_issue_prompt()
        analysis = await triage_run_agent(RunTriageAgentParams(
            owner=input.owner, repo=input.repo,
            github_token=input.github_token,
            mistral_api_key=input.mistral_api_key,
            issue_prompt=issue_prompt,
        ))

        # 3. Apply results
        self.state.eligibility = analysis.eligibility
        self.state.complexity = analysis.complexity
        self.state.issue_type = analysis.issue_type
        self.state.relevant_files = analysis.relevant_files
        self.state.suggested_approach = analysis.suggested_approach
        self.state.reasoning = analysis.reasoning

        # 4. Label + comment (activities)
        labels = self._compute_labels()
        await triage_label_issue(LabelIssueParams(
            owner=input.owner, repo=input.repo,
            issue_number=input.issue_number,
            labels=labels, github_token=input.github_token,
        ))

        comment_body = self._format_triage_comment()
        await triage_comment_issue(CommentIssueParams(
            owner=input.owner, repo=input.repo,
            issue_number=input.issue_number,
            body=comment_body, github_token=input.github_token,
        ))

        # 5. Auto-start session workflow if eligible (child workflow)
        if self.state.eligibility == "auto_assign":
            from app.workflows.hydra_session import HydraSessionWorkflow, SessionInput
            session_id = str(uuid.uuid4())
            session_input = SessionInput(
                session_id=session_id,
                repo_url=input.repo_url,
                task_description=self.state.suggested_approach or f"Fix issue #{input.issue_number}",
                issue_number=input.issue_number,
                issue_type=self.state.issue_type,
                issue_title=self.state.issue_title,
                triage_suggested_approach=self.state.suggested_approach,
                triage_relevant_files=self.state.relevant_files,
                owner=input.owner,
                repo=input.repo,
                github_token=input.github_token,
                mistral_api_key=input.mistral_api_key,
            )
            try:
                await workflow.execute_workflow(
                    HydraSessionWorkflow,
                    params=session_input,
                    execution_timeout=timedelta(days=7),
                    wait=False,
                )
            except Exception:
                # Outside Temporal — use registry for local execution
                await workflow_registry.start_workflow(
                    HydraSessionWorkflow, session_input,
                    execution_id=f"session-{session_id}",
                )
            self.state.session_id = session_id

        self.state.status = "triaged"
        return self.state

    def _format_issue_prompt(self) -> str:
        parts = [f"Issue #{self.state.issue_number}: {self.state.issue_title}"]
        if self.state.issue_body:
            parts.append(f"\n{self.state.issue_body}")
        if self.state.issue_labels:
            parts.append(f"\nLabels: {', '.join(self.state.issue_labels)}")
        return "\n".join(parts)

    def _compute_labels(self) -> list[str]:
        labels = [f"type:{self.state.issue_type}", f"complexity:{self.state.complexity}"]
        if self.state.eligibility == "auto_assign":
            labels.append("hydra-eligible")
        elif self.state.eligibility == "needs_review":
            labels.append("needs-triage")
        else:
            labels.append("not-automatable")
        return labels

    def _format_triage_comment(self) -> str:
        return f"""## Hydra Triage Analysis

**Type:** {self.state.issue_type}
**Complexity:** {self.state.complexity}
**Eligibility:** {self.state.eligibility}

**Relevant files:**
{chr(10).join(f'- `{f}`' for f in self.state.relevant_files) or '- None identified'}

**Suggested approach:**
{self.state.suggested_approach}

**Reasoning:**
{self.state.reasoning}

---
_Analyzed by Hydra Triage Agent_"""

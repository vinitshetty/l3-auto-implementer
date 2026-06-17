"""Workflow activities — each side effect as a retriable unit.
Decorated with @activity() for Mistral Workflows SDK.
Activities construct their own clients from params since the SDK's
DI cannot auto-construct classes that require init arguments."""

from datetime import timedelta

from pydantic import BaseModel

from mistralai.workflows import activity

from app.config import settings
from app.schemas import ConfidenceSummary, TestResultPayload, VibeSummary


# --- Pydantic models for activity inputs/outputs (must be serializable) ---

class IssueContext(BaseModel):
    title: str = ""
    body: str = ""
    labels: list[str] = []


class FetchIssueParams(BaseModel):
    owner: str
    repo: str
    issue_number: int


class ProvisionSandboxParams(BaseModel):
    session_id: str
    repo_url: str
    token: str
    api_key: str


class CloneRepoParams(BaseModel):
    container_id: str
    repo_url: str
    branch_name: str
    token: str = ""


class RunVibeParams(BaseModel):
    container_id: str
    prompt: str
    max_turns: int = 50
    max_price: float = 5.0


class RunTestsParams(BaseModel):
    container_id: str


class ConfidenceParams(BaseModel):
    container_id: str
    task_description: str = ""
    issue_type: str = ""


class CommitAndPushParams(BaseModel):
    container_id: str
    branch_name: str
    message: str


class OpenPRParams(BaseModel):
    owner: str
    repo: str
    branch_name: str
    title: str
    body: str


class DestroyParams(BaseModel):
    container_id: str


class BuildPromptParams(BaseModel):
    task: str
    issue_title: str | None = None
    issue_body: str | None = None
    issue_type: str | None = None
    issue_labels: list[str] | None = None
    triage_approach: str | None = None
    triage_files: list[str] | None = None


class EnhanceSpecParams(BaseModel):
    container_id: str
    task_description: str
    issue_title: str | None = None
    issue_body: str | None = None
    issue_type: str | None = None
    issue_labels: list[str] | None = None
    max_turns: int = 30
    max_price: float = 3.0


class DocumentChangesParams(BaseModel):
    container_id: str
    task_description: str
    issue_number: int | None = None
    max_turns: int = 20
    max_price: float = 2.0


class UpdatePRBodyParams(BaseModel):
    owner: str
    repo: str
    pr_number: int
    body: str


# --- Activities ---

@activity(start_to_close_timeout=timedelta(minutes=2), name="fetch_issue")
async def fetch_issue(params: FetchIssueParams) -> IssueContext:
    """Fetch issue details from GitHub."""
    from app.integrations.github_client import GitHubClient
    client = GitHubClient(token=settings.github_token)
    try:
        issue = await client.get_issue(params.owner, params.repo, params.issue_number)
        return IssueContext(
            title=issue.get("title", ""),
            body=issue.get("body", ""),
            labels=[l.get("name", "") if isinstance(l, dict) else l for l in issue.get("labels", [])],
        )
    finally:
        await client.close()


@activity(start_to_close_timeout=timedelta(minutes=5), name="provision_sandbox")
async def provision_sandbox(params: ProvisionSandboxParams) -> str:
    """Create a sandbox container. Returns container_id."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()
    return await sandbox.create(params.session_id, params.repo_url, params.token, params.api_key)


@activity(start_to_close_timeout=timedelta(minutes=3), name="clone_repo")
async def clone_repo(params: CloneRepoParams) -> None:
    """Clone repo and create branch in sandbox."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()
    # Use authenticated URL for clone so push works later
    token = params.token or settings.github_token
    if token and "github.com" in params.repo_url:
        auth_url = params.repo_url.replace("https://", f"https://x-access-token:{token}@")
    else:
        auth_url = params.repo_url
    await sandbox.run_git(params.container_id, "clone", auth_url, "/workspace")
    await sandbox.run_git(params.container_id, "-C", "/workspace", "checkout", "-b", params.branch_name)


@activity(start_to_close_timeout=timedelta(minutes=10), name="run_vibe_code")
async def run_vibe_code(params: RunVibeParams) -> VibeSummary:
    """Run Vibe CLI for coding."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()
    return await sandbox.run_vibe(params.container_id, params.prompt, params.max_turns, params.max_price)


@activity(start_to_close_timeout=timedelta(minutes=5), name="run_tests")
async def run_tests(params: RunTestsParams) -> TestResultPayload:
    """Run tests in sandbox."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()
    return await sandbox.run_tests(params.container_id)


@activity(start_to_close_timeout=timedelta(minutes=2), name="generate_confidence_summary")
async def generate_confidence_summary(params: ConfidenceParams) -> ConfidenceSummary:
    """Generate a rich confidence summary with risk flags, dependency detection, and scoring."""
    import re
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()
    diff_stats = await sandbox.get_diff_stats(params.container_id)

    changed_files = diff_stats.get("changed_files", [])
    files_changed = diff_stats["files_changed"]
    lines_added = diff_stats["lines_added"]
    lines_removed = diff_stats["lines_removed"]

    risk_flags: list[str] = []
    new_dependencies: list[str] = []

    # --- Detect risky file patterns ---
    risk_patterns = {
        r"(\.env|credentials|secret|key)": "Sensitive file modified",
        r"(migration|alembic|schema)": "Database schema change",
        r"(auth|login|permission|security)": "Authentication/security code changed",
        r"(config|settings|\.yml|\.yaml|\.toml)": "Configuration file modified",
        r"(Dockerfile|docker-compose|\.dockerignore)": "Infrastructure change",
        r"(ci|workflow|\.github)": "CI/CD pipeline modified",
    }
    for f in changed_files:
        for pattern, flag in risk_patterns.items():
            if re.search(pattern, f, re.IGNORECASE) and flag not in risk_flags:
                risk_flags.append(flag)

    # --- Detect new dependencies from diff ---
    try:
        diff_result = await sandbox.exec_in_container(
            params.container_id, "git -C /workspace diff HEAD -- requirements*.txt package.json pyproject.toml Pipfile"
        )
        for line in diff_result.stdout.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                dep_match = re.search(r'["\']?([a-zA-Z0-9_-]+)["\']?\s*[>=<~^]', line)
                if dep_match:
                    new_dependencies.append(dep_match.group(1))
    except Exception:
        pass

    # --- Check if tests exist for changed files ---
    has_test_coverage = False
    try:
        result = await sandbox.exec_in_container(
            params.container_id, "find /workspace -name 'test_*' -o -name '*_test.*' | head -20"
        )
        test_files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        if test_files:
            has_test_coverage = True
        else:
            risk_flags.append("No test files found in repo")
    except Exception:
        pass

    # --- Scope check: large change warning ---
    if files_changed > 10:
        risk_flags.append(f"Large change: {files_changed} files modified")
    if lines_added + lines_removed > 500:
        risk_flags.append(f"Large diff: {lines_added + lines_removed} lines changed")

    # --- Calculate confidence score (0-100) ---
    score = 50  # base score

    # Positive signals
    if files_changed > 0:
        score += 10  # actual changes were made
    if files_changed <= 5:
        score += 10  # focused change
    if has_test_coverage:
        score += 10  # repo has tests

    # Negative signals
    score -= len(risk_flags) * 5  # each risk flag reduces confidence
    if files_changed == 0:
        score -= 30  # no changes at all
    if new_dependencies:
        score -= len(new_dependencies) * 3  # new deps add risk
    if lines_added > 200 and lines_removed < 10:
        score -= 10  # lots of new code with little cleanup

    score = max(0, min(100, score))

    # --- Build summary ---
    parts = [f"{files_changed} files changed (+{lines_added}/-{lines_removed})"]
    if changed_files:
        displayed = changed_files[:5]
        parts.append("Files: " + ", ".join(displayed))
        if len(changed_files) > 5:
            parts[-1] += f" +{len(changed_files) - 5} more"
    if new_dependencies:
        parts.append(f"New deps: {', '.join(new_dependencies)}")
    if risk_flags:
        parts.append(f"Risks: {', '.join(risk_flags)}")
    parts.append(f"Confidence: {score}/100")

    return ConfidenceSummary(
        files_changed=files_changed,
        lines_added=lines_added,
        lines_removed=lines_removed,
        changed_files=changed_files,
        new_dependencies=new_dependencies,
        risk_flags=risk_flags,
        confidence_score=score,
        summary=" | ".join(parts),
    )


@activity(start_to_close_timeout=timedelta(minutes=3), name="commit_and_push")
async def commit_and_push(params: CommitAndPushParams) -> None:
    """Stage, commit, and push changes."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()
    await sandbox.exec_in_container(params.container_id, "git -C /workspace config user.email hydra@bot")
    await sandbox.exec_in_container(params.container_id, "git -C /workspace config user.name Hydra")
    await sandbox.run_git(params.container_id, "-C", "/workspace", "add", "-A")
    await sandbox.run_git(params.container_id, "-C", "/workspace", "commit", "-m", params.message, "--allow-empty")
    await sandbox.run_git(params.container_id, "-C", "/workspace", "push", "origin", params.branch_name)


@activity(start_to_close_timeout=timedelta(minutes=2), name="open_pr")
async def open_pr(params: OpenPRParams) -> str:
    """Open a pull request. Returns PR URL (empty string if no commits to diff)."""
    from app.integrations.github_client import GitHubClient
    client = GitHubClient(token=settings.github_token)
    try:
        pr = await client.create_pull_request(
            params.owner, params.repo, title=params.title,
            head=params.branch_name, base="main", body=params.body,
        )
        if pr.get("error") == "no_commits":
            return ""
        return pr.get("pr_url", "")
    finally:
        await client.close()


@activity(start_to_close_timeout=timedelta(minutes=2), name="destroy_sandbox")
async def destroy_sandbox(params: DestroyParams) -> None:
    """Destroy sandbox container."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()
    await sandbox.destroy(params.container_id)


@activity(start_to_close_timeout=timedelta(minutes=10), name="enhance_spec")
async def enhance_spec(params: EnhanceSpecParams) -> str:
    """Run /enhance-spec skill via Vibe CLI to produce a detailed spec from raw requirements.
    Returns the enhanced spec text."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()

    # Build the prompt that triggers the enhance-spec skill
    parts = ["/enhance-spec"]
    if params.issue_title:
        type_label = "Bug" if params.issue_type == "bug" else "Feature"
        parts.append(f"{type_label}: {params.issue_title}")
    if params.issue_body:
        parts.append(f"Details: {params.issue_body}")
    if params.issue_labels:
        parts.append(f"Labels: {', '.join(params.issue_labels)}")
    parts.append(f"Task: {params.task_description}")
    parts.append("Write the enhanced spec to /workspace/SPEC.md")

    prompt = "\n\n".join(parts)
    await sandbox.run_vibe(params.container_id, prompt, params.max_turns, params.max_price)

    # Read the generated spec
    result = await sandbox.exec_in_container(
        params.container_id, "cat /workspace/SPEC.md"
    )
    if result.exit_code == 0 and result.stdout.strip():
        return result.stdout.strip()
    return params.task_description


@activity(start_to_close_timeout=timedelta(minutes=10), name="document_changes")
async def document_changes(params: DocumentChangesParams) -> str:
    """Run /document-changes skill via Vibe CLI to generate PR documentation.
    Returns the generated CHANGES.md content."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()

    parts = ["/document-changes"]
    parts.append(f"Original task: {params.task_description}")
    if params.issue_number:
        parts.append(f"This change addresses issue #{params.issue_number}")
    parts.append("Generate /workspace/CHANGES.md with full change documentation.")

    prompt = "\n\n".join(parts)
    await sandbox.run_vibe(params.container_id, prompt, params.max_turns, params.max_price)

    result = await sandbox.exec_in_container(
        params.container_id, "cat /workspace/CHANGES.md"
    )
    if result.exit_code == 0 and result.stdout.strip():
        return result.stdout.strip()
    return ""


@activity(start_to_close_timeout=timedelta(minutes=2), name="update_pr_body")
async def update_pr_body(params: UpdatePRBodyParams) -> None:
    """Update an existing PR's body with change documentation."""
    from app.integrations.github_client import GitHubClient
    client = GitHubClient(token=settings.github_token)
    try:
        await client.update_pull_request(params.owner, params.repo, params.pr_number, body=params.body)
    finally:
        await client.close()


def build_prompt(params: BuildPromptParams) -> str:
    """Build the agent prompt from task + issue + triage context.
    Pure function — not an activity (no side effects, deterministic).
    Uses /tdd-implement skill to enforce test-driven development."""
    parts = [
        "/tdd-implement",
        "CHANGE TASK — You MUST edit or create files to complete this task using TDD. Do NOT just investigate.",
    ]

    if params.issue_title:
        type_action = "fix this bug" if params.issue_type == "bug" else "implement this feature"
        parts.append(f"Task: {type_action}")
        parts.append(f"Issue: {params.issue_title}")
        if params.issue_body:
            parts.append(f"Details: {params.issue_body}")
        if params.issue_labels:
            parts.append(f"Labels: {', '.join(params.issue_labels)}")
        parts.append(f"Original task: {params.task}")
    else:
        parts.append(params.task)

    if params.triage_approach:
        parts.append(f"Suggested approach: {params.triage_approach}")
    if params.triage_files:
        parts.append(f"Relevant files: {', '.join(params.triage_files)}")

    parts.append("Follow TDD: Write failing tests FIRST, then implement code to make them pass, then refactor.")
    parts.append("Remember: You must make actual code changes. Create or modify files as needed.")

    return "\n\n".join(parts)

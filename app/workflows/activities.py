"""Workflow activities — each side effect as a retriable unit.
Decorated with @activity() for Mistral Workflows SDK.
Activities construct their own clients from params since the SDK's
DI cannot auto-construct classes that require init arguments."""

import asyncio
import logging
from datetime import timedelta

from pydantic import BaseModel

from mistralai.workflows import activity

from app.config import settings
from app.schemas import ConfidenceSummary, TestResultPayload, VibeSummary

logger = logging.getLogger(__name__)

VIBE_TIMEOUT = 300  # 5 minutes — for enhance_spec, document_changes
VIBE_CODE_TIMEOUT = 480  # 8 minutes — for actual coding (run_vibe_code)
VIBE_MAX_RETRIES = 1  # 1 retry (2 total attempts) — avoid wasting time on repeated restarts


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
    test_results: TestResultPayload | None = None


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
    repo_profile: str | None = None


class GetOrCreateRepoProfileParams(BaseModel):
    container_id: str
    repo_url: str
    owner: str
    repo: str


class EnhanceSpecParams(BaseModel):
    container_id: str
    task_description: str
    issue_title: str | None = None
    issue_body: str | None = None
    issue_type: str | None = None
    issue_labels: list[str] | None = None
    repo_profile: str | None = None
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


class UpdateSessionStatusParams(BaseModel):
    session_id: str
    status: str
    message: str = ""
    branch_name: str | None = None
    pr_url: str | None = None
    error_summary: str | None = None
    iteration_count: int | None = None
    issue_title: str | None = None
    issue_type: str | None = None
    pr_merged: bool | None = None
    test_results: TestResultPayload | None = None
    confidence: ConfidenceSummary | None = None


# --- Activities ---

@activity(start_to_close_timeout=timedelta(seconds=10), name="update_session_status")
async def update_session_status(params: UpdateSessionStatusParams) -> None:
    """Persist workflow state to DB and emit SSE event."""
    from app.database import async_session
    from app.models import HydraSession, SessionEvent
    async with async_session() as db:
        session = await db.get(HydraSession, params.session_id)
        if session:
            session.status = params.status
            for field in ("branch_name", "pr_url", "error_summary", "iteration_count",
                          "issue_title", "issue_type", "pr_merged"):
                val = getattr(params, field)
                if val is not None:
                    setattr(session, field, val)
            if params.test_results is not None:
                session.test_results_json = params.test_results.model_dump()
            if params.confidence is not None:
                session.confidence_json = params.confidence.model_dump()
        # Build rich payload with all available metadata
        payload = {"message": params.message or params.status, "status": params.status}
        if params.branch_name:
            payload["branch_name"] = params.branch_name
        if params.pr_url:
            payload["pr_url"] = params.pr_url
        if params.iteration_count is not None:
            payload["iteration"] = params.iteration_count
        if params.error_summary:
            payload["error_summary"] = params.error_summary
        if params.issue_title:
            payload["issue_title"] = params.issue_title
        if params.test_results is not None:
            payload["test_results"] = params.test_results.model_dump()
        if params.confidence is not None:
            payload["confidence"] = params.confidence.model_dump()

        event = SessionEvent(
            session_id=params.session_id,
            event_type="status_change",
            payload=payload,
        )
        db.add(event)
        await db.commit()

        # Compute and persist session metrics on terminal status
        if params.status in ("completed", "failed", "cancelled"):
            try:
                from app.observability import Tracer
                from app.event_bus import event_bus as _event_bus
                tracer = Tracer(
                    session_id=params.session_id,
                    trace_id=params.session_id,
                    event_bus=_event_bus,
                    db=db,
                )
                metrics = await tracer.compute_session_metrics()
                metrics.outcome = params.status
                metrics.failure_reason = params.error_summary
                await db.commit()
            except Exception:
                pass

    try:
        from app.event_bus import event_bus
        await event_bus.publish(params.session_id, {
            "event_type": "status_change",
            "payload": payload,
        })
    except Exception:
        pass


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
    """Run Vibe CLI for coding with timeout + retry."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()
    last_err = None
    for attempt in range(1, VIBE_MAX_RETRIES + 2):
        try:
            logger.info("run_vibe_code attempt %d/%d", attempt, VIBE_MAX_RETRIES + 1)
            return await sandbox.run_vibe(
                params.container_id, params.prompt, params.max_turns, params.max_price,
                timeout=VIBE_CODE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            last_err = f"Vibe CLI timed out after {VIBE_CODE_TIMEOUT}s (attempt {attempt})"
            logger.warning(last_err)
            await sandbox.kill_vibe_processes(params.container_id)
            if attempt > VIBE_MAX_RETRIES:
                raise TimeoutError(last_err)
    raise TimeoutError(last_err)


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
        # Use same base ref approach as get_diff_stats
        base_result = await sandbox.exec_in_container(
            params.container_id,
            "sh -c 'cd /workspace && git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD origin/master 2>/dev/null || git rev-list --max-parents=0 HEAD'",
        )
        base_ref = base_result.stdout.strip().splitlines()[0] if base_result.stdout.strip() else "HEAD"
        diff_result = await sandbox.exec_in_container(
            params.container_id, f"git -C /workspace diff {base_ref} -- requirements*.txt package.json pyproject.toml Pipfile"
        )
        for line in diff_result.stdout.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                dep_match = re.search(r'["\']?([a-zA-Z0-9_-]+)["\']?\s*[>=<~^]', line)
                if dep_match:
                    new_dependencies.append(dep_match.group(1))
    except Exception:
        pass

    # --- Check test coverage ---
    has_test_coverage = False
    if params.test_results and params.test_results.total > 0:
        has_test_coverage = True
    else:
        try:
            result = await sandbox.exec_in_container(
                params.container_id,
                "find /workspace -name 'test_*' -o -name '*_test.*' -o -name '*.test.*' -o -name '*_spec.*' -o -name '*.spec.*' -o -type d -name tests -o -type d -name __tests__ | head -20"
            )
            test_files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
            if test_files:
                has_test_coverage = True
        except Exception:
            pass
    if not has_test_coverage:
        risk_flags.append("No test files found in repo")

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


@activity(start_to_close_timeout=timedelta(minutes=15), name="get_or_create_repo_profile")
async def get_or_create_repo_profile(params: GetOrCreateRepoProfileParams) -> str:
    """Get cached repo profile or generate a new one. Returns profile_text."""
    from app.config import settings
    if not settings.repo_profile_enabled:
        return ""

    from app.database import async_session
    from app.models import RepoProfile
    from app.sandbox.manager import SandboxManager
    from app.repo_profiler import generate_repo_profile
    from sqlalchemy import select

    sandbox = SandboxManager()

    # Get current HEAD SHA
    head_result = await sandbox.exec_in_container(
        params.container_id, "git -C /workspace rev-parse HEAD"
    )
    current_sha = head_result.stdout.strip()[:40]

    # Check for cached profile
    async with async_session() as db:
        stmt = select(RepoProfile).where(RepoProfile.repo_url == params.repo_url)
        result = await db.execute(stmt)
        cached = result.scalar_one_or_none()

        if cached:
            # Check staleness: count commits between cached SHA and current HEAD
            distance_result = await sandbox.exec_in_container(
                params.container_id,
                f"git -C /workspace rev-list --count {cached.head_sha}..HEAD 2>/dev/null || echo 999"
            )
            try:
                commit_distance = int(distance_result.stdout.strip())
            except ValueError:
                commit_distance = 999

            if commit_distance <= settings.repo_profile_stale_commits:
                logger.info(
                    "Using cached repo profile for %s (age: %d commits)",
                    params.repo_url, commit_distance,
                )
                return cached.profile_text

            logger.info(
                "Repo profile stale for %s (%d commits behind), regenerating",
                params.repo_url, commit_distance,
            )

    # Generate new profile
    logger.info("Generating repo profile for %s", params.repo_url)
    profile_data = await generate_repo_profile(sandbox, params.container_id)

    # Upsert into DB
    async with async_session() as db:
        stmt = select(RepoProfile).where(RepoProfile.repo_url == params.repo_url)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            for key, value in profile_data.items():
                setattr(existing, key, value)
        else:
            profile = RepoProfile(
                repo_url=params.repo_url,
                owner=params.owner,
                repo_name=params.repo,
                **profile_data,
            )
            db.add(profile)

        await db.commit()

    logger.info("Repo profile saved for %s (%d chars)", params.repo_url, len(profile_data["profile_text"]))
    return profile_data["profile_text"]


@activity(start_to_close_timeout=timedelta(minutes=10), name="enhance_spec")
async def enhance_spec(params: EnhanceSpecParams) -> str:
    """Analyze the codebase and produce a detailed implementation spec.
    Returns the enhanced spec text. Retries on timeout."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()

    # Build a concrete prompt for Vibe CLI (no custom skill prefixes)
    parts = [
        "CHANGE TASK — You MUST create the file /workspace/SPEC.md with a detailed implementation spec.",
        "",
        "Analyze the codebase in /workspace and write a detailed spec for the following task:",
    ]
    if params.issue_title:
        type_label = "Bug fix" if params.issue_type == "bug" else "Feature"
        parts.append(f"{type_label}: {params.issue_title}")
    if params.issue_body:
        parts.append(f"Description: {params.issue_body}")
    if params.issue_labels:
        parts.append(f"Labels: {', '.join(params.issue_labels)}")
    if params.task_description and params.task_description != params.issue_title:
        parts.append(f"Additional context: {params.task_description}")
    if params.repo_profile:
        parts.append("")
        parts.append(f"Repository context:\n{params.repo_profile}")
    parts.append("")
    parts.append("Your SPEC.md must include:")
    parts.append("1. Which files need to be modified or created")
    parts.append("2. What specific changes to make in each file")
    parts.append("3. Any new dependencies or imports needed")
    parts.append("4. Edge cases to handle")
    parts.append("")
    parts.append("Write the spec to /workspace/SPEC.md. Do NOT implement the changes yet, only write the spec.")

    prompt = "\n".join(parts)
    last_err = None
    for attempt in range(1, VIBE_MAX_RETRIES + 2):
        try:
            logger.info("enhance_spec attempt %d/%d", attempt, VIBE_MAX_RETRIES + 1)
            await sandbox.run_vibe(
                params.container_id, prompt, params.max_turns, params.max_price,
                timeout=VIBE_TIMEOUT,
            )
            break
        except asyncio.TimeoutError:
            last_err = f"enhance_spec timed out after {VIBE_TIMEOUT}s (attempt {attempt})"
            logger.warning(last_err)
            await sandbox.kill_vibe_processes(params.container_id)
            if attempt > VIBE_MAX_RETRIES:
                logger.warning("enhance_spec exhausted retries, falling back to raw task")
                if params.issue_title:
                    fallback = params.issue_title
                    if params.issue_body and params.issue_body != params.issue_title:
                        fallback += f"\n\n{params.issue_body}"
                    return fallback
                return params.task_description

    # Read the generated spec
    result = await sandbox.exec_in_container(
        params.container_id, "cat /workspace/SPEC.md"
    )
    if result.exit_code == 0 and result.stdout.strip():
        return result.stdout.strip()
    # Fallback: use issue title+body as a better spec than raw task_description
    if params.issue_title:
        fallback = params.issue_title
        if params.issue_body and params.issue_body != params.issue_title:
            fallback += f"\n\n{params.issue_body}"
        return fallback
    return params.task_description


@activity(start_to_close_timeout=timedelta(minutes=10), name="document_changes")
async def document_changes(params: DocumentChangesParams) -> str:
    """Generate PR documentation via Vibe CLI.
    Returns the generated CHANGES.md content."""
    from app.sandbox.manager import SandboxManager
    sandbox = SandboxManager()

    parts = [
        "CHANGE TASK — You MUST create the file /workspace/CHANGES.md documenting all changes made.",
        "",
        f"Original task: {params.task_description}",
    ]
    if params.issue_number:
        parts.append(f"This change addresses issue #{params.issue_number}")
    parts.append("")
    parts.append("Run `git diff` to see what was changed, then write /workspace/CHANGES.md with:")
    parts.append("- Summary of changes")
    parts.append("- Files modified and why")
    parts.append("- Testing notes")

    prompt = "\n\n".join(parts)
    for attempt in range(1, VIBE_MAX_RETRIES + 2):
        try:
            logger.info("document_changes attempt %d/%d", attempt, VIBE_MAX_RETRIES + 1)
            await sandbox.run_vibe(
                params.container_id, prompt, params.max_turns, params.max_price,
                timeout=VIBE_TIMEOUT,
            )
            break
        except asyncio.TimeoutError:
            logger.warning("document_changes timed out after %ds (attempt %d)", VIBE_TIMEOUT, attempt)
            await sandbox.kill_vibe_processes(params.container_id)
            if attempt > VIBE_MAX_RETRIES:
                logger.warning("document_changes exhausted retries, skipping")
                return ""

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

    if params.repo_profile:
        parts.append(f"Repository context:\n{params.repo_profile}")

    if params.triage_approach:
        parts.append(f"Suggested approach: {params.triage_approach}")
    if params.triage_files:
        parts.append(f"Relevant files: {', '.join(params.triage_files)}")

    parts.append("Follow TDD: Write failing tests FIRST, then implement code to make them pass, then refactor.")
    parts.append("Remember: You must make actual code changes. Create or modify files as needed.")

    return "\n\n".join(parts)

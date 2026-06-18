"""Session Workflow — the brain. Owns all state, decisions, and transitions.

Uses Mistral Workflows SDK (@workflow.define) for durability, signals,
queries, and activity execution. Backed by Temporal under the hood."""

from datetime import timedelta

from pydantic import BaseModel, Field

from mistralai.workflows import workflow, get_execution_id


async def _wait_condition(predicate, timeout: timedelta | None = None):
    """Wait for a condition using Mistral's hosted Temporal via workflow.wait_condition."""
    await workflow.wait_condition(predicate, timeout=timeout)

with workflow.unsafe.imports_passed_through():
    from app.schemas import (
        CIResultPayload,
        ConfidenceSummary,
        TestResultPayload,
    )
    from app.workflows.activities import (
        BuildPromptParams,
        CloneRepoParams,
        CommitAndPushParams,
        ConfidenceParams,
        DestroyParams,
        DocumentChangesParams,
        EnhanceSpecParams,
        FetchIssueParams,
        OpenPRParams,
        ProvisionSandboxParams,
        RunTestsParams,
        RunVibeParams,
        UpdatePRBodyParams,
        UpdateSessionStatusParams,
        build_prompt,
        clone_repo,
        commit_and_push,
        destroy_sandbox,
        document_changes,
        enhance_spec,
        fetch_issue,
        generate_confidence_summary,
        open_pr,
        provision_sandbox,
        run_tests,
        run_vibe_code,
        update_pr_body,
        update_session_status,
    )

import logging
logger = logging.getLogger(__name__)


# --- Workflow input (Pydantic model for SDK serialization) ---

class SessionInput(BaseModel):
    session_id: str
    repo_url: str
    task_description: str
    max_iterations: int = 3
    issue_number: int | None = None
    issue_type: str | None = None
    issue_title: str | None = None
    triage_suggested_approach: str | None = None
    triage_relevant_files: list[str] = Field(default_factory=list)
    owner: str = ""
    repo: str = ""
    github_token: str = ""
    mistral_api_key: str = ""
    vibe_max_turns: int = 50
    vibe_max_price: float = 5.0


# --- Workflow state (held in memory by the workflow, queryable) ---

class WorkflowState(BaseModel):
    status: str = "pending"
    iteration: int = 0
    max_iterations: int = 3
    container_id: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    pr_merged: bool = False
    test_results: TestResultPayload | None = None
    ci_results: list[CIResultPayload] = Field(default_factory=list)
    error_summary: str | None = None
    human_assists: list[str] = Field(default_factory=list)
    review_comments: list[dict] = Field(default_factory=list)
    confidence: ConfidenceSummary | None = None
    triage_suggested_approach: str | None = None
    triage_relevant_files: list[str] = Field(default_factory=list)
    issue_number: int | None = None
    issue_type: str | None = None
    issue_title: str | None = None
    issue_body: str | None = None
    issue_labels: list[str] = Field(default_factory=list)
    enhanced_spec: str | None = None
    change_docs: str | None = None
    pr_number: int | None = None


# --- Signal payloads ---

class CISignalData(BaseModel):
    payload: CIResultPayload

class HumanAssistData(BaseModel):
    guidance: str

class ReviewFeedbackData(BaseModel):
    action: str
    comments: list[dict] = Field(default_factory=list)


# --- The Workflow ---

@workflow.define(name="hydra_session", execution_timeout=timedelta(days=7))
class HydraSessionWorkflow:
    """The coding agent workflow — orchestrates sandbox, coding, testing,
    CI monitoring, PR review, and human-in-the-loop."""

    def __init__(self):
        self.state = WorkflowState()
        self._ci_received = False
        self._human_received = False
        self._review_received = False
        self._cancelled = False

    # --- Queries ---

    @workflow.query(name="get_status", description="Get workflow status and state")
    def get_status(self) -> WorkflowState:
        return self.state

    @workflow.query(name="get_test_results", description="Get latest test results")
    def get_test_results(self) -> TestResultPayload | None:
        return self.state.test_results

    @workflow.query(name="get_pr_info", description="Get PR information")
    def get_pr_info(self) -> dict:
        return {
            "pr_url": self.state.pr_url,
            "merged": self.state.pr_merged,
            "review_comments": self.state.review_comments,
        }

    @workflow.query(name="get_confidence", description="Get confidence summary")
    def get_confidence(self) -> ConfidenceSummary | None:
        return self.state.confidence

    # --- Signals ---

    @workflow.signal(name="ci_result", description="CI result signal from webhook")
    async def signal_ci_result(self, data: CISignalData) -> None:
        self.state.ci_results.append(data.payload)
        self._ci_received = True

    @workflow.signal(name="human_assist", description="Human guidance signal")
    async def signal_human_assist(self, data: HumanAssistData) -> None:
        self.state.human_assists.append(data.guidance)
        self._human_received = True

    @workflow.signal(name="review_feedback", description="PR review feedback signal")
    async def signal_review_feedback(self, data: ReviewFeedbackData) -> None:
        self.state.review_comments.extend(data.comments)
        self._review_received = True

    @workflow.signal(name="cancel", description="Cancel the workflow")
    async def signal_cancel(self) -> None:
        self._cancelled = True

    # --- Main workflow entrypoint ---

    @workflow.entrypoint
    async def run(self, input: SessionInput) -> WorkflowState:
        self.state.status = "running"
        self.state.max_iterations = input.max_iterations
        self.state.triage_suggested_approach = input.triage_suggested_approach
        self.state.triage_relevant_files = input.triage_relevant_files

        branch_name = f"hydra/{input.session_id[:8]}"
        self.state.branch_name = branch_name

        await update_session_status(UpdateSessionStatusParams(
            session_id=input.session_id, status="running",
            branch_name=branch_name,
            issue_title=input.issue_title,
            issue_type=input.issue_type,
            message=f"Starting workflow on branch {branch_name} for: {input.task_description[:100]}",
        ))

        # -- Phase 0: Setup --
        await self._phase_setup(input)

        # -- Phase 1: Enhance Spec --
        await self._phase_enhance_spec(input)

        # Build prompt using enhanced spec
        spec_task = self.state.enhanced_spec or input.task_description
        prompt = build_prompt(BuildPromptParams(
            task=spec_task,
            issue_title=self.state.issue_title,
            issue_body=self.state.issue_body,
            issue_type=self.state.issue_type,
            issue_labels=self.state.issue_labels,
            triage_approach=self.state.triage_suggested_approach,
            triage_files=self.state.triage_relevant_files,
        ))

        # -- Phase 2: Code and Test --
        await self._phase_code_and_test(input, prompt)

        if self._cancelled:
            return await self._cleanup("cancelled", session_id=input.session_id)

        # -- Phase 3: PR creation --
        await self._phase_create_pr(input, branch_name)

        if self.state.error_summary == "no_changes":
            return await self._cleanup("failed", failure_reason="no_changes", session_id=input.session_id)

        if not self.state.pr_url:
            return await self._cleanup("failed", failure_reason="pr_creation_failed", session_id=input.session_id)

        # -- Phase 4: Document Changes & attach to PR --
        await self._phase_document_changes(input)

        await update_session_status(UpdateSessionStatusParams(
            session_id=input.session_id, status="ci_monitoring",
            pr_url=self.state.pr_url, branch_name=branch_name, iteration_count=1,
            message=f"PR created: {self.state.pr_url}",
        ))

        # -- Phase 5: CI monitoring loop --
        self.state.status = "ci_monitoring"
        for i in range(self.state.max_iterations):
            self.state.iteration = i + 1

            result = await self._phase_ci_iteration(input, branch_name, prompt)

            if result == "completed":
                return await self._cleanup("completed", session_id=input.session_id)
            elif result == "cancelled":
                return await self._cleanup("cancelled", session_id=input.session_id)
            elif result == "review_feedback":
                # Handle review comments received during CI monitoring
                self.state.status = "running"
                await update_session_status(UpdateSessionStatusParams(
                    session_id=input.session_id, status="running",
                    message="Addressing review feedback",
                ))
                feedback = "\n".join(
                    f"- {c.get('author', '?')}: {c.get('body', '')}"
                    for c in self.state.review_comments
                ) or "Address the PR review feedback"
                await run_vibe_code(RunVibeParams(
                    container_id=self.state.container_id,
                    prompt=f"Address this PR review feedback:\n{feedback}\n\nOriginal task: {prompt}",
                ))
                self.state.confidence = await generate_confidence_summary(ConfidenceParams(
                    container_id=self.state.container_id,
                    task_description=input.task_description,
                    issue_type=input.issue_type or "",
                    test_results=self.state.test_results,
                ))
                await commit_and_push(CommitAndPushParams(
                    container_id=self.state.container_id,
                    branch_name=branch_name,
                    message="hydra: address review feedback",
                ))
                self.state.review_comments = []
                self._review_received = False
                self.state.status = "ci_monitoring"
                await update_session_status(UpdateSessionStatusParams(
                    session_id=input.session_id, status="ci_monitoring",
                    iteration_count=self.state.iteration,
                    message=f"Pushed review fixes, waiting for CI (iteration {self.state.iteration}/{self.state.max_iterations})",
                ))
                continue
            elif result == "continue":
                continue

        return await self._cleanup("failed", failure_reason="max_iterations", session_id=input.session_id)

    async def _phase_setup(self, input: SessionInput):
        if input.issue_number:
            issue_ctx = await fetch_issue(FetchIssueParams(
                owner=input.owner, repo=input.repo,
                issue_number=input.issue_number,
            ))
            self.state.issue_number = input.issue_number
            self.state.issue_type = input.issue_type
            self.state.issue_title = issue_ctx.title or input.issue_title
            self.state.issue_body = issue_ctx.body
            self.state.issue_labels = issue_ctx.labels

        if self.state.container_id is None:
            self.state.container_id = await provision_sandbox(ProvisionSandboxParams(
                session_id=input.session_id, repo_url=input.repo_url,
                token=input.github_token, api_key=input.mistral_api_key,
            ))
            await clone_repo(CloneRepoParams(
                container_id=self.state.container_id,
                repo_url=input.repo_url,
                branch_name=self.state.branch_name,
                token=input.github_token,
            ))

        issue_info = f" — Issue #{self.state.issue_number}: {self.state.issue_title}" if self.state.issue_number else ""
        await update_session_status(UpdateSessionStatusParams(
            session_id=input.session_id, status="running",
            issue_title=self.state.issue_title, issue_type=self.state.issue_type,
            message=f"Sandbox ready, cloned {input.repo_url} on branch {self.state.branch_name}{issue_info}",
        ))

    async def _phase_enhance_spec(self, input: SessionInput):
        """Analyze codebase and produce a detailed implementation spec."""
        if not self.state.container_id:
            return
        try:
            self.state.enhanced_spec = await enhance_spec(EnhanceSpecParams(
                container_id=self.state.container_id,
                task_description=input.task_description,
                issue_title=self.state.issue_title,
                issue_body=self.state.issue_body,
                issue_type=self.state.issue_type,
                issue_labels=self.state.issue_labels,
            ))
            logger.info("Enhanced spec generated (%d chars)", len(self.state.enhanced_spec))
        except Exception as e:
            logger.warning("enhance_spec failed, using raw task: %s", e)
            self.state.enhanced_spec = input.task_description

    async def _phase_document_changes(self, input: SessionInput):
        """Generate change documentation and attach to PR."""
        if not self.state.container_id or not self.state.pr_url:
            return
        try:
            self.state.change_docs = await document_changes(DocumentChangesParams(
                container_id=self.state.container_id,
                task_description=input.task_description,
                issue_number=self.state.issue_number,
            ))
            if self.state.change_docs and self.state.pr_number:
                pr_body_parts = []
                if self.state.issue_number:
                    action = "Fixes" if self.state.issue_type == "bug" else "Implements"
                    pr_body_parts.append(f"{action} #{self.state.issue_number}")
                if self.state.confidence:
                    pr_body_parts.append(f"\n{self.state.confidence.summary}")
                pr_body_parts.append("\n---\n")
                pr_body_parts.append(self.state.change_docs)

                await update_pr_body(UpdatePRBodyParams(
                    owner=input.owner,
                    repo=input.repo,
                    pr_number=self.state.pr_number,
                    body="\n\n".join(pr_body_parts),
                ))
                await commit_and_push(CommitAndPushParams(
                    container_id=self.state.container_id,
                    branch_name=self.state.branch_name,
                    message="hydra: add change documentation",
                ))
            logger.info("Change documentation generated (%d chars)", len(self.state.change_docs or ""))
        except Exception as e:
            logger.warning("document_changes failed: %s", e)

    async def _phase_code_and_test(self, input: SessionInput, prompt: str):
        if not self.state.container_id:
            return

        vibe_result = await run_vibe_code(RunVibeParams(
            container_id=self.state.container_id, prompt=prompt,
            max_turns=input.vibe_max_turns, max_price=input.vibe_max_price,
        ))
        logger.info("Vibe result: turns=%s, tool_calls=%s, files=%s",
                     vibe_result.turns, vibe_result.tool_calls, vibe_result.files_modified)

        self.state.test_results = await run_tests(RunTestsParams(
            container_id=self.state.container_id,
        ))

        # Auto-fix if tests fail
        if self.state.test_results and self.state.test_results.failed > 0:
            failure_info = "\n".join(
                f"- {f.name}: {f.error}" for f in self.state.test_results.failures
            )
            fix_prompt = f"Fix failing tests:\n{failure_info}"
            await run_vibe_code(RunVibeParams(
                container_id=self.state.container_id, prompt=fix_prompt,
            ))
            self.state.test_results = await run_tests(RunTestsParams(
                container_id=self.state.container_id,
            ))

        # Report coding + test results
        tr = self.state.test_results
        test_msg = f"Tests: {tr.passed}/{tr.total} passed, {tr.failed} failed" if tr else "No tests run"
        await update_session_status(UpdateSessionStatusParams(
            session_id=input.session_id, status="running",
            message=f"Coding complete ({vibe_result.files_modified} files modified, {vibe_result.turns} turns). {test_msg}",
        ))

    async def _phase_create_pr(self, input: SessionInput, branch_name: str):
        if not self.state.container_id:
            return

        self.state.confidence = await generate_confidence_summary(ConfidenceParams(
            container_id=self.state.container_id,
            task_description=input.task_description,
            issue_type=input.issue_type or "",
            test_results=self.state.test_results,
        ))
        logger.info("Confidence: files_changed=%s",
                     self.state.confidence.files_changed if self.state.confidence else "N/A")

        # Skip PR if no files were changed
        if self.state.confidence and self.state.confidence.files_changed == 0:
            self.state.error_summary = "no_changes"
            return

        await commit_and_push(CommitAndPushParams(
            container_id=self.state.container_id,
            branch_name=branch_name,
            message=f"hydra: {input.task_description[:50]}",
        ))

        # Build PR body
        pr_body_parts = []
        if self.state.issue_number:
            action = "Fixes" if self.state.issue_type == "bug" else "Implements"
            pr_body_parts.append(f"{action} #{self.state.issue_number}")
        if self.state.confidence:
            pr_body_parts.append(f"\n{self.state.confidence.summary}")

        pr_result = await open_pr(OpenPRParams(
            owner=input.owner, repo=input.repo, branch_name=branch_name,
            title=f"hydra: {input.task_description[:60]}",
            body="\n\n".join(pr_body_parts),
        ))
        self.state.pr_url = pr_result
        if pr_result:
            try:
                self.state.pr_number = int(pr_result.rstrip("/").split("/")[-1])
            except (ValueError, IndexError):
                pass

        conf = self.state.confidence
        conf_msg = f"Confidence: {conf.confidence_score}/100, {conf.files_changed} files (+{conf.lines_added}/-{conf.lines_removed})" if conf else ""
        await update_session_status(UpdateSessionStatusParams(
            session_id=input.session_id, status="running",
            pr_url=self.state.pr_url,
            message=f"PR #{self.state.pr_number} created: {self.state.pr_url}. {conf_msg}",
        ))

    async def _phase_ci_iteration(
        self, input: SessionInput, branch_name: str, prompt: str,
    ) -> str:
        """Returns 'completed', 'cancelled', 'review_feedback', or 'continue'."""
        self._ci_received = False
        await _wait_condition(
            lambda: self._ci_received or self._review_received or self._cancelled,
            timeout=timedelta(hours=1),
        )

        if self._cancelled:
            return "cancelled"

        if self._review_received and not self._ci_received:
            return "review_feedback"

        latest_ci = self.state.ci_results[-1] if self.state.ci_results else None
        if not latest_ci:
            return "continue"

        ci_passed = latest_ci.conclusion == "success"

        if ci_passed:
            self.state.status = "pr_review"
            result = await self._phase_pr_review(input, branch_name)
            return result

        # CI failed — try auto-fix
        self.state.status = "running"
        error_details = latest_ci.conclusion or "CI failed"
        if latest_ci.test_results:
            failures = "\n".join(
                f"- {f.name}: {f.error}" for f in latest_ci.test_results.failures
            )
            error_details = f"CI failed:\n{failures}"

        await run_vibe_code(RunVibeParams(
            container_id=self.state.container_id,
            prompt=f"CI failed. Fix the issues:\n{error_details}",
        ))

        self.state.test_results = await run_tests(RunTestsParams(
            container_id=self.state.container_id,
        ))

        if self.state.test_results and self.state.test_results.failed > 0:
            return await self._request_human_help(input, branch_name)

        await commit_and_push(CommitAndPushParams(
            container_id=self.state.container_id,
            branch_name=branch_name,
            message="hydra: fix CI failures",
        ))
        self.state.status = "ci_monitoring"
        return "continue"

    async def _phase_pr_review(
        self, input: SessionInput, branch_name: str,
    ) -> str:
        self._review_received = False
        await _wait_condition(
            lambda: self._review_received or self._cancelled,
            timeout=timedelta(hours=24),
        )

        if self._cancelled:
            return "cancelled"

        if self.state.review_comments:
            feedback = "\n".join(c.get("body", "") for c in self.state.review_comments)
            self.state.status = "running"

            await run_vibe_code(RunVibeParams(
                container_id=self.state.container_id,
                prompt=f"PR review feedback:\n{feedback}\nAddress these comments.",
            ))

            self.state.test_results = await run_tests(RunTestsParams(
                container_id=self.state.container_id,
            ))

            await commit_and_push(CommitAndPushParams(
                container_id=self.state.container_id,
                branch_name=branch_name,
                message="hydra: address review feedback",
            ))

            self.state.review_comments = []
            self.state.status = "ci_monitoring"
            return "continue"

        return "completed"

    async def _request_human_help(
        self, input: SessionInput, branch_name: str,
    ) -> str:
        self.state.status = "needs_human"
        self.state.error_summary = self._build_error_context()

        self._human_received = False
        await _wait_condition(
            lambda: self._human_received or self._cancelled,
            timeout=timedelta(hours=48),
        )

        if self._cancelled:
            return "cancelled"

        hint = self.state.human_assists[-1] if self.state.human_assists else ""
        self.state.status = "running"

        await run_vibe_code(RunVibeParams(
            container_id=self.state.container_id,
            prompt=f"Human guidance: {hint}\nFix the issues.",
        ))

        self.state.test_results = await run_tests(RunTestsParams(
            container_id=self.state.container_id,
        ))

        await commit_and_push(CommitAndPushParams(
            container_id=self.state.container_id,
            branch_name=branch_name,
            message="hydra: apply human guidance",
        ))
        self.state.status = "ci_monitoring"
        return "continue"

    async def _cleanup(self, final_status: str, failure_reason: str | None = None,
                       session_id: str | None = None) -> WorkflowState:
        self.state.status = final_status
        if failure_reason:
            self.state.error_summary = failure_reason

        if self.state.container_id:
            await destroy_sandbox(DestroyParams(
                container_id=self.state.container_id,
            ))

        if session_id:
            parts = [f"Workflow {final_status}"]
            if self.state.pr_url:
                parts.append(f"PR: {self.state.pr_url}")
            if failure_reason:
                parts.append(f"Reason: {failure_reason}")
            if self.state.iteration > 0:
                parts.append(f"After {self.state.iteration} iteration(s)")

            await update_session_status(UpdateSessionStatusParams(
                session_id=session_id, status=final_status,
                error_summary=failure_reason,
                pr_url=self.state.pr_url,
                branch_name=self.state.branch_name,
                iteration_count=self.state.iteration,
                message=" | ".join(parts),
            ))

        return self.state

    def _build_error_context(self) -> str:
        parts = [f"Iteration {self.state.iteration} of {self.state.max_iterations}"]
        if self.state.test_results and self.state.test_results.failures:
            parts.append("Failing tests:")
            for f in self.state.test_results.failures[:5]:
                parts.append(f"  - {f.name}: {f.error}")
        if self.state.ci_results:
            latest = self.state.ci_results[-1]
            parts.append(f"Latest CI: {latest.conclusion}")
        return "\n".join(parts)

"""Tests for HydraSessionWorkflow — uses mocked activities.
Tests run without Temporal by patching workflow.wait_condition
and calling activities locally."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas import CIResultPayload, TestResultPayload, TestFailure, VibeSummary, ConfidenceSummary
from app.workflows.hydra_session import (
    HydraSessionWorkflow,
    SessionInput,
    WorkflowState,
    CISignalData,
    HumanAssistData,
    ReviewFeedbackData,
)


# --- Helpers ---

def _make_input(**overrides) -> SessionInput:
    defaults = dict(
        session_id="test-session-id",
        repo_url="https://github.com/test/repo",
        task_description="Fix the login bug",
        max_iterations=3,
        owner="test",
        repo="repo",
        github_token="token",
        mistral_api_key="key",
    )
    defaults.update(overrides)
    return SessionInput(**defaults)


async def _wait_for_status(wf, status: str, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while wf.state.status != status:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Workflow didn't reach '{status}', stuck at '{wf.state.status}'")
        await asyncio.sleep(0.01)


async def _wait_for_iteration(wf, iteration: int, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while wf.state.iteration < iteration:
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Workflow didn't reach iteration {iteration}, at {wf.state.iteration}")
        await asyncio.sleep(0.01)


async def _mock_wait_condition(predicate, timeout=None, timeout_summary=None):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("wait_condition timed out in test")


def _make_activity_mocks(test_results_sequence=None):
    if test_results_sequence is None:
        test_results_sequence = [
            TestResultPayload(total=5, passed=5, failed=0, skipped=0)
        ]
    call_idx = [0]

    async def mock_fetch_issue(params):
        from app.workflows.activities import IssueContext
        return IssueContext(title="Bug report", body="Something is broken", labels=["bug"])

    async def mock_provision(params):
        return "container-123"

    async def mock_clone(params):
        return None

    async def mock_run_vibe(params):
        return VibeSummary(turns=2, tool_calls=5, files_modified=["main.py"], tokens_used=1000)

    async def mock_run_tests(params):
        idx = min(call_idx[0], len(test_results_sequence) - 1)
        call_idx[0] += 1
        return test_results_sequence[idx]

    async def mock_confidence(params):
        return ConfidenceSummary(files_changed=2, lines_added=10, lines_removed=3,
                                summary="Changed 2 files (+10/-3)")

    async def mock_commit(params):
        return None

    async def mock_open_pr(params):
        return "https://github.com/test/repo/pull/1"

    async def mock_destroy(params):
        return None

    return {
        "app.workflows.hydra_session.fetch_issue": AsyncMock(side_effect=mock_fetch_issue),
        "app.workflows.hydra_session.provision_sandbox": AsyncMock(side_effect=mock_provision),
        "app.workflows.hydra_session.clone_repo": AsyncMock(side_effect=mock_clone),
        "app.workflows.hydra_session.run_vibe_code": AsyncMock(side_effect=mock_run_vibe),
        "app.workflows.hydra_session.run_tests": AsyncMock(side_effect=mock_run_tests),
        "app.workflows.hydra_session.generate_confidence_summary": AsyncMock(side_effect=mock_confidence),
        "app.workflows.hydra_session.commit_and_push": AsyncMock(side_effect=mock_commit),
        "app.workflows.hydra_session.open_pr": AsyncMock(side_effect=mock_open_pr),
        "app.workflows.hydra_session.enhance_spec": AsyncMock(return_value="Enhanced spec for testing"),
        "app.workflows.hydra_session.document_changes": AsyncMock(return_value="# Changes\n- Test changes"),
        "app.workflows.hydra_session.update_pr_body": AsyncMock(return_value=None),
        "app.workflows.hydra_session.destroy_sandbox": AsyncMock(side_effect=mock_destroy),
        "app.workflows.hydra_session.get_or_create_repo_profile": AsyncMock(return_value=""),
    }


def _start_patches(test_results_sequence=None):
    mocks = _make_activity_mocks(test_results_sequence)
    patches = [patch(k, v) for k, v in mocks.items()]
    patches.append(patch("app.workflows.hydra_session.workflow.wait_condition", side_effect=_mock_wait_condition))
    for p in patches:
        p.start()
    return patches


def _stop_patches(patches):
    for p in patches:
        p.stop()


# --- Tests ---

async def test_happy_path():
    patches = _start_patches()
    try:
        wf = HydraSessionWorkflow()

        async def run_signals():
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name="CI", status="completed", conclusion="success",
            )))
            await _wait_for_status(wf, "pr_review")
            await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        task = asyncio.create_task(run_signals())
        await wf.run(_make_input())
        await task

        assert wf.state.status == "completed"
        assert wf.state.pr_url == "https://github.com/test/repo/pull/1"
    finally:
        _stop_patches(patches)


async def test_ci_fail_autofix():
    patches = _start_patches()
    try:
        wf = HydraSessionWorkflow()

        async def run_signals():
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name="CI", status="completed", conclusion="failure",
            )))
            await _wait_for_iteration(wf, 2)
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name="CI", status="completed", conclusion="success",
            )))
            await _wait_for_status(wf, "pr_review")
            await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        task = asyncio.create_task(run_signals())
        await wf.run(_make_input())
        await task

        assert wf.state.status == "completed"
        assert wf.state.iteration == 2
    finally:
        _stop_patches(patches)


async def test_ci_fail_needs_human():
    patches = _start_patches(test_results_sequence=[
        TestResultPayload(total=5, passed=5, failed=0, skipped=0),
        TestResultPayload(total=5, passed=3, failed=2, skipped=0,
                          failures=[TestFailure(name="test_auth", error="auth broken")]),
        TestResultPayload(total=5, passed=5, failed=0, skipped=0),
    ])
    try:
        wf = HydraSessionWorkflow()

        async def run_signals():
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name="CI", status="completed", conclusion="failure",
            )))
            await _wait_for_status(wf, "needs_human")
            await wf.signal_human_assist(HumanAssistData(guidance="Check the auth module"))
            await _wait_for_iteration(wf, 2)
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name="CI", status="completed", conclusion="success",
            )))
            await _wait_for_status(wf, "pr_review")
            await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        task = asyncio.create_task(run_signals())
        await wf.run(_make_input())
        await task

        assert wf.state.status == "completed"
        assert len(wf.state.human_assists) == 1
    finally:
        _stop_patches(patches)


async def test_max_iterations_failed():
    patches = _start_patches()
    try:
        wf = HydraSessionWorkflow()

        async def run_signals():
            for i in range(3):
                await _wait_for_iteration(wf, i + 1)
                await _wait_for_status(wf, "ci_monitoring")
                await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                    check_name="CI", status="completed", conclusion="failure",
                )))

        task = asyncio.create_task(run_signals())
        await wf.run(_make_input(max_iterations=3))
        await task

        assert wf.state.status == "failed"
        assert wf.state.error_summary == "max_iterations"
    finally:
        _stop_patches(patches)


async def test_cancel_during_ci_wait():
    patches = _start_patches()
    try:
        wf = HydraSessionWorkflow()

        async def run_signals():
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_cancel()

        task = asyncio.create_task(run_signals())
        await wf.run(_make_input())
        await task

        assert wf.state.status == "cancelled"
    finally:
        _stop_patches(patches)


async def test_pr_review_changes_requested():
    patches = _start_patches()
    try:
        wf = HydraSessionWorkflow()

        async def run_signals():
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name="CI", status="completed", conclusion="success",
            )))
            await _wait_for_status(wf, "pr_review")
            await wf.signal_review_feedback(ReviewFeedbackData(
                action="changes_requested",
                comments=[{"author": "reviewer", "body": "Please add error handling"}],
            ))
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name="CI", status="completed", conclusion="success",
            )))
            await _wait_for_status(wf, "pr_review")
            await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        task = asyncio.create_task(run_signals())
        await wf.run(_make_input())
        await task

        assert wf.state.status == "completed"
    finally:
        _stop_patches(patches)


async def test_workflow_queries():
    wf = HydraSessionWorkflow()
    assert wf.state.status == "pending"
    assert wf.state.test_results is None
    assert wf.state.pr_url is None
    assert wf.state.confidence is None


async def test_workflow_with_issue_context():
    patches = _start_patches()
    try:
        wf = HydraSessionWorkflow()

        async def run_signals():
            await _wait_for_status(wf, "ci_monitoring")
            await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
                check_name="CI", status="completed", conclusion="success",
            )))
            await _wait_for_status(wf, "pr_review")
            await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        task = asyncio.create_task(run_signals())
        await wf.run(_make_input(issue_number=42, issue_type="bug"))
        await task

        assert wf.state.issue_number == 42
        assert wf.state.issue_type == "bug"
        assert wf.state.issue_title == "Bug report"
    finally:
        _stop_patches(patches)

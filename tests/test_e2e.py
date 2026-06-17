"""E2E integration tests — full pipeline with mocked activities.
Uses patched activities and workflow.wait_condition for testing
without a Temporal server."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas import CIResultPayload, TestFailure, TestResultPayload, VibeSummary, ConfidenceSummary
from app.workflows.hydra_session import (
    HydraSessionWorkflow,
    SessionInput,
    CISignalData,
    HumanAssistData,
    ReviewFeedbackData,
)


# --- Helpers ---

def _test_result(passed=5, failed=0, failures=None):
    return TestResultPayload(
        total=passed + failed, passed=passed, failed=failed,
        skipped=0, failures=failures or [],
    )


async def _mock_wait_condition(predicate, timeout=None, timeout_summary=None):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("wait_condition timed out in test")


def _make_patches(test_results_fn=None):
    if test_results_fn is None:
        test_results_fn = AsyncMock(return_value=_test_result())

    destroy_mock = AsyncMock(return_value=None)
    patches_dict = {
        "app.workflows.hydra_session.fetch_issue": AsyncMock(return_value=MagicMock(
            title="Login bug", body="Login fails", labels=["bug"]
        )),
        "app.workflows.hydra_session.provision_sandbox": AsyncMock(return_value="container-e2e"),
        "app.workflows.hydra_session.clone_repo": AsyncMock(return_value=None),
        "app.workflows.hydra_session.run_vibe_code": AsyncMock(return_value=VibeSummary(turns=3, tool_calls=5)),
        "app.workflows.hydra_session.run_tests": test_results_fn,
        "app.workflows.hydra_session.generate_confidence_summary": AsyncMock(return_value=ConfidenceSummary(
            files_changed=2, lines_added=30, lines_removed=5, summary="Changed 2 files"
        )),
        "app.workflows.hydra_session.commit_and_push": AsyncMock(return_value=None),
        "app.workflows.hydra_session.open_pr": AsyncMock(return_value="https://github.com/test/repo/pull/1"),
        "app.workflows.hydra_session.destroy_sandbox": destroy_mock,
        "app.workflows.hydra_session.workflow.wait_condition": AsyncMock(side_effect=_mock_wait_condition),
    }
    started = [patch(k, v) for k, v in patches_dict.items()]
    for p in started:
        p.start()
    return started, destroy_mock


def _stop(patches):
    for p in patches:
        p.stop()


def make_input(**overrides):
    defaults = {
        "session_id": "e2e-session-1",
        "repo_url": "https://github.com/test/repo",
        "task_description": "Fix the login bug",
        "max_iterations": 3,
        "owner": "test",
        "repo": "repo",
    }
    defaults.update(overrides)
    return SessionInput(**defaults)


async def _wait_status(wf, target, timeout=2.0):
    for _ in range(int(timeout / 0.02)):
        if wf.state.status == target:
            return True
        await asyncio.sleep(0.02)
    return False


# --- Tests ---

@pytest.mark.asyncio
async def test_e2e_happy_path():
    patches, destroy_mock = _make_patches()
    try:
        wf = HydraSessionWorkflow()
        task = asyncio.create_task(wf.run(make_input()))

        assert await _wait_status(wf, "ci_monitoring")
        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="success",
        )))
        assert await _wait_status(wf, "pr_review")
        await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        await asyncio.wait_for(task, timeout=5.0)
        assert wf.state.status == "completed"
        assert wf.state.pr_url == "https://github.com/test/repo/pull/1"
        destroy_mock.assert_called_once()
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_e2e_ci_fail_autofix():
    patches, _ = _make_patches()
    try:
        wf = HydraSessionWorkflow()
        task = asyncio.create_task(wf.run(make_input()))
        assert await _wait_status(wf, "ci_monitoring")

        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="failure",
        )))
        assert await _wait_status(wf, "ci_monitoring", timeout=3.0)

        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="success",
        )))
        assert await _wait_status(wf, "pr_review")
        await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        await asyncio.wait_for(task, timeout=5.0)
        assert wf.state.status == "completed"
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_e2e_needs_human():
    call_count = 0

    async def mock_run_tests(params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _test_result()
        elif call_count == 2:
            return _test_result(passed=3, failed=2, failures=[
                TestFailure(name="test_1", error="assert False"),
            ])
        else:
            return _test_result()

    patches, _ = _make_patches(test_results_fn=AsyncMock(side_effect=mock_run_tests))
    try:
        wf = HydraSessionWorkflow()
        task = asyncio.create_task(wf.run(make_input()))
        assert await _wait_status(wf, "ci_monitoring")

        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="failure",
            test_results=TestResultPayload(
                passed=3, failed=2, skipped=0, total=5,
                failures=[TestFailure(name="test_1", error="assert False")],
            ),
        )))

        assert await _wait_status(wf, "needs_human", timeout=3.0)
        await wf.signal_human_assist(HumanAssistData(guidance="Try using the mock_auth fixture"))
        assert await _wait_status(wf, "ci_monitoring", timeout=3.0)

        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="success",
        )))
        assert await _wait_status(wf, "pr_review")
        await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        await asyncio.wait_for(task, timeout=5.0)
        assert wf.state.status == "completed"
        assert len(wf.state.human_assists) == 1
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_e2e_review_changes_requested():
    patches, _ = _make_patches()
    try:
        wf = HydraSessionWorkflow()
        task = asyncio.create_task(wf.run(make_input()))
        assert await _wait_status(wf, "ci_monitoring")

        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="success",
        )))
        assert await _wait_status(wf, "pr_review")

        await wf.signal_review_feedback(ReviewFeedbackData(
            action="changes_requested",
            comments=[{"body": "Use constants instead of magic numbers"}],
        ))
        assert await _wait_status(wf, "ci_monitoring", timeout=3.0)

        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="success",
        )))
        assert await _wait_status(wf, "pr_review")
        await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        await asyncio.wait_for(task, timeout=5.0)
        assert wf.state.status == "completed"
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_e2e_cancel():
    patches, _ = _make_patches()
    try:
        wf = HydraSessionWorkflow()
        task = asyncio.create_task(wf.run(make_input()))
        assert await _wait_status(wf, "ci_monitoring")

        await wf.signal_cancel()
        await asyncio.wait_for(task, timeout=5.0)
        assert wf.state.status == "cancelled"
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_e2e_max_iterations():
    patches, _ = _make_patches()
    try:
        wf = HydraSessionWorkflow()
        task = asyncio.create_task(wf.run(make_input(max_iterations=1)))
        assert await _wait_status(wf, "ci_monitoring")

        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="failure",
        )))

        await asyncio.wait_for(task, timeout=5.0)
        assert wf.state.status == "failed"
        assert wf.state.error_summary == "max_iterations"
    finally:
        _stop(patches)


@pytest.mark.asyncio
async def test_e2e_with_issue_context():
    patches, _ = _make_patches()
    try:
        wf = HydraSessionWorkflow()
        task = asyncio.create_task(wf.run(make_input(
            issue_number=42, issue_type="bug",
        )))
        assert await _wait_status(wf, "ci_monitoring")

        assert wf.state.issue_number == 42
        assert wf.state.issue_type == "bug"

        await wf.signal_ci_result(CISignalData(payload=CIResultPayload(
            check_name="tests", status="completed", conclusion="success",
        )))
        assert await _wait_status(wf, "pr_review")
        await wf.signal_review_feedback(ReviewFeedbackData(action="approved", comments=[]))

        await asyncio.wait_for(task, timeout=5.0)
        assert wf.state.status == "completed"
    finally:
        _stop(patches)

"""Tests for TriageWorkflow — uses mocked activities."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.workflows.triage import (
    TriageWorkflow,
    TriageInput,
    TriageState,
    TriageAnalysis,
    _parse_analysis,
)


# --- Helpers ---

def _make_input(**overrides) -> TriageInput:
    defaults = dict(
        repo_url="https://github.com/test/repo",
        issue_number=42,
        owner="test",
        repo="repo",
        github_token="token",
        mistral_api_key="key",
    )
    defaults.update(overrides)
    return TriageInput(**defaults)


def _make_patches(issue_data=None, analysis=None):
    if issue_data is None:
        issue_data = {
            "title": "Login button broken",
            "body": "The login button doesn't work when clicked",
            "labels": [{"name": "bug"}],
        }
    if analysis is None:
        analysis = TriageAnalysis(
            eligibility="auto_assign", complexity="simple", issue_type="bug",
            relevant_files=["src/login.py"],
            suggested_approach="Fix click handler",
            reasoning="Simple bug",
        )

    patches_dict = {
        "app.workflows.triage.triage_fetch_issue": AsyncMock(return_value=issue_data),
        "app.workflows.triage.triage_run_agent": AsyncMock(return_value=analysis),
        "app.workflows.triage.triage_label_issue": AsyncMock(return_value=None),
        "app.workflows.triage.triage_comment_issue": AsyncMock(return_value=None),
    }
    started = [patch(k, v) for k, v in patches_dict.items()]
    for p in started:
        p.start()
    return started, patches_dict


def _stop(patches):
    for p in patches:
        p.stop()


# --- Tests ---

async def test_simple_bug_auto_assign():
    """Simple bug -> analyzed -> auto_assign."""
    patches, mocks = _make_patches()
    wf_patch = patch("app.workflows.triage.workflow.execute_workflow", new_callable=AsyncMock)
    uuid_patch = patch("app.workflows.triage.uuid.uuid4", return_value="test-session-uuid")
    registry_patch = patch("app.workflows.triage.workflow_registry.start_workflow", new_callable=AsyncMock)
    mock_exec_wf = wf_patch.start()
    mock_uuid = uuid_patch.start()
    mock_registry = registry_patch.start()

    try:
        wf = TriageWorkflow()
        await wf.run(_make_input())

        assert wf.state.status == "triaged"
        assert wf.state.eligibility == "auto_assign"
        assert wf.state.complexity == "simple"
        assert wf.state.issue_type == "bug"
        assert wf.state.issue_title == "Login button broken"
        assert wf.state.session_id == "test-session-uuid"

        mocks["app.workflows.triage.triage_fetch_issue"].assert_called_once()
        mocks["app.workflows.triage.triage_run_agent"].assert_called_once()
        mocks["app.workflows.triage.triage_label_issue"].assert_called_once()
        mocks["app.workflows.triage.triage_comment_issue"].assert_called_once()
    finally:
        _stop(patches)
        wf_patch.stop()
        uuid_patch.stop()
        registry_patch.stop()


async def test_complex_not_eligible():
    """Complex issue -> not_eligible."""
    patches, _ = _make_patches(
        issue_data={
            "title": "Redesign authentication system",
            "body": "We need to migrate from JWT to OAuth2",
            "labels": [{"name": "feature"}, {"name": "breaking-change"}],
        },
        analysis=TriageAnalysis(
            eligibility="not_eligible", complexity="complex", issue_type="feature",
            relevant_files=["src/auth/", "src/middleware/"],
            suggested_approach="Requires architectural redesign",
            reasoning="Too complex for automated agent",
        ),
    )
    try:
        wf = TriageWorkflow()
        await wf.run(_make_input())

        assert wf.state.eligibility == "not_eligible"
        assert wf.state.complexity == "complex"
        assert wf.state.issue_type == "feature"
    finally:
        _stop(patches)


async def test_medium_needs_review():
    """Medium feature -> needs_review."""
    patches, _ = _make_patches(
        issue_data={
            "title": "Add dark mode toggle",
            "body": "Add a toggle for dark/light theme",
            "labels": [{"name": "enhancement"}],
        },
        analysis=TriageAnalysis(
            eligibility="needs_review", complexity="medium", issue_type="feature",
            relevant_files=["src/theme.py", "static/style.css"],
            suggested_approach="Add theme toggle and CSS variables",
            reasoning="Medium complexity, needs team approval",
        ),
    )
    try:
        wf = TriageWorkflow()
        await wf.run(_make_input())

        assert wf.state.eligibility == "needs_review"
        assert wf.state.session_id is None
    finally:
        _stop(patches)


async def test_triage_query():
    """Verify triage query returns correct state."""
    wf = TriageWorkflow()
    assert wf.state.status == "analyzing"


async def test_triage_labels():
    """Verify label computation."""
    wf = TriageWorkflow()
    wf.state.issue_type = "bug"
    wf.state.complexity = "simple"

    wf.state.eligibility = "auto_assign"
    labels = wf._compute_labels()
    assert "hydra-eligible" in labels

    wf.state.eligibility = "needs_review"
    labels = wf._compute_labels()
    assert "needs-triage" in labels

    wf.state.eligibility = "not_eligible"
    labels = wf._compute_labels()
    assert "not-automatable" in labels


async def test_parse_analysis():
    """Verify JSON parsing from agent response."""
    content = '''Here is my analysis:
```json
{"issue_type": "bug", "complexity": "simple", "eligibility": "auto_assign",
 "relevant_files": ["a.py"], "suggested_approach": "Fix it", "reasoning": "Easy"}
```'''
    analysis = _parse_analysis(content)
    assert analysis.eligibility == "auto_assign"
    assert analysis.relevant_files == ["a.py"]

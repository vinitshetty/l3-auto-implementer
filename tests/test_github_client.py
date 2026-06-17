"""Tests for GitHubClient — uses httpx mock transport."""

import json
import httpx
import pytest

from app.integrations.github_client import GitHubClient


def _mock_response(data, status_code=200):
    return httpx.Response(status_code=status_code, json=data)


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: dict[str, dict | list] | None = None):
        self.responses = responses or {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url_str = str(request.url)
        path = request.url.path
        # Match longest pattern first to avoid prefix conflicts
        matches = [(p, d) for p, d in self.responses.items() if p in url_str]
        if matches:
            # Sort by pattern length descending — most specific wins
            matches.sort(key=lambda x: len(x[0]), reverse=True)
            return httpx.Response(200, json=matches[0][1])
        return httpx.Response(404, json={"message": "Not found"})


@pytest.fixture
def mock_transport():
    return MockTransport({
        "/repos/test/repo/issues/42": {
            "title": "Bug report", "body": "Details", "state": "open",
            "html_url": "https://github.com/test/repo/issues/42",
            "labels": [{"name": "bug"}],
        },
        "/repos/test/repo/contents/": [
            {"name": "src", "path": "src", "type": "dir"},
            {"name": "README.md", "path": "README.md", "type": "file"},
        ],
        "/repos/test/repo/contents/src/main.py": "print('hello')",
        "/repos/test/repo/commits": [
            {"sha": "abc1234567", "commit": {"message": "fix: login bug"}},
        ],
        "/search/code": {"items": [{"path": "src/auth.py", "score": 10.5}]},
        "/search/issues": {"items": [{"number": 10, "title": "Similar bug", "state": "closed"}]},
        "/repos/test/repo/issues/42/labels": [{"name": "bug"}],
        "/repos/test/repo/issues/42/comments": {"id": 1},
        "/repos/test/repo/pulls": {
            "html_url": "https://github.com/test/repo/pull/1", "number": 1,
        },
        "/repos/test/repo/commits/abc123/check-runs": {
            "check_runs": [
                {"name": "test", "status": "completed", "conclusion": "success", "details_url": ""},
            ],
        },
    })


@pytest.fixture
def client(mock_transport):
    http = httpx.AsyncClient(transport=mock_transport, base_url="https://api.github.com")
    return GitHubClient(token="test-token", http_client=http)


async def test_get_issue(client):
    result = await client.get_issue("test", "repo", 42)
    assert result["title"] == "Bug report"
    assert result["state"] == "open"


async def test_get_repo_tree(client):
    result = await client.get_repo_tree("test", "repo")
    assert len(result) == 2
    assert result[0]["name"] == "src"


async def test_search_code(client):
    result = await client.search_code("test", "repo", "login")
    assert len(result) == 1
    assert result[0]["path"] == "src/auth.py"


async def test_get_recent_commits(client):
    result = await client.get_recent_commits("test", "repo")
    assert len(result) == 1
    assert "login" in result[0]["message"]


async def test_search_issues(client):
    result = await client.search_issues("test", "repo", "bug")
    assert len(result) == 1
    assert result[0]["number"] == 10


async def test_add_labels(client):
    await client.add_labels("test", "repo", 42, ["bug", "priority"])
    # No exception means success


async def test_create_comment(client):
    await client.create_comment("test", "repo", 42, "Test comment")


async def test_create_pull_request(client):
    result = await client.create_pull_request(
        "test", "repo", head="feature", base="main",
        title="Test PR", body="Description",
    )
    assert result["pr_url"] == "https://github.com/test/repo/pull/1"
    assert result["pr_number"] == 1


async def test_get_check_runs(client):
    result = await client.get_check_runs("test", "repo", "abc123")
    assert len(result) == 1
    assert result[0]["conclusion"] == "success"

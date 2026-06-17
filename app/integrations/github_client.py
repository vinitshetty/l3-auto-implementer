"""GitHub REST API client with optional trace instrumentation."""

import httpx

from app.models import Span
from app.observability import Tracer


class GitHubClient:
    """Async GitHub API client. Every HTTP call can be traced."""

    def __init__(self, token: str, http_client: httpx.AsyncClient | None = None):
        self.token = token
        self._client = http_client

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=30.0,
            )
        return self._client

    async def _request(
        self, method: str, url: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
        **kwargs,
    ) -> httpx.Response:
        client = self._get_client()

        async def _do():
            return await client.request(method, url, **kwargs)

        if tracer and parent_span:
            async with tracer.span("github_api", kind="http", parent=parent_span,
                                   url=url, method=method) as s:
                resp = await _do()
                s.span_metadata = {
                    "url": url, "method": method,
                    "status_code": resp.status_code,
                    "response_bytes": len(resp.content),
                }
                return resp
        return await _do()

    async def get_issue(
        self, owner: str, repo: str, issue_number: int,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> dict:
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/issues/{issue_number}",
            tracer=tracer, parent_span=parent_span,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title", ""),
            "body": data.get("body", ""),
            "labels": data.get("labels", []),
            "state": data.get("state", ""),
            "url": data.get("html_url", ""),
        }

    async def get_repo_tree(
        self, owner: str, repo: str, path: str = "",
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> list[dict]:
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}",
            tracer=tracer, parent_span=parent_span,
        )
        resp.raise_for_status()
        items = resp.json()
        if isinstance(items, list):
            return [{"name": i["name"], "path": i["path"], "type": i["type"]} for i in items]
        return [{"name": items.get("name", ""), "path": path, "type": "file"}]

    async def get_file_content(
        self, owner: str, repo: str, path: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> str:
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}",
            tracer=tracer, parent_span=parent_span,
            headers={"Accept": "application/vnd.github.v3.raw"},
        )
        resp.raise_for_status()
        return resp.text

    async def search_code(
        self, owner: str, repo: str, query: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> list[dict]:
        resp = await self._request(
            "GET", "/search/code",
            tracer=tracer, parent_span=parent_span,
            params={"q": f"{query} repo:{owner}/{repo}"},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return [{"path": i["path"], "score": i.get("score", 0)} for i in items[:10]]

    async def get_recent_commits(
        self, owner: str, repo: str, path: str = "", limit: int = 10,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> list[dict]:
        params = {"per_page": limit}
        if path:
            params["path"] = path
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/commits",
            tracer=tracer, parent_span=parent_span,
            params=params,
        )
        resp.raise_for_status()
        return [
            {"sha": c["sha"][:7], "message": c["commit"]["message"].splitlines()[0]}
            for c in resp.json()
        ]

    async def search_issues(
        self, owner: str, repo: str, query: str, state: str = "closed",
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> list[dict]:
        resp = await self._request(
            "GET", "/search/issues",
            tracer=tracer, parent_span=parent_span,
            params={"q": f"{query} repo:{owner}/{repo} is:{state}"},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return [{"number": i["number"], "title": i["title"], "state": i["state"]} for i in items[:5]]

    async def add_labels(
        self, owner: str, repo: str, issue_number: int, labels: list[str],
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ):
        await self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
            tracer=tracer, parent_span=parent_span,
            json={"labels": labels},
        )

    async def create_comment(
        self, owner: str, repo: str, issue_number: int, body: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ):
        await self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            tracer=tracer, parent_span=parent_span,
            json={"body": body},
        )

    async def create_pull_request(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> dict:
        resp = await self._request(
            "POST", f"/repos/{owner}/{repo}/pulls",
            tracer=tracer, parent_span=parent_span,
            json={"head": head, "base": base, "title": title, "body": body},
        )
        if resp.status_code == 422:
            import logging
            error_body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
            logging.getLogger(__name__).warning("PR creation 422: %s", error_body)
            # Check if "No commits between" — means branch has no diff from base
            error_messages = [
                e.get("message", "") for e in (error_body.get("errors", []) if isinstance(error_body, dict) else [])
            ]
            if any("No commits between" in msg for msg in error_messages):
                return {"pr_url": "", "pr_number": 0, "error": "no_commits"}
            # PR may already exist for this branch — try to find it
            existing = await self._request(
                "GET", f"/repos/{owner}/{repo}/pulls",
                tracer=tracer, parent_span=parent_span,
                params={"head": f"{owner}:{head}", "state": "open"},
            )
            prs = existing.json() if existing.status_code == 200 else []
            if prs:
                return {"pr_url": prs[0].get("html_url", ""), "pr_number": prs[0].get("number", 0)}
            # Not a duplicate — raise with details
            resp.raise_for_status()
        resp.raise_for_status()
        data = resp.json()
        return {"pr_url": data.get("html_url", ""), "pr_number": data.get("number", 0)}

    async def get_check_runs(
        self, owner: str, repo: str, ref: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> list[dict]:
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/commits/{ref}/check-runs",
            tracer=tracer, parent_span=parent_span,
        )
        resp.raise_for_status()
        runs = resp.json().get("check_runs", [])
        return [
            {
                "name": r.get("name", ""),
                "status": r.get("status", ""),
                "conclusion": r.get("conclusion"),
                "details_url": r.get("details_url", ""),
            }
            for r in runs
        ]

    async def get_pr_status(
        self, owner: str, repo: str, pr_number: int,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> dict:
        resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/{pr_number}",
            tracer=tracer, parent_span=parent_span,
        )
        resp.raise_for_status()
        data = resp.json()

        # Fetch reviews
        reviews_resp = await self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            tracer=tracer, parent_span=parent_span,
        )
        reviews = reviews_resp.json() if reviews_resp.status_code == 200 else []

        return {
            "state": data.get("state", ""),
            "merged": data.get("merged", False),
            "mergeable": data.get("mergeable"),
            "review_comments": [
                {"author": r.get("user", {}).get("login", ""), "body": r.get("body", "")}
                for r in reviews
                if r.get("state") in ("CHANGES_REQUESTED", "COMMENTED")
            ],
        }

    async def close(self):
        if self._client:
            await self._client.aclose()

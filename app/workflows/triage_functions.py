"""Function calling tools for the triage agent.
Each function calls GitHub API via httpx to analyze a repo."""

import httpx

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_repo_file_tree",
            "description": "List files and directories in the repo to understand structure",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Subdirectory path (empty for root)"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the repo to understand code",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a pattern across the codebase",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search string or regex"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_commits",
            "description": "Get recent commits to understand recent changes",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional file path filter"},
                    "limit": {"type": "integer", "description": "Number of commits", "default": 10},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_similar_issues",
            "description": "Search closed issues for similar problems",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms"}
                },
                "required": ["query"],
            },
        },
    },
]


class TriageTools:
    """Executes triage function calls against GitHub API."""

    def __init__(self, owner: str, repo: str, token: str, http_client: httpx.AsyncClient | None = None):
        self.owner = owner
        self.repo = repo
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
            )
        return self._client

    async def execute(self, function_name: str, arguments: dict) -> str:
        dispatch = {
            "get_repo_file_tree": self.get_repo_file_tree,
            "read_file": self.read_file,
            "search_code": self.search_code,
            "get_recent_commits": self.get_recent_commits,
            "find_similar_issues": self.find_similar_issues,
        }
        fn = dispatch.get(function_name)
        if not fn:
            return f"Unknown function: {function_name}"
        return await fn(**arguments)

    async def get_repo_file_tree(self, path: str = "") -> str:
        client = self._get_client()
        url = f"/repos/{self.owner}/{self.repo}/contents/{path}"
        resp = await client.get(url)
        if resp.status_code != 200:
            return f"Error: {resp.status_code}"
        items = resp.json()
        if isinstance(items, list):
            return "\n".join(f"{'[dir]' if i['type'] == 'dir' else '[file]'} {i['path']}" for i in items)
        return str(items.get("name", ""))

    async def read_file(self, path: str) -> str:
        client = self._get_client()
        url = f"/repos/{self.owner}/{self.repo}/contents/{path}"
        resp = await client.get(url, headers={"Accept": "application/vnd.github.v3.raw"})
        if resp.status_code != 200:
            return f"Error: {resp.status_code}"
        return resp.text[:5000]

    async def search_code(self, query: str) -> str:
        client = self._get_client()
        url = f"/search/code?q={query}+repo:{self.owner}/{self.repo}"
        resp = await client.get(url)
        if resp.status_code != 200:
            return f"Error: {resp.status_code}"
        data = resp.json()
        items = data.get("items", [])[:10]
        return "\n".join(f"{i['path']} (score: {i.get('score', 0):.1f})" for i in items)

    async def get_recent_commits(self, path: str = "", limit: int = 10) -> str:
        client = self._get_client()
        params = {"per_page": limit}
        if path:
            params["path"] = path
        url = f"/repos/{self.owner}/{self.repo}/commits"
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return f"Error: {resp.status_code}"
        commits = resp.json()
        return "\n".join(
            f"{c['sha'][:7]} {c['commit']['message'].splitlines()[0]}" for c in commits
        )

    async def find_similar_issues(self, query: str) -> str:
        client = self._get_client()
        url = f"/search/issues?q={query}+repo:{self.owner}/{self.repo}+is:closed"
        resp = await client.get(url)
        if resp.status_code != 200:
            return f"Error: {resp.status_code}"
        data = resp.json()
        items = data.get("items", [])[:5]
        return "\n".join(f"#{i['number']} {i['title']} ({i['state']})" for i in items)

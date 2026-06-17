import json
from dataclasses import dataclass

from app.models import Span
from app.observability import Tracer
from app.schemas import TestResultPayload, TestFailure, VibeSummary


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class SandboxManager:
    """Docker container lifecycle for coding sandboxes.
    All methods accept tracer + parent_span for observability."""

    def __init__(self, docker_client=None):
        self._docker = docker_client

    def _get_docker(self):
        if self._docker is None:
            import docker
            self._docker = docker.from_env()
        return self._docker

    async def exec_in_container(
        self, container_id: str, cmd: str | list[str],
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> ExecResult:
        """Single instrumentation point for all container operations."""
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)

        async def _exec():
            client = self._get_docker()
            container = client.containers.get(container_id)
            # Pass lists directly to preserve argument boundaries (e.g. commit messages with spaces)
            exec_cmd = cmd if isinstance(cmd, list) else cmd_str
            result = container.exec_run(exec_cmd, demux=True)
            stdout = (result.output[0] or b"").decode("utf-8", errors="replace") if isinstance(result.output, tuple) else (result.output or b"").decode("utf-8", errors="replace")
            stderr = (result.output[1] or b"").decode("utf-8", errors="replace") if isinstance(result.output, tuple) else ""
            return ExecResult(
                exit_code=result.exit_code,
                stdout=stdout,
                stderr=stderr,
            )

        if tracer and parent_span:
            async with tracer.span("container_exec", kind="container_op", parent=parent_span,
                                   cmd=cmd_str) as s:
                result = await _exec()
                s.span_metadata = {
                    "cmd": cmd_str,
                    "exit_code": result.exit_code,
                    "stdout_lines": len(result.stdout.splitlines()),
                    "stderr": result.stderr[:500] if result.stderr else None,
                }
                return result
        else:
            return await _exec()

    async def create(
        self, session_id: str, repo_url: str, token: str, api_key: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> str:
        """Create and start a sandbox container. Returns container_id."""
        async def _create():
            client = self._get_docker()
            container = client.containers.run(
                "hydra-sandbox:latest",
                detach=True,
                environment={
                    "MISTRAL_API_KEY": api_key,
                    "GITHUB_TOKEN": token,
                    "REPO_URL": repo_url,
                    "SESSION_ID": session_id,
                },
                labels={"hydra-session": session_id},
                name=f"hydra-{session_id[:8]}",
                remove=False,
            )
            return container.id

        if tracer and parent_span:
            async with tracer.span("docker_create", kind="container_op", parent=parent_span) as s:
                container_id = await _create()
                return container_id
        else:
            return await _create()

    async def run_vibe(
        self, container_id: str, prompt: str, max_turns: int = 50, max_price: float = 5.0,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> VibeSummary:
        """Run Vibe CLI in container and parse NDJSON output into trace spans."""
        # Escape quotes in prompt for shell
        safe_prompt = prompt.replace('"', '\\"')
        cmd = f'vibe -p "{safe_prompt}" --workdir /workspace --max-turns {max_turns} --max-price {max_price} --output streaming --trust'
        result = await self.exec_in_container(container_id, cmd, tracer, parent_span)

        turns = 0
        tool_calls = 0
        files_modified: list[str] = []
        tokens_used = 0

        has_tracer = tracer and parent_span
        vibe_span = None
        turn_span = None
        tc_span = None

        if has_tracer:
            vibe_span = await tracer.start_span("vibe_session", kind="agent", parent=parent_span)

        try:
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                role = event.get("role", "")
                etype = event.get("type", "")

                # Vibe streaming format: role-based conversation messages
                if role == "assistant":
                    turns += 1
                    if has_tracer:
                        if turn_span:
                            await tracer.end_span(turn_span)
                        turn_span = await tracer.start_span(
                            f"agent_turn_{turns}", kind="agent_turn", parent=vibe_span
                        )
                    # Track reasoning/thinking
                    reasoning = event.get("reasoning_content", "")
                    if reasoning and has_tracer and turn_span:
                        await tracer.event(turn_span, "agent_thinking", {
                            "content": str(reasoning)[:500]
                        })
                    # Track tool calls in this turn
                    for tc in (event.get("tool_calls") or []):
                        tool_calls += 1
                        func = tc.get("function", {})
                        tool_name = func.get("name", "")
                        if has_tracer:
                            tc_span = await tracer.start_span(
                                tool_name or "tool", kind="tool_call",
                                parent=turn_span or vibe_span,
                            )
                        # Detect file modifications from tool call arguments
                        if tool_name in ("write_file", "search_replace", "file_write", "file_edit"):
                            try:
                                args = json.loads(func.get("arguments", "{}"))
                                path = args.get("path", "") or args.get("file_path", "")
                                if path and path not in files_modified:
                                    files_modified.append(path)
                            except (json.JSONDecodeError, TypeError):
                                pass

                elif role == "tool":
                    if has_tracer and tc_span:
                        await tracer.end_span(tc_span)
                        tc_span = None

                # Legacy event format support
                elif etype == "turn_start":
                    turns += 1
                elif etype == "tool_call":
                    tool_calls += 1
                elif etype == "tool_result":
                    tool_name = event.get("tool", "")
                    if tool_name in ("file_write", "file_edit"):
                        path = event.get("path", "")
                        if path and path not in files_modified:
                            files_modified.append(path)
                elif etype == "usage":
                    tokens_used += event.get("total_tokens", 0)
                elif etype == "error":
                    if has_tracer and vibe_span:
                        await tracer.event(vibe_span, "agent_error", {
                            "error": event.get("message", "")
                        })

            if has_tracer and turn_span:
                await tracer.end_span(turn_span)
            if has_tracer and vibe_span:
                await tracer.event(vibe_span, "vibe_summary", {
                    "turns": turns, "tool_calls": tool_calls,
                    "files_modified": files_modified, "tokens_used": tokens_used,
                })
        finally:
            if has_tracer and vibe_span:
                await tracer.end_span(vibe_span)

        return VibeSummary(
            turns=turns, tool_calls=tool_calls,
            files_modified=files_modified, tokens_used=tokens_used,
        )

    async def run_tests(
        self, container_id: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> TestResultPayload:
        """Run pytest in container with JSON report and emit per-test events."""
        cmd = "pytest --json-report --json-report-file=/tmp/report.json -v"
        exec_result = await self.exec_in_container(container_id, cmd, tracer, parent_span)

        # Read the JSON report
        report_result = await self.exec_in_container(
            container_id, "cat /tmp/report.json", tracer, parent_span
        )

        total = passed = failed = skipped = 0
        failures: list[TestFailure] = []
        has_tracer = tracer and parent_span

        try:
            report = json.loads(report_result.stdout)
            summary = report.get("summary", {})
            total = summary.get("total", 0)
            passed = summary.get("passed", 0)
            failed = summary.get("failed", 0)
            skipped = summary.get("skipped", 0)

            test_span = None
            if has_tracer:
                test_span = await tracer.start_span("run_tests_suite", kind="container_op", parent=parent_span)

            for test in report.get("tests", []):
                outcome = test.get("outcome", "unknown")
                duration = int(test.get("duration", 0) * 1000)
                error_msg = None
                tb = None
                if outcome == "failed":
                    longrepr = test.get("longrepr", "")
                    error_msg = str(longrepr)[:500]
                    tb = str(longrepr)
                    failures.append(TestFailure(
                        name=test.get("nodeid", ""),
                        error=error_msg,
                        traceback=tb,
                    ))
                if has_tracer and test_span:
                    await tracer.event(test_span, "test_case", {
                        "name": test.get("nodeid", ""),
                        "outcome": outcome,
                        "duration_ms": duration,
                        "error": error_msg,
                    })

            if has_tracer and test_span:
                await tracer.event(test_span, "test_summary", {
                    "total": total, "passed": passed,
                    "failed": failed, "skipped": skipped,
                })
                await tracer.end_span(test_span)

        except (json.JSONDecodeError, KeyError):
            failed = 1
            total = 1

        return TestResultPayload(
            total=total, passed=passed, failed=failed,
            skipped=skipped, failures=failures,
        )

    async def run_git(
        self, container_id: str, *args: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> str:
        cmd = ["git"] + list(args)
        result = await self.exec_in_container(container_id, cmd, tracer, parent_span)
        return result.stdout

    async def get_diff_stats(
        self, container_id: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ) -> dict:
        import re

        # Stage all changes first so new files are included in diff
        await self.exec_in_container(
            container_id, "git -C /workspace add -A", tracer, parent_span
        )
        result = await self.exec_in_container(
            container_id, "git -C /workspace diff --cached --stat HEAD", tracer, parent_span
        )
        lines = result.stdout.strip().splitlines()
        files_changed = 0
        lines_added = 0
        lines_removed = 0
        changed_files: list[str] = []

        for line in lines[:-1]:  # last line is summary
            parts = line.strip().split("|")
            if len(parts) >= 1:
                changed_files.append(parts[0].strip())
                files_changed += 1

        if lines:
            summary = lines[-1]
            add_match = re.search(r"(\d+) insertion", summary)
            del_match = re.search(r"(\d+) deletion", summary)
            if add_match:
                lines_added = int(add_match.group(1))
            if del_match:
                lines_removed = int(del_match.group(1))

        return {
            "files_changed": files_changed,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "changed_files": changed_files,
        }

    async def destroy(
        self, container_id: str,
        tracer: Tracer | None = None, parent_span: Span | None = None,
    ):
        async def _destroy():
            client = self._get_docker()
            container = client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)

        if tracer and parent_span:
            async with tracer.span("container_remove", kind="container_op", parent=parent_span):
                await _destroy()
        else:
            await _destroy()

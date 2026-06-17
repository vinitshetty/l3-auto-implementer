import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.event_bus import EventBus
from app.models import SessionEvent, SessionMetrics, Span


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tracer:
    """Every workflow run = 1 trace. Every activity/phase/wait = 1 span.
    Spans nest. Events attach to spans. Everything flows to UI via event bus."""

    def __init__(self, session_id: str, trace_id: str, event_bus: EventBus, db: AsyncSession):
        self.session_id = session_id
        self.trace_id = trace_id
        self.event_bus = event_bus
        self.db = db

    @asynccontextmanager
    async def span(self, name: str, kind: str = "activity",
                   parent: Span | None = None, **metadata):
        s = Span(
            id=str(uuid.uuid4()),
            trace_id=self.trace_id,
            parent_span_id=parent.id if parent else None,
            session_id=self.session_id,
            name=name,
            kind=kind,
            status="running",
            started_at=_utcnow(),
            span_metadata=metadata if metadata else None,
        )
        await self._persist_span(s)
        await self._publish_event("span_start", {
            "span_id": s.id, "name": name, "kind": kind, **(metadata or {})
        })
        try:
            yield s
            s.status = "completed"
        except Exception as e:
            s.status = "failed"
            s.error = str(e)
            raise
        finally:
            s.ended_at = _utcnow()
            s.duration_ms = int((s.ended_at - s.started_at).total_seconds() * 1000)
            await self._persist_span(s)
            await self._publish_event("span_end", {
                "span_id": s.id, "name": name, "status": s.status,
                "duration_ms": s.duration_ms, "error": s.error,
            })

    async def start_span(self, name: str, kind: str = "activity",
                         parent: Span | None = None, **metadata) -> Span:
        s = Span(
            id=str(uuid.uuid4()),
            trace_id=self.trace_id,
            parent_span_id=parent.id if parent else None,
            session_id=self.session_id,
            name=name,
            kind=kind,
            status="running",
            started_at=_utcnow(),
            span_metadata=metadata if metadata else None,
        )
        await self._persist_span(s)
        await self._publish_event("span_start", {
            "span_id": s.id, "name": name, "kind": kind, **(metadata or {})
        })
        return s

    async def end_span(self, span: Span, status: str = "completed", error: str | None = None):
        span.status = status
        span.error = error
        span.ended_at = _utcnow()
        span.duration_ms = int((span.ended_at - span.started_at).total_seconds() * 1000)
        await self._persist_span(span)
        await self._publish_event("span_end", {
            "span_id": span.id, "name": span.name, "status": span.status,
            "duration_ms": span.duration_ms, "error": span.error,
        })

    async def event(self, span: Span, event_type: str, payload: dict):
        evt = SessionEvent(
            id=str(uuid.uuid4()),
            session_id=self.session_id,
            span_id=span.id,
            trace_id=self.trace_id,
            event_type=event_type,
            payload=payload,
        )
        await self._persist_event(evt)
        await self._publish_event(event_type, {
            **payload, "span_id": span.id, "trace_id": self.trace_id,
        })

    async def compute_session_metrics(self) -> SessionMetrics:
        spans = await self._get_all_spans()
        events = await self._get_all_events()

        total_duration = 0
        coding_duration = 0
        testing_duration = 0
        ci_wait_duration = 0
        review_wait_duration = 0
        provision_duration = 0
        iterations = 0
        test_runs = 0
        tool_calls_count = 0

        for s in spans:
            dur = s.duration_ms or 0
            if s.name.startswith("run_vibe") and s.kind == "activity":
                coding_duration += dur
            elif s.name == "run_tests_suite" or (s.name == "run_tests" and s.kind == "activity"):
                testing_duration += dur
                test_runs += 1
            elif s.name == "wait_ci":
                ci_wait_duration += dur
            elif s.name == "wait_review":
                review_wait_duration += dur
            elif s.name == "provision_sandbox":
                provision_duration += dur
            elif s.name.startswith("coding_iteration") or s.name.startswith("ci_iteration"):
                iterations += 1
            if s.kind == "tool_call":
                tool_calls_count += 1

        if spans:
            starts = [s.started_at.replace(tzinfo=None) if s.started_at else None for s in spans]
            starts = [s for s in starts if s]
            if starts:
                first = min(starts)
                ended = [s.ended_at.replace(tzinfo=None) if s.ended_at else None for s in spans]
                ended = [e for e in ended if e]
                if ended:
                    total_duration = int((max(ended) - first).total_seconds() * 1000)

        total_tests = 0
        total_tokens = 0
        vibe_turns = 0
        files_modified: set[str] = set()

        for e in events:
            if e.event_type == "test_summary":
                total_tests += e.payload.get("total", 0)
            elif e.event_type == "vibe_summary":
                total_tokens += e.payload.get("tokens_used", 0)
                vibe_turns += e.payload.get("turns", 0)
                files_modified.update(e.payload.get("files_modified", []))
            elif e.event_type == "llm_call":
                total_tokens += e.payload.get("tokens_used", 0)

        metrics = SessionMetrics(
            session_id=self.session_id,
            total_duration_ms=total_duration,
            coding_duration_ms=coding_duration,
            testing_duration_ms=testing_duration,
            ci_wait_duration_ms=ci_wait_duration,
            review_wait_duration_ms=review_wait_duration,
            provision_duration_ms=provision_duration,
            iterations=iterations,
            test_runs=test_runs,
            total_tests_executed=total_tests,
            total_tokens_used=total_tokens,
            vibe_turns=vibe_turns,
            tool_calls_count=tool_calls_count,
            files_modified_count=len(files_modified),
            outcome="pending",
        )
        self.db.add(metrics)
        await self.db.commit()
        return metrics

    async def _persist_span(self, span: Span):
        from sqlalchemy import inspect as sa_inspect
        insp = sa_inspect(span, raiseerr=False)
        if insp is None or insp.pending or insp.transient:
            self.db.add(span)
        await self.db.commit()

    async def _persist_event(self, event: SessionEvent):
        self.db.add(event)
        await self.db.commit()

    async def _publish_event(self, event_type: str, payload: dict):
        await self.event_bus.publish(self.session_id, {
            "event_type": event_type,
            **payload,
        })

    async def _get_all_spans(self) -> list[Span]:
        result = await self.db.execute(
            select(Span).where(Span.session_id == self.session_id)
        )
        return list(result.scalars().all())

    async def _get_all_events(self) -> list[SessionEvent]:
        result = await self.db.execute(
            select(SessionEvent).where(SessionEvent.session_id == self.session_id)
        )
        return list(result.scalars().all())

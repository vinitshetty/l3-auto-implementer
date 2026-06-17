"""In-process registry for running workflow instances.

When workflows run locally (outside Temporal), we need to track instances
so API endpoints can send signals to them."""

import asyncio
import logging
from datetime import timedelta

from mistralai.workflows import get_workflow_definition

logger = logging.getLogger(__name__)

# Maps execution_id -> workflow instance
_running_workflows: dict[str, object] = {}
# Maps execution_id -> asyncio.Task
_running_tasks: dict[str, asyncio.Task] = {}


def get_workflow(execution_id: str):
    return _running_workflows.get(execution_id)


def list_workflows() -> dict[str, object]:
    return dict(_running_workflows)


async def start_workflow(workflow_cls, params, execution_id: str):
    """Start a workflow as a background task, tracking the instance for signals."""
    wf_def = get_workflow_definition(workflow_cls)
    if not wf_def:
        raise ValueError(f"{workflow_cls} is not a registered workflow")

    instance = workflow_cls()
    _running_workflows[execution_id] = instance

    async def _run():
        try:
            # Call the entrypoint directly (same as execute_workflow does outside Temporal)
            await instance.run(params)
        except Exception:
            logger.exception("Workflow %s failed", execution_id)
        finally:
            _running_workflows.pop(execution_id, None)
            _running_tasks.pop(execution_id, None)

    task = asyncio.create_task(_run())
    _running_tasks[execution_id] = task
    return instance

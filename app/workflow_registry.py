"""Workflow registry — uses Mistral's hosted Temporal server.

Your laptop runs as the worker node via run_worker().
The Mistral client API is used to start workflows, send signals, and query state."""

import asyncio
import logging

from mistralai.client import Mistral

from app.config import settings

logger = logging.getLogger(__name__)

_client: Mistral | None = None


def get_client() -> Mistral:
    global _client
    if _client is None:
        _client = Mistral(api_key=settings.mistral_api_key)
    return _client


async def start_workflow(workflow_cls, params, execution_id: str):
    """Start a workflow on Mistral's Temporal server."""
    from mistralai.workflows import get_workflow_definition

    wf_def = get_workflow_definition(workflow_cls)
    if not wf_def:
        raise ValueError(f"{workflow_cls} is not a registered workflow")

    client = get_client()
    result = await asyncio.to_thread(
        client.workflows.execute_workflow,
        workflow_identifier=wf_def.name,
        execution_id=execution_id,
        input=params.model_dump(),
        wait_for_result=False,
    )
    logger.info("Workflow %s started: execution_id=%s", wf_def.name, execution_id)
    return result


async def signal_workflow(execution_id: str, signal_name: str, data: dict | None = None):
    """Send a signal to a running workflow on Mistral's Temporal server."""
    client = get_client()
    result = await asyncio.to_thread(
        client.workflows.executions.signal_workflow_execution,
        execution_id=execution_id,
        name=signal_name,
        input=data,
    )
    logger.info("Signal '%s' sent to %s", signal_name, execution_id)
    return result


async def query_workflow(execution_id: str, query_name: str, data: dict | None = None):
    """Query a running workflow on Mistral's Temporal server."""
    client = get_client()
    result = await asyncio.to_thread(
        client.workflows.executions.query_workflow_execution,
        execution_id=execution_id,
        name=query_name,
        input=data,
    )
    return result


async def cancel_workflow(execution_id: str):
    """Cancel a running workflow on Mistral's Temporal server."""
    client = get_client()
    await asyncio.to_thread(
        client.workflows.executions.cancel_workflow_execution,
        execution_id=execution_id,
    )
    logger.info("Workflow %s cancelled", execution_id)


async def get_workflow_execution(execution_id: str):
    """Get workflow execution status from Mistral's Temporal server."""
    client = get_client()
    return await asyncio.to_thread(
        client.workflows.executions.get_workflow_execution,
        execution_id=execution_id,
    )

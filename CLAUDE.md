# Hydra Demo — Async Coding Agent

## What This Project Is

An autonomous coding agent that takes a GitHub issue, writes code to fix it, creates a PR, monitors CI, handles PR review feedback, and iterates until done. Built with FastAPI, Mistral Vibe CLI, Mistral Workflows SDK, Docker sandboxes, and SQLite.

## Quick Start

### Prerequisites
- Python 3.12+
- Docker running (for sandbox containers)
- `.env` file with required keys (see below)
- `hydra-sandbox` Docker image built

### Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker build -f Dockerfile.sandbox -t hydra-sandbox:latest .
```

### Environment Variables (.env)
```
MISTRAL_API_KEY=...        # Required — powers Vibe CLI coding + Agents API triage
GITHUB_TOKEN=...           # Required — repo access, PR creation, issue reading
GITHUB_WEBHOOK_SECRET=...  # Optional — for webhook signature verification
SLACK_BOT_TOKEN=...        # Optional — Slack integration
SLACK_SIGNING_SECRET=...   # Optional — Slack integration
DATABASE_URL=sqlite+aiosqlite:///./hydra.db
MAX_CI_ITERATIONS=3
VIBE_MAX_TURNS=50
VIBE_MAX_PRICE=5.0
```

### Run the Server
```bash
uvicorn app.main:app --reload --port 8000
```
The UI is at http://localhost:8000/static/index.html

### Run GitHub Poller (instead of webhooks)
```bash
python scripts/poll_github.py --interval 15
```
This polls GitHub for CI status and PR comments, sending signals to running workflows. Use this when you don't have webhooks configured.

### Run Tests
```bash
pytest                    # all 82 tests
pytest tests/test_workflow.py  # just workflow tests
pytest -x -v              # stop on first failure, verbose
```

## Architecture

### Two-Workflow Design

**Session Workflow** (`app/workflows/hydra_session.py`):
```
provision sandbox → fetch issue → code (Vibe CLI) → test → create PR
  → CI monitoring loop → PR review loop → complete/fail
```

**Triage Workflow** (`app/workflows/triage.py`):
```
GitHub issue opened → Mistral Agents API analyzes → auto-assign / needs-review / not-eligible
  → auto-assign triggers child Session Workflow
```

### Session State Machine
```
pending → running → ci_monitoring → pr_review → completed
                  → ci_monitoring → needs_human → running (resumed)
                  → pr_review → changes_requested → running (re-code) → ci_monitoring
                  → needs_human → cancelled
                  → running → failed
                  → ci_monitoring → failed (max_iterations)
```

### Key Signals (how external events reach workflows)
- `ci_result` — CI completed (via webhook or poll_github.py)
- `review_feedback` — PR review or comment (via webhook or poll_github.py)
- `human_assist` — human guidance from UI/Slack
- `cancel` — cancel from UI

### Project Layout
```
app/
  main.py                    # FastAPI app, lifespan, router mounting
  config.py                  # Settings from .env via pydantic-settings
  database.py                # SQLAlchemy async engine + session factory
  models.py                  # DB models (HydraSession, TriageResult, Span, etc.)
  schemas.py                 # Pydantic request/response schemas
  event_bus.py               # In-memory SSE event bus
  observability.py           # Tracer for span-based observability
  workflow_registry.py       # In-process workflow instance tracking
  api/
    sessions.py              # CRUD + signals: /api/sessions, /api/sessions/{id}/signal/*
    webhooks.py              # GitHub webhook handler: /api/webhooks/github
    events.py                # SSE stream: /api/sessions/{id}/events
  workflows/
    hydra_session.py         # Session workflow (the main coding loop)
    triage.py                # Triage workflow (issue analysis)
    activities.py            # All side-effect activities (fetch_issue, provision_sandbox, etc.)
    triage_functions.py      # Mistral function-calling tools for triage
  integrations/
    github_client.py         # Async GitHub REST API client
    slack_bot.py             # Slack Bolt integration
  sandbox/
    manager.py               # Docker container lifecycle + Vibe CLI execution
scripts/
  poll_github.py             # GitHub poller for CI/reviews (replaces webhooks)
static/                      # Vanilla HTML/JS UI (index, session detail, triage)
tests/                       # 11 test files, pytest-asyncio, in-memory SQLite
docs/plans/                  # Architecture docs and wave specs
Dockerfile.sandbox           # Sandbox image: Python 3.12 + git + pytest + mistral-vibe
```

## API Endpoints

### Sessions
- `POST /api/sessions` — Create session (starts workflow). Body: `{repo_url, task_description, issue_number?, issue_type?, max_iterations?}`
- `GET /api/sessions` — List all sessions
- `GET /api/sessions/{id}` — Get session (enriched with live workflow state)
- `GET /api/sessions/{id}/live` — Live workflow state (status, confidence, CI results, etc.)
- `POST /api/sessions/{id}/signal/ci?conclusion=success|failure` — Manual CI signal
- `POST /api/sessions/{id}/signal/review` — Manual review signal. Body: `{action, comments}`
- `POST /api/sessions/{id}/assist` — Human guidance. Body: `{guidance}`
- `POST /api/sessions/{id}/cancel` — Cancel workflow
- `POST /api/sessions/{id}/retry` — Retry failed/cancelled session

### Webhooks
- `POST /api/webhooks/github` — Receives `issues.opened`, `check_suite`, `check_run`, `pull_request_review`

### Triage
- `GET /api/triage` — List triage results
- `POST /api/triage/{id}/approve` — Approve and start session
- `POST /api/triage/{id}/reject` — Reject

### Observability
- `GET /api/sessions/{id}/trace` — Span tree
- `GET /api/sessions/{id}/metrics` — Session metrics
- `GET /api/metrics/summary` — Aggregate metrics
- `GET /api/sessions/{id}/events` — SSE event stream

## How the Sandbox Works

Each session gets a Docker container from `hydra-sandbox:latest`:
1. Container created with `MISTRAL_API_KEY` and `GITHUB_TOKEN` as env vars
2. Repo cloned into `/workspace`, new branch `hydra/{session_id[:8]}` created
3. **Vibe CLI** runs inside the container: `vibe -p "<prompt>" --workdir /workspace --trust --output streaming`
4. Tests run via `pytest --json-report`
5. Changes committed and pushed from inside the container
6. PR created via GitHub API
7. Container destroyed on completion/failure

## Key Implementation Details

### Vibe CLI Prompt
The `build_prompt()` function in `activities.py` prefixes all prompts with `"CHANGE TASK"` to force Vibe into edit mode (otherwise it may classify the task as investigate-only and make no changes).

### Workflow Registry
Since workflows run in-process (not on Temporal), `workflow_registry.py` tracks instances in a dict so API endpoints can send signals to them. Workflows are started as `asyncio.Task`s.

### wait_condition Fallback
`_wait_condition()` in `hydra_session.py` tries `workflow.wait_condition()` (Temporal SDK) first, falls back to polling (`asyncio.sleep(0.1)`) when running locally.

### Confidence Scoring
After coding, `generate_confidence_summary()` analyzes the diff for:
- Risk flags (sensitive files, large changes, CI/CD modifications)
- New dependencies
- Test coverage presence
- Produces a 0-100 confidence score

### Testing Patterns
- All tests use in-memory SQLite (`conftest.py`)
- Workflow tests mock `workflow.wait_condition` with polling
- All activities mocked with `AsyncMock`
- Access `wf.state` directly (SDK serializes Pydantic returns to dicts)
- E2E tests run without tracer (`db=None`) to avoid SQLite lifecycle issues

### Known Gotchas
- `Span.span_metadata` — Python attr renamed from `metadata` to avoid SQLAlchemy conflict; DB column is still `metadata`
- `SpanResponse.span_metadata` — Pydantic field matches the Python attr name
- `.env` must exist in project root (not committed to git)
- `hydra-sandbox` Docker image must be built before running sessions
- `poll_github.py` reads `GITHUB_TOKEN` from `.env` if not in environment
- Repos without CI will stay in `ci_monitoring` forever unless you send a manual signal: `POST /api/sessions/{id}/signal/ci?conclusion=success`

## Common Operations

### Fix an issue end-to-end
```bash
# 1. Start server
uvicorn app.main:app --reload --port 8000

# 2. Create a session
curl -X POST http://localhost:8000/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/owner/repo", "task_description": "Fix issue #1", "issue_number": 1, "issue_type": "bug"}'

# 3. Start poller (for repos without webhooks)
python scripts/poll_github.py --interval 15

# 4. For repos without CI, manually signal success after PR is created:
curl -X POST "http://localhost:8000/api/sessions/{session_id}/signal/ci?conclusion=success"
```

### Clean up orphaned Docker containers
```bash
docker ps -a --filter "label=hydra-session" --format "{{.ID}}" | xargs docker rm -f
```

"""Tests for repo profiler — unit tests for detection logic and profile generation."""

import pytest
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.repo_profiler import (
    _assemble_profile_text,
    _detect_ci_setup,
    _detect_entry_points,
    _infer_dir_purpose,
)
from app.sandbox.manager import ExecResult


# --- Pure function tests ---

def test_infer_dir_purpose():
    assert _infer_dir_purpose("src", "") == "source code"
    assert _infer_dir_purpose("tests", "") == "test suite"
    assert _infer_dir_purpose("docs", "") == "documentation"
    assert _infer_dir_purpose("api", "") == "API endpoints"
    assert _infer_dir_purpose("mymodule", "") == "project module"


def test_detect_entry_points():
    tree = "main.py\napp/main.py\nutils.py\nREADME.md\nserver.js"
    tech = {"languages": ["python", "javascript"]}
    entries = _detect_entry_points(tree, tech)
    assert "main.py" in entries
    assert "app/main.py" in entries
    assert "server.js" in entries
    assert "utils.py" not in entries
    assert "README.md" not in entries


def test_detect_ci_setup():
    tree = ".github/workflows/ci.yml\n.github/workflows/deploy.yml\nsrc/main.py"
    ci = _detect_ci_setup(tree, {})
    assert ci["provider"] == "github-actions"
    assert ".github/workflows/ci.yml" in ci["config_files"]


def test_detect_ci_setup_no_ci():
    tree = "src/main.py\nREADME.md"
    ci = _detect_ci_setup(tree, {})
    assert ci["provider"] == ""
    assert ci["config_files"] == []


def test_assemble_profile_text():
    text = _assemble_profile_text(
        file_tree="src/main.py\ntests/test_main.py",
        tech_stack={"languages": ["python"], "frameworks": ["FastAPI"], "build_tools": ["pip"], "test_framework": "pytest"},
        architecture_summary="A web API service.",
        module_map={"src": {"purpose": "source code"}, "tests": {"purpose": "test suite"}},
        entry_points=["src/main.py"],
        test_setup={"framework": "pytest", "test_dirs": ["tests"], "run_cmd": "pytest"},
        ci_setup={"provider": "github-actions", "config_files": [".github/workflows/ci.yml"]},
        conventions={"linters": ["ruff"], "naming": "snake_case"},
    )
    assert "# Repository Profile" in text
    assert "FastAPI" in text
    assert "pytest" in text
    assert "src/main.py" in text
    assert "snake_case" in text
    assert "github-actions" in text
    assert "A web API service." in text


def test_assemble_profile_text_empty():
    text = _assemble_profile_text(
        file_tree="",
        tech_stack={},
        architecture_summary="",
        module_map={},
        entry_points=[],
        test_setup={},
        ci_setup={},
        conventions={},
    )
    assert "# Repository Profile" in text


# --- Tests with mocked sandbox ---

@pytest.mark.asyncio
async def test_detect_tech_stack_python():
    from app.repo_profiler import _detect_tech_stack

    sandbox = AsyncMock()
    sandbox.exec_in_container = AsyncMock(return_value=ExecResult(
        exit_code=0,
        stdout='[tool.pytest]\n[project]\ndependencies = ["fastapi", "sqlalchemy"]',
        stderr="",
    ))

    tree = "pyproject.toml\napp/main.py\ntests/test_app.py"
    stack = await _detect_tech_stack(sandbox, "container-1", tree)

    assert "python" in stack["languages"]
    assert "FastAPI" in stack["frameworks"]


@pytest.mark.asyncio
async def test_detect_tech_stack_javascript():
    from app.repo_profiler import _detect_tech_stack

    sandbox = AsyncMock()
    sandbox.exec_in_container = AsyncMock(return_value=ExecResult(
        exit_code=0,
        stdout='{"dependencies": {"react": "^18.0.0", "next": "^13.0.0"}}',
        stderr="",
    ))

    tree = "package.json\ntsconfig.json\nsrc/index.ts\npages/index.tsx"
    stack = await _detect_tech_stack(sandbox, "container-1", tree)

    assert "javascript" in stack["languages"]
    assert "typescript" in stack["languages"]
    assert "React" in stack["frameworks"]
    assert "Next.js" in stack["frameworks"]


@pytest.mark.asyncio
async def test_build_module_map():
    from app.repo_profiler import _build_module_map

    sandbox = AsyncMock()

    # First call: list directories
    # Second call: count files in "src"
    # Third call: check markers in "src"
    # Fourth call: count files in "tests"
    # Fifth call: check markers in "tests"
    sandbox.exec_in_container = AsyncMock(side_effect=[
        ExecResult(exit_code=0, stdout="src\ntests\n", stderr=""),
        ExecResult(exit_code=0, stdout="15", stderr=""),
        ExecResult(exit_code=0, stdout="/workspace/src/__init__.py", stderr=""),
        ExecResult(exit_code=0, stdout="8", stderr=""),
        ExecResult(exit_code=0, stdout="", stderr=""),
    ])

    result = await _build_module_map(sandbox, "container-1")
    assert "src" in result
    assert result["src"]["purpose"] == "source code"
    assert "tests" in result
    assert result["tests"]["purpose"] == "test suite"


@pytest.mark.asyncio
async def test_read_key_files():
    from app.repo_profiler import _read_key_files

    sandbox = AsyncMock()
    sandbox.exec_in_container = AsyncMock(side_effect=lambda cid, cmd, **kw: ExecResult(
        exit_code=0 if "README.md" in cmd else 1,
        stdout="# My Project\nA cool project." if "README.md" in cmd else "",
        stderr="",
    ))

    result = await _read_key_files(sandbox, "container-1")
    assert "README.md" in result
    assert "My Project" in result["README.md"]


@pytest.mark.asyncio
async def test_get_or_create_profile_disabled():
    """When repo_profile_enabled=False, activity returns empty string."""
    from app.workflows.activities import get_or_create_repo_profile, GetOrCreateRepoProfileParams

    with patch("app.config.settings") as mock_settings:
        mock_settings.repo_profile_enabled = False
        result = await get_or_create_repo_profile(GetOrCreateRepoProfileParams(
            container_id="c1", repo_url="https://github.com/o/r", owner="o", repo="r",
        ))
        assert result == ""


# --- API tests ---

@pytest.mark.asyncio
async def test_list_repo_profiles(client, db_session):
    """GET /api/repo-profiles returns empty list initially."""
    resp = await client.get("/api/repo-profiles")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_repo_profile_not_found(client, db_session):
    resp = await client.get("/api/repo-profiles/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_repo_profile_not_found(client, db_session):
    resp = await client.delete("/api/repo-profiles/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_repo_profile_crud(db_engine):
    """Create a profile in DB and verify CRUD endpoints."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.database import get_db
    from app.models import RepoProfile

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    # Also patch app.database.async_session used by the API endpoints
    async def override_get_db():
        async with session_factory() as session:
            yield session

    # Patch both the FastAPI dependency and the module-level async_session
    app.dependency_overrides[get_db] = override_get_db

    with patch("app.api.repo_profiles.async_session", session_factory):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Insert a profile directly
            async with session_factory() as db:
                profile = RepoProfile(
                    repo_url="https://github.com/test/repo",
                    owner="test",
                    repo_name="repo",
                    head_sha="abc123",
                    profile_text="# Test Profile",
                    tech_stack={"languages": ["python"]},
                )
                db.add(profile)
                await db.commit()
                await db.refresh(profile)
                profile_id = profile.id

            # List
            resp = await client.get("/api/repo-profiles")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["repo_url"] == "https://github.com/test/repo"

            # Get
            resp = await client.get(f"/api/repo-profiles/{profile_id}")
            assert resp.status_code == 200
            assert resp.json()["profile_text"] == "# Test Profile"

            # Delete
            resp = await client.delete(f"/api/repo-profiles/{profile_id}")
            assert resp.status_code == 200
            assert resp.json()["status"] == "deleted"

            # Verify deleted
            resp = await client.get(f"/api/repo-profiles/{profile_id}")
            assert resp.status_code == 404

    app.dependency_overrides.clear()

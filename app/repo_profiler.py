"""Repo Profiler — generates a cached understanding of a repository.

Runs inside the sandbox container after clone to extract:
- File tree (pruned of noise dirs)
- Tech stack detection
- Key file contents (README, config, etc.)
- Module map, entry points, test setup, CI setup

Then uses Vibe CLI to generate an architecture summary.
The assembled profile_text is injected into coding prompts.
"""

import json
import logging
import re

from app.sandbox.manager import SandboxManager, ExecResult

logger = logging.getLogger(__name__)

# Directories to exclude from the file tree
PRUNE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "coverage", ".coverage",
    "vendor", "target", "bin", "obj", ".gradle", ".idea", ".vscode",
    "eggs", "*.egg-info", ".eggs",
}

# Key files to read for understanding
KEY_FILES = [
    "README.md", "README.rst", "README.txt", "README",
    "CONTRIBUTING.md", "ARCHITECTURE.md", "CLAUDE.md",
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "tsconfig.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".github/workflows/ci.yml", ".github/workflows/ci.yaml",
    ".github/workflows/test.yml", ".github/workflows/test.yaml",
    ".github/workflows/main.yml", ".github/workflows/main.yaml",
]

# Tech stack detection from file presence
STACK_INDICATORS = {
    "pyproject.toml": ("python", "pyproject"),
    "setup.py": ("python", "setuptools"),
    "requirements.txt": ("python", "pip"),
    "Pipfile": ("python", "pipenv"),
    "package.json": ("javascript", "npm"),
    "yarn.lock": ("javascript", "yarn"),
    "pnpm-lock.yaml": ("javascript", "pnpm"),
    "tsconfig.json": ("typescript", "tsc"),
    "go.mod": ("go", "go-modules"),
    "Cargo.toml": ("rust", "cargo"),
    "pom.xml": ("java", "maven"),
    "build.gradle": ("java", "gradle"),
    "Gemfile": ("ruby", "bundler"),
    "mix.exs": ("elixir", "mix"),
    "composer.json": ("php", "composer"),
}

FRAMEWORK_PATTERNS = {
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "express": "Express.js",
    "nextjs": "Next.js",
    "next": "Next.js",
    "react": "React",
    "vue": "Vue.js",
    "angular": "Angular",
    "spring": "Spring",
    "rails": "Rails",
    "gin": "Gin",
    "actix": "Actix",
    "axum": "Axum",
    "phoenix": "Phoenix",
    "laravel": "Laravel",
}

TEST_FRAMEWORK_FILES = {
    "pytest.ini": "pytest",
    "conftest.py": "pytest",
    "jest.config.js": "jest",
    "jest.config.ts": "jest",
    "vitest.config.ts": "vitest",
    ".mocharc.yml": "mocha",
    "karma.conf.js": "karma",
}


async def generate_repo_profile(
    sandbox: SandboxManager,
    container_id: str,
) -> dict:
    """Generate a complete repo profile from a cloned repo in /workspace.

    Returns a dict matching RepoProfile fields (minus id/repo_url/owner/repo_name/timestamps).
    """
    head_sha = await _get_head_sha(sandbox, container_id)
    file_tree = await _get_file_tree(sandbox, container_id)
    tech_stack = await _detect_tech_stack(sandbox, container_id, file_tree)
    key_files = await _read_key_files(sandbox, container_id)
    module_map = await _build_module_map(sandbox, container_id)
    entry_points = _detect_entry_points(file_tree, tech_stack)
    test_setup = await _detect_test_setup(sandbox, container_id, file_tree, tech_stack)
    ci_setup = _detect_ci_setup(file_tree, key_files)
    conventions = await _detect_conventions(sandbox, container_id, tech_stack)
    architecture_summary = await _generate_architecture_summary(
        sandbox, container_id, file_tree, tech_stack, key_files, module_map,
    )

    profile_text = _assemble_profile_text(
        file_tree, tech_stack, architecture_summary,
        module_map, entry_points, test_setup, ci_setup, conventions,
    )

    return {
        "head_sha": head_sha,
        "file_tree": file_tree,
        "tech_stack": tech_stack,
        "architecture_summary": architecture_summary,
        "module_map": module_map,
        "conventions": conventions,
        "entry_points": entry_points,
        "test_setup": test_setup,
        "ci_setup": ci_setup,
        "key_files_content": key_files,
        "profile_text": profile_text,
    }


async def _get_head_sha(sandbox: SandboxManager, container_id: str) -> str:
    result = await sandbox.exec_in_container(
        container_id, "git -C /workspace rev-parse HEAD"
    )
    return result.stdout.strip()[:40]


async def _get_file_tree(sandbox: SandboxManager, container_id: str) -> str:
    """Get pruned file tree, max 500 lines."""
    prune_args = " ".join(
        f'-not -path "*/{d}/*" -not -path "*/{d}"' for d in PRUNE_DIRS
    )
    cmd = (
        f"find /workspace -maxdepth 4 {prune_args} "
        f"-not -name '*.pyc' -not -name '*.pyo' -not -name '.DS_Store' "
        f"| sed 's|^/workspace/||' | sort | head -500"
    )
    result = await sandbox.exec_in_container(container_id, f"sh -c '{cmd}'")
    return result.stdout.strip()


async def _detect_tech_stack(
    sandbox: SandboxManager, container_id: str, file_tree: str,
) -> dict:
    """Detect languages, frameworks, build tools, and test frameworks."""
    languages = set()
    build_tools = set()
    frameworks = set()

    tree_lines = file_tree.lower().splitlines()
    tree_set = {line.strip() for line in tree_lines}

    for indicator_file, (lang, tool) in STACK_INDICATORS.items():
        if indicator_file.lower() in tree_set or any(
            line.endswith(f"/{indicator_file.lower()}") for line in tree_lines
        ):
            languages.add(lang)
            build_tools.add(tool)

    # Detect frameworks from dependency files
    dep_content = ""
    for dep_file in ["pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml"]:
        result = await sandbox.exec_in_container(
            container_id, f"cat /workspace/{dep_file} 2>/dev/null"
        )
        if result.exit_code == 0:
            dep_content += result.stdout.lower()

    for pattern, framework in FRAMEWORK_PATTERNS.items():
        if pattern in dep_content:
            frameworks.add(framework)

    # Detect test framework
    test_framework = None
    for tf_file, tf_name in TEST_FRAMEWORK_FILES.items():
        if tf_file.lower() in tree_set or any(
            line.endswith(f"/{tf_file.lower()}") for line in tree_lines
        ):
            test_framework = tf_name
            break

    if not test_framework and "pytest" in dep_content:
        test_framework = "pytest"
    if not test_framework and "jest" in dep_content:
        test_framework = "jest"

    # Detect by file extensions if no indicators found
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
        ".php": "php", ".ex": "elixir", ".cs": "csharp", ".cpp": "cpp",
    }
    for line in tree_lines:
        for ext, lang in ext_map.items():
            if line.endswith(ext):
                languages.add(lang)

    return {
        "languages": sorted(languages),
        "frameworks": sorted(frameworks),
        "build_tools": sorted(build_tools),
        "test_framework": test_framework,
    }


async def _read_key_files(sandbox: SandboxManager, container_id: str) -> dict:
    """Read key files, truncated to 3000 chars each."""
    key_files = {}
    for path in KEY_FILES:
        result = await sandbox.exec_in_container(
            container_id, f"head -c 3000 /workspace/{path} 2>/dev/null"
        )
        if result.exit_code == 0 and result.stdout.strip():
            key_files[path] = result.stdout.strip()
    return key_files


async def _build_module_map(sandbox: SandboxManager, container_id: str) -> dict:
    """Map top-level directories to their purpose based on contents."""
    result = await sandbox.exec_in_container(
        container_id,
        "ls -1d /workspace/*/ 2>/dev/null | sed 's|/workspace/||' | sed 's|/$||'"
    )
    if result.exit_code != 0:
        return {}

    module_map = {}
    for dir_name in result.stdout.strip().splitlines():
        dir_name = dir_name.strip()
        if not dir_name or dir_name.startswith(".") or dir_name in PRUNE_DIRS:
            continue

        # Count files and detect purpose
        count_result = await sandbox.exec_in_container(
            container_id,
            f"find /workspace/{dir_name} -maxdepth 2 -type f | head -20 | wc -l"
        )
        file_count = count_result.stdout.strip()

        # Check for __init__.py (Python package), index files, etc.
        init_result = await sandbox.exec_in_container(
            container_id,
            f"ls /workspace/{dir_name}/__init__.py /workspace/{dir_name}/index.* "
            f"/workspace/{dir_name}/main.* /workspace/{dir_name}/mod.rs 2>/dev/null"
        )
        markers = init_result.stdout.strip()

        purpose = _infer_dir_purpose(dir_name, markers)
        module_map[dir_name] = {
            "purpose": purpose,
            "file_count": file_count,
        }

    return module_map


def _infer_dir_purpose(dir_name: str, markers: str) -> str:
    """Heuristic purpose inference from directory name."""
    name = dir_name.lower()
    purpose_map = {
        "src": "source code",
        "lib": "library code",
        "app": "application code",
        "api": "API endpoints",
        "cmd": "CLI commands",
        "pkg": "packages",
        "internal": "internal packages",
        "tests": "test suite",
        "test": "test suite",
        "spec": "test specs",
        "docs": "documentation",
        "doc": "documentation",
        "scripts": "utility scripts",
        "tools": "tooling",
        "config": "configuration",
        "configs": "configuration",
        "migrations": "database migrations",
        "static": "static assets",
        "public": "public assets",
        "templates": "templates",
        "components": "UI components",
        "pages": "page components",
        "routes": "route handlers",
        "handlers": "request handlers",
        "services": "service layer",
        "models": "data models",
        "utils": "utilities",
        "helpers": "helper functions",
        "middleware": "middleware",
        "integrations": "third-party integrations",
        "plugins": "plugins",
        "fixtures": "test fixtures",
        "examples": "examples",
        "sandbox": "sandbox/isolation",
        "workflows": "workflow definitions",
        "deploy": "deployment config",
        "infra": "infrastructure",
        "ci": "CI/CD",
    }
    return purpose_map.get(name, "project module")


def _detect_entry_points(file_tree: str, tech_stack: dict) -> list:
    """Find likely entry point files."""
    entry_patterns = [
        r"^main\.\w+$", r"^app\.\w+$", r"^index\.\w+$",
        r"^server\.\w+$", r"^cli\.\w+$", r"^manage\.py$",
        r"app/main\.\w+$", r"src/main\.\w+$", r"src/index\.\w+$",
        r"cmd/.*main\.\w+$",
    ]
    entries = []
    for line in file_tree.splitlines():
        path = line.strip()
        for pattern in entry_patterns:
            if re.search(pattern, path):
                entries.append(path)
                break
    return entries[:10]


async def _detect_test_setup(
    sandbox: SandboxManager, container_id: str, file_tree: str, tech_stack: dict,
) -> dict:
    """Detect test directories and how to run tests."""
    test_dirs = []
    for line in file_tree.splitlines():
        path = line.strip()
        if re.search(r"(^tests?/|/__tests__/|/spec/)", path):
            dir_part = path.rsplit("/", 1)[0] if "/" in path else path
            if dir_part not in test_dirs:
                test_dirs.append(dir_part)

    test_dirs = test_dirs[:10]

    framework = tech_stack.get("test_framework", "")
    run_cmd = ""
    if framework == "pytest":
        run_cmd = "pytest"
    elif framework == "jest":
        run_cmd = "npx jest"
    elif framework == "vitest":
        run_cmd = "npx vitest run"
    elif framework == "mocha":
        run_cmd = "npx mocha"

    # Try to detect from package.json scripts
    if not run_cmd:
        result = await sandbox.exec_in_container(
            container_id, "cat /workspace/package.json 2>/dev/null"
        )
        if result.exit_code == 0:
            try:
                pkg = json.loads(result.stdout)
                scripts = pkg.get("scripts", {})
                if "test" in scripts:
                    run_cmd = f"npm test  # {scripts['test']}"
            except json.JSONDecodeError:
                pass

    # Try Makefile
    if not run_cmd:
        result = await sandbox.exec_in_container(
            container_id, "grep -E '^test:' /workspace/Makefile 2>/dev/null"
        )
        if result.exit_code == 0 and result.stdout.strip():
            run_cmd = "make test"

    return {
        "framework": framework,
        "test_dirs": test_dirs,
        "run_cmd": run_cmd,
    }


def _detect_ci_setup(file_tree: str, key_files: dict) -> dict:
    """Detect CI/CD provider and config."""
    ci_files = []
    provider = ""

    for line in file_tree.splitlines():
        path = line.strip()
        if ".github/workflows" in path and path.endswith((".yml", ".yaml")):
            ci_files.append(path)
            provider = "github-actions"
        elif path in (".travis.yml", ".circleci/config.yml", "Jenkinsfile", ".gitlab-ci.yml"):
            ci_files.append(path)
            if not provider:
                provider = path.split(".")[0].replace("/", "-") if "/" in path else path.replace(".", "")

    return {
        "provider": provider,
        "config_files": ci_files[:5],
    }


async def _detect_conventions(
    sandbox: SandboxManager, container_id: str, tech_stack: dict,
) -> dict:
    """Detect code conventions from linter configs and code samples."""
    conventions = {}

    # Check for linter/formatter configs
    linter_files = {
        ".eslintrc.js": "eslint", ".eslintrc.json": "eslint", ".eslintrc.yml": "eslint",
        ".prettierrc": "prettier", ".prettierrc.json": "prettier",
        "ruff.toml": "ruff", ".flake8": "flake8",
        ".rubocop.yml": "rubocop", ".golangci.yml": "golangci-lint",
        "biome.json": "biome",
    }
    linters = []
    for fname, linter in linter_files.items():
        result = await sandbox.exec_in_container(
            container_id, f"test -f /workspace/{fname} && echo yes"
        )
        if result.exit_code == 0 and "yes" in result.stdout:
            linters.append(linter)

    # Check pyproject.toml for tool configs
    result = await sandbox.exec_in_container(
        container_id, "grep -E '\\[tool\\.(ruff|black|isort|mypy|pylint)' /workspace/pyproject.toml 2>/dev/null"
    )
    if result.exit_code == 0:
        for match in re.findall(r"\[tool\.(\w+)", result.stdout):
            if match not in linters:
                linters.append(match)

    conventions["linters"] = linters

    # Detect naming convention from a sample of source files
    if "python" in tech_stack.get("languages", []):
        result = await sandbox.exec_in_container(
            container_id,
            "grep -rh 'def ' /workspace --include='*.py' | head -20"
        )
        if result.exit_code == 0:
            funcs = re.findall(r"def (\w+)", result.stdout)
            snake = sum(1 for f in funcs if "_" in f)
            camel = sum(1 for f in funcs if f != f.lower() and "_" not in f)
            conventions["naming"] = "snake_case" if snake >= camel else "camelCase"

    return conventions


async def _generate_architecture_summary(
    sandbox: SandboxManager,
    container_id: str,
    file_tree: str,
    tech_stack: dict,
    key_files: dict,
    module_map: dict,
) -> str:
    """Use Vibe CLI to generate a concise architecture summary."""
    # Build context for the LLM
    context_parts = [
        "CHANGE TASK — You MUST create the file /workspace/REPO_PROFILE.md with a concise architecture summary.",
        "",
        "Analyze this repository and write a brief architecture overview.",
        "",
        f"Tech stack: {json.dumps(tech_stack)}",
        f"Top-level modules: {json.dumps(module_map)}",
        "",
    ]

    # Include README if available
    for readme_key in ["README.md", "README.rst", "README.txt", "README"]:
        if readme_key in key_files:
            content = key_files[readme_key][:2000]
            context_parts.append(f"README:\n{content}")
            break

    context_parts.extend([
        "",
        "Write /workspace/REPO_PROFILE.md with these sections (keep each section to 2-3 sentences):",
        "1. **Overview** — What this project does",
        "2. **Architecture** — How it's structured (key patterns, layers)",
        "3. **Key Components** — The most important modules/packages and what they do",
        "4. **Data Flow** — How data moves through the system",
        "5. **Dependencies** — Key external dependencies and what they're used for",
        "",
        "Be concise. This will be used as context for an AI coding agent.",
        "Do NOT include setup instructions or contribution guidelines.",
    ])

    prompt = "\n".join(context_parts)

    try:
        await sandbox.run_vibe(
            container_id, prompt, max_turns=20, max_price=2.0, timeout=180,
        )
        result = await sandbox.exec_in_container(
            container_id, "cat /workspace/REPO_PROFILE.md"
        )
        if result.exit_code == 0 and result.stdout.strip():
            # Clean up the generated file so it doesn't interfere with coding
            await sandbox.exec_in_container(
                container_id, "rm -f /workspace/REPO_PROFILE.md"
            )
            return result.stdout.strip()
    except Exception as e:
        logger.warning("Architecture summary generation failed: %s", e)

    # Fallback: build a basic summary from what we know
    parts = []
    languages = tech_stack.get("languages", [])
    frameworks = tech_stack.get("frameworks", [])
    if languages:
        parts.append(f"Languages: {', '.join(languages)}")
    if frameworks:
        parts.append(f"Frameworks: {', '.join(frameworks)}")
    if module_map:
        parts.append("Modules: " + ", ".join(
            f"{k} ({v.get('purpose', '?')})" for k, v in module_map.items()
        ))
    return "\n".join(parts) if parts else ""


def _assemble_profile_text(
    file_tree: str,
    tech_stack: dict,
    architecture_summary: str,
    module_map: dict,
    entry_points: list,
    test_setup: dict,
    ci_setup: dict,
    conventions: dict,
) -> str:
    """Assemble the final profile text for injection into prompts."""
    sections = ["# Repository Profile", ""]

    # Architecture
    if architecture_summary:
        sections.append("## Architecture")
        sections.append(architecture_summary)
        sections.append("")

    # Tech stack
    languages = tech_stack.get("languages", [])
    frameworks = tech_stack.get("frameworks", [])
    if languages or frameworks:
        sections.append("## Tech Stack")
        if languages:
            sections.append(f"- Languages: {', '.join(languages)}")
        if frameworks:
            sections.append(f"- Frameworks: {', '.join(frameworks)}")
        build = tech_stack.get("build_tools", [])
        if build:
            sections.append(f"- Build: {', '.join(build)}")
        tf = tech_stack.get("test_framework")
        if tf:
            sections.append(f"- Testing: {tf}")
        sections.append("")

    # Module map
    if module_map:
        sections.append("## Project Structure")
        for dir_name, info in module_map.items():
            sections.append(f"- `{dir_name}/` — {info.get('purpose', 'module')}")
        sections.append("")

    # Entry points
    if entry_points:
        sections.append("## Entry Points")
        for ep in entry_points:
            sections.append(f"- `{ep}`")
        sections.append("")

    # Test setup
    if test_setup.get("run_cmd"):
        sections.append("## Testing")
        sections.append(f"- Run: `{test_setup['run_cmd']}`")
        if test_setup.get("test_dirs"):
            sections.append(f"- Dirs: {', '.join(test_setup['test_dirs'][:5])}")
        sections.append("")

    # CI
    if ci_setup.get("provider"):
        sections.append("## CI/CD")
        sections.append(f"- Provider: {ci_setup['provider']}")
        sections.append("")

    # Conventions
    if conventions.get("linters"):
        sections.append("## Conventions")
        sections.append(f"- Linters: {', '.join(conventions['linters'])}")
        if conventions.get("naming"):
            sections.append(f"- Naming: {conventions['naming']}")
        sections.append("")

    # Pruned file tree (limit to 100 lines for prompt context)
    tree_lines = file_tree.splitlines()[:100]
    if tree_lines:
        sections.append("## File Tree (top-level)")
        sections.append("```")
        sections.extend(tree_lines)
        if len(file_tree.splitlines()) > 100:
            sections.append(f"... and {len(file_tree.splitlines()) - 100} more")
        sections.append("```")

    return "\n".join(sections)

"""Validate project-local agent configuration and the pinned Python environment."""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import tomllib
from pathlib import Path

try:
    import yaml
except ImportError:  # Report this through the environment check rather than a traceback.
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
AGENT_MODEL = "gpt-6-luna"
REQUIRED_MODULES = ("fastapi", "pydantic", "sqlmodel", "PIL", "pypdfium2", "langchain", "langchain_core", "yaml", "httpx", "requests", "dotenv", "multipart")
REQUIRED_AGENTS = {"economy-scout", "economy-worker"}


def check_environment() -> list[str]:
    errors: list[str] = []
    if sys.version_info < (3, 12):
        errors.append(f"Python 3.12+ required; found {sys.version.split()[0]}")
    for module in REQUIRED_MODULES:
        if importlib.util.find_spec(module) is None:
            errors.append(f"required dependency missing: {module}")
    return errors


def check_agents() -> list[str]:
    errors: list[str] = []
    agent_dir = ROOT / ".codex" / "agents"
    for path in sorted(agent_dir.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: invalid TOML: {exc}")
            continue
        if data.get("name") != path.stem:
            errors.append(f"{path.relative_to(ROOT)}: name must match filename {path.stem!r}")
        if data.get("model") != AGENT_MODEL:
            errors.append(f"{path.relative_to(ROOT)}: expected model {AGENT_MODEL!r}")
        if data.get("model_reasoning_effort") != "high":
            errors.append(f"{path.relative_to(ROOT)}: expected model_reasoning_effort='high'")
        for field in ("name", "description", "developer_instructions"):
            if not isinstance(data.get(field), str) or not data[field].strip():
                errors.append(f"{path.relative_to(ROOT)}: {field} must be a non-empty string")
    if not list(agent_dir.glob("*.toml")):
        errors.append("no .codex/agents/*.toml files found")
    missing = REQUIRED_AGENTS - {path.stem for path in agent_dir.glob("*.toml")}
    if missing:
        errors.append(f"required agents missing: {', '.join(sorted(missing))}")
    scout = agent_dir / "economy-scout.toml"
    if scout.is_file():
        try:
            scout_data = tomllib.loads(scout.read_text(encoding="utf-8"))
            if scout_data.get("sandbox_mode") != "read-only":
                errors.append(".codex/agents/economy-scout.toml: sandbox_mode must be 'read-only'")
        except (OSError, tomllib.TOMLDecodeError):
            pass  # The TOML error is already reported above.
    return errors


def check_skills() -> tuple[list[str], list[tuple[str, int]]]:
    errors: list[str] = []
    counts: list[tuple[str, int]] = []
    skills_dir = ROOT / ".agents" / "skills"
    for skill in sorted(skills_dir.glob("*/SKILL.md")):
        rel = skill.relative_to(ROOT)
        content = skill.read_text(encoding="utf-8")
        lines = len(content.splitlines())
        counts.append((str(rel), lines))
        match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", content, re.DOTALL)
        if not match:
            errors.append(f"{rel}: missing valid YAML frontmatter delimiters")
            continue
        if len(content.splitlines()) > 100:
            errors.append(f"{rel}: {len(content.splitlines())} lines exceeds the 100-line limit")
        if yaml is None:
            errors.append(f"{rel}: PyYAML is required to validate frontmatter")
            continue
        try:
            values = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: invalid YAML frontmatter: {exc}")
            continue
        if not isinstance(values, dict):
            errors.append(f"{rel}: YAML frontmatter must be a mapping")
            continue
        if values.get("name") != skill.parent.name:
            errors.append(f"{rel}: frontmatter name must match skill folder {skill.parent.name!r}")
        if not isinstance(values.get("name"), str) or not values["name"].strip():
            errors.append(f"{rel}: frontmatter name must be a non-empty string")
        if not isinstance(values.get("description"), str) or not values["description"].strip():
            errors.append(f"{rel}: frontmatter description must be a non-empty string")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue
            local_target = target.split("#", 1)[0]
            if local_target and not (skill.parent / local_target).resolve().is_file():
                errors.append(f"{rel}: broken relative reference {target!r}")
    if not counts:
        errors.append("no .agents/skills/*/SKILL.md files found")
    return errors, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-environment", action="store_true", help="check Python and required modules only")
    args = parser.parse_args()
    errors = check_environment()
    if not errors:
        print(f"Environment OK: Python {sys.version.split()[0]}")
    if not args.check_environment:
        errors.extend(check_agents())
        skill_errors, counts = check_skills()
        errors.extend(skill_errors)
        for path, lines in counts:
            print(f"Skill lines: {path} ({lines})")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Agent configuration OK" if not args.check_environment else "Required dependencies OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

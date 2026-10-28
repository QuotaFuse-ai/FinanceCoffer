"""Skill installation (``skill install``).

Copies the bundled ``skills/financecoffer`` package (``SKILL.md`` plus its
``references/``) into an agent's skill directory. The destination is
``<root>/financecoffer``; ``--path`` replaces the root, otherwise the root is
derived from ``--target``. Existing files are only overwritten with ``--force``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import typer

from financecoffer.cli.options import from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.cli.placeholder import add_group
from financecoffer.core.errors import (
    ConfigError,
    DependencyError,
    FinanceCofferError,
    InvalidArgumentError,
)
from financecoffer.core.redaction import redact_text

SKILL_NAME = "financecoffer"
TARGET_ROOTS: dict[str, str] = {
    "claude": ".claude/skills",
    "cursor": ".cursor/skills",
    "codex": ".codex/skills",
}
TARGETS: tuple[str, ...] = tuple(TARGET_ROOTS)


def source_dir() -> Path:
    """Locate the bundled Skill package in a source checkout."""

    candidate = Path(__file__).resolve().parents[4] / "skills" / SKILL_NAME
    if not (candidate / "SKILL.md").is_file():
        raise DependencyError(
            "the bundled Skill package is not available",
            details={
                "hint": "run from a source checkout that contains skills/financecoffer",
            },
        )
    return candidate


def _files(source: Path) -> list[Path]:
    return sorted(path for path in source.rglob("*") if path.is_file())


def _root(target: str | None, path: str | None) -> Path:
    if path is not None:
        cleaned = path.strip()
        if not cleaned:
            raise InvalidArgumentError(
                "--path must not be empty",
                details={"field": "path", "hint": "pass a directory"},
            )
        root = Path(cleaned).expanduser()
        if root.exists() and not root.is_dir():
            raise InvalidArgumentError(
                "--path must be a directory; the Skill is copied into DIR/" + SKILL_NAME,
                details={"field": "path", "path": str(root)},
            )
        return root
    if target is None:
        raise InvalidArgumentError(
            "either --target or --path is required",
            details={"field": "target", "valid": list(TARGETS)},
        )
    if target not in TARGET_ROOTS:
        raise InvalidArgumentError(
            "target must be one of: " + ", ".join(TARGETS),
            details={"field": "target", "value": target, "valid": list(TARGETS)},
        )
    return Path.cwd() / TARGET_ROOTS[target]


def _install(source: Path, destination: Path, *, force: bool) -> dict[str, Any]:
    if destination.exists() and not destination.is_dir():
        raise InvalidArgumentError(
            "the destination exists and is not a directory",
            details={"path": str(destination), "field": "path"},
        )
    if destination.exists() and not force:
        raise InvalidArgumentError(
            "the Skill is already installed; pass --force to overwrite it",
            details={"path": str(destination), "field": "force", "hint": "--force"},
        )

    installed: list[str] = []
    overwritten = 0
    try:
        for path in _files(source):
            relative = path.relative_to(source)
            target = destination / relative
            if target.exists():
                overwritten += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            installed.append(relative.as_posix())
    except OSError as exc:
        # A read-only root, a file where a directory is expected or a lost
        # source file must surface as an envelope, never a traceback.
        raise ConfigError(
            "cannot write the Skill into the destination directory",
            details={
                "path": str(destination),
                "error": redact_text(str(exc).strip())[:200],
                "hint": "check that the install root is a writable directory",
            },
        ) from exc

    return {
        "path": str(destination),
        "source": str(source),
        "force": force,
        "overwritten": overwritten,
        "files": installed,
    }


def register(app: typer.Typer) -> None:
    skill = add_group(app, name="skill", help_text="Install the bundled agent Skill.")

    @skill.command(
        "install",
        help=(
            "Copy the Skill package into an agent directory; missing directories"
            " are created automatically."
        ),
    )
    def install_command(
        ctx: typer.Context,
        target: str | None = typer.Option(
            None,
            "--target",
            help="Agent layout: " + " | ".join(TARGETS) + " (default root per target).",
        ),
        path: str | None = typer.Option(
            None,
            "--path",
            help=(
                "Install root (created if missing); the Skill is copied into"
                " DIR/" + SKILL_NAME + "."
            ),
        ),
        force: bool = typer.Option(False, "--force", help="Overwrite an existing installation."),
    ) -> None:
        options = from_context(ctx)
        try:
            source = source_dir()
            root = _root(target, path)
            destination = root / SKILL_NAME
            payload = _install(source, destination, force=force)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        payload["target"] = target
        emit_success(options, payload)


__all__ = ["SKILL_NAME", "TARGET_ROOTS", "TARGETS", "register", "source_dir"]

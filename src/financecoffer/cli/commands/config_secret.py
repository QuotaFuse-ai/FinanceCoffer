"""``fcoffer config secret``: manage the credential stores behind references.

These commands move a secret *into* a store and print the reference to write into
``.env``. The secret comes from an interactive prompt, so it never touches argv or
shell history, and no command prints a value back -- ``list`` reports stores and
targets, never contents.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer

from financecoffer.cli.options import GlobalOptions, from_context
from financecoffer.cli.output import emit_failure, emit_success
from financecoffer.config.loader import credential_sources, env_var_name
from financecoffer.core.envelope import Meta
from financecoffer.core.errors import (
    ConfigError,
    FinanceCofferError,
    InvalidArgumentError,
)
from financecoffer.core.secrets import (
    CREDENTIAL_FIELDS,
    DEFAULT_KEYRING_SERVICE,
    KeyringBackend,
    SecretReference,
    default_keyring_backend,
    parse_reference,
    resolve_reference,
)

_STORES: tuple[str, ...] = ("keyring", "file")


def _require_field(options: GlobalOptions, value: str) -> str:
    """Accept a credential field with or without its ``FC_`` prefix."""

    name = value.strip().lower().removeprefix("fc_")
    if name in CREDENTIAL_FIELDS:
        return name
    emit_failure(
        options,
        InvalidArgumentError(
            f"{value} is not a credential field",
            details={"field": value, "valid": [env_var_name(item) for item in CREDENTIAL_FIELDS]},
        ),
    )


def _require_reference(options: GlobalOptions, field: str, raw: str) -> SecretReference:
    """Return the configured reference, refusing a literal or a malformed one."""

    try:
        reference = parse_reference(raw, field=field)
    except ConfigError as exc:
        emit_failure(options, exc)
    if reference is None:
        emit_failure(
            options,
            InvalidArgumentError(
                f"{env_var_name(field)} holds a literal secret, not a reference",
                details={
                    "field": env_var_name(field),
                    "hint": "rotate it with: fcoffer config secret set " + field,
                },
            ),
        )
    if reference.scheme == "env":
        emit_failure(
            options,
            InvalidArgumentError(
                "an env: reference names a variable this process does not own",
                details={
                    "field": env_var_name(field),
                    "hint": f"unset {reference.target} where the deployment sets it",
                },
            ),
        )
    return reference


def _write_file(path: Path, secret: str, *, dpapi: bool) -> str:
    payload = secret.encode("utf-8")
    if dpapi:
        from financecoffer.core import wincred

        payload = wincred.protect(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if os.name != "nt":  # pragma: no cover - platform specific
        path.chmod(0o600)
    return str(path)


def _set_result(options: GlobalOptions, field: str, *, store: str, target: str) -> None:
    emit_success(
        options,
        {
            "field": env_var_name(field),
            "store": store,
            "target": target,
            "reference": f"{store}:{target}",
            "next": f"write {env_var_name(field)}={store}:{target} into .env or the environment",
        },
        meta=Meta(source="config"),
    )


def _entry(field: str, raw: str, *, backend: KeyringBackend) -> dict[str, Any]:
    """Describe one configured credential: its store and target, never its value.

    The backend is passed in, not defaulted per platform: a listing should only
    consult the store the operator actually configured, and a unit test of this
    command must not touch the machine's real Credential Manager.
    """

    entry: dict[str, Any] = {
        "env": env_var_name(field),
        "configured": True,
        "type": "literal",
        "target": "(held in the configuration source)",
        "status": "ok",
        "reason": f"rotate it with: fcoffer config secret set {field}",
    }
    try:
        reference = parse_reference(raw, field=field)
    except ConfigError as exc:
        entry.update(type="invalid", target="", status="error", reason=exc.message)
        return entry
    if reference is None:
        return entry
    entry.update(type=reference.scheme, target=reference.describe(), reason="")
    try:
        resolve_reference(field, raw, backend=backend)
    except ConfigError as exc:
        entry.update(status="unresolved", reason=exc.message)
    return entry


def register(parent: typer.Typer) -> None:
    """Attach the ``secret`` sub-group to the ``config`` group."""

    secret = typer.Typer(
        name="secret",
        help="Manage the credential stores that configuration references point at.",
        no_args_is_help=True,
        add_completion=False,
    )
    parent.add_typer(secret)

    @secret.command("set", help="Store a credential in a keyring or file; print its reference.")
    def set_command(
        ctx: typer.Context,
        field: str = typer.Argument(help="Credential field: db_url, mcp_token, ..."),
        store: str = typer.Option("keyring", "--store", help="Where to store it: keyring | file."),
        service: str = typer.Option(
            DEFAULT_KEYRING_SERVICE, "--service", help="Keyring service name."
        ),
        account: str = typer.Option(
            "", "--account", help="Keyring account (defaults to the field)."
        ),
        file_path: str = typer.Option(
            "", "--file", help="Path for --store file; absolute is safest across forks."
        ),
        dpapi: bool = typer.Option(
            False,
            "--dpapi",
            help="Windows only: DPAPI-protect a --store file so it decrypts for this user.",
        ),
    ) -> None:
        options = from_context(ctx)
        name = _require_field(options, field)
        if store not in _STORES:
            emit_failure(
                options,
                InvalidArgumentError(
                    "--store must be keyring or file",
                    details={"field": "store", "value": store, "valid": list(_STORES)},
                ),
            )
        path = Path(file_path.strip()).expanduser() if file_path.strip() else None
        if (store == "file") != (path is not None):
            emit_failure(
                options,
                InvalidArgumentError(
                    "--store file needs --file <path>, and --file needs --store file",
                    details={"field": "file", "hint": "use an absolute path"},
                ),
            )
        if dpapi and store != "file":
            emit_failure(
                options,
                InvalidArgumentError(
                    "--dpapi protects a --store file",
                    details={"field": "dpapi", "hint": "combine it with --store file"},
                ),
            )
        if dpapi and os.name != "nt":  # pragma: no cover - platform specific
            emit_failure(
                options,
                InvalidArgumentError(
                    "--dpapi is a Windows facility",
                    details={"field": "dpapi", "hint": "drop it on macOS and Linux"},
                ),
            )
        if path is not None and path.is_dir():
            emit_failure(
                options,
                InvalidArgumentError(
                    "--file names a directory",
                    details={"field": "file", "value": str(path)},
                ),
            )
        prompt = typer.prompt(
            f"{env_var_name(name)} secret", hide_input=True, confirmation_prompt=True
        )
        try:
            if store == "file":
                assert path is not None  # narrowed by the --file check above
                target = _write_file(path, prompt, dpapi=dpapi)
            else:
                backend = default_keyring_backend()
                account_name = account.strip() or name
                service_name = service.strip() or DEFAULT_KEYRING_SERVICE
                backend.set(service_name, account_name, prompt)
                target = f"{service_name}/{account_name}"
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        except (OSError, ValueError) as exc:
            emit_failure(
                options,
                ConfigError(
                    f"the secret could not be written: {getattr(exc, 'strerror', None) or exc}",
                    details={"field": env_var_name(name), "hint": "check the target's permissions"},
                ),
            )
        _set_result(options, name, store=store, target=target)

    @secret.command("list", help="List the credential references in use; never prints a value.")
    def list_command(ctx: typer.Context) -> None:
        options = from_context(ctx)
        sources = credential_sources()
        backend = default_keyring_backend()
        fields = [
            _entry(name, sources[name], backend=backend)
            for name in CREDENTIAL_FIELDS
            if name in sources
        ]
        emit_success(
            options,
            {
                "fields": fields,
                "unset": [env_var_name(name) for name in CREDENTIAL_FIELDS if name not in sources],
            },
            meta=Meta(source="config"),
        )

    @secret.command("delete", help="Remove the credential material a reference points at.")
    def delete_command(
        ctx: typer.Context,
        field: str = typer.Argument(help="Credential field: db_url, mcp_token, ..."),
        confirm: bool = typer.Option(
            False, "--confirm", help="Required: this destroys the stored secret."
        ),
        service: str = typer.Option("", "--service", help="Keyring service (overrides .env)."),
        account: str = typer.Option("", "--account", help="Keyring account (overrides .env)."),
        file_path: str = typer.Option("", "--file", help="File to remove (overrides .env)."),
    ) -> None:
        options = from_context(ctx)
        name = _require_field(options, field)
        if not confirm:
            emit_failure(
                options,
                InvalidArgumentError(
                    "deleting a stored credential needs --confirm",
                    details={"field": env_var_name(name), "hint": "pass --confirm to destroy it"},
                ),
            )
        raw = file_path.strip() or credential_sources().get(name, "")
        if not raw:
            emit_failure(
                options,
                InvalidArgumentError(
                    f"{env_var_name(name)} holds no reference to delete",
                    details={
                        "field": env_var_name(name),
                        "hint": "pass --file <path>, or --service and --account",
                    },
                ),
            )
        reference = _require_reference(options, name, raw)
        try:
            if reference.scheme == "file":
                path = Path(reference.target).expanduser()
                removed = path.is_file()
                if removed:
                    path.unlink()
                store, target = "file", str(path)
            else:
                backend = default_keyring_backend()
                service_name = service.strip() or reference.service
                account_name = account.strip() or reference.account
                store, target = backend.name, f"{service_name}/{account_name}"
                removed = backend.delete(service_name, account_name)
        except FinanceCofferError as exc:
            emit_failure(options, exc)
        except OSError as exc:
            emit_failure(
                options,
                ConfigError(
                    f"the credential could not be removed: {exc.strerror or type(exc).__name__}",
                    details={"field": env_var_name(name)},
                ),
            )
        emit_success(
            options,
            {
                "field": env_var_name(name),
                "store": store,
                "target": target,
                "removed": removed,
            },
            meta=Meta(source="config"),
        )


__all__ = ["register"]

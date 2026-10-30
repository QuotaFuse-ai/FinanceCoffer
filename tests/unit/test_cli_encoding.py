"""Regression tests: envelopes must leave the CLI as UTF-8 on any host code page."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

from financecoffer.cli.encoding import ensure_utf8_stdio
from financecoffer.cli.output import emit_envelope
from financecoffer.core.envelope import Envelope

PAYLOAD = "现金分红 融资融券"


def test_emit_envelope_reencodes_a_legacy_stdout(monkeypatch) -> None:
    buffer = io.BytesIO()
    legacy = io.TextIOWrapper(buffer, encoding="cp936", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", legacy)

    emit_envelope(Envelope.success({"note": PAYLOAD}, trace_id="quot-159"))

    legacy.flush()
    assert PAYLOAD in buffer.getvalue().decode("utf-8")


def test_ensure_utf8_stdio_ignores_streams_it_cannot_reconfigure(monkeypatch) -> None:
    class _Stubborn:
        encoding = "cp1252"

        @staticmethod
        def reconfigure(**_: object) -> None:
            raise ValueError("stream is detached")

    monkeypatch.setattr(sys, "stdout", _Stubborn())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    ensure_utf8_stdio()


def test_cli_writes_utf8_when_the_host_code_page_is_not(tmp_path: Path) -> None:
    """Force a GBK host code page; the redirected file must still be UTF-8."""

    env = {key: value for key, value in os.environ.items() if key != "PYTHONUTF8"}
    env["PYTHONIOENCODING"] = "gbk"
    env["FC_EVENT_STREAM"] = PAYLOAD
    output = tmp_path / "envelope.json"

    with output.open("wb") as handle:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "financecoffer",
                "--output",
                "json",
                "config",
                "show",
                "--json",
            ],
            cwd=tmp_path,
            env=env,
            stdout=handle,
            stderr=subprocess.PIPE,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert PAYLOAD in output.read_bytes().decode("utf-8")

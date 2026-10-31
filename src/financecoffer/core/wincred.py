"""Windows Credential Manager and DPAPI access through ``ctypes``.

Deliberately dependency-free: the operating system's own ``advapi32`` and
``crypt32`` are reached over ``ctypes`` instead of pulling in ``keyring`` or
``pywin32`` (DEC-0031). Every function here is Windows-only and raises
:class:`OSError` on another platform -- callers dispatch on ``os.name`` and never
reach this module from macOS or Linux.

Generic credentials (``CRED_TYPE_GENERIC``) are what ``cmdkey`` and the Control
Panel show, so an operator can inspect or delete an entry without this tool.
The secret is stored as UTF-16LE bytes, which is the encoding those tools use.
"""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes
from typing import Any, NamedTuple

CRED_TYPE_GENERIC = 0x1
CRED_PERSIST_LOCAL_MACHINE = 0x2

DPAPI_MARKER = "dpapi:v1:"

_ERROR_NOT_FOUND = 1168
_ERROR_NO_SUCH_LOGON_SESSION = 1312
_ERROR_INVALID_PARAMETER = 87

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class Credential(NamedTuple):
    """A Credential Manager entry without its secret."""

    target: str
    user_name: str


def _require_windows() -> None:
    if os.name != "nt":  # pragma: no cover - platform specific
        raise OSError("Windows credential and DPAPI helpers require Windows")


def _win_error(function: str, ok: int) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{function} failed with Windows error code {code}")


def _load(name: str) -> Any:
    _require_windows()
    return ctypes.WinDLL(name, use_last_error=True)


class _FileTime(ctypes.Structure):  # noqa: UP013 - ctypes.Structure base is fixed
    _fields_ = [
        ("dw_low_date_time", wintypes.DWORD),
        ("dw_high_date_time", wintypes.DWORD),
    ]


class _CredentialAttributeW(ctypes.Structure):  # noqa: UP013 - ctypes.Structure base is fixed
    _fields_ = [
        ("keyword", wintypes.LPWSTR),
        ("flags", wintypes.DWORD),
        ("type", wintypes.DWORD),
        ("value_size", wintypes.DWORD),
        ("value", wintypes.LPVOID),
    ]


class _CredentialW(ctypes.Structure):  # noqa: UP013 - ctypes.Structure base is fixed
    _fields_ = [
        ("flags", wintypes.DWORD),
        ("type", wintypes.DWORD),
        ("last_write_time", _FileTime),
        ("persist", wintypes.DWORD),
        ("attribute_count", wintypes.DWORD),
        ("attributes", ctypes.POINTER(_CredentialAttributeW)),
        ("target_name_size", wintypes.DWORD),
        ("target_name", wintypes.LPWSTR),
        ("comment_size", wintypes.DWORD),
        ("comment", wintypes.LPWSTR),
        ("user_name_size", wintypes.DWORD),
        ("user_name", wintypes.LPWSTR),
        ("credential_blob_size", wintypes.DWORD),
        ("credential_blob", wintypes.LPVOID),
        ("is_published", wintypes.DWORD),
        ("target_alias", wintypes.LPWSTR),
    ]


class _DataBlob(ctypes.Structure):  # noqa: UP013 - ctypes.Structure base is fixed
    _fields_ = [
        ("cb_data", wintypes.DWORD),
        ("pb_data", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    """Copy a ``DATA_BLOB`` out; the caller frees it with ``LocalFree``."""

    return ctypes.string_at(blob.pb_data, blob.cb_data)


def _free(data: _DataBlob) -> None:
    kernel32 = _load("kernel32")
    kernel32.LocalFree(ctypes.cast(data.pb_data, wintypes.HLOCAL))


def _bytes_to_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def protect(data: bytes, *, entropy: bytes = b"") -> bytes:
    """Return a DPAPI blob bound to the current user, tagged with :data:`DPAPI_MARKER`."""

    crypt32 = _load("crypt32")
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    source, source_buffer = _bytes_to_blob(data)
    entropy_blob: _DataBlob | None = None
    entropy_buffer: ctypes.Array[ctypes.c_char] | None = None
    if entropy:
        entropy_blob, entropy_buffer = _bytes_to_blob(entropy)
    output = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        "FinanceCoffer secret",
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    _ = source_buffer, entropy_buffer
    if not ok:
        raise _win_error("CryptProtectData", ok)
    try:
        protected = _blob_to_bytes(output)
    finally:
        _free(output)
    return (DPAPI_MARKER + base64.b64encode(protected).decode("ascii")).encode("ascii")


def unprotect(payload: bytes) -> bytes:
    """Undo :func:`protect`; ``payload`` carries the :data:`DPAPI_MARKER` prefix."""

    crypt32 = _load("crypt32")
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    try:
        raw = base64.b64decode(payload[len(DPAPI_MARKER) :], validate=True)
    except ValueError as exc:
        raise ValueError("DPAPI payload is not valid base64") from exc
    source, source_buffer = _bytes_to_blob(raw)
    output = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    _ = source_buffer
    if not ok:
        raise _win_error("CryptUnprotectData", ok)
    try:
        return _blob_to_bytes(output)
    finally:
        _free(output)


def read_password(target: str) -> str | None:
    """Return the secret stored under ``target``, or ``None`` when absent."""

    advapi32 = _load("advapi32")
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    credential = ctypes.POINTER(_CredentialW)()
    ok = advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(credential))
    if not ok:
        code = ctypes.get_last_error()
        if code in (_ERROR_NOT_FOUND, _ERROR_NO_SUCH_LOGON_SESSION):
            return None
        raise _win_error("CredReadW", ok)
    try:
        entry = credential.contents
        size = int(entry.credential_blob_size)
        if size == 0 or entry.credential_blob is None:
            return ""
        raw = ctypes.string_at(
            ctypes.cast(entry.credential_blob, ctypes.POINTER(ctypes.c_char)), size
        )
    finally:
        advapi32.CredFree(ctypes.cast(credential, wintypes.LPVOID))
    return raw.decode("utf-16-le")


def write_password(target: str, user_name: str, secret: str) -> None:
    """Store ``secret`` for ``target`` (create or replace)."""

    advapi32 = _load("advapi32")
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    blob = secret.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    credential = _CredentialW()
    credential.flags = 0
    credential.type = CRED_TYPE_GENERIC
    credential.persist = CRED_PERSIST_LOCAL_MACHINE
    credential.target_name = target
    credential.target_name_size = len(target) * ctypes.sizeof(wintypes.WCHAR)
    credential.user_name = user_name
    credential.user_name_size = len(user_name) * ctypes.sizeof(wintypes.WCHAR)
    credential.credential_blob_size = len(blob)
    credential.credential_blob = ctypes.cast(buffer, wintypes.LPVOID)
    ok = advapi32.CredWriteW(ctypes.byref(credential), 0)
    _ = buffer
    if not ok:
        raise _win_error("CredWriteW", ok)


def delete_password(target: str) -> bool:
    """Remove ``target``; ``False`` means it was not there."""

    advapi32 = _load("advapi32")
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    ok = advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
    if ok:
        return True
    code = ctypes.get_last_error()
    if code in (_ERROR_NOT_FOUND, _ERROR_NO_SUCH_LOGON_SESSION, _ERROR_INVALID_PARAMETER):
        return False
    raise _win_error("CredDeleteW", ok)


def enumerate_credentials(prefix: str = "") -> list[Credential]:
    """List stored credentials whose target starts with ``prefix`` (never a secret)."""

    advapi32 = _load("advapi32")
    advapi32.CredEnumerateW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_CredentialW))),
    ]
    advapi32.CredEnumerateW.restype = wintypes.BOOL
    count = wintypes.DWORD()
    table = ctypes.POINTER(ctypes.POINTER(_CredentialW))()
    ok = advapi32.CredEnumerateW(
        f"{prefix}*" if prefix else None,
        0x1,  # CRED_ENUMERATE_ALL_CREDENTIALS
        ctypes.byref(count),
        ctypes.byref(table),
    )
    if not ok:
        code = ctypes.get_last_error()
        if code in (_ERROR_NOT_FOUND, _ERROR_NO_SUCH_LOGON_SESSION):
            return []
        raise _win_error("CredEnumerateW", ok)
    found: list[Credential] = []
    try:
        for index in range(int(count.value)):
            entry = table[index].contents
            target = entry.target_name or ""
            if prefix and not target.startswith(prefix):
                continue
            found.append(Credential(target=target, user_name=entry.user_name or ""))
    finally:
        advapi32.CredFree(ctypes.cast(table, wintypes.LPVOID))
    return found


__all__ = [
    "CRED_TYPE_GENERIC",
    "CRED_PERSIST_LOCAL_MACHINE",
    "DPAPI_MARKER",
    "Credential",
    "delete_password",
    "enumerate_credentials",
    "protect",
    "read_password",
    "unprotect",
    "write_password",
]

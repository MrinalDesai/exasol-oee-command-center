"""Exasol connection helper (pyexasol).

Configuration via environment variables (see .env.example):
    EXA_DSN          host:port, default localhost:8563 (check `exasol info`)
    EXA_USER         default sys
    EXA_PASSWORD     default exasol (Exasol Personal local default)
    EXA_TLS_VERIFY   1 to verify the server certificate; default 0, because
                     local Exasol Personal uses a self-signed certificate
"""
from __future__ import annotations

import os
import ssl
from pathlib import Path

import pyexasol

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _load_env_file():
    """Read KEY=VALUE lines from .env without overriding real environment variables."""
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def connect(schema: str | None = None, **extra) -> pyexasol.ExaConnection:
    _load_env_file()
    verify = os.environ.get("EXA_TLS_VERIFY", "0") == "1"
    kwargs = dict(
        dsn=os.environ.get("EXA_DSN", "localhost:8563"),
        user=os.environ.get("EXA_USER", "sys"),
        password=os.environ.get("EXA_PASSWORD", "exasol"),
        encryption=True,
        compression=True,
    )
    if not verify:
        kwargs["websocket_sslopt"] = {"cert_reqs": ssl.CERT_NONE}
    if schema:
        kwargs["schema"] = schema
    kwargs.update(extra)
    return pyexasol.connect(**kwargs)

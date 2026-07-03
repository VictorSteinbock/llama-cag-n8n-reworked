"""Tests for the stdlib-only root CLI (llamacag.py).

Placed under api/tests/ so the existing `pytest api` job collects it; the shim
imports the repo-root module (llamacag is pure stdlib, so no extra deps). F6's
test_prepare.py reuses this same shim.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import llamacag  # noqa: E402


def _stub_status_env(monkeypatch):
    """Neutralize the docker/compose/resource parts so a test can drive just the
    HTTP portion of cmd_status."""
    monkeypatch.setattr(llamacag, "docker_ready", lambda: True)
    monkeypatch.setattr(llamacag, "run_compose", lambda *a, **k: None)
    monkeypatch.setattr(llamacag, "read_env", lambda: {})
    monkeypatch.setattr(llamacag, "print_resource_snapshot", lambda: None)


def test_status_survives_stats_outage(monkeypatch):
    _stub_status_env(monkeypatch)

    def boom(url, timeout=5.0):
        raise OSError("connection refused")

    monkeypatch.setattr(llamacag, "http_get", boom)

    # Health checks and the /stats one-liner both fail, but status is a nicety:
    # cmd_status must still succeed.
    assert llamacag.cmd_status(argparse.Namespace()) == 0


def test_status_prints_usage_line(monkeypatch, capsys):
    _stub_status_env(monkeypatch)
    stats_body = json.dumps({
        "windows": {"24h": {"queries": 3}, "all": {"tokens_reused": 123456}},
        "savings": {"estimated_usd": None},
    })

    def ok(url, timeout=5.0):
        if url.endswith("/stats"):
            return 200, stats_body
        return 200, '{"status": "ok"}'

    monkeypatch.setattr(llamacag, "http_get", ok)

    assert llamacag.cmd_status(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "usage: 3 queries/24h" in out
    assert "123,456 tokens reused all-time" in out

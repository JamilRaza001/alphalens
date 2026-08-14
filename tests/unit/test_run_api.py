"""Unit tests for scripts/run_api.py (S18) -- the dev entrypoint.

``uvicorn.run`` is monkeypatched, so nothing binds a socket and nothing cold-starts the agent.
``scripts`` is importable as an implicit namespace package from the repo root.
"""

from __future__ import annotations

from typing import Any

import pytest


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import scripts.run_api as entrypoint

    captured: dict[str, Any] = {}

    def _fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(entrypoint.uvicorn, "run", _fake_run)
    entrypoint.main()
    return captured


def test_defaults_to_loopback_port_8000(monkeypatch: pytest.MonkeyPatch) -> None:
    """D7: local dev only -- loopback, never 0.0.0.0."""
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.delenv("API_PORT", raising=False)

    captured = _capture(monkeypatch)

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000


def test_host_and_port_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_HOST", "127.0.0.2")
    monkeypatch.setenv("API_PORT", "8123")

    captured = _capture(monkeypatch)

    assert captured["host"] == "127.0.0.2"
    assert captured["port"] == 8123  # int, not str -- uvicorn rejects a string port


def test_serves_the_app_factory_without_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """`factory=True` so uvicorn calls `create_app()` itself; `reload=False` so a file save does
    not re-run the lifespan and re-load the ~80 MB reranker."""
    captured = _capture(monkeypatch)

    assert captured["app"] == "alphalens.api.app:create_app"
    assert captured["factory"] is True
    assert captured["reload"] is False


def test_import_does_not_start_a_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment is read inside `main()`, never at import time (AC1's discipline).

    Re-executing the module body must call nothing: a stray `uvicorn.run(...)` or `os.environ`
    read at module scope would make `import scripts.run_api` bind a socket.
    """
    import importlib

    import scripts.run_api as entrypoint

    calls: list[Any] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("API_PORT", "not-an-int")  # would raise if read at import time

    importlib.reload(entrypoint)

    assert calls == []

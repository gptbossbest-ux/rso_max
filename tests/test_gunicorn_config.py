from __future__ import annotations

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(monkeypatch, **values):
    for name in ("WEB_WORKERS", "WEB_THREADS", "WEB_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    return runpy.run_path(str(ROOT / "gunicorn.conf.py"))


def test_gunicorn_defaults_use_both_cpu_workers(monkeypatch):
    settings = _load(monkeypatch)
    assert (settings["workers"], settings["threads"], settings["timeout"]) == (2, 4, 60)
    assert settings["bind"] == "0.0.0.0:5000"


@pytest.mark.parametrize(("name", "value"), [
    ("WEB_WORKERS", "two"), ("WEB_WORKERS", 0), ("WEB_WORKERS", 17),
    ("WEB_THREADS", 0), ("WEB_THREADS", 65),
    ("WEB_TIMEOUT", 9), ("WEB_TIMEOUT", 601),
])
def test_gunicorn_invalid_values_fail_fast(monkeypatch, name, value):
    with pytest.raises(RuntimeError, match=name):
        _load(monkeypatch, **{name: value})


def test_compose_uses_config_file_and_keeps_api_single_worker():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert 'command: ["gunicorn", "-c", "gunicorn.conf.py", "web:app"]' in compose
    assert "WEB_WORKERS:" not in compose
    assert "WEB_THREADS:" not in compose
    assert "WEB_TIMEOUT:" not in compose
    assert "${APP_RUNTIME_ENV_FILE:-.env.runtime}" in compose
    assert compose.index("${APP_ENV_FILE:-.env}") < compose.index(
        "${APP_RUNTIME_ENV_FILE:-.env.runtime}",
    )
    assert "uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 1" in compose


def test_runtime_env_values_win_without_root_compose_interpolation(monkeypatch):
    settings = _load(
        monkeypatch, WEB_WORKERS=3, WEB_THREADS=6, WEB_TIMEOUT=90,
    )
    assert (settings["workers"], settings["threads"], settings["timeout"]) == (3, 6, 90)

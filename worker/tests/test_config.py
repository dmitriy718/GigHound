"""Config tests: env parsing, token validation, per-platform proxy lookup."""
import pytest

from worker.config import Config, SUPPORTED_PLATFORMS


def test_defaults(monkeypatch):
    for var in ("GIGHOUND_WORKER_TOKEN", "WORKER_ID", "WORKER_PLATFORMS",
                "WORKER_HEADLESS", "WORKER_ALLOW_SUBMIT"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config(worker_token="t")
    assert cfg.api_url == "http://localhost:8000"
    assert cfg.headless is True
    assert cfg.allow_submit is False
    assert cfg.platforms == SUPPORTED_PLATFORMS
    assert cfg.worker_id  # hostname-pid default


def test_platforms_from_env(monkeypatch):
    monkeypatch.setenv("WORKER_PLATFORMS", "fiverr, upwork")
    cfg = Config(worker_token="t")
    assert cfg.platforms == ("fiverr", "upwork")


def test_validate_requires_token():
    with pytest.raises(RuntimeError, match="GIGHOUND_WORKER_TOKEN"):
        Config(worker_token="").validate()


def test_validate_rejects_unknown_platform():
    with pytest.raises(RuntimeError, match="unsupported"):
        Config(worker_token="t", platforms=("fiverr", "myspace")).validate()


def test_proxy_per_platform(monkeypatch):
    monkeypatch.setenv("WORKER_PROXY_UPWORK", "http://u:p@host:8080")
    cfg = Config(worker_token="t")
    assert cfg.proxy_for("upwork") == "http://u:p@host:8080"
    assert cfg.proxy_for("fiverr") is None


def test_context_idle_sec_from_env(monkeypatch):
    monkeypatch.delenv("WORKER_CONTEXT_IDLE_SEC", raising=False)
    assert Config(worker_token="t").context_idle_sec == 1800
    monkeypatch.setenv("WORKER_CONTEXT_IDLE_SEC", "300")
    assert Config(worker_token="t").context_idle_sec == 300


def test_task_timeout_sec_from_env(monkeypatch):
    monkeypatch.delenv("WORKER_TASK_TIMEOUT_SEC", raising=False)
    assert Config(worker_token="t").task_timeout_sec == 600
    monkeypatch.setenv("WORKER_TASK_TIMEOUT_SEC", "120")
    assert Config(worker_token="t").task_timeout_sec == 120


def test_allow_submit_per_platform_override_wins(monkeypatch):
    monkeypatch.delenv("WORKER_ALLOW_SUBMIT_UPWORK", raising=False)
    cfg = Config(worker_token="t", allow_submit=False)
    assert cfg.allow_submit_for("upwork") is False
    monkeypatch.setenv("WORKER_ALLOW_SUBMIT_UPWORK", "1")
    assert cfg.allow_submit_for("upwork") is True
    assert cfg.allow_submit_for("fiverr") is False  # other platforms unaffected


def test_allow_submit_global_fallback_and_explicit_off(monkeypatch):
    cfg = Config(worker_token="t", allow_submit=True)
    # empty override (e.g. compose passthrough default) means unset → global
    monkeypatch.setenv("WORKER_ALLOW_SUBMIT_FIVERR", "")
    assert cfg.allow_submit_for("fiverr") is True
    # an explicit per-platform off wins over the global on
    monkeypatch.setenv("WORKER_ALLOW_SUBMIT_FIVERR", "0")
    assert cfg.allow_submit_for("fiverr") is False

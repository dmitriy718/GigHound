"""Worker configuration from environment (see worker/README.md)."""
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# platforms the worker can serve; keep in sync with worker/platforms.py
SUPPORTED_PLATFORMS = ("fiverr", "upwork", "peopleperhour", "guru")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    api_url: str = field(
        default_factory=lambda: os.getenv("GIGHOUND_API_URL", "http://localhost:8000"))
    worker_token: str = field(
        default_factory=lambda: os.getenv("GIGHOUND_WORKER_TOKEN", ""))
    worker_id: str = field(default_factory=lambda: os.getenv(
        "WORKER_ID", f"{socket.gethostname()}-{os.getpid()}"))
    platforms: tuple[str, ...] = field(default_factory=lambda: tuple(
        p.strip() for p in os.getenv("WORKER_PLATFORMS", "").split(",")
        if p.strip()) or SUPPORTED_PLATFORMS)
    headless: bool = field(default_factory=lambda: _bool("WORKER_HEADLESS", True))
    session_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "WORKER_SESSION_DIR", str(Path(__file__).parent / ".sessions"))))
    # final-submit gate for manual-assist handlers (see README safety model);
    # global default — WORKER_ALLOW_SUBMIT_<PLATFORM> overrides per platform
    allow_submit: bool = field(default_factory=lambda: _bool("WORKER_ALLOW_SUBMIT", False))
    # per-task wall-clock budget, enforced at pacing checkpoints (worker loop
    # is single-threaded; see browser.arm_task_deadline)
    task_timeout_sec: float = field(default_factory=lambda: float(
        os.getenv("WORKER_TASK_TIMEOUT_SEC", "600")))
    poll_interval_sec: float = field(default_factory=lambda: float(
        os.getenv("WORKER_POLL_INTERVAL_SEC", "45")))
    poll_jitter_sec: float = field(default_factory=lambda: float(
        os.getenv("WORKER_POLL_JITTER_SEC", "15")))
    # idle browser contexts are closed (and rebuilt on demand) after this long
    context_idle_sec: float = field(default_factory=lambda: float(
        os.getenv("WORKER_CONTEXT_IDLE_SEC", "1800")))
    timezone: str = field(default_factory=lambda: os.getenv(
        "WORKER_TIMEZONE", "America/New_York"))
    locale: str = field(default_factory=lambda: os.getenv("WORKER_LOCALE", "en-US"))
    # circadian window, hours in `timezone` ("8-23"); "" / "off" disables it
    active_hours: str = field(default_factory=lambda: os.getenv(
        "WORKER_ACTIVE_HOURS", "8-23"))

    def active_hours_window(self) -> tuple[int, int] | None:
        """Parse active_hours "8-23" → (8, 23); None disables the gate."""
        raw = (self.active_hours or "").strip()
        if not raw or raw.lower() in ("off", "none", "always"):
            return None
        try:
            lo_s, hi_s = raw.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            raise RuntimeError(
                f"WORKER_ACTIVE_HOURS must look like '8-23', got {raw!r}")
        if not (0 <= lo < hi <= 24):
            raise RuntimeError(
                f"WORKER_ACTIVE_HOURS must satisfy 0 <= start < end <= 24, got {raw!r}")
        return lo, hi

    def proxy_for(self, platform: str) -> str | None:
        """Per-platform proxy URL, e.g. WORKER_PROXY_UPWORK=http://user:pass@host:port"""
        return os.getenv(f"WORKER_PROXY_{platform.upper()}") or None

    def allow_submit_for(self, platform: str) -> bool:
        """Per-platform final-submit gate: WORKER_ALLOW_SUBMIT_<PLATFORM>
        (e.g. WORKER_ALLOW_SUBMIT_UPWORK=1) wins; the global
        WORKER_ALLOW_SUBMIT is the fallback default."""
        v = os.getenv(f"WORKER_ALLOW_SUBMIT_{platform.upper()}")
        if v:  # an empty value (e.g. compose passthrough default) means unset
            return v.lower() in ("1", "true", "yes", "on")
        return self.allow_submit

    def validate(self) -> None:
        if not self.worker_token:
            raise RuntimeError(
                "GIGHOUND_WORKER_TOKEN is not set — the worker cannot "
                "authenticate against the backend. Set it (same value as the "
                "backend's) in the environment or .env."
            )
        unknown = set(self.platforms) - set(SUPPORTED_PLATFORMS)
        if unknown:
            raise RuntimeError(
                f"WORKER_PLATFORMS contains unsupported platforms: {sorted(unknown)} "
                f"(supported: {list(SUPPORTED_PLATFORMS)})"
            )


def load_config() -> Config:
    cfg = Config()
    cfg.validate()
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    return cfg

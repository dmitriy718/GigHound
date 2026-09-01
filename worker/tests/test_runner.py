"""Runner tests: claim-before-execute ordering, CAPTCHA escalation shape,
crash safety. Client and handlers are fakes; no browser."""
import httpx
import pytest

from worker import runner
from worker.browser import CaptchaDetectedError
from worker.client import ClaimConflictError, StealthTaskOut
from worker.handlers.base import HandlerContext


class FakeClient:
    def __init__(self, conflict_on=(), complete_error=None):
        self.calls = []
        self.conflict_on = set(conflict_on)
        self.completed = []
        self.complete_error = complete_error

    def claim_task(self, task_id):
        self.calls.append(("claim", task_id))
        if task_id in self.conflict_on:
            raise ClaimConflictError("already claimed")
        return StealthTaskOut(id=task_id, user_id=1, platform="fiverr",
                              task_type=self.task_types.get(task_id, "scrape_gig_metrics"),
                              payload={})

    def complete_task(self, task_id, success, result):
        self.calls.append(("complete", task_id))
        if self.complete_error is not None:
            raise self.complete_error
        self.completed.append({"id": task_id, "success": success, "result": result})
        return {"id": task_id, "status": "done" if success else "failed"}

    task_types = {}


class FakeBrowser:
    def __init__(self):
        self.reaps = 0
        self.pages_closed = []

    def reap_idle_contexts(self):
        self.reaps += 1

    def close_pages(self, platform, user_id):
        self.pages_closed.append((platform, user_id))


def make_ctx(client):
    from worker.config import Config
    # active_hours="" disables the circadian gate so these tests are
    # deterministic regardless of the wall clock
    return HandlerContext(config=Config(worker_token="t", active_hours=""),
                          client=client, browser=FakeBrowser())


def ok_handler(task, ctx):
    return {"scraped": []}


def test_claim_before_execute(monkeypatch):
    client = FakeClient()
    order = []

    def handler(task, ctx):
        order.append("handler")
        return {}

    monkeypatch.setattr(runner, "get_handler", lambda tt: handler)
    runner.process_task(StealthTaskOut(id=1, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"), make_ctx(client))
    assert client.calls == [("claim", 1), ("complete", 1)]
    assert order == ["handler"]  # executed only after a successful claim
    assert client.completed[0]["success"] is True


def test_claim_conflict_skips_execution(monkeypatch):
    client = FakeClient(conflict_on={1})
    called = []
    monkeypatch.setattr(runner, "get_handler",
                        lambda tt: lambda t, c: called.append(t.id) or {})
    runner.process_task(StealthTaskOut(id=1, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"), make_ctx(client))
    assert called == [] and client.completed == []


def test_captcha_escalation_result_shape(monkeypatch):
    client = FakeClient()

    def handler(task, ctx):
        raise CaptchaDetectedError("#challenge-running", "fiverr")

    monkeypatch.setattr(runner, "get_handler", lambda tt: handler)
    runner.process_task(StealthTaskOut(id=2, user_id=1, platform="fiverr",
                                       task_type="fetch_buyer_requests"), make_ctx(client))
    outcome = client.completed[0]
    assert outcome["success"] is False
    assert outcome["result"]["captcha"] is True
    assert outcome["result"]["marker"] == "#challenge-running"
    assert outcome["result"]["platform"] == "fiverr"


def test_handler_crash_reports_failure(monkeypatch):
    client = FakeClient()

    def handler(task, ctx):
        raise RuntimeError("selector drifted")

    monkeypatch.setattr(runner, "get_handler", lambda tt: handler)
    runner.process_task(StealthTaskOut(id=3, user_id=1, platform="fiverr",
                                       task_type="create_gig_draft"), make_ctx(client))
    outcome = client.completed[0]
    assert outcome["success"] is False
    assert "selector drifted" in outcome["result"]["error"]
    assert outcome["result"]["error_type"] == "RuntimeError"


def test_pages_closed_after_task(monkeypatch):
    """Task hygiene: the task's pages are closed whether it succeeds or
    fails, so leftover DOM state never leaks into the next task."""
    client = FakeClient()
    ctx = make_ctx(client)
    monkeypatch.setattr(runner, "get_handler", lambda tt: ok_handler)
    runner.process_task(StealthTaskOut(id=10, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"), ctx)
    assert ctx.browser.pages_closed == [("fiverr", 1)]

    def crashing(task, ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "get_handler", lambda tt: crashing)
    runner.process_task(StealthTaskOut(id=11, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"), ctx)
    assert ctx.browser.pages_closed == [("fiverr", 1), ("fiverr", 1)]


def test_task_timeout_reports_failure(monkeypatch):
    """A handler that burns past WORKER_TASK_TIMEOUT_SEC is cut off at the
    next pacing checkpoint and reported as a timeout failure."""
    from worker import browser
    client = FakeClient()

    def handler(task, ctx):
        browser._real_sleep(0.2)  # e.g. wedged mid typing-plan
        browser._sleep(0)  # the next pacing checkpoint raises TaskTimeoutError

    monkeypatch.setattr(runner, "get_handler", lambda tt: handler)
    ctx = make_ctx(client)
    ctx.config.task_timeout_sec = 0.05
    runner.process_task(StealthTaskOut(id=12, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"), ctx)
    outcome = client.completed[0]
    assert outcome["success"] is False
    assert outcome["result"] == {"error": "task timeout", "timeout_sec": 0.05}
    assert browser._TASK_DEADLINE is None  # disarmed for the next task
    assert ctx.browser.pages_closed == [("fiverr", 1)]


def test_unknown_task_type_fails_task(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(runner, "get_handler", lambda tt: None)
    runner.process_task(StealthTaskOut(id=4, user_id=1, platform="guru",
                                       task_type="mystery"), make_ctx(client))
    outcome = client.completed[0]
    assert outcome["success"] is False
    assert "no handler" in outcome["result"]["error"]


def test_poll_once_iterates_platforms(monkeypatch):
    client = FakeClient()
    polled = []

    def poll(platform):
        polled.append(platform)
        return [StealthTaskOut(id=9, user_id=1, platform=platform,
                               task_type="scrape_gig_metrics")]

    client.poll_tasks = poll
    monkeypatch.setattr(runner, "get_handler", lambda tt: ok_handler)
    ctx = make_ctx(client)
    ctx.config.platforms = ("fiverr", "upwork")
    assert runner.poll_once(ctx) == 2
    assert polled == ["fiverr", "upwork"]
    assert ctx.browser.reaps == 1  # idle contexts reaped at sweep start


def test_success_path_claim_conflict_is_benign(monkeypatch):
    # handler already finalized the task server-side → complete() 409s
    client = FakeClient(complete_error=ClaimConflictError("already done"))
    monkeypatch.setattr(runner, "get_handler", lambda tt: ok_handler)
    runner.process_task(StealthTaskOut(id=5, user_id=1, platform="fiverr",
                                       task_type="scrape_proposal_status"),
                        make_ctx(client))  # must not raise
    assert client.calls == [("claim", 5), ("complete", 5)]


def test_success_path_transport_error_is_tolerated(monkeypatch):
    client = FakeClient(complete_error=httpx.ConnectError("backend down"))
    monkeypatch.setattr(runner, "get_handler", lambda tt: ok_handler)
    runner.process_task(StealthTaskOut(id=6, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"),
                        make_ctx(client))  # must not raise
    assert client.calls == [("claim", 6), ("complete", 6)]


def test_failure_report_transport_error_is_tolerated(monkeypatch):
    client = FakeClient(complete_error=httpx.ConnectError("backend down"))

    def handler(task, ctx):
        raise RuntimeError("selector drifted")

    monkeypatch.setattr(runner, "get_handler", lambda tt: handler)
    runner.process_task(StealthTaskOut(id=7, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"),
                        make_ctx(client))  # must not raise


def test_poll_once_tolerates_transport_error(monkeypatch):
    client = FakeClient()

    def poll(platform):
        raise httpx.ConnectError("backend down")

    client.poll_tasks = poll
    ctx = make_ctx(client)
    ctx.config.platforms = ("fiverr", "upwork")
    assert runner.poll_once(ctx) == 0  # must not raise


def test_poll_once_survives_bad_task(monkeypatch):
    client = FakeClient()

    def poll(platform):
        return [StealthTaskOut(id=8, user_id=1, platform=platform,
                               task_type="scrape_gig_metrics"),
                StealthTaskOut(id=9, user_id=1, platform=platform,
                               task_type="scrape_gig_metrics")]

    client.poll_tasks = poll
    processed = []

    def fake_process(task, ctx):
        processed.append(task.id)
        if task.id == 8:
            raise ValueError("boom")  # e.g. claim blew up outside the guards

    monkeypatch.setattr(runner, "process_task", fake_process)
    ctx = make_ctx(client)
    ctx.config.platforms = ("fiverr",)
    assert runner.poll_once(ctx) == 2  # second task still processed
    assert processed == [8, 9]


# ---------------- circadian active-hours gate ----------------

def test_circadian_gate_window_boundaries():
    from worker.config import Config
    cfg = Config(worker_token="t", active_hours="8-23")
    assert runner._may_run_now("scrape_gig_metrics", cfg, hour=10) is True
    assert runner._may_run_now("scrape_gig_metrics", cfg, hour=3) is False
    assert runner._may_run_now("scrape_gig_metrics", cfg, hour=8) is True   # inclusive
    assert runner._may_run_now("scrape_gig_metrics", cfg, hour=23) is False  # exclusive
    # legacy alias resolves to a scrape kind and is gated too
    assert runner._may_run_now("gig_scrape_metrics", cfg, hour=3) is False


def test_circadian_gate_always_allows_submits():
    from worker.config import Config
    cfg = Config(worker_token="t", active_hours="8-23")
    for kind in ("submit_upwork_proposal", "submit_fiverr_offer", "submit_proposal"):
        assert runner._may_run_now(kind, cfg, hour=3) is True
    # disabled window → everything runs
    cfg_off = Config(worker_token="t", active_hours="")
    assert runner._may_run_now("scrape_gig_metrics", cfg_off, hour=3) is True


def test_active_hours_window_parsing():
    from worker.config import Config
    assert Config(worker_token="t", active_hours="8-23").active_hours_window() == (8, 23)
    assert Config(worker_token="t", active_hours="").active_hours_window() is None
    assert Config(worker_token="t", active_hours="off").active_hours_window() is None
    with pytest.raises(RuntimeError, match="WORKER_ACTIVE_HOURS"):
        Config(worker_token="t", active_hours="soonish").active_hours_window()
    with pytest.raises(RuntimeError, match="WORKER_ACTIVE_HOURS"):
        Config(worker_token="t", active_hours="23-8").active_hours_window()


def test_poll_once_skips_scrapes_outside_window_runs_submits(monkeypatch):
    from worker.config import Config
    client = FakeClient()
    client.poll_tasks = lambda platform: [
        StealthTaskOut(id=1, user_id=1, platform=platform,
                       task_type="scrape_gig_metrics"),
        StealthTaskOut(id=2, user_id=1, platform=platform,
                       task_type="submit_upwork_proposal"),
    ]
    processed = []
    monkeypatch.setattr(runner, "process_task",
                        lambda t, c: processed.append((t.id, t.task_type)))
    # pin the clock to 3am — outside the default 8-23 window
    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            from datetime import datetime
            return datetime(2026, 9, 1, 3, 0, tzinfo=tz)

    monkeypatch.setattr(runner, "datetime", _FakeDatetime)
    ctx = HandlerContext(config=Config(worker_token="t", active_hours="8-23"),
                         client=client, browser=FakeBrowser())
    ctx.config.platforms = ("fiverr",)
    assert runner.poll_once(ctx) == 1
    assert processed == [(2, "submit_upwork_proposal")]
    assert ("claim", 1) not in client.calls  # scrape never claimed, stays queued

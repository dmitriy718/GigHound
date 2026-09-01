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
    pass


def make_ctx(client):
    from worker.config import Config
    return HandlerContext(config=Config(worker_token="t"), client=client,
                          browser=FakeBrowser())


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

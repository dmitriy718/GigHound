"""Client tests: auth header, retry/backoff, claim conflict, payload parsing.
No browser involved — transport is httpx.MockTransport."""
import httpx
import pytest

from worker.client import (BackendError, ClaimConflictError, StealthTaskOut,
                           WorkerClient)


def make_client(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return WorkerClient("http://backend", "secret-token", "w-1",
                        client=httpx.Client(
                            base_url="http://backend", transport=transport,
                            headers={"Authorization": "Bearer secret-token"}),
                        **kwargs)


def test_auth_header_sent():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])

    make_client(handler).poll_tasks("fiverr")
    assert seen["auth"] == "Bearer secret-token"


def test_poll_parses_tasks():
    def handler(request):
        assert request.url.params["platform"] == "upwork"
        assert request.url.params["status"] == "pending"
        return httpx.Response(200, json=[{
            "id": 7, "user_id": 3, "platform": "upwork",
            "task_type": "submit_upwork_proposal",
            "payload": {"job_url": "https://x"}, "status": "pending",
        }])

    tasks = make_client(handler).poll_tasks("upwork")
    assert tasks == [StealthTaskOut(id=7, user_id=3, platform="upwork",
                                    task_type="submit_upwork_proposal",
                                    payload={"job_url": "https://x"})]


def test_retry_on_5xx_then_success(monkeypatch):
    monkeypatch.setattr("worker.client.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=[])

    tasks = make_client(handler, max_retries=3).poll_tasks("fiverr")
    assert tasks == [] and calls["n"] == 3


def test_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr("worker.client.time.sleep", lambda *_: None)

    def handler(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(httpx.HTTPStatusError):
        make_client(handler, max_retries=2).poll_tasks("fiverr")


def test_claim_conflict_raises():
    def handler(request):
        return httpx.Response(409, json={"detail": "stealth task already claimed"})

    with pytest.raises(ClaimConflictError):
        make_client(handler).claim_task(1)


def test_4xx_raises_backend_error():
    def handler(request):
        return httpx.Response(422, json={"detail": "user_id required"})

    with pytest.raises(BackendError):
        make_client(handler).post_buyer_requests(1, [])


def test_complete_and_result_posts():
    sent = []

    def handler(request):
        sent.append((request.method, request.url.path, request.read()))
        if request.url.path.endswith("/complete"):
            return httpx.Response(200, json={"id": 1, "status": "done"})
        return httpx.Response(200, json={"queued": 2})

    client = make_client(handler)
    assert client.complete_task(1, True, {"fetched": 2})["status"] == "done"
    assert b'"worker_id": "w-1"' in sent[0][2] or b'"worker_id":"w-1"' in sent[0][2]
    assert client.post_buyer_requests(5, [{"title": "x"}])["queued"] == 2
    assert b'"user_id": 5' in sent[-1][2] or b'"user_id":5' in sent[-1][2]


def test_get_stealth_session():
    state = {"cookies": [{"name": "s", "value": "v"}], "origins": []}

    def handler(request):
        assert request.url.path == "/api/gigs/stealth-session"
        assert request.url.params["platform"] == "fiverr"
        assert request.url.params["user_id"] == "7"
        return httpx.Response(200, json={"storage_state": state,
                                         "credentials_present": True})

    session = make_client(handler).get_stealth_session("fiverr", 7)
    assert session == {"storage_state": state, "credentials_present": True}


def test_get_stealth_session_absent():
    def handler(request):
        return httpx.Response(200, json={"storage_state": None,
                                         "credentials_present": False})

    session = make_client(handler).get_stealth_session("guru", 3)
    assert session["storage_state"] is None
    assert session["credentials_present"] is False

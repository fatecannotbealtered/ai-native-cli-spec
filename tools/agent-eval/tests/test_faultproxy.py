"""Gap 4: the fault proxy for lost-reply, in process and as the detached CLI lane.py starts, and its wiring."""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse

import pytest

import collect
import faultproxy
import lane
import plan
from config import CFG, ConfigError, write_private
from conftest import planned_trials

NOTE = "/api/v4/projects/1/issues/14/notes"
RULE = {"method": "POST", "path": r"/issues/14/notes$"}
DROPPED = (http.client.HTTPException, ConnectionError, OSError)  # how a client sees a reply that never came


def conn(url: str) -> http.client.HTTPConnection:
    u = urllib.parse.urlsplit(url)
    return http.client.HTTPConnection(u.hostname, u.port, timeout=10)


def request(url: str, method: str, path: str, body=None, **kw):
    c = conn(url)
    try:
        c.request(method, path, body=body, **kw)
        r = c.getresponse()
        return r.status, r.read(), r
    finally:
        c.close()


@pytest.fixture
def proxy(upstream, tmp_path):
    p = faultproxy.FaultProxy(upstream.url, RULE, log_path=tmp_path / "fault-proxy.json").start()
    yield p
    p.stop()


def test_forwards_reads_on_a_keep_alive_connection(proxy, upstream):
    c = conn(proxy.url)
    for path in ("/api/v4/projects?search=pay", "/api/v4/projects/1"):
        c.request("GET", path)
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read()) == {"method": "GET", "path": path}
    c.close()
    assert [s["path"] for s in upstream.seen] == ["/api/v4/projects?search=pay", "/api/v4/projects/1"]
    assert {s["host"] for s in upstream.seen} == {urllib.parse.urlsplit(upstream.url).netloc}
    assert proxy.log["dropped"] is None and proxy.log["request_count"] == 2


def test_drops_only_the_first_armed_write_after_the_upstream_got_it(proxy, upstream, tmp_path):
    body = b'{"body":"Reproduced on staging, investigating."}'
    with pytest.raises(DROPPED):
        request(proxy.url, "POST", NOTE, body, headers={"Content-Type": "application/json"})
    assert [(s["method"], s["path"], s["body"]) for s in upstream.seen] == [("POST", NOTE, body)]

    status, _, _ = request(proxy.url, "POST", NOTE, body)  # exactly once: a re-sent write goes through
    assert status == 201 and len(upstream.seen) == 2

    log = json.loads((tmp_path / "fault-proxy.json").read_text(encoding="utf-8"))
    assert log["dropped"]["method"] == "POST" and log["dropped"]["path"] == NOTE
    assert log["dropped"]["upstream_status"] == 201 and log["dropped"]["request_bytes"] == len(body)
    assert log["armed"] is False
    assert faultproxy.summary(log) == {
        "armed": True, "dropped": True, "matched_after_drop": 1, "requests": 2,
        "dropped_request": {"method": "POST", "path": NOTE, "upstream_status": 201}}


def test_other_requests_and_refused_writes_leave_it_armed(proxy, upstream):
    assert request(proxy.url, "POST", "/api/v4/projects/1/issues/15/notes", b"{}")[0] == 201  # path does not match
    assert request(proxy.url, "PUT", NOTE, b"{}")[0] == 201  # method does not match
    upstream.status[NOTE] = 422  # the upstream refuses the armed write: nothing changed, so pass it through
    assert request(proxy.url, "POST", NOTE, b"{}")[0] == 422
    assert proxy.log["armed"] is True and proxy.log["dropped"] is None
    upstream.status[NOTE] = 201
    with pytest.raises(DROPPED):
        request(proxy.url, "POST", NOTE, b"{}")
    assert proxy.log["dropped"]["seq"] == 4 and len(upstream.seen) == 4


def test_default_rule_matches_any_write(upstream, tmp_path):
    with faultproxy.FaultProxy(upstream.url, {}) as p:
        assert request(p.url, "GET", NOTE)[0] == 200
        assert request(p.url, "HEAD", NOTE)[0] == 200
        with pytest.raises(DROPPED):
            request(p.url, "DELETE", "/api/v4/projects/1/repository/branches/old-experiment")
        assert request(p.url, "DELETE", "/api/v4/projects/1/repository/branches/old-experiment")[0] == 201


def test_request_bodies_reach_the_upstream_whole(proxy, upstream):
    path = "/api/v4/projects/1/issues/2"
    chunks = [b'{"add_labels":', b'"needs-triage",', b'"x":"' + b"y" * 70000 + b'"}']
    assert request(proxy.url, "PUT", path, iter(chunks), encode_chunked=True)[0] == 201
    assert request(proxy.url, "PUT", path, b"plain body")[0] == 201
    assert request(proxy.url, "DELETE", path)[0] == 201
    (chunked, plain, empty) = upstream.seen
    assert chunked["body"] == b"".join(chunks) and chunked["transfer_encoding"] is None
    assert plain["body"] == b"plain body" and empty["body"] == b""


def test_concurrent_requests(proxy, upstream):
    results = []

    def get(i):
        results.append(request(proxy.url, "GET", f"/api/v4/projects/{i}")[0])

    threads = [threading.Thread(target=get, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert results == [200] * 24 and len(upstream.seen) == 24


def test_location_and_link_headers_point_back_at_the_proxy(proxy, upstream):
    upstream.headers["Link"] = '<{origin}/api/v4/projects?page=2>; rel="next"'
    _, _, r = request(proxy.url, "GET", "/api/v4/projects")
    assert r.getheader("Link") == f'<{proxy.url}/api/v4/projects?page=2>; rel="next"'


def test_stop_is_prompt_with_idle_keep_alive_connections(upstream, tmp_path):
    p = faultproxy.FaultProxy(upstream.url, RULE, log_path=tmp_path / "log.json").start()
    idle = conn(p.url)
    idle.request("GET", "/api/v4/version")
    idle.getresponse().read()  # the connection stays open, its handler waiting for the next request
    started = time.monotonic()
    log = p.stop()
    assert time.monotonic() - started < 3
    assert log["stopped_at"] and json.loads((tmp_path / "log.json").read_text(encoding="utf-8"))["stopped_at"]
    with pytest.raises(OSError):
        request(p.url, "GET", "/api/v4/version")
    idle.close()


def test_cli_process_started_and_stopped_like_lane_does(upstream, tmp_path):
    log_path, stop_path = tmp_path / "p.json", tmp_path / "p.stop"
    state = faultproxy.spawn(upstream.url, RULE, log_path, stop_path, max_seconds=60)
    try:
        assert state["arm"] == {"method": ["POST"], "path": RULE["path"]}
        assert state["tool_url"].startswith("http://127.0.0.1:")
        assert request(state["tool_url"], "GET", "/api/v4/projects/1")[0] == 200
        with pytest.raises(DROPPED):
            request(state["tool_url"], "POST", NOTE, b"{}")
    finally:
        final = faultproxy.halt(log_path, stop_path)
    assert final["stopped_at"] and final["dropped"]["upstream_status"] == 201
    assert [s["method"] for s in upstream.seen] == ["GET", "POST"]


def test_cli_rejects_bad_arguments(capsys):
    assert faultproxy.main(["--upstream", "ftp://example"]) == 2
    with pytest.raises(SystemExit):
        faultproxy.main(["--upstream", "http://127.0.0.1:9", "--arm-path", "x"])  # needs --arm


def test_lane_prep_points_the_trial_at_the_proxy_and_post_collects_the_log(upstream, monkeypatch):
    monkeypatch.setitem(CFG._values, "upstream_url", upstream.url)
    monkeypatch.setattr(lane, "PROXY_MAX_SECONDS", 60)
    wid = "lane1x001"
    ws = CFG.workspaces_dir / wid
    ws.mkdir(parents=True)
    (ws / "run-config.json").write_text(json.dumps({"real_bin": "bin"}), encoding="utf-8")
    try:
        tool_url = lane.start_proxy("lane1", wid, RULE)
        assert json.loads((ws / "run-config.json").read_text(encoding="utf-8")) == {
            "real_bin": "bin", "set_env": {"GITLAB_CLI_HOST": tool_url}}
        assert not (ws / "fault-proxy.json").exists()  # the live log stays out of the agent's workspace
        with pytest.raises(DROPPED):
            request(tool_url, "POST", NOTE, b"{}")
    finally:
        lane.stop_proxy("lane1", wid)
    log = json.loads((ws / "fault-proxy.json").read_text(encoding="utf-8"))
    assert log["stopped_at"] and log["dropped"]["path"] == NOTE
    assert json.loads((ws / "run-config.json").read_text(encoding="utf-8")) == {"real_bin": "bin"}
    assert lane.stop_proxy("lane1", "lane1x999") is None  # a trial without a proxy

    trial = {"wid": wid, "fault_proxy": RULE}
    assert collect.fault_proxy_log(CFG.runs_dir / "lane1", trial)["dropped"]["upstream_status"] == 201
    assert collect.fault_proxy_error(trial, log, [{"argv": ["x"], "trial_env": ["GITLAB_CLI_HOST"]}]) is None
    assert "rebuild" in collect.fault_proxy_error(trial, log, [{"argv": ["x"]}])  # an old recorder ignored it
    assert "no fault-proxy log" in collect.fault_proxy_error(trial, None, [])


def test_plan_marks_lost_reply_trials(eval_files, monkeypatch):
    monkeypatch.setattr(plan, "LOST_REPLY", {"T05-comment": RULE})
    args = ["--tasks", "T05-comment,T01-count-bugs", "--arms", "A", "--skill", "off", "--models", "m"]
    write_private(CFG.secrets_dir / "recorder.json", json.dumps({"set_env": {}, "trial_env": []}))
    with pytest.raises(ConfigError, match="trial_env"):  # a recorder config from before the proxy
        plan.main(["--batch", "lost0", "--prefix", "lo0x"] + args)

    write_private(CFG.secrets_dir / "recorder.json", json.dumps({"set_env": {}, "trial_env": ["GITLAB_CLI_HOST"]}))
    assert plan.main(["--batch", "lost1", "--prefix", "lo1x"] + args) == 0
    trials = planned_trials("lost1")
    assert trials[("T05-comment", "A")]["fault_proxy"] == RULE
    assert "fault_proxy" not in trials[("T01-count-bugs", "A")]

    monkeypatch.setattr(plan, "LOST_REPLY", {"T01-count-bugs": {}})
    with pytest.raises(ConfigError, match="write lane"):
        plan.main(["--batch", "lost2", "--prefix", "lo2x"] + args)
    monkeypatch.setattr(plan, "LOST_REPLY", {"T05-comment": {"path": "("}})
    with pytest.raises(ConfigError, match="LOST_REPLY"):
        plan.main(["--batch", "lost3", "--prefix", "lo3x"] + args)

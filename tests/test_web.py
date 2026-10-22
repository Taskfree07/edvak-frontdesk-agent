"""Web UI server: metadata, live event stream for a run, evaluation and state after a batch, saved-run replay.
Uses keyword-rules mode, so no network or API key is needed."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from frontdesk.config import Settings
from frontdesk.web.server import RunManager, make_handler


@pytest.fixture
def server(tmp_path):
    s = Settings(mode="rules")
    s.runs_dir = tmp_path / "runs"
    s.runs_dir.mkdir()
    (s.runs_dir / "latest").mkdir()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(RunManager(s), s))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", s
    httpd.shutdown()
    httpd.server_close()
    shutil.rmtree(s.runs_dir, ignore_errors=True)


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def stream(url):
    """Read the Server-Sent Events stream until the end event."""
    events = []
    with urllib.request.urlopen(url, timeout=60) as r:
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event: end"):
                break
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_page_and_meta_are_served_without_secrets(server):
    base, _ = server
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        assert b"Maplewood front desk" in r.read()
    m = get(base + "/api/meta")
    assert len(m["sample"]) == 40 and m["patients"] and set(m["keys"]) == {"gemini", "groq", "anthropic"}
    assert all(isinstance(v, bool) for v in m["keys"].values())          # never the key itself
    assert "M024" in m["labels"]


def test_single_message_streams_every_pipeline_step(server):
    base, _ = server
    run = post(base + "/api/run", {"kind": "single", "mode": "rules", "text": "Can I get my lisinopril refilled?",
                                   "contact": "555-0102"})["run"]
    types = [e["type"] for e in stream(f"{base}/api/runs/{run}/stream")]
    for t in ("run.started", "mcp.connected", "message.received", "sender.identified", "safety.checked",
              "understanding.ready", "tool.result", "actions.derived", "reply.composed", "reply.validated",
              "result.ready", "run.finished", "state.ready", "run.saved"):
        assert t in types, t
    assert "eval.ready" not in types                                     # a single new message has no label


def test_emergency_is_streamed_as_emergency(server):
    base, _ = server
    run = post(base + "/api/run", {"kind": "single", "mode": "rules", "text": "I want to hang myself",
                                   "contact": "555-0102"})["run"]
    res = next(e for e in stream(f"{base}/api/runs/{run}/stream") if e["type"] == "result.ready")
    assert res["data"]["result"]["actions"] == ["escalate_emergency"]


def test_sample_subset_is_scored_and_human_work_listed(server):
    base, _ = server
    run = post(base + "/api/run", {"kind": "sample", "mode": "rules", "ids": "M003, M001, M024"})["run"]
    evs = stream(f"{base}/api/runs/{run}/stream")
    ev = next(e for e in evs if e["type"] == "eval.ready")["data"]
    assert ev["summary"]["messages"] == 3 and ev["summary"]["emergency_recall"] == 1.0
    assert ev["summary"]["must_not_violations"] == 0
    st = next(e for e in evs if e["type"] == "state.ready")["data"]
    assert "clinician_urgent" in st["queues"] and st["refill_requests"]


def test_saved_runs_can_be_listed_and_replayed(server):
    base, _ = server
    run = post(base + "/api/run", {"kind": "single", "mode": "rules", "text": "What are your clinic hours?",
                                   "contact": "555-0123"})["run"]
    stream(f"{base}/api/runs/{run}/stream")
    assert any(r["run"] == run for r in get(base + "/api/runs"))
    detail = get(f"{base}/api/runs/{run}")
    assert detail["meta"]["messages"] == 1 and any(e["type"] == "result.ready" for e in detail["events"])


def test_bad_requests_and_path_traversal_are_refused(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/run", {"kind": "single", "text": "  "})
    assert e.value.code == 400
    for path in ("/api/runs/../../.env", "/api/runs/..%2F..%2F.env", "/../.env", "/%2e%2e/.env"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + path)
        assert e.value.code == 404

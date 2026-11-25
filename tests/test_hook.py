import io
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from plan_review import hook
from plan_review.server import ReviewServer

PLAN = """# Add caching

## Context
Lookups are slow.

## Tasks

### T1. Add the cache
labels: blast=module · undo=easy

Wrap lookup in an LRU cache.
"""


def _input(tmp_path, markdown=PLAN):
    plan_file = tmp_path / "plans" / "happy-blue-whale.md"
    plan_file.parent.mkdir(exist_ok=True)
    plan_file.write_text(markdown)
    return {"tool_name": "ExitPlanMode", "session_id": "s1", "tool_input": {"plan": markdown, "planFilePath": str(plan_file)}}


class FakeReviewer:
    """Stands in for the browser: records payloads, returns scripted submissions."""

    def __init__(self, *submissions):
        self.submissions = list(submissions)
        self.payloads = []

    def __call__(self, payload, save_draft):
        self.payloads.append(payload)
        save_draft({"general": "half-typed"})
        return self.submissions.pop(0)


def test_revise_then_reply_then_approve(tmp_path):
    state_dir = tmp_path / "state"
    reviewer = FakeReviewer(
        {"decision": "revise", "comments": [{"target": "T1", "text": "Which eviction policy?", "verdict": "question"}]},
        {"decision": "approve", "comments": [{"target": "T1", "text": "Log cache hits too."}]},
    )

    out = hook.run(_input(tmp_path), state_dir, reviewer)
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "c1 on T1 (Add the cache) [question]" in reason and "Which eviction policy?" in reason

    revised = PLAN + "\n## Replies\n- c1: LRU with 1000 entries.\n"
    out = hook.run(_input(tmp_path, revised), state_dir, reviewer)
    second = reviewer.payloads[1]
    assert second["round"] == 2
    thread = second["threads"][0]
    assert [m["author"] for m in thread["messages"]] == ["you", "claude"]
    assert thread["messages"][1]["text"] == "LRU with 1000 entries."
    assert second["draft"] is None  # round 1's draft is cleared once it is submitted
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "allow"
    approved = decision["updatedInput"]["plan"]
    assert "## Replies" not in approved
    assert "## Reviewer notes" in approved and "Log cache hits too." in approved
    assert decision["updatedInput"]["planFilePath"].endswith("happy-blue-whale.md")

    key_dir = state_dir / "happy-blue-whale"
    assert (key_dir / "round-1.md").exists() and (key_dir / "round-2.md").exists()
    assert not (key_dir / "draft.json").exists()


def test_closed_page_and_terminal_fall_back(tmp_path):
    assert hook.run(_input(tmp_path), tmp_path / "s", lambda p, d: None) is None
    assert hook.run(_input(tmp_path), tmp_path / "s", lambda p, d: {"decision": "terminal"}) is None


def test_other_tools_and_missing_plan_are_ignored(tmp_path):
    called = []
    serve = lambda p, d: called.append(p)  # noqa: E731
    assert hook.run({"tool_name": "Bash", "tool_input": {}}, tmp_path, serve) is None
    assert hook.run({"tool_name": "ExitPlanMode", "tool_input": {}}, tmp_path, serve) is None
    assert called == []


def test_plan_read_from_file_when_not_in_input(tmp_path):
    data = _input(tmp_path)
    del data["tool_input"]["plan"]
    reviewer = FakeReviewer({"decision": "approve"})
    out = hook.run(data, tmp_path / "s", reviewer)
    assert reviewer.payloads[0]["title"] == "Add caching"
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_main_fails_open_on_corrupt_state(tmp_path, monkeypatch, capsys):
    data = _input(tmp_path)
    state_dir = tmp_path / "state"
    (state_dir / "happy-blue-whale").mkdir(parents=True)
    (state_dir / "happy-blue-whale" / "state.json").write_text("{not json")
    monkeypatch.setenv("PLAN_REVIEW_STATE_DIR", str(state_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""
    assert "error, falling back" in (state_dir / "hook.log").read_text()


def test_main_fails_open_on_bad_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLAN_REVIEW_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


# ---------- Real HTTP server ----------


def _request(url, data=None, host=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if host:
        req.add_header("Host", host)
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, res.read()


def _serve_in_background(server, **timeouts):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("value", server.wait(**timeouts)))
    t.start()
    time.sleep(0.2)
    return t, result


def test_server_round_trip():
    drafts = []
    server = ReviewServer({"title": "P"}, drafts.append)
    t, result = _serve_in_background(server, total_timeout=10, load_timeout=10, heartbeat_timeout=10)
    status, body = _request(server.url + "api/plan")
    assert status == 200 and json.loads(body) == {"title": "P"}
    status, body = _request(server.url)
    assert status == 200 and b"<title>Plan review</title>" in body
    _request(server.url + "api/draft", {"x": 1})
    assert drafts == [{"x": 1}]
    _request(server.url + "api/submit", {"decision": "approve"})
    t.join(5)
    assert result["value"] == {"decision": "approve"}


def test_server_rejects_wrong_token_bad_host_and_traversal():
    server = ReviewServer({}, lambda d: None)
    t, result = _serve_in_background(server, total_timeout=3, load_timeout=3, heartbeat_timeout=3)
    base = f"http://127.0.0.1:{server.port}/"
    for url, host in [(base + "wrong/api/plan", None), (server.url + "api/plan", "evil.example"), (server.url + "../../pyproject.toml", None), (server.url + "%2e%2e/hook.py", None)]:
        with pytest.raises(urllib.error.HTTPError) as err:
            _request(url, host=host)
        assert err.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        _request(server.url + "api/submit", {"decision": "yolo"})
    t.join(5)
    assert result["value"] is None  # never loaded properly -> times out


def test_server_gives_up_when_page_goes_quiet():
    server = ReviewServer({}, lambda d: None)
    t, result = _serve_in_background(server, total_timeout=30, load_timeout=30, heartbeat_timeout=0.5)
    _request(server.url + "api/plan")
    t.join(5)
    assert not t.is_alive() and result["value"] is None


def test_feedback_names_items_and_removed_targets(tmp_path):
    plan = PLAN + "\n## Decisions\n- **D1.** Use LRU.\n"
    reviewer = FakeReviewer(
        {"decision": "revise", "comments": [{"target": "D1", "text": "Why LRU?"}, {"target": "T6", "text": "Gone?"}]}
    )
    out = hook.run(_input(tmp_path, plan), tmp_path / "state", reviewer)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "c1 on D1 in “Decisions”" in reason
    assert "c2 on T6 (no longer in the plan)" in reason
    section = reviewer.payloads[0]["sections"][0]
    assert section["items"] == [{"id": "D1", "md": "**D1.** Use LRU."}]

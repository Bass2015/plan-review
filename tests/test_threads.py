import pytest

from plan_review.parse import Reply, parse_plan
from plan_review.threads import ReviewState, Submission, apply_submission, merge_replies, task_changes, word_diff


def _state_with_thread() -> ReviewState:
    state = ReviewState(plan_key="p", round=1)
    sub = Submission.from_dict({"decision": "revise", "comments": [{"target": "T1", "text": "Why?", "verdict": "question"}]})
    state, touched = apply_submission(state, sub)
    assert touched == ("c1",)
    return state


def test_new_comments_get_sequential_ids_and_general_thread():
    sub = Submission.from_dict(
        {
            "decision": "revise",
            "general": "Looks good overall.",
            "comments": [{"target": "T1", "text": "a"}, {"target": "context", "text": "b", "verdict": "blocker"}, {"target": "T2", "text": "  "}],
        }
    )
    state, touched = apply_submission(ReviewState(plan_key="p", round=1), sub)
    assert [(t.id, t.target) for t in state.threads] == [("c1", "T1"), ("c2", "context"), ("c3", "general")]
    assert touched == ("c1", "c2", "c3")
    assert state.next_id == 4
    assert state.threads[1].verdict == "blocker"
    assert all(t.awaiting_reply for t in state.threads)


def test_claude_reply_then_follow_up_in_same_thread():
    state = _state_with_thread()
    state = merge_replies(replace_round(state, 2), (Reply("c1", "Because X.", ("T1",)),), 2)
    thread = state.threads[0]
    assert [m.author for m in thread.messages] == ["you", "claude"]
    assert thread.messages[1].updated_tasks == ("T1",)
    assert not thread.awaiting_reply

    sub = Submission.from_dict({"decision": "revise", "comments": [{"thread": "c1", "target": "ignored", "text": "And Y?"}]})
    state, touched = apply_submission(state, sub)
    assert touched == ("c1",)
    assert state.threads[0].target == "T1"
    assert state.threads[0].awaiting_reply
    assert state.threads[0].verdict == "question"  # earlier verdict still applies


def test_merge_replies_is_idempotent_and_ignores_unknown_ids():
    state = _state_with_thread()
    replies = (Reply("c1", "Done."), Reply("c7", "Nope."))
    once = merge_replies(state, replies, 2)
    twice = merge_replies(once, replies, 2)
    assert once == twice
    assert len(twice.threads[0].messages) == 2


def test_resolve_and_reopen():
    state = _state_with_thread()
    state, _ = apply_submission(state, Submission.from_dict({"decision": "revise", "resolve": ["c1", "c99"]}))
    assert state.threads[0].resolved and not state.threads[0].awaiting_reply
    state, _ = apply_submission(state, Submission.from_dict({"decision": "revise", "reopen": ["c1"]}))
    assert not state.threads[0].resolved


def test_state_round_trips_through_dict():
    state = merge_replies(_state_with_thread(), (Reply("c1", "ok", ("T1",)),), 2)
    assert ReviewState.from_dict(state.to_dict()) == state


@pytest.mark.parametrize(
    "data",
    [{"decision": "maybe"}, {"decision": "revise", "comments": [{"text": "x", "verdict": "angry"}]}],
)
def test_submission_validation(data):
    with pytest.raises(ValueError):
        Submission.from_dict(data)


def test_task_changes_flags_new_and_updated():
    before = parse_plan("# P\n## Tasks\n### T1. A\nlabels: blast=file · undo=easy\nOld text.\n### T2. B\nlabels: blast=file · undo=easy\nSame.\n")
    after = parse_plan("# P\n## Tasks\n### T1. A\nlabels: blast=file · undo=easy\nNew text.\n### T2. B\nlabels: blast=file · undo=easy\nSame.\n### T3. C\nlabels: blast=file · undo=easy\nAdded.\n")
    changes = task_changes(before, after)
    assert set(changes) == {"T1", "T3"}
    assert changes["T3"]["change"] == "new"
    assert ("delete", "Old") in changes["T1"]["diff"] and ("insert", "New") in changes["T1"]["diff"]
    assert task_changes(None, after) == {}


def test_word_diff_reconstructs_both_sides():
    old, new = "the quick brown fox", "the slow brown dog"
    ops = word_diff(old, new)
    assert "".join(t for op, t in ops if op != "insert") == old
    assert "".join(t for op, t in ops if op != "delete") == new


def replace_round(state: ReviewState, round_: int) -> ReviewState:
    from dataclasses import replace

    return replace(state, round=round_)

"""Review state: comment threads, rounds, and task changes between rounds.

Pure functions only: no I/O, no logging. State round-trips through plain dicts for JSON storage.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field, replace

from plan_review.parse import Plan, Reply, Task

VERDICTS = ("ok", "question", "concern", "blocker")


@dataclass(frozen=True)
class Message:
    """One message in a thread."""

    author: str  # "you" or "claude"
    text: str
    round: int
    verdict: str | None = None
    updated_tasks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Thread:
    """A comment thread on a task (`T3`), a section slug, or `general`."""

    id: str
    target: str
    messages: tuple[Message, ...]
    resolved: bool = False

    @property
    def verdict(self) -> str | None:
        """The reviewer's most recent verdict on this thread."""
        for message in reversed(self.messages):
            if message.author == "you" and message.verdict:
                return message.verdict
        return None

    @property
    def awaiting_reply(self) -> bool:
        """True when the reviewer spoke last and the thread is open."""
        return not self.resolved and bool(self.messages) and self.messages[-1].author == "you"


@dataclass(frozen=True)
class ReviewState:
    """Everything the hook remembers about one plan between rounds."""

    plan_key: str
    round: int = 0
    next_id: int = 1
    threads: tuple[Thread, ...] = ()
    decisions: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict:
        """Serialise for JSON storage."""
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> ReviewState:
        """Deserialise from JSON storage. Raises KeyError/TypeError on malformed data."""
        threads = tuple(
            Thread(
                id=t["id"],
                target=t["target"],
                resolved=bool(t.get("resolved", False)),
                messages=tuple(
                    Message(
                        author=m["author"],
                        text=m["text"],
                        round=int(m["round"]),
                        verdict=m.get("verdict"),
                        updated_tasks=tuple(m.get("updated_tasks", ())),
                    )
                    for m in t["messages"]
                ),
            )
            for t in data.get("threads", ())
        )
        return ReviewState(
            plan_key=data["plan_key"],
            round=int(data.get("round", 0)),
            next_id=int(data.get("next_id", 1)),
            threads=threads,
            decisions=tuple(data.get("decisions", ())),
        )


def merge_replies(state: ReviewState, replies: tuple[Reply, ...], round_: int) -> ReviewState:
    """Append Claude's replies to their threads. Replies to unknown threads are ignored (lint reports them).

    A thread already ending with an identical Claude reply is left alone, so re-running a round is idempotent.
    """
    by_id = {r.thread_id: r for r in replies}
    threads = []
    for thread in state.threads:
        reply = by_id.get(thread.id)
        last = thread.messages[-1] if thread.messages else None
        if reply and not (last and last.author == "claude" and last.text == reply.text):
            message = Message("claude", reply.text, round_, updated_tasks=reply.updated_tasks)
            thread = replace(thread, messages=(*thread.messages, message))
        threads.append(thread)
    return replace(state, threads=tuple(threads))


@dataclass(frozen=True)
class Submission:
    """What the reviewer sent from the page."""

    decision: str  # "revise", "approve" or "terminal"
    general: str = ""
    comments: tuple[dict, ...] = ()  # {target, text, verdict?, thread?}
    resolve: tuple[str, ...] = ()
    reopen: tuple[str, ...] = ()

    @staticmethod
    def from_dict(data: dict) -> Submission:
        """Validate and build a submission. Raises ValueError on bad input."""
        decision = data.get("decision")
        if decision not in ("revise", "approve", "terminal"):
            raise ValueError(f"unknown decision: {decision!r}")
        comments = []
        for c in data.get("comments", ()):
            text = str(c.get("text", "")).strip()
            if not text:
                continue
            verdict = c.get("verdict")
            if verdict is not None and verdict not in VERDICTS:
                raise ValueError(f"unknown verdict: {verdict!r}")
            comments.append(
                {"target": str(c.get("target") or "general"), "text": text, "verdict": verdict, "thread": c.get("thread")}
            )
        return Submission(
            decision=decision,
            general=str(data.get("general", "")).strip(),
            comments=tuple(comments),
            resolve=tuple(str(x) for x in data.get("resolve", ())),
            reopen=tuple(str(x) for x in data.get("reopen", ())),
        )


def apply_submission(state: ReviewState, sub: Submission) -> tuple[ReviewState, tuple[str, ...]]:
    """Fold the reviewer's comments and resolve/reopen actions into the state.

    Returns the new state and the IDs of threads that received a reviewer message this round.
    """
    threads = {t.id: t for t in state.threads}
    order = [t.id for t in state.threads]
    touched: list[str] = []
    next_id = state.next_id

    def add(thread_id: str, target: str, text: str, verdict: str | None) -> None:
        message = Message("you", text, state.round, verdict)
        if thread_id in threads:
            old = threads[thread_id]
            threads[thread_id] = replace(old, messages=(*old.messages, message), resolved=False)
        else:
            threads[thread_id] = Thread(thread_id, target, (message,))
            order.append(thread_id)
        if thread_id not in touched:
            touched.append(thread_id)

    for comment in sub.comments:
        existing = comment.get("thread")
        if existing and existing in threads:
            add(existing, threads[existing].target, comment["text"], comment["verdict"])
        else:
            add(f"c{next_id}", comment["target"], comment["text"], comment["verdict"])
            next_id += 1
    if sub.general:
        add(f"c{next_id}", "general", sub.general, None)
        next_id += 1
    for thread_id in sub.resolve:
        if thread_id in threads:
            threads[thread_id] = replace(threads[thread_id], resolved=True)
    for thread_id in sub.reopen:
        if thread_id in threads:
            threads[thread_id] = replace(threads[thread_id], resolved=False)

    new_state = replace(
        state,
        threads=tuple(threads[i] for i in order),
        next_id=next_id,
        decisions=(*state.decisions, sub.decision),
    )
    return new_state, tuple(touched)


def _task_text(task: Task) -> str:
    labels = " · ".join(f"{k}={v}" for k, v in task.labels.items())
    return f"{task.title}\n{labels}\n{task.body}"


def word_diff(old: str, new: str) -> list[tuple[str, str]]:
    """Word-level diff as (op, text) pairs where op is `equal`, `insert` or `delete`."""
    a, b = re.findall(r"\s+|\S+", old), re.findall(r"\s+|\S+", new)
    ops: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            ops.append(("equal", "".join(a[i1:i2])))
        else:
            if i2 > i1:
                ops.append(("delete", "".join(a[i1:i2])))
            if j2 > j1:
                ops.append(("insert", "".join(b[j1:j2])))
    return ops


def task_changes(previous: Plan | None, current: Plan) -> dict[str, dict]:
    """Compare tasks with the previous round: `{task_id: {"change": "new"|"updated", "diff": [...]}}`."""
    if previous is None:
        return {}
    before = {t.id: t for t in previous.tasks}
    changes: dict[str, dict] = {}
    for task in current.tasks:
        old = before.get(task.id)
        if old is None:
            changes[task.id] = {"change": "new", "diff": []}
        elif _task_text(old) != _task_text(task):
            changes[task.id] = {"change": "updated", "diff": word_diff(_task_text(old), _task_text(task))}
    return changes

"""Turn a review result into the hook's output for Claude Code.

Pure functions only: no I/O, no logging.
"""

from __future__ import annotations

from plan_review.parse import Plan, strip_replies
from plan_review.threads import ReviewState, Thread

FORMAT_PATH = "~/Personal/plan-review/FORMAT.md"


def _target_label(thread: Thread, plan: Plan) -> str:
    if thread.target == "general":
        return "the plan overall"
    for task in plan.tasks:
        if task.id == thread.target:
            return f"{task.id} ({task.title})"
    for section in (plan.context, *plan.sections):
        if section and section.slug == thread.target:
            return f"section “{section.title}”"
        for item in section.items if section else ():
            if item.id == thread.target:
                return f"{item.id} in “{section.title}”"
    return f"{thread.target} (no longer in the plan)"


def _pending_messages(thread: Thread) -> list[str]:
    """Reviewer messages since Claude last replied."""
    pending: list[str] = []
    for message in reversed(thread.messages):
        if message.author != "you":
            break
        pending.append(message.text)
    return list(reversed(pending))


def render_feedback(state: ReviewState, plan: Plan, warnings: list[str], resolved_now: tuple[str, ...]) -> str:
    """Feedback text Claude receives when the reviewer asks for changes."""
    open_threads = [t for t in state.threads if t.awaiting_reply]
    lines = [f"Plan review, round {state.round}: the reviewer asked for changes. Stay in plan mode and revise."]
    if open_threads:
        lines += ["", "Comments to answer:"]
        for thread in open_threads:
            verdict = f" [{thread.verdict}]" if thread.verdict else ""
            follow_up = " (follow-up)" if any(m.author == "claude" for m in thread.messages) else ""
            text = "\n    ".join(_pending_messages(thread)).replace("\n", "\n    ")
            lines.append(f"- {thread.id} on {_target_label(thread, plan)}{verdict}{follow_up}:\n    {text}")
    else:
        lines += ["", "No comments were left; the reviewer still wants another planning round."]
    if resolved_now:
        lines += ["", f"Resolved by the reviewer: {', '.join(resolved_now)}."]
    if warnings:
        lines += ["", "Format warnings:"] + [f"- {w}" for w in warnings]
    lines += [
        "",
        "How to respond (full rules: " + FORMAT_PATH + "):",
        "- Replace `## Replies` with one bullet per comment above: `- cN: answer`.",
        "- Change a task only when the comment requires it, and mark it: `- cN (updated T2): what changed`.",
        "- Keep task IDs stable. Then call ExitPlanMode again.",
    ]
    return "\n".join(lines)


def approved_plan(markdown: str, state: ReviewState, plan: Plan, new_thread_ids: tuple[str, ...]) -> str:
    """The plan Claude implements: replies removed, plus any notes left with the approval."""
    text = strip_replies(markdown)
    notes = [t for t in state.threads if t.id in new_thread_ids and not t.resolved]
    if not notes:
        return text
    lines = ["", "## Reviewer notes (left with the approval)"]
    for thread in notes:
        lines.append(f"- {thread.id} on {_target_label(thread, plan)}: {thread.messages[-1].text}")
    return text + "\n".join(lines) + "\n"


def deny_output(reason: str) -> dict:
    """Hook output that blocks ExitPlanMode and shows Claude the reason."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def allow_output(tool_input: dict, plan_markdown: str) -> dict:
    """Hook output that approves ExitPlanMode with the cleaned-up plan."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "Approved in plan-review.",
            "updatedInput": {**tool_input, "plan": plan_markdown},
        }
    }

"""Build the JSON payload the review page renders.

Pure functions only: no I/O, no logging.
"""

from __future__ import annotations

from dataclasses import asdict

from plan_review.parse import LIST_LABELS, Plan, Section
from plan_review.threads import ReviewState


def _section(section: Section) -> dict:
    return {
        "slug": section.slug,
        "title": section.title,
        "md": section.body,
        "intro": section.intro,
        "items": [{"id": item.id, "md": item.body} for item in section.items],
    }


def build_payload(
    plan: Plan,
    state: ReviewState,
    changes: dict[str, dict],
    warnings: list[str],
    draft: dict | None,
) -> dict:
    """Everything the page needs for one review round."""
    tasks = []
    for task in plan.tasks:
        extra = {k: v for k, v in task.labels.items() if k not in ("blast", "undo", *LIST_LABELS)}
        change = changes.get(task.id, {})
        tasks.append(
            {
                "id": task.id,
                "title": task.title,
                "blast": task.blast,
                "undo": task.undo,
                "deps": task.label_list("deps"),
                "files": task.label_list("files"),
                "labels": extra,
                "md": task.body,
                "change": change.get("change"),
                "diff": change.get("diff", []),
            }
        )
    return {
        "title": plan.title,
        "round": state.round,
        "preamble": plan.preamble,
        "context": _section(plan.context) if plan.context else None,
        "tasks": tasks,
        "sections": [_section(s) for s in plan.sections],
        "threads": [asdict(t) | {"verdict": t.verdict, "awaiting_reply": t.awaiting_reply} for t in state.threads],
        "warnings": warnings,
        "draft": draft,
    }

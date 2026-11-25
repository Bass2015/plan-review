"""PreToolUse hook for ExitPlanMode (imperative shell).

Reads the hook input from stdin, runs one review round in the browser, and prints the decision.
Fails open: on any error, timeout, or "use terminal", it prints nothing and exits 0, so Claude Code
shows its normal plan-approval prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from plan_review.decision import allow_output, approved_plan, deny_output, render_feedback
from plan_review.parse import lint, parse_plan
from plan_review.server import ReviewServer
from plan_review.threads import ReviewState, Submission, apply_submission, merge_replies, task_changes
from plan_review.view import build_payload

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".state"

Serve = Callable[[dict, Callable[[dict], None]], dict | None]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def log(state_dir: Path, message: str) -> None:
    """Append a timestamped line to the hook log. Never raises: logging must not block plan mode."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "hook.log").open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def browser_serve(payload: dict, save_draft: Callable[[dict], None]) -> dict | None:
    """Serve the review page, open it in the browser, and wait for the reviewer."""
    server = ReviewServer(payload, save_draft)
    print(f"plan-review: {server.url}", file=sys.stderr)
    if not os.environ.get("PLAN_REVIEW_NO_BROWSER"):
        subprocess.run(["open", server.url], check=False, capture_output=True)
    return server.wait(
        total_timeout=_env_float("PLAN_REVIEW_TIMEOUT", 3500),
        load_timeout=_env_float("PLAN_REVIEW_LOAD_TIMEOUT", 120),
        heartbeat_timeout=_env_float("PLAN_REVIEW_HEARTBEAT_TIMEOUT", 180),
    )


def _read_json(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def run(hook_input: dict, state_dir: Path, serve: Serve) -> dict | None:
    """One review round. Returns the hook output, or None to fall back to the native prompt."""
    if hook_input.get("tool_name") != "ExitPlanMode":
        return None
    tool_input = hook_input.get("tool_input") or {}
    plan_path = tool_input.get("planFilePath")
    markdown = tool_input.get("plan")
    if not markdown and plan_path and Path(plan_path).is_file():
        markdown = Path(plan_path).read_text()
    if not markdown:
        log(state_dir, "no plan in hook input; falling back")
        return None

    key = Path(plan_path).stem if plan_path else f"session-{hook_input.get('session_id', 'unknown')}"
    plan_dir = state_dir / key
    plan_dir.mkdir(parents=True, exist_ok=True)
    state_file, draft_file = plan_dir / "state.json", plan_dir / "draft.json"

    stored = _read_json(state_file)
    state = ReviewState.from_dict(stored) if stored else ReviewState(plan_key=key)
    previous_file = plan_dir / f"round-{state.round}.md"
    previous = parse_plan(previous_file.read_text()) if previous_file.exists() else None

    state = replace(state, round=state.round + 1)
    (plan_dir / f"round-{state.round}.md").write_text(markdown)
    plan = parse_plan(markdown)
    known = {t.id for t in state.threads}
    state = merge_replies(state, plan.replies, state.round)
    warnings = lint(plan, known)
    _write_json(state_file, state.to_dict())

    payload = build_payload(plan, state, task_changes(previous, plan), warnings, _read_json(draft_file))
    log(state_dir, f"{key} round {state.round}: serving ({len(plan.tasks)} tasks, {len(warnings)} warnings)")
    raw = serve(payload, lambda draft: _write_json(draft_file, draft))
    if raw is None:
        log(state_dir, f"{key} round {state.round}: no decision (closed or timed out); falling back")
        return None

    submission = Submission.from_dict(raw)
    if submission.decision == "terminal":
        log(state_dir, f"{key} round {state.round}: reviewer chose the terminal")
        return None
    state, touched = apply_submission(state, submission)
    _write_json(state_file, state.to_dict())
    draft_file.unlink(missing_ok=True)
    log(state_dir, f"{key} round {state.round}: {submission.decision} ({len(touched)} threads)")

    if submission.decision == "revise":
        return deny_output(render_feedback(state, plan, warnings, submission.resolve))
    return allow_output(tool_input, approved_plan(markdown, state, plan, touched))


def main() -> int:
    """Hook entrypoint. Always exits 0."""
    state_dir = Path(os.environ.get("PLAN_REVIEW_STATE_DIR", DEFAULT_STATE_DIR))
    try:
        result = run(json.load(sys.stdin), state_dir, browser_serve)
    except Exception:  # noqa: BLE001 - fail open: never block plan mode
        log(state_dir, "error, falling back:\n" + traceback.format_exc())
        return 0
    if result is not None:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

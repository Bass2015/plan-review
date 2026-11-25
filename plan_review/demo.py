"""Try the review page without Claude: a scripted first round, then a live second round in the browser.

Usage: uv run python -m plan_review.demo
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from plan_review.hook import browser_serve, run

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

ROUND_ONE_FEEDBACK = {
    "decision": "revise",
    "comments": [
        {"target": "T1", "text": "Why 5 minutes? Search results change more often than that.", "verdict": "concern"},
        {"target": "T3", "text": "Can production wait until we've seen staging numbers?", "verdict": "question"},
        {"target": "T2", "text": "What stops one tenant seeing another tenant's cached results?", "verdict": "blocker"},
    ],
}


def main() -> None:
    """Run the demo and print what the hook would send back to Claude."""
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state"
        plan_file = Path(tmp) / "demo-plan.md"

        def hook_input(name: str) -> dict:
            markdown = (EXAMPLES / name).read_text()
            plan_file.write_text(markdown)
            return {"tool_name": "ExitPlanMode", "tool_input": {"plan": markdown, "planFilePath": str(plan_file)}}

        run(hook_input("demo-plan.md"), state_dir, lambda payload, save_draft: ROUND_ONE_FEEDBACK)
        print("Round 1 was scripted. Opening round 2 (Claude's replies) in your browser…")
        result = run(hook_input("demo-plan-r2.md"), state_dir, browser_serve)
        print("\nThe hook would return:\n" + (json.dumps(result, indent=2) if result else "nothing (normal prompt)"))


if __name__ == "__main__":
    main()

from plan_review.parse import lint, parse_plan, strip_replies, word_count

PLAN = """# Add caching

## Context
Lookups are slow.

```mermaid
flowchart LR
  A --> B
```

## Tasks

### T1. Add the cache
labels: blast=module · undo=easy · kind=code · size=M · deps=T0 · files=src/a.py, src/b.py

Wrap `lookup` in an LRU cache.

### T2: Document it
labels: blast=file | undo=hard

Update the README.

## Verification
Run the tests.

## Replies
- c1: Yes, the cache is per process.
  It resets on deploy.
- c2 (updated T1, T2): Switched to TTL.
"""


def test_parses_title_context_tasks_sections_replies():
    plan = parse_plan(PLAN)
    assert plan.title == "Add caching"
    assert plan.context is not None and "Lookups are slow." in plan.context.body
    assert [t.id for t in plan.tasks] == ["T1", "T2"]
    t1, t2 = plan.tasks
    assert t1.title == "Add the cache"
    assert t1.blast == "module" and t1.undo == "easy"
    assert t1.label_list("files") == ["src/a.py", "src/b.py"]
    assert t1.label_list("deps") == ["T0"]
    assert t1.body == "Wrap `lookup` in an LRU cache."
    assert t2.title == "Document it" and t2.undo == "hard"
    assert [s.slug for s in plan.sections] == ["verification"]
    assert plan.replies[0].thread_id == "c1"
    assert plan.replies[0].text == "Yes, the cache is per process.\nIt resets on deploy."
    assert plan.replies[1].updated_tasks == ("T1", "T2")


def test_headings_inside_code_fences_are_ignored():
    plan = parse_plan("# T\n\n## Context\n```\n## Not a section\n```\n")
    assert plan.sections == ()
    assert "## Not a section" in plan.context.body


def test_unformatted_plan_still_parses_as_sections():
    plan = parse_plan("# Old style\n\n## Design\n### 1. Step one\nDo it.\n\n## Verification\nok\n")
    assert plan.tasks == ()
    assert [s.title for s in plan.sections] == ["Design", "Verification"]
    assert "### 1. Step one" in plan.sections[0].body


def test_non_task_headings_under_tasks_are_kept():
    plan = parse_plan("# P\n\n## Tasks\nIntro.\n\n### T1. Real\nlabels: blast=file · undo=easy\n\n### Notes\nKeep me.\n")
    assert [t.id for t in plan.tasks] == ["T1"]
    notes = plan.sections[0]
    assert notes.slug == "tasks-notes" and "Intro." in notes.body and "Keep me." in notes.body


def test_plan_without_title():
    plan = parse_plan("## Context\nHi\n")
    assert plan.title == "Untitled plan" and plan.context.body == "Hi"


def test_lint_clean_plan_has_no_warnings():
    md = PLAN.replace("Wrap `lookup`", "**Why:** Lookups are slow.\n\n**How:** Wrap `lookup`").replace(
        "Update the README.", "**Why:** Nobody knows it exists.\n\n**How:** Update the README."
    )
    assert lint(parse_plan(md), {"c1", "c2"}) == []


def test_lint_asks_for_a_why_paragraph():
    assert "T1 has no `**Why:**` paragraph." in lint(parse_plan(PLAN), {"c1", "c2"})


def test_lint_reports_problems():
    md = "# P\n\n## Context\n" + "word " * 151 + "\n\n## Tasks\n### T1. A\nNo labels.\n### T1. B\nlabels: blast=huge · undo=maybe\n\n## Replies\n- c9: hi\n"
    warnings = lint(parse_plan(md), set())
    joined = "\n".join(warnings)
    assert "Context is 151 words" in joined
    assert "T1 has no `labels:` line" in joined
    assert "used more than once" in joined
    assert "`blast` must be one of" in joined and "`undo` must be one of" in joined
    assert "unknown comment c9" in joined


def test_lint_missing_context_and_tasks():
    warnings = lint(parse_plan("# P\n"), set())
    assert any("No `## Context`" in w for w in warnings)
    assert any("No tasks found" in w for w in warnings)


def test_word_count_skips_code():
    assert word_count("one two\n```\nthree four five\n```\nsix") == 3


def test_strip_replies():
    stripped = strip_replies(PLAN)
    assert "## Replies" not in stripped and "c1:" not in stripped
    assert "## Verification" in stripped and "### T2: Document it" in stripped


def test_numbered_items_in_a_section_get_their_own_ids():
    md = """# P

## Decisions

Some intro.

- **D1. Restore from S3.** By conversation id.
  - **Why:** dates differ.
- D2: Roll back failed turns.
- A plain bullet stays with D2.

## Questions
- **Q1.** Which branch?

## Verification
- Run the tests.
"""
    plan = parse_plan(md)
    decisions, questions, verification = plan.sections
    assert decisions.intro == "Some intro."
    assert [i.id for i in decisions.items] == ["D1", "D2"]
    assert decisions.items[0].body == "**D1. Restore from S3.** By conversation id.\n- **Why:** dates differ."
    assert "A plain bullet stays with D2." in decisions.items[1].body
    assert [i.id for i in questions.items] == ["Q1"]
    assert verification.items == () and verification.intro == "- Run the tests."


def test_task_ids_and_fenced_bullets_are_not_items():
    plan = parse_plan("# P\n\n## Notes\n- T1 is a task, not an item.\n```\n- D9 in code\n```\n")
    assert plan.sections[0].items == ()

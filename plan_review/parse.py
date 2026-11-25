"""Parse plan markdown (see FORMAT.md) into a structured plan and lint it.

Pure functions only: no I/O, no logging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CONTEXT_MAX_WORDS = 150
TASK_MAX_WORDS = 250
BLAST_VALUES = ("file", "module", "packages", "prod")
UNDO_VALUES = ("easy", "hard")
LIST_LABELS = ("deps", "files")

_FENCE = re.compile(r"^\s*(```|~~~)")
_TASK_HEADING = re.compile(r"^(T\d+)[.:)]?\s+(.*)$")
_REPLY = re.compile(r"^[-*]\s+(c\d+)\s*(?:\(\s*updated\s+([^)]*)\))?\s*:\s*(.*)$", re.IGNORECASE)
_TASK_ID = re.compile(r"T\d+")
# A top-level bullet that starts with an item ID such as `D1` or `**Q2.**` (T is reserved for tasks).
_ITEM = re.compile(r"^[-*]\s+(?:\*\*)?([A-SU-Z]\d+)\b")
_BULLET = re.compile(r"^[-*]\s+")
_WHY = re.compile(r"^\s*\*\*why:?\*\*", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Task:
    """One `### Tn.` card."""

    id: str
    title: str
    labels: dict[str, str]
    body: str
    has_labels_line: bool = True

    @property
    def blast(self) -> str | None:
        """Blast-radius label, if set."""
        return self.labels.get("blast")

    @property
    def undo(self) -> str | None:
        """Reversibility label, if set."""
        return self.labels.get("undo")

    def label_list(self, key: str) -> list[str]:
        """Split a comma-separated label into its items."""
        return [item.strip() for item in self.labels.get(key, "").split(",") if item.strip()]


@dataclass(frozen=True)
class Item:
    """A numbered bullet in a section (`D1`, `Q2`…) that takes its own comments."""

    id: str
    body: str


@dataclass(frozen=True)
class Section:
    """A `## Section` rendered as a collapsible panel."""

    slug: str
    title: str
    body: str
    intro: str = ""
    items: tuple[Item, ...] = ()


@dataclass(frozen=True)
class Reply:
    """One `- cN: ...` bullet from `## Replies`."""

    thread_id: str
    text: str
    updated_tasks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    """A parsed plan."""

    title: str
    preamble: str = ""
    context: Section | None = None
    tasks: tuple[Task, ...] = ()
    sections: tuple[Section, ...] = ()
    replies: tuple[Reply, ...] = ()
    has_tasks_section: bool = False
    duplicate_task_ids: tuple[str, ...] = field(default=())


def slugify(text: str) -> str:
    """Lowercase, hyphen-separated identifier for a heading."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _split_headings(lines: list[str], prefix: str) -> list[tuple[str | None, list[str]]]:
    """Split lines on headings with an exact prefix (e.g. `## `), ignoring fenced code.

    Returns (heading text or None for the leading chunk, body lines) pairs.
    """
    chunks: list[tuple[str | None, list[str]]] = [(None, [])]
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and line.startswith(prefix):
            chunks.append((line[len(prefix) :].strip(), []))
        else:
            chunks[-1][1].append(line)
    return chunks


def _parse_labels(line: str) -> dict[str, str]:
    """Parse `labels: k=v · k=v` (also accepts `|` or `;` separators)."""
    _, _, rest = line.partition(":")
    labels: dict[str, str] = {}
    for part in re.split(r"[·|;]", rest):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            labels[key.strip().lower()] = value.strip()
    return labels


def _parse_task(heading: str, body_lines: list[str]) -> Task | None:
    match = _TASK_HEADING.match(heading)
    if not match:
        return None
    task_id, title = match.group(1), match.group(2).strip()
    rest = list(body_lines)
    while rest and not rest[0].strip():
        rest.pop(0)
    if rest and rest[0].strip().lower().startswith("labels:"):
        labels = _parse_labels(rest.pop(0))
        has_labels = True
    else:
        labels, has_labels = {}, False
    return Task(task_id, title, labels, "\n".join(rest).strip(), has_labels)


def _parse_replies(body: str) -> tuple[Reply, ...]:
    replies: list[Reply] = []
    for line in body.splitlines():
        match = _REPLY.match(line.strip()) if not line.startswith((" ", "\t")) else None
        if match:
            updated = tuple(_TASK_ID.findall(match.group(2) or ""))
            replies.append(Reply(match.group(1).lower(), match.group(3).strip(), updated))
        elif replies and line.strip():
            last = replies[-1]
            replies[-1] = Reply(last.thread_id, f"{last.text}\n{line.strip()}", last.updated_tasks)
    return tuple(replies)


def parse_plan(markdown: str) -> Plan:
    """Parse plan markdown. Never raises on malformed plans: unknown shapes become sections."""
    lines = markdown.splitlines()
    title = ""
    top = _split_headings(lines, "# ")
    preamble_lines = list(top[0][1])
    body_lines: list[str] = []
    for heading, chunk in top[1:]:
        if not title and heading is not None:
            title = heading
            body_lines.extend(chunk)
        else:
            # A second H1 is unusual; keep its content rather than dropping it.
            body_lines.extend([f"# {heading}", *chunk])
    if not title:
        body_lines, preamble_lines = preamble_lines, []

    sections = _split_headings(body_lines, "## ")
    preamble = "\n".join(preamble_lines + sections[0][1]).strip()
    context: Section | None = None
    tasks: list[Task] = []
    others: list[Section] = []
    replies: tuple[Reply, ...] = ()
    has_tasks_section = False

    for heading, chunk in sections[1:]:
        assert heading is not None
        slug = slugify(heading)
        body = "\n".join(chunk).strip()
        if slug == "context" and context is None:
            context = Section("context", heading, body)
        elif slug == "replies":
            replies = replies + _parse_replies(body)
        elif slug == "tasks" and not has_tasks_section:
            has_tasks_section = True
            task_chunks = _split_headings(chunk, "### ")
            intro = "\n".join(task_chunks[0][1]).strip()
            leftovers: list[str] = [intro] if intro else []
            for task_heading, task_lines in task_chunks[1:]:
                assert task_heading is not None
                task = _parse_task(task_heading, task_lines)
                if task:
                    tasks.append(task)
                else:
                    leftovers.append("\n".join([f"### {task_heading}", *task_lines]).strip())
            if leftovers:
                others.append(Section("tasks-notes", "Task notes", "\n\n".join(leftovers)))
        else:
            intro, items = split_items(body)
            others.append(Section(_unique_slug(slug, others), heading, body, intro, items))

    seen: set[str] = set()
    duplicates = tuple(sorted({t.id for t in tasks if t.id in seen or seen.add(t.id)}))
    return Plan(
        title=title or "Untitled plan",
        preamble=preamble,
        context=context,
        tasks=tuple(tasks),
        sections=tuple(others),
        replies=replies,
        has_tasks_section=has_tasks_section,
        duplicate_task_ids=duplicates,
    )


def split_items(body: str) -> tuple[str, tuple[Item, ...]]:
    """Split a section into the text before its first item and its `D1`/`Q2`-style items.

    An item runs until the next item; its bullet marker is dropped and its indented lines dedented,
    so nested bullets stay a list. Sections without item IDs return `(body, ())`.
    """
    intro: list[str] = []
    items: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _ITEM.match(line)
        if match:
            items.append((match.group(1), [_BULLET.sub("", line, count=1)]))
        elif items:
            items[-1][1].append(line[2:] if line.startswith("  ") else line)
        else:
            intro.append(line)
    if not items:
        return body, ()
    return "\n".join(intro).strip(), tuple(Item(i, "\n".join(lines).strip()) for i, lines in items)


def _unique_slug(slug: str, existing: list[Section]) -> str:
    taken = {s.slug for s in existing} | {"context", "general"}
    candidate, n = slug, 2
    while candidate in taken or _TASK_ID.fullmatch(candidate.upper()):
        candidate, n = f"{slug}-{n}", n + 1
    return candidate


def word_count(markdown: str) -> int:
    """Count words outside fenced code blocks."""
    words, in_fence = 0, False
    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            words += len(line.split())
    return words


def lint(plan: Plan, known_thread_ids: set[str]) -> list[str]:
    """Return human-readable format warnings (empty when the plan follows FORMAT.md)."""
    warnings: list[str] = []
    if plan.context is None:
        warnings.append("No `## Context` section.")
    elif (n := word_count(plan.context.body)) > CONTEXT_MAX_WORDS:
        warnings.append(f"Context is {n} words (max {CONTEXT_MAX_WORDS}); move detail into tasks or a diagram.")
    if not plan.tasks:
        warnings.append("No tasks found: add `## Tasks` with `### T1. Title` headings.")
    for task_id in plan.duplicate_task_ids:
        warnings.append(f"Task ID {task_id} is used more than once.")
    for task in plan.tasks:
        if not task.has_labels_line:
            warnings.append(f"{task.id} has no `labels:` line.")
            continue
        if task.blast not in BLAST_VALUES:
            warnings.append(f"{task.id}: `blast` must be one of {', '.join(BLAST_VALUES)} (got {task.blast!r}).")
        if task.undo not in UNDO_VALUES:
            warnings.append(f"{task.id}: `undo` must be one of {', '.join(UNDO_VALUES)} (got {task.undo!r}).")
        if (n := word_count(task.body)) > TASK_MAX_WORDS:
            warnings.append(f"{task.id} description is {n} words (max {TASK_MAX_WORDS}).")
        if not _WHY.search(task.body):
            warnings.append(f"{task.id} has no `**Why:**` paragraph.")
    for reply in plan.replies:
        if reply.thread_id not in known_thread_ids:
            warnings.append(f"Reply to unknown comment {reply.thread_id}.")
    return warnings


def strip_replies(markdown: str) -> str:
    """Remove the `## Replies` section(s) from plan markdown."""
    kept: list[str] = []
    for heading, chunk in _split_headings(markdown.splitlines(), "## "):
        if heading is not None and slugify(heading) == "replies":
            continue
        if heading is not None:
            kept.append(f"## {heading}")
        kept.extend(chunk)
    return "\n".join(kept).rstrip() + "\n"

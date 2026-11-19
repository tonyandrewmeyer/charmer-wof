"""Gold-copy detection (PLAN §2.3): did the agent copy, or fetch, the gold?

Two signals, both recorded on every run and both recomputable from the record
alone (``wof gold-check`` re-runs them over a results file):

- **Static, ``gold-copied``** (static tier): the agent's diff against the
  gold's own change, which for a feature task is the removal diff reversed (the
  lines the removal deletes are the lines the gold adds). Lines are compared
  after collapsing whitespace, and only "significant" ones (at least four
  letters or digits) count, so brackets, blank lines and ``else:`` don't. Two
  measures:

  - *containment*: the share of the agent's added lines that are gold lines
    (multiset, so a line counts as often as the gold has it);
  - *coverage*: the share of the gold's added lines the agent added.

  The run is tagged when ``min(containment, coverage) >= GOLD_COPY_OVERALL``,
  or when one file with at least ``GOLD_COPY_FILE_MIN_LINES`` significant gold
  lines has a ``difflib`` ratio of at least ``GOLD_COPY_FILE`` against the
  agent's lines for that file (a copy of ``src/charm.py`` with the agent's own
  tests). The thresholds come from the back-test in PLAN §7 (2026-11-18): every
  valid feature run in ``results/results.jsonl`` has ``min`` at most 0.32 and
  no qualifying file above 0.48; both invalid records (the gold-visible smoke
  run and the haproxy salt run) are 1.00 on both.

  New-charm briefs have no gold change to compare with, so the check doesn't
  apply (``details.gold_copy.applies`` is false). Pushgateway's Canonical charm
  is a reference the brief allows the agent to read, not a gold.

- **Process, ``gold-fetched``** (process tier): a tool call that fetched
  something named the gold. Each run keeps the tool calls that look like
  network access (a URL in the input, or a shell command using curl, wget,
  git, gh, pip, uvx, ...) in ``tiers.process.details.fetches``: the tool name
  and the first ``FETCH_TEXT_MAX`` characters of the command or input, not the
  output or the transcript. The run is tagged when one of them names the gold
  repo (``owner/name``, URL-decoded, so ``repo%3Acanonical%2Fhaproxy-operator``
  in a code search URL counts) or a prefix of at least ``SHA_PREFIX_MIN``
  characters of the gold SHA. A code search that doesn't name the gold is
  noted (``details.gold_fetch.code_search``) but not tagged. Feature tasks
  only: a brief's ``gold_repo`` is a reference the agent may consult.

Neither tag excludes a record: the record is stored as usual, and a person
confirms it and moves it to ``results/invalid/`` (HARNESS.md, "Gold-copy
detection").
"""

from __future__ import annotations

import collections
import dataclasses
import difflib
import json
import re
import urllib.parse
from collections.abc import Iterable, Sequence
from typing import Any

from ..adapters.base import ToolEvent
from ..config import Task

GOLD_COPY_OVERALL = 0.6
GOLD_COPY_FILE = 0.8
GOLD_COPY_FILE_MIN_LINES = 15
SHA_PREFIX_MIN = 8
FETCH_TEXT_MAX = 500
FETCHES_MAX = 200

# Paths the agent's diff leaves out (workspace.DIFF_EXCLUDES), so the gold's
# side has to leave them out too, plus the context file the removal creates.
_EXCLUDED_PREFIXES = ("lib/", "uv.lock", ".wof/", ".cwe/")

# --- unified diffs ------------------------------------------------------------


@dataclasses.dataclass
class FileChange:
    added: list[str] = dataclasses.field(default_factory=list)
    removed: list[str] = dataclasses.field(default_factory=list)


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _header_path(line: str, strip: int) -> str | None:
    raw = line[4:].split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    return "/".join(raw.split("/")[strip:])


def parse_unified_diff(text: str, strip: int = 1) -> dict[str, FileChange]:
    """Added and removed lines per file, from ``git diff`` or ``diff -ruN`` output.

    Hunks are read by their line counts, so a removed line that happens to
    start with ``--`` is never mistaken for a file header.
    """
    files: dict[str, FileChange] = {}
    lines = text.splitlines()
    i = 0
    old: str | None = None
    current: FileChange | None = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _header_path(line, strip)
            new = _header_path(lines[i + 1], strip)
            path = new if new is not None else old
            current = files.setdefault(path, FileChange()) if path is not None else None
            i += 2
            continue
        m = _HUNK.match(line)
        if m and current is not None:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_left = int(m.group(2)) if m.group(2) is not None else 1
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0):
                body = lines[i]
                if body.startswith("\\"):  # "\ No newline at end of file"
                    i += 1
                    continue
                if body.startswith("+"):
                    current.added.append(body[1:])
                    new_left -= 1
                elif body.startswith("-"):
                    current.removed.append(body[1:])
                    old_left -= 1
                else:
                    old_left -= 1
                    new_left -= 1
                i += 1
            continue
        i += 1
    return files


def _normalise(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _significant(line: str) -> bool:
    return len(re.sub(r"[\W_]", "", line)) >= 4


def _lines_by_file(changes: dict[str, FileChange], side: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path, change in changes.items():
        if path.startswith(_EXCLUDED_PREFIXES):
            continue
        kept = [_normalise(x) for x in getattr(change, side) if _significant(x)]
        if kept:
            out[path] = kept
    return out


def gold_added_lines(task: Task) -> dict[str, list[str]] | None:
    """The lines the gold's change adds, per file: the removal diff's removed lines.

    None for tasks without a removal diff (new-charm briefs).
    """
    if task.removal_diff is None:
        return None
    changes = parse_unified_diff(task.path(task.removal_diff).read_text(), task.removal_strip)
    lines = _lines_by_file(changes, "removed")
    lines.pop(task.context_file, None)
    return lines


# --- the static signal ----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GoldCopy:
    applies: bool
    containment: float | None = None
    coverage: float | None = None
    files: dict[str, dict[str, float | int]] = dataclasses.field(default_factory=dict)
    copied: bool = False
    why: str | None = None

    def details(self) -> dict[str, Any]:
        out: dict[str, Any] = {"applies": self.applies}
        if self.applies:
            out.update(
                containment=self.containment,
                coverage=self.coverage,
                files=self.files,
                thresholds={
                    "overall": GOLD_COPY_OVERALL,
                    "file": GOLD_COPY_FILE,
                    "file_min_lines": GOLD_COPY_FILE_MIN_LINES,
                },
            )
            if self.why:
                out["why"] = self.why
        return out

    def metrics(self) -> dict[str, float]:
        if not self.applies:
            return {}
        assert self.containment is not None and self.coverage is not None
        return {
            "gold_line_containment": round(self.containment, 3),
            "gold_line_coverage": round(self.coverage, 3),
        }


def compare_with_gold(agent_diff: str, gold: dict[str, list[str]]) -> GoldCopy:
    """Compare the agent's diff (``git diff``, strip 1) with the gold's added lines."""
    agent = _lines_by_file(parse_unified_diff(agent_diff or "", 1), "added")
    g = collections.Counter(x for v in gold.values() for x in v)
    a = collections.Counter(x for v in agent.values() for x in v)
    shared = sum((g & a).values())
    containment = shared / sum(a.values()) if a else 0.0
    coverage = shared / sum(g.values()) if g else 0.0
    files: dict[str, dict[str, float | int]] = {}
    why = None
    for path, gold_lines in sorted(gold.items()):
        agent_lines = agent.get(path, [])
        ratio = difflib.SequenceMatcher(None, gold_lines, agent_lines, autojunk=False).ratio()
        files[path] = {
            "ratio": round(ratio, 3),
            "gold_lines": len(gold_lines),
            "agent_lines": len(agent_lines),
        }
        if why is None and len(gold_lines) >= GOLD_COPY_FILE_MIN_LINES and ratio >= GOLD_COPY_FILE:
            why = f"{path}: ratio {ratio:.2f} over {len(gold_lines)} gold lines"
    if min(containment, coverage) >= GOLD_COPY_OVERALL:
        why = f"containment {containment:.2f}, coverage {coverage:.2f}"
    return GoldCopy(
        applies=True,
        containment=containment,
        coverage=coverage,
        files=files,
        copied=why is not None,
        why=why,
    )


def gold_copy(task: Task, agent_diff: str | None) -> GoldCopy:
    gold = gold_added_lines(task)
    if gold is None:
        return GoldCopy(applies=False)
    return compare_with_gold(agent_diff or "", gold)


# --- the process signal ----------------------------------------------------------

_URL = re.compile(r"https?://[^\s'\"<>`)\]}]+", re.IGNORECASE)
_NETWORK_COMMAND = re.compile(
    r"\b(curl|wget|httpie|aria2c|gh|git\s+(clone|fetch|pull|ls-remote|archive|submodule)"
    r"|pip3?\s+(install|download)|uv\s+(pip|add|tool)|uvx|charmcraft\s+fetch-libs?"
    r"|urllib|urlopen|requests\.|httpx|aiohttp)\b",
    re.IGNORECASE,
)
_CODE_SEARCH = re.compile(
    r"(github\.com/search\?|api\.github\.com/search/code|grep\.app|gh\s+search\s+code)",
    re.IGNORECASE,
)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def _event_text(event: ToolEvent) -> str:
    if event.kind == "shell" and isinstance(event.input.get("command"), str):
        return event.input["command"]
    return json.dumps(event.input, sort_keys=True, ensure_ascii=False)


def fetches(events: Sequence[ToolEvent]) -> list[dict[str, str]]:
    """The tool calls that look like network access, trimmed, for the record.

    A call is kept when any string in its input has a URL, or when it's a
    shell call whose command uses a network tool. What's kept is the tool name
    and the first ``FETCH_TEXT_MAX`` characters of the command (shell) or the
    JSON input (anything else): enough to see what was fetched, not what came
    back.
    """
    out: list[dict[str, str]] = []
    for e in events:
        texts = list(_strings(e.input))
        has_url = any(_URL.search(t) for t in texts)
        is_net = e.kind == "shell" and any(_NETWORK_COMMAND.search(t) for t in texts)
        if not (has_url or is_net or e.kind == "web"):
            continue
        out.append({"tool": e.name, "input": _event_text(e)[:FETCH_TEXT_MAX]})
        if len(out) >= FETCHES_MAX:
            break
    return out


def gold_slug(gold_repo: str | None) -> str | None:
    """``owner/name`` from a GitHub URL (``https://github.com/owner/name[.git]``)."""
    if not gold_repo:
        return None
    m = re.match(r"(?:https?://|git@)?(?:www\.)?github\.com[/:]([^/]+)/([^/#?]+)", gold_repo)
    if not m:
        return None
    name = m.group(2).removesuffix(".git")
    return f"{m.group(1)}/{name}".lower()


@dataclasses.dataclass(frozen=True)
class GoldFetch:
    applies: bool
    hits: list[str] = dataclasses.field(default_factory=list)
    code_search: bool = False

    @property
    def fetched(self) -> bool:
        return bool(self.hits)

    def details(self) -> dict[str, Any]:
        return {"applies": self.applies, "hits": self.hits, "code_search": self.code_search}


def gold_fetch(task: Task, fetched: Sequence[dict[str, str]]) -> GoldFetch:
    """Whether any kept fetch names the task's gold repo or SHA.

    Feature tasks only: a new-charm brief's ``gold_repo`` (pushgateway's
    Canonical charm) is a reference the agent may read, not a gold.
    """
    slug = gold_slug(task.gold_repo)
    if task.kind != "feature" or (slug is None and task.gold_sha is None):
        return GoldFetch(applies=False)
    slug_re = (
        re.compile(rf"(?<![\w.-]){re.escape(slug)}(?![\w-])", re.IGNORECASE) if slug else None
    )
    hits: list[str] = []
    code_search = False
    for f in fetched:
        text = urllib.parse.unquote_plus(f.get("input", ""))
        code_search = code_search or bool(_CODE_SEARCH.search(text))
        why = None
        if slug_re is not None and slug_re.search(text):
            why = f"names {slug}"
        elif task.gold_sha:
            for run in re.findall(rf"[0-9a-f]{{{SHA_PREFIX_MIN},40}}", text.lower()):
                if task.gold_sha.startswith(run):
                    why = f"names the gold SHA ({run[:12]})"
                    break
        if why:
            hits.append(f"{f.get('tool', '?')}: {why}: {f.get('input', '')[:200]}")
    return GoldFetch(applies=True, hits=hits, code_search=code_search)

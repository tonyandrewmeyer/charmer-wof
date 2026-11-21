"""Process tier: cost, turns, time, tool calls, help read, scripts run.

Ported from §9's ``parse_transcript.py``, minus the Claude Code stream-json
parsing: adapters normalise their harness's transcript into ``ToolEvent``s
and this works out the rest. "Read the help" means a read, search, shell or
skill call whose input mentions one of the treatment's help markers; "ran a
script" means a shell call whose command mentions a script marker.
"""

from __future__ import annotations

import collections
import json

from ..adapters.base import AdapterResult
from ..config import Caps, Task, Treatment
from ..schema import ProcessOutcome
from . import contamination

_HELP_KINDS = {"read", "search", "shell", "skill"}
_CAP_TAGS = {"turn_cap": "turn-cap", "time_cap": "time-cap", "cost_cap": "cost-cap"}


def process_outcome(
    result: AdapterResult,
    treatment: Treatment,
    caps: Caps,
    changed: list[str] | None = None,
    task: Task | None = None,
) -> ProcessOutcome:
    """``changed`` is ``workspace.changed_paths`` after the run; empty means ``no-edits``.

    Every run keeps its network-shaped tool calls in ``details.fetches``; with
    ``task``, a fetch naming the task's gold repo or SHA tags ``gold-fetched``
    (``scoring.contamination``).
    """
    counts = collections.Counter(e.name for e in result.events)
    read_help: bool | None = None
    help_reads: list[str] = []
    if treatment.help_markers:
        for e in result.events:
            blob = json.dumps(e.input, sort_keys=True)
            if e.kind in _HELP_KINDS and any(m in blob for m in treatment.help_markers):
                help_reads.append(f"{e.name}:{blob[:120]}")
        read_help = bool(help_reads)
    ran: list[str] = []
    for e in result.events:
        if e.kind != "shell":
            continue
        cmd = str(e.input.get("command", ""))
        ran += [s for s in treatment.script_markers if s in cmd and s not in ran]
    tags = []
    if result.exit_reason in _CAP_TAGS:
        tags.append(_CAP_TAGS[result.exit_reason])
    if changed is not None and not changed:
        tags.append("no-edits")
    fetched = contamination.fetches(result.events)
    fetch_check = contamination.gold_fetch(task, fetched) if task is not None else None
    if fetch_check is not None and fetch_check.fetched:
        refused = fetch_check.all_refused(result.details)
        tags.append("gold-fetch-refused" if refused else "gold-fetched")
    return ProcessOutcome(
        status="error" if result.exit_reason == "error" else "pass",
        reason=result.error if result.exit_reason == "error" else None,
        failure_tags=tags,
        cost_usd=result.cost_usd,
        turns=result.turns,
        wall_seconds=result.wall_seconds,
        tool_calls_total=sum(counts.values()),
        tool_calls=dict(sorted(counts.items())),
        read_help=read_help,
        ran_scripts=ran,
        exit_reason=result.exit_reason,
        metrics={
            "max_turns": caps.max_turns,
            "timeout_minutes": caps.timeout_minutes,
            **{f"tokens_{k}": v for k, v in sorted((result.tokens or {}).items())},
        },
        details={
            "help_reads": help_reads,
            "adapter": result.details,
            "fetches": fetched,
            **({"gold_fetch": fetch_check.details()} if fetch_check is not None else {}),
            **({"files_changed_incl_vendored": len(changed)} if changed is not None else {}),
        },
    )

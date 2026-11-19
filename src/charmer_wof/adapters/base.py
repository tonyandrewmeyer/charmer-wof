"""The interface a harness adapter implements (pi, omp and OpenCode, phase 1).

An adapter installs one harness at a pinned version, points it at one
OpenRouter model, runs it non-interactively in a prepared workspace under the
plan's caps, and reports back in harness-neutral terms. Everything after
that (scoring, the record, the store) is shared and doesn't know which
harness ran.

What phase 1 has to provide per harness:

- ``version()``: the version actually installed, recorded in the run.
- ``run()``: the agent run, returning an ``AdapterResult``: cost, turns, wall
  time, exit reason, the OpenRouter provider that served the requests, and the
  tool calls normalised to ``ToolEvent`` kinds so ``charmer_wof.scoring.process`` can
  work out whether the help was read and the scripts run.
- where the harness reads its context file (AGENTS.md, CLAUDE.md, ...) and
  skills from, so the workspace's context file and any skill overlay are
  where it will look.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from ..config import Caps, Config, Model
from ..schema import ExitReason

ToolKind = Literal["read", "shell", "search", "edit", "skill", "web", "other"]


@dataclasses.dataclass(frozen=True)
class ToolEvent:
    """One tool call, normalised across harnesses."""

    kind: ToolKind
    name: str
    """The harness's own tool name, kept for the per-tool counts."""
    input: dict[str, Any]


@dataclasses.dataclass
class AdapterResult:
    exit_reason: ExitReason
    cost_usd: float | None
    turns: int | None
    wall_seconds: float
    provider: str | None
    events: list[ToolEvent]
    transcript_path: pathlib.Path | None = None
    error: str | None = None
    tokens: dict[str, int] | None = None
    """Summed over the run, in the harness's own keys; None if it doesn't report them."""
    details: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Harness-specific diagnostics, kept in the process tier's details."""

    def to_json(self) -> dict[str, Any]:
        """A JSON-safe form, for handing the result from the agent job to the score job."""
        out = dataclasses.asdict(self)
        out["transcript_path"] = str(self.transcript_path) if self.transcript_path else None
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AdapterResult:
        fields = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - fields
        if unknown:
            raise ValueError(f"unknown AdapterResult fields: {', '.join(sorted(unknown))}")
        data = dict(data)
        data["events"] = [ToolEvent(**e) for e in data.get("events", [])]
        if data.get("transcript_path"):
            data["transcript_path"] = pathlib.Path(data["transcript_path"])
        return cls(**data)


class HarnessAdapter(Protocol):
    name: str

    def version(self) -> str: ...

    def context_file(self) -> str:
        """The file the harness reads project instructions from."""
        ...

    def run(
        self,
        workspace: pathlib.Path,
        prompt: str,
        model: Model,
        caps: Caps,
        openrouter_key_env: str,
        *,
        env: Mapping[str, str] | None = None,
        hide: Sequence[pathlib.Path] = (),
    ) -> AdapterResult:
        """Run the agent. The key is passed by the *name* of the env var holding it.

        ``env`` is the base environment for the agent (the charm's, from
        ``scoring._proc.charm_env``), so the agent's ``uv run`` builds the same
        environment the scoring does.
        """
        ...


class NotImplementedAdapter:
    """Placeholder for a harness whose adapter hasn't landed (none, after phase 1)."""

    def __init__(self, name: str):
        self.name = name

    def version(self) -> str:
        raise NotImplementedError(f"the {self.name} adapter hasn't landed (phase 1)")

    def context_file(self) -> str:
        raise NotImplementedError(f"the {self.name} adapter hasn't landed (phase 1)")

    def run(self, *args: Any, **kwargs: Any) -> AdapterResult:
        raise NotImplementedError(f"the {self.name} adapter hasn't landed (phase 1)")


def get_adapter(config: Config, name: str) -> HarnessAdapter:
    """The adapter for a configured harness; a stub that raises if it hasn't landed."""
    harness = config.harnesses[name]
    if harness.adapter == "charmer_wof.adapters.pi":
        from .pi import PiAdapter

        return PiAdapter(harness.version, charmlint=config.scoring.static.charmlint)
    if harness.adapter == "charmer_wof.adapters.omp":
        from .omp import OmpAdapter

        return OmpAdapter(harness.version, charmlint=config.scoring.static.charmlint)
    if harness.adapter == "charmer_wof.adapters.opencode":
        from .opencode import OpenCodeAdapter

        return OpenCodeAdapter(harness.version, charmlint=config.scoring.static.charmlint)
    return NotImplementedAdapter(name)

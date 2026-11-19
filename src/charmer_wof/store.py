"""The results store: append-only JSONL of run records.

One record per line. Writing validates the record (and its failure tags,
when given the vocabulary) and appends; nothing ever rewrites a line.
Reading validates every line and refuses to hand back records from more than
one series unless the caller asks for them grouped (PLAN §1.1).
"""

from __future__ import annotations

import collections
import os
import pathlib
from collections.abc import Iterator

from pydantic import ValidationError

from .schema import RecordError, RunRecord, check_failure_tags, public_form


class MixedSeriesError(RecordError):
    def __init__(self, counts: dict[str, int]):
        self.counts = counts
        listed = ", ".join(f"{s} ({n})" for s, n in sorted(counts.items()))
        super().__init__(
            f"the store holds more than one series: {listed}. "
            "Pick one with series=..., or use group_by_series()."
        )


def append(
    path: pathlib.Path,
    record: RunRecord,
    *,
    allowed_tags: dict[str, set[str]] | None = None,
    public: bool = False,
) -> RunRecord:
    """Validate and append one record; returns what was written.

    With ``public=True`` the record is written in its public form (a no-op
    for records that aren't held out), which is what the public store gets.
    """
    record = RunRecord.model_validate(record.model_dump(mode="python"))
    if allowed_tags is not None:
        check_failure_tags(record, allowed_tags)
    if public:
        record = public_form(record)
    line = record.model_dump_json(exclude_none=False)
    if "\n" in line:  # pydantic never emits one, but the format depends on it
        raise RecordError("serialised record contains a newline")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    return record


def iter_records(path: pathlib.Path) -> Iterator[RunRecord]:
    """Every record in the store, validated; raises on the first bad line."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield RunRecord.model_validate_json(line)
            except ValidationError as e:
                raise RecordError(f"{path}:{lineno}: invalid record: {e}") from e


def group_by_series(path: pathlib.Path) -> dict[str, list[RunRecord]]:
    groups: dict[str, list[RunRecord]] = collections.defaultdict(list)
    for record in iter_records(path):
        groups[record.series].append(record)
    return dict(groups)


def read(path: pathlib.Path, *, series: str | None = None) -> list[RunRecord]:
    """Records from one series.

    With ``series=None`` the store must hold exactly one series (or none);
    otherwise this raises ``MixedSeriesError`` rather than silently mixing
    results scored under different rubrics, judges or scoring code.
    """
    groups = group_by_series(path)
    if series is not None:
        return groups.get(series, [])
    if len(groups) > 1:
        raise MixedSeriesError({s: len(rs) for s, rs in groups.items()})
    return next(iter(groups.values()), [])

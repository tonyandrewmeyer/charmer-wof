#!/usr/bin/env python3
# Copyright 2026 Tony Meyer
# See LICENSE file for licensing details.
"""Add the ingress endpoint and charm-lib entry to charmcraft.yaml, then fetch the lib.

Idempotent: each step is skipped when it has already been done. Run from the
charm's root directory (the one containing charmcraft.yaml):

    python3 .claude/skills/add-ingress/scripts/add_ingress_endpoint.py

Pass --no-fetch to skip `charmcraft fetch-lib` (e.g. when offline).
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys

LIB_MODULE = "charms.traefik_k8s.v2.ingress"
LIB_FILE = pathlib.Path("lib/charms/traefik_k8s/v2/ingress.py")

ENDPOINT_BLOCK = """requires:
  ingress:
    interface: ingress
    optional: true
    limit: 1
"""
CHARM_LIB_ENTRY = """  - lib: traefik_k8s.ingress
    version: "2"
"""


def _add_endpoint(text: str) -> tuple[str, bool]:
    if re.search(r"^requires:\s*$", text, re.M):
        if re.search(r"^  ingress:\s*$", text, re.M):
            return text, False
        # Append under the existing requires: block.
        return re.sub(
            r"^requires:\s*\n",
            "requires:\n" + ENDPOINT_BLOCK[len("requires:\n") :],
            text,
            count=1,
            flags=re.M,
        ), True
    # No requires: block yet; put one before the configuration section.
    marker = re.search(r"^config:\s*$", text, re.M)
    if marker:
        return text[: marker.start()] + ENDPOINT_BLOCK + "\n" + text[marker.start() :], True
    return text.rstrip("\n") + "\n\n" + ENDPOINT_BLOCK, True


def _add_charm_lib(text: str) -> tuple[str, bool]:
    if re.search(r"lib:\s*traefik_k8s\.ingress\b", text):
        return text, False
    if re.search(r"^charm-libs:\s*\[\s*\]\s*$", text, re.M):
        return re.sub(
            r"^charm-libs:\s*\[\s*\]\s*$",
            "charm-libs:\n" + CHARM_LIB_ENTRY.rstrip("\n"),
            text,
            count=1,
            flags=re.M,
        ), True
    if re.search(r"^charm-libs:\s*$", text, re.M):
        return re.sub(
            r"^charm-libs:\s*\n", "charm-libs:\n" + CHARM_LIB_ENTRY, text, count=1, flags=re.M
        ), True
    return text.rstrip("\n") + "\n\ncharm-libs:\n" + CHARM_LIB_ENTRY, True


def _add_pydantic(pyproject: pathlib.Path) -> bool:
    if not pyproject.exists():
        return False
    text = pyproject.read_text()
    if re.search(r"[\"']pydantic\b", text):
        return False
    new, n = re.subn(
        r"(^dependencies\s*=\s*\[\n)",
        r'\1    # Required by the traefik_k8s.ingress charm library.\n    "pydantic >= 2",\n',
        text,
        count=1,
        flags=re.M,
    )
    if n:
        pyproject.write_text(new)
    return bool(n)


def main() -> int:
    """Apply the charmcraft.yaml/pyproject.toml changes and fetch the library."""
    fetch = "--no-fetch" not in sys.argv
    charmcraft = pathlib.Path("charmcraft.yaml")
    if not charmcraft.exists():
        print("FAIL: run this from the charm root (no charmcraft.yaml here)")
        return 1
    text = charmcraft.read_text()
    text, added_endpoint = _add_endpoint(text)
    text, added_lib = _add_charm_lib(text)
    charmcraft.write_text(text)
    print(f"charmcraft.yaml: requires.ingress {'added' if added_endpoint else 'already present'}")
    state = "added" if added_lib else "already present"
    print(f"charmcraft.yaml: charm-libs traefik_k8s.ingress {state}")
    added_dep = _add_pydantic(pathlib.Path("pyproject.toml"))
    state = "added" if added_dep else "already present or not applicable"
    print(f"pyproject.toml: pydantic dependency {state}")

    if LIB_FILE.exists():
        print(f"{LIB_FILE}: already fetched")
    elif not fetch:
        print(f"{LIB_FILE}: not fetched (--no-fetch)")
    else:
        exe = shutil.which("charmcraft") or "/snap/bin/charmcraft"
        result = subprocess.run(  # noqa: S603
            [exe, "fetch-lib", LIB_MODULE], capture_output=True, text=True
        )
        if result.returncode != 0 or not LIB_FILE.exists():
            print(f"FAIL: charmcraft fetch-lib failed:\n{result.stdout}\n{result.stderr}")
            return 1
        print(f"{LIB_FILE}: fetched")
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

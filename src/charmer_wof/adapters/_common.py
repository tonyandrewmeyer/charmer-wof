"""What the pi, omp and OpenCode adapters share (PLAN §3.1, phase 1).

Both harnesses are run the same way: a subprocess in its own mount namespace
with the gold checkout and this repo hidden, a JSON event stream on stdout
read line by line (and kept verbatim), caps enforced on that stream where the
harness has none of its own, and every OpenRouter generation id looked up
afterwards for the provider that served it. HARNESS.md has the reasoning.
"""

from __future__ import annotations

import collections
import json
import os
import pathlib
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

CONSTRAINTS_FILE = "agent-constraints.txt"

# Mount an empty read-only tmpfs over each path before the ``--``, then exec
# the command after it. Exit 97 means a mount failed, so nothing ran.
_HIDE_SCRIPT = (
    'while [ "$1" != "--" ]; do '
    'mount -t tmpfs -o size=1m,mode=0555 wof-hidden "$1" || exit 97; shift; '
    'done; shift; exec "$@"'
)
HIDE_FAILED = 97

GENERATION_URL = "https://openrouter.ai/api/v1/generation?id={id}"


class AdapterError(RuntimeError):
    """The adapter refused to run (or couldn't set the run up)."""


# --- isolation -----------------------------------------------------------------


def context_files_above(workspace: pathlib.Path, names: Iterable[str]) -> list[pathlib.Path]:
    """Context files the harness would load from the workspace's parents."""
    names = tuple(names)
    found = []
    for parent in workspace.resolve().parents:
        found += [parent / n for n in names if (parent / n).is_file()]
    return found


def hide_wrapper(hide: Sequence[pathlib.Path]) -> list[str]:
    """The command prefix that runs the rest with ``hide`` masked, or [] for nothing to hide."""
    paths = [str(p) for p in hide if p.exists()]
    if not paths:
        return []
    if shutil.which("unshare") is None:
        raise AdapterError("`unshare` is needed to hide the gold checkout from the agent")
    unshare = ["unshare", "--mount"] + ([] if os.geteuid() == 0 else ["--map-root-user"])
    return [*unshare, "sh", "-c", _HIDE_SCRIPT, "wof-hide", *paths, "--"]


def resolve_hidden(workspace: pathlib.Path, hide: Sequence[pathlib.Path]) -> list[pathlib.Path]:
    """``hide`` resolved; refuses a workspace that is itself inside a hidden path."""
    ws = workspace.resolve()
    resolved = [pathlib.Path(p).resolve() for p in hide]
    for h in resolved:
        if ws == h or h in ws.parents:
            raise AdapterError(f"the workspace {ws} is inside a hidden path {h}")
    return resolved


def write_constraints(path: pathlib.Path, charmlint: str) -> pathlib.Path:
    """The ``UV_CONSTRAINT`` file that pins the agent's ``uvx charmlint``."""
    path.write_text(f"charmlint=={charmlint}\n")
    return path


# --- served-by lookup ------------------------------------------------------------

Fetcher = Callable[[str, str], dict[str, Any] | None]


def fetch_generation(gen_id: str, key: str) -> dict[str, Any] | None:
    url = GENERATION_URL.format(id=urllib.parse.quote(gen_id))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("data")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def served_by(
    response_ids: list[str],
    key: str,
    fetch: Fetcher = fetch_generation,
    retry_wait: float = 5.0,
    attempts: int = 5,
) -> dict[str, Any]:
    """Look up each OpenRouter generation: provider, routed model, billed cost.

    The generation endpoint can lag the response (in the omp one-line call by
    more than 5 s; after the OpenCode smoke run, the last two of 60 were still
    missing after 65 s), so ids that aren't found are retried, waiting
    ``retry_wait``, then 3x, 9x and 27x that (5, 15, 45 and 135 s by default:
    up to about 3.5 minutes).
    """
    providers: collections.Counter[str] = collections.Counter()
    models: collections.Counter[str] = collections.Counter()
    billed = 0.0
    missing = list(response_ids)
    for attempt in range(attempts):
        if attempt and not missing:
            break
        if attempt:
            time.sleep(retry_wait * 3 ** (attempt - 1))
        still = []
        for gen_id in missing:
            data = fetch(gen_id, key)
            if not data:
                still.append(gen_id)
                continue
            providers[str(data.get("provider_name"))] += 1
            models[str(data.get("model"))] += 1
            billed += float(data.get("total_cost") or 0.0)
        missing = still
    return {
        "providers": dict(providers),
        "served_models": dict(models),
        "billed_cost_usd": round(billed, 6),
        "generations_found": len(response_ids) - len(missing),
        "generations_missing": len(missing),
    }


def provider_label(providers: Mapping[str, int]) -> str | None:
    """The record's single ``provider``: the one provider, or all of them, busiest first."""
    if not providers:
        return None
    return "+".join(p for p, _ in sorted(providers.items(), key=lambda kv: (-kv[1], kv[0])))


# --- the event stream --------------------------------------------------------------


def stream(
    popen: Callable[..., subprocess.Popen],
    cmd: list[str],
    workspace: pathlib.Path,
    env: dict[str, str],
    events_path: pathlib.Path,
    *,
    feed: Callable[[dict[str, Any]], None],
    check: Callable[[float], str | None],
    on_bad_line: Callable[[], None],
    stop: Callable[[subprocess.Popen], None],
    poll: float = 5.0,
) -> tuple[str, int, str, float]:
    """Run ``cmd``, keep its stdout in ``events_path``, feed each JSON line to ``feed``.

    After every line (and at least every ``poll`` seconds) ``check(elapsed)``
    says whether a cap has been reached; the first exit reason it returns stops
    the process. Returns (exit reason, exit code, stderr, wall seconds).
    """
    start = time.monotonic()
    proc = popen(
        cmd,
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    lines: queue.Queue[bytes | None] = queue.Queue()
    stderr_chunks: list[bytes] = []

    def pump_out() -> None:
        for raw in iter(proc.stdout.readline, b""):
            lines.put(raw)
        lines.put(None)

    def pump_err() -> None:
        stderr_chunks.append(proc.stderr.read())

    threads = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err)]
    for t in threads:
        t.start()

    exit_reason = "completed"
    with events_path.open("wb") as out:
        while True:
            try:
                raw = lines.get(timeout=poll)
            except queue.Empty:
                raw = b""
            if raw is None:
                break
            if raw:
                out.write(raw)
                try:
                    record = json.loads(raw.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    on_bad_line()
                    record = None
                if isinstance(record, dict):
                    feed(record)
            if exit_reason == "completed":
                reason = check(time.monotonic() - start)
                if reason:
                    exit_reason = reason
                    stop(proc)
    rc = proc.wait()
    for t in threads:
        t.join(timeout=5)
    stderr = b"".join(stderr_chunks).decode(errors="replace")
    return exit_reason, rc, stderr, time.monotonic() - start


def stop_process(proc: subprocess.Popen) -> None:
    """SIGTERM the process group, then SIGKILL if it hasn't gone in 10 s."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# --- a recording relay to OpenRouter -------------------------------------------------

_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "accept-encoding",
}  # fmt: skip
_GEN_ID = re.compile(rb'"id"\s*:\s*"(gen-[A-Za-z0-9_-]+)"')
_MODEL = re.compile(rb'"model"\s*:\s*"([^"]+)"')


class RecordingRelay:
    """A local HTTP relay to OpenRouter that notes each response's generation id.

    For a harness that doesn't report OpenRouter's generation ids (OpenCode):
    the harness's OpenRouter base URL is pointed at ``url``, every request is
    forwarded unchanged to ``upstream`` (headers and body as sent, so the
    harness's own key is the one billed), and the response is streamed back as
    it arrives while the relay notes the first ``"id": "gen-..."`` in it and the
    ``model`` the request asked for. It only listens on 127.0.0.1, and runs in
    the adapter's process, outside the agent's mount namespace.
    """

    def __init__(
        self,
        upstream: str = "https://openrouter.ai",
        timeout: float = 1800.0,
        *,
        keep_bodies: bool = False,
    ):
        import http.server

        self.upstream = upstream.rstrip("/")
        self.timeout = timeout
        self.keep_bodies = keep_bodies
        self.bodies: list[bytes] = []
        """Request bodies as sent (only with ``keep_bodies``; for probes and tests)."""
        self.generation_ids: list[str] = []
        self.requested_models: collections.Counter[str] = collections.Counter()
        self.statuses: collections.Counter[int] = collections.Counter()
        self.paths: collections.Counter[str] = collections.Counter()
        self.errors: list[str] = []
        self.offered_tools: list[str] = []
        """Tool names in the first request that offered any (what the model could call)."""
        self.client_resets = 0
        """Responses the harness stopped reading before the end (after the final event)."""
        self._lock = threading.Lock()
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                relay._forward(self)

            def do_POST(self) -> None:
                relay._forward(self)

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                # The harness closing an idle keep-alive connection between
                # requests isn't an error worth a traceback on stderr.
                import sys

                if isinstance(sys.exc_info()[1], ConnectionError):
                    return
                super().handle_error(request, client_address)

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> RecordingRelay:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _forward(self, handler: Any) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else None
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in _HOP_HEADERS}
        model = _MODEL.search(body or b"")
        with self._lock:
            self.paths[handler.path.split("?", 1)[0]] += 1
            if model and handler.command == "POST":
                self.requested_models[model.group(1).decode()] += 1
            if body and not self.offered_tools:
                self.offered_tools = _tool_names(body)
            if body and self.keep_bodies:
                self.bodies.append(body)
        req = urllib.request.Request(
            self.upstream + handler.path, data=body, headers=headers, method=handler.command
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            resp = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            with self._lock:
                self.errors.append(f"{handler.path}: {e}")
            handler.send_error(502, "relay could not reach upstream")
            return
        with self._lock:
            self.statuses[resp.status] += 1
        handler.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in _HOP_HEADERS:
                handler.send_header(k, v)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        seen = b""
        found = False
        try:
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                if not found:
                    seen = seen[-256:] + chunk
                    m = _GEN_ID.search(seen)
                    if m:
                        found = True
                        with self._lock:
                            self.generation_ids.append(m.group(1).decode())
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            with self._lock:
                self.client_resets += 1
        finally:
            resp.close()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "relay_requests": sum(self.paths.values()),
                "relay_paths": dict(self.paths),
                "relay_statuses": {str(k): v for k, v in self.statuses.items()},
                "requested_models": dict(self.requested_models),
                "relay_errors": list(self.errors),
                "relay_client_resets": self.client_resets,
                "offered_tools": list(self.offered_tools),
                "generation_ids": list(self.generation_ids),
            }


def _tool_names(body: bytes) -> list[str]:
    """The tool names an OpenAI-style chat request offers the model."""
    try:
        tools = json.loads(body).get("tools") or []
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return []
    names = [(t.get("function") or {}).get("name") or t.get("name") for t in tools]
    return sorted(str(n) for n in names if n)


# --- OpenRouter's model list ---------------------------------------------------

MODELS_URL = "https://openrouter.ai/api/v1/models"

ModelsFetcher = Callable[[], list[dict[str, Any]] | None]


def fetch_models() -> list[dict[str, Any]] | None:
    try:
        with urllib.request.urlopen(MODELS_URL, timeout=30) as resp:
            return json.load(resp).get("data")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def model_limits(openrouter_id: str, models: list[dict[str, Any]] | None) -> dict[str, int]:
    """``contextWindow`` and ``maxTokens`` for a model, from OpenRouter's model list.

    Matches the id or the ``canonical_slug`` (a dated id is the slug of an
    undated listing). Empty if the model isn't listed or the list couldn't be read.
    """
    for m in models or []:
        if openrouter_id not in (m.get("id"), m.get("canonical_slug")) or ":" in str(m.get("id")):
            continue
        out = {}
        if isinstance(m.get("context_length"), int):
            out["contextWindow"] = m["context_length"]
        top = (m.get("top_provider") or {}).get("max_completion_tokens")
        if isinstance(top, int):
            out["maxTokens"] = top
        return out
    return {}

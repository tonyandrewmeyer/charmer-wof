"""The agent's network: its own namespace, with one way out through an allowlist.

The agent runs in a network namespace of its own (``unshare --net``), where the
only interface is loopback, so nothing it starts can reach the internet
directly or resolve a name. Inside the namespace, a small forwarder listens on
loopback ports and passes each connection to a Unix socket in a directory the
adapter creates. Outside, in the adapter's process, the socket for
``PROXY_PORT`` is an HTTP ``CONNECT`` proxy that only opens tunnels to the
hosts in ``AGENT_EGRESS``; the others relay to ports on the host's loopback
(OpenCode's recording relay).

The harnesses and the tools the agent runs (uv, pip, curl, git, charmcraft)
reach the internet through the proxy because the adapter sets ``HTTPS_PROXY``
and friends. A tool that ignores them gets no network at all, which is the
point: the allowlist doesn't depend on every process honouring the proxy, or
on the CI runner's own egress filter (harden-runner allows GitHub's domains
whatever its allowlist says, so an agent could fetch its gold from GitHub).
The tunnels carry TLS end to end; the proxy sees only the host it is asked
for, which it records.
"""

from __future__ import annotations

import collections
import contextlib
import os
import pathlib
import socket
import socketserver
import tempfile
import threading
from collections.abc import Iterable, Sequence
from typing import Any

# Hosts the agent may reach, port 443 only. Kept in step with the agent job's
# harden-runner allowlist in .github/workflows/eval.yml.
AGENT_EGRESS = (
    # The model (OpenCode goes through the recording relay instead).
    "openrouter.ai",
    # The agent's uv run / uvx: ops, pytest, charmlint, charmcraft.
    "pypi.org",
    "files.pythonhosted.org",
    # charmcraft fetch-lib[s]: the new-charm briefs start with no lib/.
    "api.charmhub.io",
    "storage.snapcraftcontent.com",
)

# The proxy's port inside the agent's namespace (a fresh namespace, so free).
PROXY_PORT = 3128

# Exit code of the isolation wrapper when the namespace's network couldn't be
# set up (loopback or the forwarder), so the agent never ran.
NET_FAILED = 96

# The forwarder, run with the adapter's own Python inside the namespace before
# anything is hidden. Arguments: "port=socket,port=socket,..." and the pid of
# the wrapper shell, which execs into the agent. It binds every port, then
# forks: the parent exits 0 (so the wrapper knows the ports are listening) and
# the child serves with stdio on /dev/null, so it doesn't hold the agent's
# stdout open, and exits once the agent's pid is gone.
FORWARDER = r"""
import os, socket, sys, threading
pairs = [s.split("=", 1) for s in sys.argv[1].split(",") if s]
listeners = []
for port, path in pairs:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", int(port)))
    s.listen(128)
    listeners.append((s, path))
if os.fork():
    os._exit(0)
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)

def pipe(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass

def serve(listener, path):
    while True:
        conn, _ = listener.accept()
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            up.connect(path)
        except OSError:
            conn.close()
            continue
        threading.Thread(target=pipe, args=(conn, up), daemon=True).start()
        threading.Thread(target=pipe, args=(up, conn), daemon=True).start()

for lp in listeners:
    threading.Thread(target=serve, args=lp, daemon=True).start()
import time
agent = int(sys.argv[2])
while True:
    time.sleep(1)
    try:
        os.kill(agent, 0)
    except ProcessLookupError:
        os._exit(0)
    except PermissionError:
        pass
"""

# Bring loopback up, start the forwarder, mask each hidden path, then exec the
# agent. Arguments: python, forwarder spec, hidden paths..., "--", command...
#
# The agent also gets a private XDG_RUNTIME_DIR, a tmpfs over RUNTIME_DIR. In
# the namespace the agent is uid 0 and everything the real root owns shows as
# uid 65534, and platformdirs (charmcraft's runtime directory) refuses any
# XDG_RUNTIME_DIR below a directory owned by anyone but root or the caller, so
# no ordinary path passes: `charmcraft fetch-lib` failed in every block-mode
# run until this. A tmpfs mounted on a top-level directory is owned by the
# agent, and `/` itself isn't checked. The mount is only seen by the agent.
RUNTIME_DIR = "/srv"
_WRAPPER_SCRIPT = (
    "ip link set lo up || exit 96; "
    'py="$1"; spec="$2"; shift 2; '
    '"$py" -I -c "$WOF_FORWARDER" "$spec" "$$" || exit 96; '
    "unset WOF_FORWARDER; "
    f"mount -t tmpfs -o size=16m,mode=0700 wof-runtime {RUNTIME_DIR} || exit 96; "
    f"export XDG_RUNTIME_DIR={RUNTIME_DIR}; "
    'while [ "$1" != "--" ]; do '
    'mount -t tmpfs -o size=1m,mode=0555 wof-hidden "$1" || exit 97; shift; '
    'done; shift; exec "$@"'
)


def allowed(host: str, allow: Iterable[str] = AGENT_EGRESS) -> bool:
    """Whether ``host`` is on the list: an exact name, or under a ``*.`` entry."""
    host = host.lower().rstrip(".")
    for entry in allow:
        entry = entry.lower()
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        b.shutdown(socket.SHUT_WR)


def _splice(a: socket.socket, b: socket.socket) -> None:
    """Copy both ways until both sides have finished."""
    t = threading.Thread(target=_pipe, args=(b, a), daemon=True)
    t.start()
    _pipe(a, b)
    t.join()


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class AgentNetwork:
    """The Unix sockets behind the agent's namespace, and what went through them.

    ``forward`` lists host loopback ports to make reachable at the same port
    inside the namespace (the recording relay's). Use as a context manager
    around the agent's run; ``wrapper()`` gives the command prefix and ``env()``
    the proxy variables for the agent's environment.
    """

    def __init__(
        self,
        allow: Sequence[str] = AGENT_EGRESS,
        forward: Sequence[int] = (),
        *,
        connect_timeout: float = 30.0,
    ):
        self.allow = tuple(allow)
        self.forward = tuple(forward)
        self.connect_timeout = connect_timeout
        self.allowed: collections.Counter[str] = collections.Counter()
        self.refused: collections.Counter[str] = collections.Counter()
        self.failed: collections.Counter[str] = collections.Counter()
        self._lock = threading.Lock()
        self._servers: list[_UnixServer] = []
        self._dir: str | None = None

    # --- lifecycle

    def __enter__(self) -> AgentNetwork:
        # Under /tmp: a Unix socket path has to fit in 108 bytes.
        self._dir = tempfile.mkdtemp(prefix="wof-net-", dir="/tmp")
        os.chmod(self._dir, 0o700)
        net = self

        class Proxy(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                net._proxy(self)

        self._serve(self.socket_path(PROXY_PORT), Proxy)
        for port in self.forward:

            class Relay(socketserver.BaseRequestHandler):
                target = port

                def handle(self) -> None:
                    try:
                        up = socket.create_connection(("127.0.0.1", self.target), timeout=10)
                    except OSError:
                        return
                    up.settimeout(None)
                    with up:
                        _splice(self.request, up)

            self._serve(self.socket_path(port), Relay)
        return self

    def __exit__(self, *exc: Any) -> None:
        for s in self._servers:
            s.shutdown()
            s.server_close()
        if self._dir:
            for p in pathlib.Path(self._dir).iterdir():
                p.unlink(missing_ok=True)
            os.rmdir(self._dir)

    def _serve(self, path: str, handler: type) -> None:
        server = _UnixServer(path, handler)
        self._servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def socket_path(self, port: int) -> str:
        assert self._dir, "use AgentNetwork as a context manager"
        return os.path.join(self._dir, f"{port}.sock")

    # --- the proxy

    def _proxy(self, handler: socketserver.StreamRequestHandler) -> None:
        line = handler.rfile.readline(8192).decode("latin-1").strip()
        while handler.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
            pass
        parts = line.split()
        method = parts[0].upper() if parts else ""
        target = parts[1] if len(parts) > 1 else ""
        if method != "CONNECT":
            # Plain-HTTP proxying isn't offered: everything on the list is HTTPS.
            host = target.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            self._count(self.refused, f"{host or '?'} ({method or '?'})")
            handler.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        host, _, port = target.rpartition(":")
        host = host.strip("[]")
        if port != "443" or not allowed(host, self.allow):
            self._count(self.refused, target)
            handler.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            up = socket.create_connection((host, 443), timeout=self.connect_timeout)
        except OSError:
            self._count(self.failed, host)
            handler.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        self._count(self.allowed, host)
        up.settimeout(None)
        handler.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        handler.wfile.flush()
        with up:
            _splice(handler.connection, up)

    def _count(self, counter: collections.Counter[str], key: str) -> None:
        with self._lock:
            counter[key] += 1

    # --- what the adapter uses

    def spec(self) -> str:
        ports = (PROXY_PORT, *self.forward)
        return ",".join(f"{p}={self.socket_path(p)}" for p in ports)

    def env(self) -> dict[str, str]:
        """The agent's proxy variables.

        HTTPS only. Everything on the allowlist is HTTPS, and a charm's own unit
        tests often make plain-HTTP requests to made-up hosts (alertmanager's
        to ``http://fqdn:9093``) that are meant to fail as connection errors;
        through a proxy they would get a 403 response instead, and the agent
        would see different test failures from the ones a developer sees. So
        plain HTTP goes direct and fails as it would without a network.
        ``isolated`` drops any inherited ``HTTP_PROXY``/``ALL_PROXY``.
        """
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        no_proxy = "127.0.0.1,localhost,::1"
        return {
            "HTTPS_PROXY": proxy,
            "https_proxy": proxy,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
            # Node's fetch ignores the variables above unless asked (pi).
            "NODE_USE_ENV_PROXY": "1",
            # Handed to the wrapper script, which unsets it before the agent starts.
            "WOF_FORWARDER": FORWARDER,
        }

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "egress_allowed": dict(self.allowed),
                "egress_refused": dict(self.refused),
                "egress_failed": dict(self.failed),
            }


def isolation_wrapper(
    network: AgentNetwork, hide: Sequence[pathlib.Path], python: str
) -> list[str]:
    """The command prefix: own mount and network namespaces, ``hide`` masked."""
    import shutil

    for tool in ("unshare", "ip"):
        if shutil.which(tool) is None:
            from ._common import AdapterError

            raise AdapterError(f"`{tool}` is needed to isolate the agent")
    unshare = ["unshare", "--mount", "--net"] + ([] if os.geteuid() == 0 else ["--map-root-user"])
    paths = [str(p) for p in hide if p.exists()]
    return [
        *unshare,
        "sh",
        "-c",
        _WRAPPER_SCRIPT,
        "wof-isolate",
        python,
        network.spec(),
        *paths,
        "--",
    ]


# Proxy variables the agent must not inherit (see ``AgentNetwork.env``).
_PLAIN_PROXY_VARS = ("http_proxy", "all_proxy")

# Exit codes of the wrapper that mean the agent never ran: the network
# couldn't be set up, or a path couldn't be hidden (``_common.HIDE_FAILED``).
ISOLATION_FAILED = (NET_FAILED, 97)


@contextlib.contextmanager
def isolated(
    cmd: Sequence[str],
    env: dict[str, str],
    hide: Sequence[pathlib.Path],
    forward: Sequence[int] = (),
):
    """The agent's command and environment, isolated, and the network behind them.

    Yields ``(cmd, env, network)``: ``cmd`` wrapped to run in its own mount and
    network namespaces with ``hide`` masked and a private ``XDG_RUNTIME_DIR``
    (see ``RUNTIME_DIR``), and ``env`` with the proxy variables.
    """
    import sys

    with AgentNetwork(forward=forward) as net:
        full_env = {k: v for k, v in env.items() if k.lower() not in _PLAIN_PROXY_VARS}
        full_env.update(net.env())
        yield [*isolation_wrapper(net, hide, sys.executable), *cmd], full_env, net

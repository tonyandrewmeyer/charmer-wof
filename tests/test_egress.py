"""The agent's network (adapters._egress): the allowlist, the proxy, and the namespace."""

from __future__ import annotations

import os
import pathlib
import socket
import socketserver
import subprocess
import sys
import threading
import time

import pytest

from charmer_wof.adapters import _egress


def test_allowed_matches_names_and_wildcards():
    allow = ("openrouter.ai", "*.example.com")
    assert _egress.allowed("openrouter.ai", allow)
    assert _egress.allowed("OpenRouter.AI.", allow)
    assert _egress.allowed("a.b.example.com", allow)
    assert not _egress.allowed("example.com", allow)
    assert not _egress.allowed("api.openrouter.ai", allow)
    assert not _egress.allowed("openrouter.ai.evil.test", allow)


def test_default_allowlist_has_no_github_host():
    for host in ("github.com", "api.github.com", "codeload.github.com",
                 "raw.githubusercontent.com", "objects.githubusercontent.com"):  # fmt: skip
        assert not _egress.allowed(host)


def _ask(path: str, request: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(path)
        s.sendall(request)
        return s.recv(4096)


def test_proxy_refuses_hosts_off_the_list_and_plain_http():
    with _egress.AgentNetwork(allow=("openrouter.ai",)) as net:
        path = net.socket_path(_egress.PROXY_PORT)
        reply = _ask(path, b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
        reply = _ask(path, b"CONNECT openrouter.ai:80 HTTP/1.1\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
        reply = _ask(path, b"GET http://pypi.org/simple/ HTTP/1.1\r\nHost: pypi.org\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
        assert net.summary() == {
            "egress_allowed": {},
            "egress_refused": {"github.com:443": 1, "openrouter.ai:80": 1, "pypi.org (GET)": 1},
            "egress_failed": {},
        }


def test_env_points_every_client_at_the_proxy():
    with _egress.AgentNetwork() as net:
        env = net.env()
    proxy = f"http://127.0.0.1:{_egress.PROXY_PORT}"
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        assert env[k] == proxy
    assert env["NODE_USE_ENV_PROXY"] == "1"
    assert "127.0.0.1" in env["NO_PROXY"]


class _Echo(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.sendall(b"relay:" + self.request.recv(100))


def _run_isolated(script: str, hide=(), forward=()) -> tuple[subprocess.CompletedProcess, dict]:
    with _egress.isolated(["sh", "-c", script], dict(os.environ), list(hide), forward) as (
        cmd,
        env,
        net,
    ):
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=60, start_new_session=True
        )
        return proc, net.summary()


def _skip_without_namespaces(proc: subprocess.CompletedProcess) -> None:
    if proc.returncode in _egress.ISOLATION_FAILED or "unshare" in proc.stderr:
        pytest.skip(f"no user/network namespaces here: {proc.stderr.strip()[-200:]}")


def test_isolated_agent_reaches_only_the_proxy_and_forwarded_ports(tmp_path):
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    secret = tmp_path / "gold"
    secret.mkdir()
    (secret / "charm.py").write_text("the answer")
    py = sys.executable
    script = f"""
{py} - <<'EOF'
import os, socket
def try_connect(addr):
    try:
        socket.create_connection(addr, timeout=3).close()
        return "connected"
    except OSError as e:
        return type(e).__name__
print("direct", try_connect(("1.1.1.1", 443)))
s = socket.create_connection(("127.0.0.1", {_egress.PROXY_PORT}), timeout=5)
s.sendall(b"CONNECT github.com:443 HTTP/1.1\\r\\n\\r\\n")
print("proxy", s.recv(100).split(b"\\r\\n")[0].decode())
r = socket.create_connection(("127.0.0.1", {port}), timeout=5)
r.sendall(b"hi")
print("forward", r.recv(100).decode())
print("hidden", os.listdir({str(secret)!r}))
rt = os.environ["XDG_RUNTIME_DIR"]
print("runtime", rt, oct(os.stat(rt).st_mode & 0o777))
EOF
"""
    proc, summary = _run_isolated(script, hide=[secret], forward=(port,))
    server.shutdown()
    _skip_without_namespaces(proc)
    lines = dict(line.split(" ", 1) for line in proc.stdout.splitlines())
    assert lines["direct"] in ("OSError", "ConnectionRefusedError", "TimeoutError"), proc.stdout
    assert lines["proxy"] == "HTTP/1.1 403 Forbidden"
    assert lines["forward"] == "relay:hi"
    assert lines["hidden"] == "[]"
    assert lines["runtime"] == f"{_egress.RUNTIME_DIR} 0o700"
    assert summary["egress_refused"] == {"github.com:443": 1}
    assert (secret / "charm.py").read_text() == "the answer"  # only hidden inside


def test_forwarder_exits_with_the_agent():
    proc, _ = _run_isolated("echo started")
    _skip_without_namespaces(proc)
    deadline = time.monotonic() + 5
    found: list[str] = []
    while time.monotonic() < deadline:
        found = subprocess.run(
            ["pgrep", "-f", "import os, socket, sys, threading"], capture_output=True, text=True
        ).stdout.split()
        if not [pid for pid in found if pathlib.Path(f"/proc/{pid}").exists()]:
            return
        time.sleep(0.5)
    pytest.fail(f"forwarder still running: {found}")

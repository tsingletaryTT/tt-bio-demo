"""scripts/run-webview.sh: the browser viewer's own launcher.

Unlike scripts/run-demo.sh's test file, this one runs the REAL script
end to end rather than stubbing interpreters: webview.bridge and
runner.mock both import nothing from torch/tt_bio (webview/README.md's own
claim, checked directly here), so there is no device to accidentally open
and nothing that needs faking. A real HTTP request against the real bridge
is a stronger check than asserting on argv.

The one thing worth pinning structurally rather than by running the script
is the socket-path default: this script and scripts/run-demo.sh must agree
on where the daemon lives WITHOUT either one hardcoding a copy of the
other's path -- the same "one name, two places, nothing checking they still
agree" defect class this project has paid for before (weights-cache
variables, the Ctrl+A exit code, the .boltz path spelling). See
test_the_socket_default_is_the_same_expression_in_both_scripts below.
"""

import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUN_WEBVIEW = REPO_ROOT / "scripts" / "run-webview.sh"
RUN_DEMO = REPO_ROOT / "scripts" / "run-demo.sh"
VENV_UI_PYTHON = REPO_ROOT / ".venvs" / "venv-ui" / "bin" / "python3"

pytestmark = pytest.mark.skipif(
    not VENV_UI_PYTHON.is_file(),
    reason="venv-ui not built (scripts/setup-venvs.sh) -- nothing to run")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_script_is_executable():
    assert RUN_WEBVIEW.is_file()
    assert RUN_WEBVIEW.stat().st_mode & 0o111, "run-webview.sh is not executable"


def test_help_exits_zero_and_documents_the_real_flags():
    result = subprocess.run(
        ["bash", str(RUN_WEBVIEW), "--help"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    for flag in ("--socket", "--port", "--host", "--mock", "--open"):
        assert flag in result.stdout, f"--help doesn't mention {flag}"


def test_an_unknown_option_is_rejected_loudly():
    result = subprocess.run(
        ["bash", str(RUN_WEBVIEW), "--not-a-real-flag"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "unknown option" in result.stderr


def test_mock_mode_serves_a_real_bridge_over_http():
    """No daemon, no device, no --hw needed: `--mock` replays a real
    fixture over a throwaway socket and the bridge in front of it answers
    real HTTP requests. Polls rather than sleeping a fixed amount, since
    process startup time is not this test's own property to assume."""
    port = _free_port()
    proc = subprocess.Popen(
        ["bash", str(RUN_WEBVIEW), "--mock", "--port", str(port)],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        deadline = time.monotonic() + 15
        last_exc = None
        html = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
                    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    chunks = []
                    while True:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    html = b"".join(chunks)
                break
            except OSError as exc:
                last_exc = exc
                time.sleep(0.3)
        assert html is not None, f"bridge never came up on 127.0.0.1:{port}: {last_exc}"
        assert html.startswith(b"HTTP/1.0 200") or html.startswith(b"HTTP/1.1 200")
        assert b"tt-bio-demo" in html
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_mock_mode_rejects_an_unknown_fixture():
    result = subprocess.run(
        ["bash", str(RUN_WEBVIEW), "--mock", "tests/fixtures/streams/nope.jsonl",
         "--port", str(_free_port())],
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "no such fixture" in result.stderr


def _default_socket_expr(script_text):
    """Pull the two lines that together define the default daemon socket
    path, stripped of leading whitespace -- the exact text, not a
    re-derivation of what it MEANS, so this only passes when both scripts
    spell it identically."""
    runtime_dir = re.search(r'^RUNTIME_DIR="([^"]+)"', script_text, re.MULTILINE)
    socket_default = re.search(r'^SOCKET="([^"]+)"', script_text, re.MULTILINE)
    assert runtime_dir, "no RUNTIME_DIR= line found"
    assert socket_default, "no SOCKET= line found"
    return runtime_dir.group(1), socket_default.group(1)


def test_the_socket_default_is_the_same_expression_in_both_scripts():
    webview_runtime, webview_socket = _default_socket_expr(RUN_WEBVIEW.read_text())
    demo_runtime, demo_socket = _default_socket_expr(RUN_DEMO.read_text())
    assert webview_runtime == demo_runtime, (
        "run-webview.sh and run-demo.sh no longer agree on RUNTIME_DIR's "
        f"default: {webview_runtime!r} vs {demo_runtime!r}")
    assert webview_socket == demo_socket, (
        "run-webview.sh and run-demo.sh no longer agree on the daemon "
        f"socket's default path: {webview_socket!r} vs {demo_socket!r}")

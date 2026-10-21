"""The reviewed executor travels over stdin, never a Linux-sized argv item."""

import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
from unittest import mock

import pytest
import test_release_controller as existing

controller = existing.controller


def frame(source, *, header=None, suffix=b"\n"):
    header = header or {"source_length": len(source), "source_sha256": controller.sha(source)}
    return json.dumps(header).encode() + b"\n" + source + suffix


def bootstrap(source, data):
    return subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", controller.EXECUTOR_BOOTSTRAP, controller.sha(source)],
        input=data,
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_large_reviewed_source_executes_main_and_preserves_payload_and_permits():
    body = b"""import hashlib, json, sys
stream = sys.stdin.buffer
header = json.loads(stream.readline())
payload = stream.read(header['length'])
separator = stream.read(1)
permit = json.loads(stream.readline())
print(json.dumps({'payload_sha256': hashlib.sha256(payload).hexdigest(), 'separator': separator.decode(), 'permit': permit, 'name': __name__, 'file': __file__}))
"""
    source = b"#" + b"reviewed-comment-" * 9000 + b"\n" + body
    assert len(source) > 128 * 1024
    payload = json.dumps({"action": "inspect", "fixture": "safe"}).encode()
    permit = {"permit": "a" * 32}
    data = (
        frame(source)
        + json.dumps({"length": len(payload)}).encode()
        + b"\n"
        + payload
        + b"\n"
        + json.dumps(permit).encode()
        + b"\n"
    )
    result = bootstrap(source, data)
    assert result.returncode == 0 and result.stderr == b""
    observed = json.loads(result.stdout)
    assert observed == {
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "separator": "\n",
        "permit": permit,
        "name": "__main__",
        "file": "/__prism_reviewed_executor__.py",
    }
    command = controller.ssh_argv(source)[-1]
    assert len(command.encode()) < 8192
    assert "reviewed-comment-" not in command
    assert shlex.split(command)[-2:] == [controller.EXECUTOR_BOOTSTRAP, controller.sha(source)]
    assert "-I -B -S" in command


@pytest.mark.parametrize(
    "case",
    [
        "header_hash",
        "body_hash",
        "truncated",
        "too_long",
        "bool_length",
        "extra_key",
        "separator",
        "oversized_header",
    ],
)
def test_invalid_source_is_rejected_before_any_source_execution(case):
    source = b"print('SOURCE_EXECUTED')\n"
    expected = controller.sha(source)
    if case == "header_hash":
        data = frame(source, header={"source_length": len(source), "source_sha256": "0" * 64})
    elif case == "body_hash":
        data = frame(source + b"x", header={"source_length": len(source) + 1, "source_sha256": expected})
    elif case == "truncated":
        data = frame(
            source, header={"source_length": len(source) + 10, "source_sha256": expected}, suffix=b""
        )
    elif case == "too_long":
        data = frame(source, header={"source_length": 512 * 1024 + 1, "source_sha256": expected})
    elif case == "bool_length":
        data = frame(source, header={"source_length": True, "source_sha256": expected})
    elif case == "extra_key":
        data = frame(
            source, header={"source_length": len(source), "source_sha256": expected, "arbitrary_digest": True}
        )
    elif case == "separator":
        data = frame(source, suffix=b"x")
    else:
        data = b"x" * 257 + b"\n" + source
    result = bootstrap(source, data)
    assert result.returncode != 0
    assert result.stdout.strip() == b'{"error":"executor_bootstrap_rejected"}'
    assert b"SOURCE_EXECUTED" not in result.stdout + result.stderr
    assert not result.stderr


def test_expected_argv_digest_cannot_be_replaced_by_the_stdin_header():
    source = b"print('SOURCE_EXECUTED')\n"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", controller.EXECUTOR_BOOTSTRAP, "0" * 64],
        input=frame(source),
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0 and b"SOURCE_EXECUTED" not in result.stdout + result.stderr


def test_controller_frames_source_then_the_original_request_without_argv_source():
    class Input(io.BytesIO):
        saved = b""

        def close(self):
            self.saved = self.getvalue()
            super().close()

    class Process:
        def __init__(self):
            read, write = os.pipe()
            self.stdout = os.fdopen(read, "rb")
            os.write(write, b'{"result":{"production_verified":false}}\n')
            os.close(write)
            self.stdin, self.code = Input(), None

        def poll(self):
            return self.code

        def wait(self, timeout=None):
            self.code = 0
            return 0

    source, request = b"# reviewed inert source\n", {"action": "inspect"}
    process = Process()
    try:
        controller.execute(
            request,
            source,
            "/nonexistent/prism-stop",
            popen=lambda *args, **kwargs: process,
            gate=lambda flag: None,
        )
        stream = io.BytesIO(process.stdin.saved)
        header = json.loads(stream.readline())
        assert header == {"source_length": len(source), "source_sha256": controller.sha(source)}
        assert stream.read(header["source_length"]) == source
        assert stream.read(1) == b"\n"
        request_header = json.loads(stream.readline())
        assert json.loads(stream.read(request_header["length"])) == request
        assert stream.read() == b"\n"
    finally:
        process.stdout.close()


def test_only_the_previously_reviewed_digest_can_load_the_local_executor(tmp_path):
    source = b"# reviewed inert source\n"
    (tmp_path / "remote_release.py").write_bytes(source)
    with mock.patch.object(controller, "ROOT", tmp_path):
        with pytest.raises(controller.ControlError, match="reviewed_executor_digest_mismatch"):
            controller.trusted_source("remote_release.py", "0" * 64)
        assert controller.trusted_source("remote_release.py", controller.sha(source)) == source
    with pytest.raises(controller.ControlError, match="reviewed_executor_source_limits"):
        controller.ssh_argv(b"x" * (512 * 1024 + 1))

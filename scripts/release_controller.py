#!/usr/bin/env python3
"""Mac-side fixed-target release controller. One explicit stage per invocation.

No credentials are stored. Defaults to a plan; --execute is required for SSH.
Receipts are operator-reviewed evidence, not cryptographic attestations of tests.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import select
import shlex
import subprocess
import time
import types
import urllib.request
from pathlib import Path

HOST = "ubuntu@124.223.168.224"
ISSUE = "https://api.github.com/repos/doubleh-ai/prism/issues/9"
ACTIONS = ("inspect", "prepare", "stage", "quiesce", "switch", "verify", "rollback", "resume")
ROOT = Path(__file__).resolve().parent


class ControlError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ControlError("operator_issue_redirect_rejected")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def outside_git(path):
    path = Path(path).expanduser().resolve(strict=True)
    if any((parent / ".git").exists() for parent in (path.parent, *path.parents)):
        raise ControlError("private_input_inside_git_workspace")
    if not path.is_file() or path.stat().st_size > 192 * 1024 * 1024:
        raise ControlError("invalid_private_input")
    return path


def check_gate(stop_file, opener=None):
    if Path(stop_file).exists():
        raise ControlError("local_stop_requested")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        request = urllib.request.Request(
            ISSUE,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Prism-reviewed-release-controller",
            },
        )
        with opener.open(request, timeout=10) as response:
            if response.status != 200 or response.geturl() != ISSUE:
                raise ControlError("operator_issue_unavailable")
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ControlError("operator_issue_oversized")
        issue = json.loads(raw)
        if (
            issue.get("number") != 9
            or issue.get("state") != "open"
            or issue.get("repository_url") != "https://api.github.com/repos/doubleh-ai/prism"
            or issue.get("user", {}).get("login", "").lower() != "huadous"
            or "pull_request" in issue
        ):
            raise ControlError("operator_issue_closed_or_unknown")
    except ControlError:
        raise
    except Exception:
        raise ControlError("operator_issue_unavailable") from None
    if Path(stop_file).exists():
        raise ControlError("local_stop_requested")


def trusted_source(filename, expected):
    content = (ROOT / filename).read_bytes()
    if not re.fullmatch(r"[0-9a-f]{64}", expected or "") or sha(content) != expected:
        raise ControlError("reviewed_executor_digest_mismatch")
    return content


def request_for(args):
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{7,63}", args.release_id
    ):
        raise ControlError("explicit_release_identity_required")
    source = trusted_source("remote_release.py", args.executor_sha256)
    request = {"action": args.action, "commit": args.commit, "release_id": args.release_id}
    if args.action == "inspect":
        return request, source
    receipt_path = outside_git(args.receipts)
    receipt_raw = receipt_path.read_bytes()
    receipts = json.loads(receipt_raw)
    if receipts.get("commit") != args.commit:
        raise ControlError("receipt_commit_mismatch")
    request.update(
        {
            "receipts": receipts,
            "receipts_sha256": sha(receipt_raw),
            "receipts_raw": base64.b64encode(receipt_raw).decode(),
            "archive_sha256": receipts.get("archive_sha256"),
            "manifest_sha256": receipts.get("manifest_sha256"),
        }
    )
    if args.source_manifest:
        manifest = json.loads(outside_git(args.source_manifest).read_bytes())
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        if sha(canonical) != receipts.get("expected_source_manifest_sha256"):
            raise ControlError("source_manifest_receipt_mismatch")
        request["source_manifest"] = manifest
    if args.external_source_manifest:
        external = json.loads(outside_git(args.external_source_manifest).read_bytes())
        canonical = json.dumps(external, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        if sha(canonical) != receipts.get("dependencies", {}).get("external_sources_sha256"):
            raise ControlError("external_source_manifest_receipt_mismatch")
        request["external_source_manifest"] = external
    if args.action == "prepare":
        verified_source = trusted_source("release_candidate.py", args.verifier_sha256)
        verifier = types.ModuleType("reviewed_candidate_verifier")
        verifier.__file__ = str(ROOT / "release_candidate.py")
        exec(compile(verified_source, verifier.__file__, "exec"), verifier.__dict__)
        archive = outside_git(args.archive)
        manifest = outside_git(args.manifest)
        verified = verifier.verify_candidate(
            archive, manifest, expected_commit=args.commit, repo=Path(args.repo)
        )
        if (
            verified["archive_sha256"] != request["archive_sha256"]
            or sha(manifest.read_bytes()) != request["manifest_sha256"]
        ):
            raise ControlError("candidate_receipt_mismatch")
        request["archive"] = base64.b64encode(archive.read_bytes()).decode()
        lock = outside_git(args.lock)
        wheels = Path(args.wheelhouse).resolve(strict=True)
        if not wheels.is_dir() or any((p / ".git").exists() for p in (wheels, *wheels.parents)):
            raise ControlError("wheelhouse_must_be_outside_git")
        request["dependencies"] = []
        paths = [("core.lock", lock)] + [
            (path.name, outside_git(path)) for path in sorted(wheels.glob("*.whl"))
        ]
        if len(paths) < 2 or sum(path.stat().st_size for _, path in paths) > 192 * 1024 * 1024:
            raise ControlError("wheelhouse_limits")
        for name, path in paths:
            content = path.read_bytes()
            request["dependencies"].append(
                {"name": name, "sha256": sha(content), "content": base64.b64encode(content).decode()}
            )
        lock_receipt = receipts.get("dependencies", {})
        manifest_data = json.loads(manifest.read_bytes())
        lock_entry = next(
            (entry for entry in manifest_data["files"] if entry["path"] == "requirements.lock"), {}
        )
        if lock_receipt.get("lock_sha256") != sha(lock.read_bytes()) or lock_entry.get("sha256") != sha(
            lock.read_bytes()
        ):
            raise ControlError("reviewed_lock_digest_required")
    return request, source


def ssh_argv(source):
    command = "/usr/bin/sudo -n /usr/bin/python3 -I -c " + shlex.quote(source.decode())
    return [
        "/usr/bin/ssh",
        "-T",
        "-p",
        "22",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "SendEnv=-*",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=600",
        "-o",
        "ControlPath=/tmp/prism-release-ssh-%C",
        HOST,
        command,
    ]


def execute(request, source, stop_file, *, popen=subprocess.Popen, gate=check_gate):
    gate(stop_file)
    process = popen(
        ssh_argv(source), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        payload = json.dumps(request, separators=(",", ":")).encode()
        if len(payload) > 384 * 1024 * 1024:
            raise ControlError("request_limits")
        process.stdin.write(json.dumps({"length": len(payload)}).encode() + b"\n")
        process.stdin.write(payload + b"\n")
        process.stdin.flush()
        deadline = time.monotonic() + 1800
        last_gate, cancelled = time.monotonic(), False

        def notify_stop():
            nonlocal cancelled, deadline
            cancelled = True
            # An in-flight isolated copy/install may finish. The executor consumes
            # this at its next boundary; no later service mutation is permitted.
            process.stdin.write(b'{"stop":true}\n')
            process.stdin.flush()
            deadline = min(deadline, time.monotonic() + 120)

        while time.monotonic() < deadline:
            if not cancelled and (Path(stop_file).exists() or time.monotonic() - last_gate >= 60):
                try:
                    gate(stop_file)
                    last_gate = time.monotonic()
                except ControlError:
                    notify_stop()
            if not select.select([process.stdout], [], [], 1)[0]:
                if process.poll() is not None:
                    raise ControlError("ssh_ended_state_requires_reconciliation")
                continue
            raw = process.stdout.readline(65537)
            if not raw or len(raw) > 65536:
                raise ControlError("invalid_executor_response")
            event = json.loads(raw)
            if "permit_required" in event:
                if not re.fullmatch(r"[0-9a-f]{32}", event["permit_required"]):
                    raise ControlError("invalid_boundary_permit")
                if cancelled:
                    notify_stop()
                    continue
                try:
                    gate(stop_file)
                    last_gate = time.monotonic()
                except ControlError:
                    notify_stop()
                    continue
                process.stdin.write(json.dumps({"permit": event["permit_required"]}).encode() + b"\n")
                process.stdin.flush()
            elif "result" in event:
                process.stdin.close()
                if process.wait(timeout=10) != 0:
                    raise ControlError("executor_failed_after_record")
                if cancelled:
                    raise ControlError("stop_requested_state_requires_reconciliation")
                return event["result"]
            else:
                code = event.get("error") if isinstance(event, dict) else None
                raise ControlError(
                    code
                    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,96}", code)
                    else "executor_stage_failed_check_private_journal"
                ) from None
        raise ControlError("executor_timeout_state_requires_reconciliation")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    for name in ("commit", "release-id", "executor-sha256"):
        parser.add_argument("--" + name, required=True)
    for name in (
        "receipts",
        "source-manifest",
        "external-source-manifest",
        "archive",
        "manifest",
        "repo",
        "lock",
        "wheelhouse",
        "verifier-sha256",
    ):
        parser.add_argument("--" + name)
    parser.add_argument("--stop-file", required=True, help="Private operator stop flag outside Git")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    request, source = request_for(args)
    if not args.execute:
        print(
            json.dumps(
                {
                    "action": args.action,
                    "commit": args.commit,
                    "target": HOST,
                    "execute": False,
                    "production_verified": False,
                }
            )
        )
        return
    result = execute(request, source, args.stop_file)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": str(error)
                    if isinstance(error, ControlError)
                    else "release_input_or_execution_failed"
                }
            )
        )
        raise SystemExit(1) from None

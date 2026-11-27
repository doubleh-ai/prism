#!/usr/bin/env python3
"""Stdlib-only open-source hygiene, link integrity, and asset-coherence gate.

Runs in <0.3s with `python3 -I -S -B scripts/check_oss_hygiene.py` in local
pre-commit hooks and CI to guarantee that no private host details, secrets,
broken Markdown links, or mismatched frontend cache versions enter Git.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_PREFIXES = ("static/lib/", ".git/")
SKIP_FILES = {"requirements.lock"}
FORBIDDEN_SUFFIXES = {".db", ".db3", ".sqlite", ".sqlite3", ".log", ".pem", ".key"}

# Allow loopback, unspecified, link-local, benchmark/documentation RFC ranges, and private LAN examples.
ALLOWED_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)

IPV4_RE = re.compile(
    r"(?<![0-9.])((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?![0-9.])"
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
SSH_HOST_RE = re.compile(r"\b(?:root|ubuntu|ec2-user|debian|admin)@(?!localhost\b|example\.)[A-Za-z0-9._-]+")
MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "-C", str(ROOT), "ls-files"], text=True)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _current_branch() -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _is_semver_context(line: str, match_start: int) -> bool:
    prefix = line[max(0, match_start - 12) : match_start].lower()
    return bool(re.search(r"(?:v|version\s*[=:]?\s*|python\s*|chrome/|safari/|mozilla/|oid\s*)$", prefix))


def check_branch(errors: list[str]) -> None:
    branch = _current_branch()
    if branch == "master":
        errors.append("Branch 'master' is retired; use 'main' or a topic branch (feat/*, fix/*, etc.).")


def check_files_and_leaks(files: list[str], errors: list[str]) -> None:
    for rel in files:
        if any(rel.startswith(p) for p in SKIP_PREFIXES) or rel in SKIP_FILES:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        if rel == ".env" or (rel.startswith(".env.") and rel != ".env.example"):
            errors.append(f"{rel}: environment secret file must not be tracked")
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"{rel}: forbidden runtime/credential file suffix '{path.suffix}'")
            continue

        text = path.read_text(encoding="utf-8", errors="ignore")
        if rel.startswith("tests/"):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if PRIVATE_KEY_RE.search(line):
                errors.append(f"{rel}:{lineno}: private key header detected")
            if SSH_HOST_RE.search(line):
                errors.append(f"{rel}:{lineno}: hardcoded SSH user@host detected")
            for m in IPV4_RE.finditer(line):
                if _is_semver_context(line, m.start()):
                    continue
                ip = ipaddress.ip_address(m.group(1))
                if not any(ip in net for net in ALLOWED_NETS):
                    errors.append(f"{rel}:{lineno}: public IPv4 literal {ip} is not allowed in OSS repo")


def check_markdown_links(files: list[str], errors: list[str]) -> None:
    for rel in files:
        if not rel.endswith(".md") or any(rel.startswith(p) for p in SKIP_PREFIXES):
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        in_fence = False
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for m in MD_LINK_RE.finditer(line):
                raw_target = m.group(1).strip().split()[0].strip("<>")
                if not raw_target or raw_target.startswith(
                    ("http://", "https://", "mailto:", "#", "conversation://")
                ):
                    continue
                rel_target = raw_target.split("#", 1)[0].split("?", 1)[0]
                if not rel_target:
                    continue
                resolved = (path.parent / rel_target).resolve()
                if not resolved.exists():
                    errors.append(f"{rel}:{lineno}: broken relative Markdown link '{raw_target}'")


def check_asset_coherence(errors: list[str]) -> None:
    core = (ROOT / "static/js/app.core.js").read_text(encoding="utf-8")
    sw = (ROOT / "static/sw.js").read_text(encoding="utf-8")
    m_core = re.search(r"_clientAssetVersion:\s*'(\d+)'", core)
    m_sw = re.search(r"CACHE_NAME\s*=\s*'prism-workbench-v(\d+)'", sw)
    if not m_core or not m_sw:
        errors.append("Could not parse _clientAssetVersion in app.core.js or CACHE_NAME in sw.js")
        return
    expected = m_core.group(1)
    if m_sw.group(1) != expected:
        errors.append(f"Asset version mismatch: app.core.js={expected} vs sw.js={m_sw.group(1)}")
    for rel in ("templates/app.html", "templates/futures.html", "templates/login.html", "static/sw.js"):
        txt = (ROOT / rel).read_text(encoding="utf-8")
        found = set(re.findall(r"\?v=(\d+)", txt))
        if found != {expected}:
            errors.append(f"{rel}: expected ?v={expected}, found {sorted(found)}")


def main() -> int:
    errors: list[str] = []
    files = _tracked_files()
    check_branch(errors)
    check_files_and_leaks(files, errors)
    check_markdown_links(files, errors)
    check_asset_coherence(errors)
    if errors:
        sys.stderr.write("OSS hygiene check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        return 1
    print(f'{{"ok": true, "tracked_files_checked": {len(files)}}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

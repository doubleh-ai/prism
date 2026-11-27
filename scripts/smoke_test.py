#!/usr/bin/env python3
"""Read-only basic readiness and local assets; never a release acceptance gate."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
ASSETS = (
    "templates/login.html",
    "templates/app.html",
    "static/css/app.css",
    "static/js/app.core.js",
)


def check_readiness(base: str, expected_revision: str = "") -> bool:
    """Observe the running service; no login, account writes or model request."""
    if expected_revision and re.fullmatch(r"[a-fA-F0-9]{40}", expected_revision) is None:
        return False
    try:
        with urlopen(base.rstrip("/") + "/health/ready", timeout=5) as response:
            if response.status != 200:
                return False
            body = response.read(65537)
        if len(body) > 65536:
            return False
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("status") != "ready":
            return False
        revision = payload.get("deployed_revision")
        if revision != "unknown" and not (
            isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
        ):
            return False
        return not expected_revision or revision == expected_revision.lower()
    except (OSError, ValueError, TypeError):
        return False


def main() -> int:
    base = os.environ.get("PRISM_VERIFY_URL", "http://127.0.0.1:8088")
    expected = os.environ.get("PRISM_EXPECTED_REVISION", "")
    checks = [("基础就绪与指定服务版本", check_readiness(base, expected))]
    checks.extend((asset, (ROOT / asset).is_file()) for asset in ASSETS)
    for name, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    if not expected:
        print("服务版本未与指定候选核对。")
    print("仅检查基础数据库就绪和本地资源；Native 配置、真实研究和发布验收需分别核对。")
    return 0 if all(passed for _, passed in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

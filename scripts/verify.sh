#!/bin/bash
# 只读基础就绪检查；不创建账号、不调用模型，不代表发布或研究能力验收。
# PRISM_EXPECTED_REVISION 可选：要求服务返回指定的完整提交标识。
set -euo pipefail

BASE="${PRISM_VERIFY_URL:-http://127.0.0.1:8088}"
PYTHON_BIN="${PRISM_PYTHON:-python3}"
EXPECTED_REVISION="${PRISM_EXPECTED_REVISION:-}"

if [ -n "$EXPECTED_REVISION" ] && ! [[ "$EXPECTED_REVISION" =~ ^[a-fA-F0-9]{40}$ ]]; then
    echo "PRISM_EXPECTED_REVISION 必须是完整的 40 位提交标识。" >&2
    exit 1
fi

curl --fail --silent --show-error --max-time 5 "${BASE%/}/health/ready" |
    "$PYTHON_BIN" -c '
import json
import re
import sys

try:
    payload = json.load(sys.stdin)
    revision = payload.get("deployed_revision")
    if payload.get("status") != "ready":
        raise ValueError("unready")
    if revision != "unknown" and not (
        isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
    ):
        raise ValueError("invalid_revision")
    expected = sys.argv[1].lower()
    if expected and revision != expected:
        raise ValueError("revision_mismatch")
except (ValueError, TypeError, AttributeError):
    print("基础就绪或服务版本检查未通过。", file=sys.stderr)
    sys.exit(1)
print("PASS 基础就绪检查")
print("PASS 服务版本匹配" if expected else "服务版本未与指定候选核对。")
print("此结果只证明基础数据库就绪；Native 配置、真实研究和发布验收需分别核对。")
' "$EXPECTED_REVISION"

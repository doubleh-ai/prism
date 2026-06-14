#!/bin/bash
# 自验证脚本 — 测试所有端点
set -e
BASE="http://127.0.0.1:8088"
PASS=0; FAIL=0
check() { if [ $? -eq 0 ]; then PASS=$((PASS+1)); echo "PASS $1"; else FAIL=$((FAIL+1)); echo "FAIL $1"; fi }

echo "=== 炒股分析助手 自验证 ==="

# 1. 登录页
curl -s -o /dev/null -w "%{http_code}" "$BASE/login" | grep -q 200
check "GET /login → 200"

# 2. 创建测试账号并登录
TEST_USER="vtest"
TEST_PASS="vpass"
~/stock-venv/bin/python scripts/create_user.py --username "$TEST_USER" --password "$TEST_PASS" 2>/dev/null || true
RESP=$(curl -s -X POST "$BASE/api/login" -H "Content-Type: application/json" \
  -d "{\"username\":\"$TEST_USER\",\"password\":\"$TEST_PASS\"}")
[ "$RESP" = '{"user":"vtest"}' ]
check "POST /api/login → user vtest"

# 3. 未认证重定向
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/")
[ "$CODE" = "307" ]
check "GET / (no auth) → 307"

# 4. Claude CLI 可用
~/.npm-global/bin/claude --version > /dev/null 2>&1
check "claude --version"

# 5. Claude 可调用
echo "say hello in one word" | timeout 20 ~/.npm-global/bin/claude -p - --print --output-format stream-json --verbose 2>/dev/null | grep -q "assistant"
check "claude stream-json works"

echo "=== 通过: $PASS / 失败: $FAIL ==="
[ $FAIL -eq 0 ]

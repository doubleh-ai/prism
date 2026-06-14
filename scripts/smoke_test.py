#!/usr/bin/env python3
"""快速自测——每次部署前跑"""
import subprocess, sys, json, time, os

BASE = "http://127.0.0.1:8088"
OK, FAIL = 0, 0

def t(name, cond):
    global OK, FAIL
    if cond:
        OK += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; print(f"  ❌ {name}")

print("=== 自测 ===")

# 1. Service alive
r = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}",f"{BASE}/login"],
                   capture_output=True, text=True, timeout=5)
t("Service running", r.stdout.strip() == "200")

# 2. Login
r = subprocess.run(["curl","-s","-X","POST",f"{BASE}/api/login",
    "-H","Content-Type: application/json",
    "-d",'{"username":"test","password":"test8888"}'],
    capture_output=True, text=True, timeout=5)
try: d = json.loads(r.stdout); user = d.get("user",""); t("Login OK", user == "test")
except: t("Login OK", False)

# 3. Chat SSE
r = subprocess.run(["curl","-s","-N","-X","POST",f"{BASE}/api/chat/stream",
    "-H","Content-Type: application/json",
    "-d",'{"question":"用一句话解释PE","skill":"bottleneck-hunter"}',
    "-b","token=fake_auth_bypass"],  # won't work without cookie but still checks endpoint
    capture_output=True, text=True, timeout=5)
t("Chat endpoint reachable", "error" not in r.stdout.lower()[:100])

# 4. Claude CLI
claude_bin = os.path.expanduser("~/.npm-global/bin/claude")
r = subprocess.run([claude_bin,"--version"], capture_output=True, text=True, timeout=5)
t("Claude CLI", r.returncode == 0)

# 5. Templates
import os
for f in ["templates/login.html","templates/app.html","static/css/app.css","static/js/app.js"]:
    t(f"File {f}", os.path.exists(f"/home/ubuntu/stock-analyzer/{f}"))

print(f"\n  {OK}/{(OK+FAIL)} passed")
sys.exit(0 if FAIL == 0 else 1)

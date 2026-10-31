"""Reject mixed cached assets when independently verified UI fixes are integrated."""

import re
from pathlib import Path


def test_runtime_shell_login_and_diagnostics_share_asset_generation():
    root = Path(__file__).resolve().parent.parent
    shell = (root / "templates/app.html").read_text()
    versions = re.findall(r'(?:src|href)="/static/[^"?]+\?v=([^"#]+)(?:#[^"]*)?"', shell)
    assert versions and len(set(versions)) == 1, "Mixed UI generations can revive retired entry points"
    version = versions[0]
    core = (root / "static/js/app.core.js").read_text()
    sw = (root / "static/sw.js").read_text()
    login = (root / "templates/login.html").read_text()
    assert re.search(r"_clientAssetVersion: '([^']+)'", core).group(1) == version
    assert re.search(r"prism-workbench-v([^']+)'", sw).group(1) == version
    assert f"path + '?v={version}'" in sw
    assert f"app.css?v={version}" in login

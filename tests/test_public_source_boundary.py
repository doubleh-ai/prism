"""The public app ships reusable development tools, not host-bound operations."""

import ipaddress
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    "build_jsonpath_wheel.py",
    "create_user.py",
    "import_legacy_history.py",
    "release_candidate.py",
    "smoke_test.py",
    "verify.sh",
}


def test_only_reusable_tools_are_shipped_in_the_public_script_directory():
    actual = {
        path.name for path in (ROOT / "scripts").iterdir() if path.is_file() and path.suffix in {".py", ".sh"}
    }
    assert actual == TOOLS
    assert not (ROOT / "scripts/patches").exists()
    assert not (ROOT / "scripts/fixtures").exists()


def test_public_tools_have_no_fixed_external_ip_targets_or_personal_mac_paths():
    targets = re.compile(r"(?:https?://|@)((?:\d{1,3}\.){3}\d{1,3})(?=[:/\s\"']|$)")
    home = re.compile(r"/Users/[^/\s\"']+/")
    for name in TOOLS:
        text = (ROOT / "scripts" / name).read_text()
        assert not home.search(text), name
        for raw in targets.findall(text):
            assert ipaddress.ip_address(raw).is_loopback, name


def test_public_ci_does_not_require_private_or_retired_operational_source():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    for name in (
        "release_controller.py",
        "remote_release.py",
        "secure_backup.py",
        "verify_backup_boot.py",
        "verify_database_compatibility.py",
    ):
        assert name not in workflow

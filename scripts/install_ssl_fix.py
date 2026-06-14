"""Install patch_ssl as sitecustomize.py for auto-loading on ALL Python processes.

Run once per server:
    python install_ssl_fix.py

This places a minimal sitecustomize.py in the Python site-packages directory.
After installation, every Python process automatically disables SSL verification —
no code changes needed in any skill or script.
"""
import sys
import shutil
from pathlib import Path


def main():
    # Find site-packages
    candidates = []
    for p in sys.path:
        if p and ("site-packages" in p or "dist-packages" in p):
            candidates.append(Path(p))

    if not candidates:
        print("ERROR: Could not find site-packages directory in sys.path")
        print("sys.path:", sys.path)
        return 1

    # Prefer the first writable one
    target_dir = None
    for d in candidates:
        if d.exists() and d.is_dir():
            try:
                test_file = d / ".write_test"
                test_file.touch()
                test_file.unlink()
                target_dir = d
                break
            except (OSError, PermissionError):
                continue

    if target_dir is None:
        print("ERROR: No writable site-packages directory found")
        print("Candidates:", [str(d) for d in candidates])
        return 1

    # Copy patch_ssl.py into site-packages
    src = Path(__file__).resolve().parent / "patch_ssl.py"
    if not src.exists():
        print(f"ERROR: patch_ssl.py not found at {src}")
        return 1

    dst_ssl = target_dir / "patch_ssl.py"
    shutil.copy2(src, dst_ssl)
    print(f"✓ Copied patch_ssl.py → {dst_ssl}")

    # Create sitecustomize.py that imports patch_ssl
    sitecustomize = target_dir / "sitecustomize.py"
    content = '''"""
Auto-loaded by Python before any user code.
Patches SSL globally for Chinese financial data API compatibility.
"""
try:
    import patch_ssl  # noqa
except ImportError:
    pass  # Not installed — skip silently
'''
    # Only write if different (idempotent)
    existing = sitecustomize.read_text() if sitecustomize.exists() else ""
    if existing.strip() != content.strip():
        sitecustomize.write_text(content)
        print(f"✓ Created/updated {sitecustomize}")
    else:
        print(f"═ {sitecustomize} already up to date")

    print("\nDone. SSL verification is now disabled for ALL Python processes on this server.")
    print("To verify: python -c 'import requests; print(requests.get(\"https://expired.badssl.com/\").status_code)'")
    return 0


if __name__ == "__main__":
    sys.exit(main())

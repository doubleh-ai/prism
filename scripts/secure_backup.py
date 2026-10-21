#!/usr/bin/env python3
"""Capture a private, encrypted Prism recovery point on the operator's Mac.

Fixed SSH target, no credential output and no application imports. The password
is read from an existing Keychain entry into memory and passed through stdin.
Old private Git objects are preserved inside encryption, never in a candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

SSH = [
    "/usr/bin/ssh",
    "-S",
    "/tmp/prism-release-ssh-%C",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ForwardAgent=no",
    "-o",
    "ConnectTimeout=10",
    "-p",
    "22",
    "ubuntu@124.223.168.224",
]

REMOTE = r"""
import datetime,hashlib,io,json,os,pathlib,re,shutil,sqlite3,stat,subprocess,sys,tarfile,tempfile
home=pathlib.Path('/home/ubuntu')
production=home/'stock-analyzer'
if not production.is_dir() or not (production/'.env').is_file():raise RuntimeError('required production paths are absent')
active=subprocess.run(['systemctl','show','stock-app.service','--property=WorkingDirectory','--value'],capture_output=True,text=True,check=True).stdout.strip()
if active!=str(production):raise RuntimeError('active production directory differs from this backup contract')
roots=[production,home/'Vibe-Trading',home/'TradingAgents-astock',home/'.vibe-trading',home/'.tradingagents',home/'.claude/projects',home/'.claude/skills',home/'skills',pathlib.Path('/etc/systemd/system/stock-app.service')]
roots += list(pathlib.Path('/etc/systemd/system').glob('*vibe*.service'))
roots += [pathlib.Path('/etc/systemd/system/stock-app.service.d')]
roots += [pathlib.Path('/etc/nginx')/folder/name for folder in ('sites-available','sites-enabled') for name in ('stock-analyzer','stock-app')]
stage=pathlib.Path(tempfile.mkdtemp(prefix='prism-consistent-',dir='/var/tmp'))
os.chmod(stage,0o700)
manifest={'schema':2,'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'files':[],'databases':[],'changed_during_capture':[],'missing_during_capture':[],'roots':[]}
class Reader:
 def __init__(self,f):self.f=f;self.h=hashlib.sha256()
 def read(self,n=-1):
  value=self.f.read(n);self.h.update(value);return value
def identity(value):return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_mode)
def file_hash(stream):
 result=hashlib.sha256()
 for chunk in iter(lambda:stream.read(1024*1024),b''):result.update(chunk)
 return result.hexdigest()
def changed(name):
 if name not in manifest['changed_during_capture']:manifest['changed_during_capture'].append(name)
def critical(name):
 parts=pathlib.PurePosixPath(name).parts
 if name.startswith('snapshot/etc/'):return True
 if name.startswith(('snapshot/home/ubuntu/.claude/projects','snapshot/home/ubuntu/.vibe-trading','snapshot/home/ubuntu/.tradingagents')):return False
 if name.startswith('snapshot/home/ubuntu/stock-analyzer/'):
  relative=name[len('snapshot/home/ubuntu/stock-analyzer/'):]
  return relative.split('/',1)[0] not in {'data','reports','uploads','logs','cache','.venv','venv','__pycache__','.pytest_cache'} and not re.search(r'\.log(?:\.[^/]*)?$|\.pyc$',relative)
 if name=='snapshot/home/ubuntu/stock-analyzer':return True
 if any(part in {'reports','data','logs','cache','.venv','venv','__pycache__','.pytest_cache'} for part in parts):return False
 return not re.search(r'\.log(?:\.[^/]*)?$|\.jsonl$|\.pyc$',name)
def source_fingerprints():
 result={}
 def scan(path,name):
  if not critical(name):return
  try:
   info=path.lstat()
   if stat.S_ISLNK(info.st_mode):result[name]=('link',os.readlink(path));return
   if stat.S_ISDIR(info.st_mode):
    result[name]=('directory',info.st_dev,info.st_ino,info.st_mode)
    for child in sorted(path.iterdir()):scan(child,name+'/'+child.name)
   elif stat.S_ISREG(info.st_mode):
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as stream:result[name]=('file',identity(info),file_hash(stream))
  except FileNotFoundError:changed(name)
 for root in roots:
  if root.exists() or root.is_symlink():scan(root,'snapshot/'+str(root).lstrip('/'))
 return result
try:
 initial_sources=source_fingerprints()
 for dbroot in [production/'data',home/'.vibe-trading',home/'.tradingagents',home/'Vibe-Trading/data']:
  if not dbroot.exists():continue
  for original in sorted(dbroot.rglob('*')):
   if original.is_symlink() or not original.is_file() or original.suffix not in ('.db','.sqlite','.sqlite3'):continue
   with original.open('rb') as f:header=f.read(16)
   if header!=b'SQLite format 3\0':continue
   target=stage/str(len(manifest['databases']))
   source=sqlite3.connect(original.as_uri()+'?mode=ro',uri=True,timeout=30)
   dest=sqlite3.connect(target)
   started=datetime.datetime.now(datetime.timezone.utc).isoformat()
   try:
    source.backup(dest,pages=256,sleep=0.01)
    if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('database integrity')
   finally:source.close();dest.close()
   manifest['databases'].append({'original':str(original),'path':'consistent-databases/'+target.name,'integrity':'ok','started_at':started,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
 if not any(db['original']==str(production/'data/chat.db') for db in manifest['databases']):raise RuntimeError('required database snapshot absent')
 with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz',format=tarfile.PAX_FORMAT) as archive:
  def add(path,name):
   try:
    initial=path.lstat();info=archive.gettarinfo(str(path),name)
    info.mode=stat.S_IMODE(info.mode)
   except FileNotFoundError:
    changed(name);manifest['missing_during_capture'].append(name);return
   entry={'path':name,'size':info.size,'mode':info.mode,'type':'directory'}
   if info.isfile():
    try:descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    except FileNotFoundError:
     changed(name);manifest['missing_during_capture'].append(name);return
    with os.fdopen(descriptor,'rb') as f:
     before=os.fstat(f.fileno())
     if identity(initial)!=identity(before) or info.size!=before.st_size or info.mode!=stat.S_IMODE(before.st_mode) or info.mtime!=before.st_mtime:changed(name)
     reader=Reader(f);archive.addfile(info,reader);after=os.fstat(f.fileno())
    entry.update(type='file',sha256=reader.h.hexdigest())
    if identity(before)!=identity(after):changed(name)
   elif info.issym() or info.islnk():
    entry.update(type='symlink' if info.issym() else 'hardlink',target=info.linkname);archive.addfile(info)
   elif info.isdir():archive.addfile(info)
   else:return
   try:
    if not info.isdir() and identity(initial)!=identity(path.lstat()):changed(name)
   except FileNotFoundError:changed(name)
   manifest['files'].append(entry)
   if info.isdir():
    try:children=sorted(path.iterdir())
    except FileNotFoundError:changed(name);return
    for child in children:add(child,name+'/'+child.name)
  for root in roots:
   if root.exists() or root.is_symlink():
    name='snapshot/'+str(root).lstrip('/');manifest['roots'].append(name);add(root,name)
  for db in manifest['databases']:add(stage/pathlib.Path(db['path']).name,db['path'])
  final_sources=source_fingerprints()
  for name in initial_sources.keys()|final_sources.keys():
   if initial_sources.get(name)!=final_sources.get(name):changed(name)
  manifest['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
  content=json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode()
  info=tarfile.TarInfo('backup-manifest.json');info.size=len(content);info.mode=0o600;archive.addfile(info,io.BytesIO(content))
finally:shutil.rmtree(stage)
"""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


PRODUCTION_ROOT = "snapshot/home/ubuntu/stock-analyzer"
MAIN_DATABASE = "/home/ubuntu/stock-analyzer/data/chat.db"
REQUIRED_SOURCE_FILES = {"server/main.py", "server/config.py", "server/database.py", "server/auth.py", ".env"}
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_CONTENT_BYTES = 32 * 1024 * 1024 * 1024


def critical_capture_path(name: str) -> bool:
    """Sources and configuration must be stable; live data has separate copies."""
    parts = PurePosixPath(name).parts
    if name.startswith("snapshot/etc/"):
        return True
    if name.startswith(
        (
            "snapshot/home/ubuntu/.claude/projects",
            "snapshot/home/ubuntu/.vibe-trading",
            "snapshot/home/ubuntu/.tradingagents",
        )
    ):
        return False
    if name.startswith(PRODUCTION_ROOT + "/"):
        relative = name[len(PRODUCTION_ROOT) + 1 :]
        return relative.split("/", 1)[0] not in {
            "data",
            "reports",
            "uploads",
            "logs",
            "cache",
            ".venv",
            "venv",
            "__pycache__",
            ".pytest_cache",
        } and not re.search(r"\.log(?:\.[^/]*)?$|\.pyc$", relative)
    if name == PRODUCTION_ROOT:
        return True
    if any(
        part in {"reports", "data", "logs", "cache", ".venv", "venv", "__pycache__", ".pytest_cache"}
        for part in parts
    ):
        return False
    return not re.search(r"\.log(?:\.[^/]*)?$|\.jsonl$|\.pyc$", name)


def read_manifest(path: Path) -> dict:
    with tarfile.open(path, "r:gz") as archive:
        item = archive.getmember("backup-manifest.json")
        if not item.isfile() or item.size > MAX_MANIFEST_BYTES:
            raise ValueError("Invalid backup manifest")
        result = json.load(archive.extractfile(item))
    if not isinstance(result, dict):
        raise ValueError("Invalid backup manifest")
    return result


def verify_archive(path: Path, recovery: Path | None = None, *, reuse_recovery: bool = False) -> dict:
    """Verify bytes, mandatory scope and actual restored SQLite copies.

    Links are preserved as metadata only, never extracted. A manifest's own
    integrity label is not accepted as evidence that a database was restored.
    """
    observed, seen, metadata = {}, set(), {}
    manifest = None
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            rel = PurePosixPath(item.name)
            if (
                not item.name
                or rel.is_absolute()
                or ".." in rel.parts
                or item.name in seen
                or rel.as_posix() != item.name.rstrip("/")
                or item.size < 0
            ):
                raise ValueError("Unsafe or duplicate backup entry")
            seen.add(item.name)
            total += item.size
            if total > MAX_ARCHIVE_CONTENT_BYTES:
                raise ValueError("Backup exceeds verification resource limit")
            if item.name == "backup-manifest.json":
                if manifest is not None or not item.isfile() or item.size > MAX_MANIFEST_BYTES:
                    raise ValueError("Invalid backup manifest")
                manifest = json.load(archive.extractfile(item))
                continue
            entry = {"path": item.name, "size": item.size, "mode": item.mode, "type": "directory"}
            if item.isfile():
                stream = archive.extractfile(item)
                observed[item.name] = {
                    "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                    "size": item.size,
                }
                entry.update(type="file", sha256=observed[item.name]["sha256"])
                if recovery is not None and item.name.startswith("consistent-databases/"):
                    if len(rel.parts) != 2 or not rel.name.isdigit():
                        raise ValueError("Invalid database recovery path")
                    target = recovery / rel.name
                    if target.is_symlink():
                        raise ValueError("Recovery file must not be a link")
                    if reuse_recovery:
                        if not target.is_file() or digest(target) != observed[item.name]["sha256"]:
                            raise ValueError("Restored database changed")
                    else:
                        with archive.extractfile(item) as source, target.open("xb") as dest:
                            shutil.copyfileobj(source, dest)
                        os.chmod(target, 0o600)
            elif item.issym() or item.islnk():
                entry.update(type="symlink" if item.issym() else "hardlink", target=item.linkname)
            elif not item.isdir():
                raise ValueError("Unsupported backup entry")
            metadata[item.name] = entry
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ValueError("Missing backup manifest")
    files = manifest["files"]
    if len(files) != len({x["path"] for x in files}):
        raise ValueError("Duplicate manifest paths")
    expected = {x["path"]: {"sha256": x["sha256"], "size": x["size"]} for x in files if x["type"] == "file"}
    if observed != expected or metadata != {x["path"]: x for x in files}:
        raise ValueError("Backup content or metadata differs from manifest")
    if metadata.get(PRODUCTION_ROOT, {}).get("type") != "directory":
        raise ValueError("Required production root is absent")
    if not all(PRODUCTION_ROOT + "/" + name in observed for name in REQUIRED_SOURCE_FILES):
        raise ValueError("Required production source or configuration is absent")
    databases = manifest.get("databases")
    if not isinstance(databases, list) or not databases:
        raise ValueError("No consistent database snapshots")
    db_paths = [db.get("path") for db in databases]
    originals = [db.get("original") for db in databases]
    if (
        len(set(db_paths)) != len(databases)
        or len(set(originals)) != len(databases)
        or MAIN_DATABASE not in originals
    ):
        raise ValueError("Required or unique main database snapshot absent")
    captured_databases = {name for name in observed if name.startswith("consistent-databases/")}
    if captured_databases != set(db_paths):
        raise ValueError("Database list differs from captured snapshots")
    for db in databases:
        rel = PurePosixPath(db["path"])
        if len(rel.parts) != 2 or rel.parts[0] != "consistent-databases" or not rel.name.isdigit():
            raise ValueError("Invalid consistent database path")
        if recovery is not None:
            restored = recovery / rel.name
            if digest(restored) != observed[db["path"]]["sha256"]:
                raise ValueError("Restored database differs from archived snapshot")
            conn = sqlite3.connect(restored.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Restored database integrity failure")
                if db["original"] == MAIN_DATABASE:
                    tables = {
                        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    }
                    if not {"users", "sessions", "conversations", "messages"} <= tables:
                        raise ValueError("Restored main database is not a Prism database")
            finally:
                conn.close()
    changed = list(dict.fromkeys(manifest.get("changed_during_capture") or []))
    critical = [name for name in changed if critical_capture_path(name)]
    return {
        "files": len(expected),
        "databases": len(databases),
        "changed_during_capture": len(changed),
        "critical_changed": len(critical),
        "runtime_changed": len(changed) - len(critical),
        "source_unchanged": not critical,
        "sqlite_restore_verified": recovery is not None,
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
    }


def _hdi(*args, password: bytes | None = None):
    return subprocess.run(
        ["/usr/bin/hdiutil", *map(str, args)], input=password, capture_output=True, check=True
    ).stdout


def _encrypted(value) -> bool:
    if isinstance(value, dict):
        return any(
            (key.casefold() == "encrypted" and item is True) or _encrypted(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_encrypted(item) for item in value)
    return False


def verified_base(base: Path, receipt_path: Path) -> dict:
    """Bind preserved private history to independently rechecked encrypted bytes."""
    if any((p / ".git").exists() for p in (receipt_path, *receipt_path.parents)):
        raise ValueError("Private verification must remain outside Git")
    raw = receipt_path.read_bytes()
    receipt = json.loads(raw)
    if (
        receipt.get("status") != "verified and unmounted"
        or receipt.get("unmounted") is not True
        or Path(receipt.get("base_directory", "")).resolve() != base.resolve()
        or receipt.get("base_report_sha256") != digest(base / "BACKUP-REPORT.json")
        or not all(
            receipt.get(key) is True
            for key in ("archive_verified", "filesystem_verified", "databases_verified", "git_verified")
        )
    ):
        raise ValueError("Independent base verification is absent or changed")
    images = list(base.glob("*.sparsebundle"))
    if len(images) != 1 or images[0].is_symlink():
        raise ValueError("One encrypted base image is required")
    entries = []
    for path in sorted(images[0].rglob("*")):
        if path.is_symlink():
            raise ValueError("Encrypted image must not contain links")
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(base).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": digest(path),
                }
            )
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    image_digest = hashlib.sha256(encoded).hexdigest()
    if not entries or image_digest != receipt.get("image_tree_sha256"):
        raise ValueError("Encrypted base image changed after independent verification")
    return {
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "base_report_sha256": receipt["base_report_sha256"],
        "image_tree_sha256": image_digest,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-backup", required=True, type=Path)
    parser.add_argument(
        "--base-verification",
        type=Path,
        help="Independent recheck receipt outside Git; required for execution",
    )
    parser.add_argument("--backup-root", type=Path, default=Path.home() / "Backups/Prism")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--verify-boot",
        action="store_true",
        help="Boot a private restored copy with no model credentials or external network",
    )
    args = parser.parse_args()
    if os.uname().sysname != "Darwin":
        raise ValueError("Run on the authenticated operator Mac")
    for path in (args.backup_root.resolve(), args.base_backup.resolve()):
        if any((parent / ".git").exists() for parent in (path, *path.parents)):
            raise ValueError("Private backups must remain outside Git workspaces")
    base = json.loads((args.base_backup / "BACKUP-REPORT.json").read_text())
    if base.get("status") != "verified and unmounted":
        raise ValueError("A verified encrypted base backup is required")
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "plan",
                    "target": SSH[-1],
                    "encrypted": True,
                    "verify_boot_requested": args.verify_boot,
                    "application_restore_verified": False,
                    "production_restart": False,
                }
            )
        )
        return
    if not args.base_verification:
        raise ValueError("Independent encrypted base verification is required")
    base_verification = verified_base(args.base_backup.resolve(), args.base_verification.resolve())
    password_result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            base["keychain_service"],
            "-a",
            base["keychain_account"],
            "-w",
        ],
        capture_output=True,
        check=True,
    )
    password = password_result.stdout.rstrip(b"\n") + b"\0"
    run_id = datetime.now(UTC).strftime("%Y-%m-%d-prism-preflight-%H%M%S-") + uuid.uuid4().hex[:8]
    folder = args.backup_root / run_id
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    image, mount = folder / (run_id + ".sparsebundle"), folder / "mounted"
    mount.mkdir(mode=0o700)
    mounted = False
    try:
        _hdi(
            "create",
            "-size",
            "24g",
            "-type",
            "SPARSEBUNDLE",
            "-fs",
            "APFS",
            "-volname",
            "Prism private recovery",
            "-encryption",
            "AES-256",
            "-stdinpass",
            image,
            password=password,
        )
        encrypted = _encrypted(
            plistlib.loads(_hdi("imageinfo", "-stdinpass", "-plist", image, password=password))
        )
        if not encrypted:
            raise ValueError("Image encryption could not be independently confirmed")
        _hdi("attach", "-stdinpass", "-nobrowse", "-mountpoint", mount, image, password=password)
        mounted = True
        archive, recovery = mount / "production.tar.gz", mount / "recovery-databases"
        recovery.mkdir(mode=0o700)
        with archive.open("xb") as output:
            proc = subprocess.run(
                [*SSH, "sudo -n /usr/bin/python3 -I -B -S -"],
                input=REMOTE.encode(),
                stdout=output,
                stderr=subprocess.PIPE,
            )
        if proc.returncode:
            raise RuntimeError("Remote capture failed; private partial backup retained")
        os.chmod(archive, 0o600)
        verified = verify_archive(archive, recovery)
        archive_sha = digest(archive)
        (mount / "verification.json").write_text(json.dumps(verified, indent=2))
        boot = {"requested": args.verify_boot, "ok": False, "live_models_tested": False}
        if args.verify_boot:
            spec = importlib.util.spec_from_file_location(
                "prism_backup_boot", Path(__file__).with_name("verify_backup_boot.py")
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            boot = module.verify_boot(archive, recovery, mount, read_manifest(archive), archive_sha)
        (mount / "boot-verification.json").write_text(json.dumps(boot, indent=2))
        _hdi("detach", mount)
        mounted = False
        _hdi("attach", "-readonly", "-stdinpass", "-nobrowse", "-mountpoint", mount, image, password=password)
        mounted = True
        if (
            digest(mount / "production.tar.gz") != archive_sha
            or verify_archive(mount / "production.tar.gz", mount / "recovery-databases", reuse_recovery=True)
            != verified
        ):
            raise ValueError("Reopened backup does not match")
        _hdi("detach", mount)
        mounted = False
        receipt = {
            "schema": 1,
            "status": "verified and unmounted",
            "verified_at": datetime.now(UTC).isoformat(),
            "backup_image": image.name,
            "encryption": "AES-256",
            "archive_sha256": archive_sha,
            "verification": verified,
            "sqlite_restore_verified": verified["sqlite_restore_verified"],
            "application_restore_verified": boot["ok"],
            "release_gate_ready": bool(
                verified["sqlite_restore_verified"] and verified["source_unchanged"] and boot["ok"]
            ),
            "capture_status": "source_changed"
            if verified["critical_changed"]
            else "runtime_changed"
            if verified["runtime_changed"]
            else "complete",
            "boot_verification": {key: value for key, value in boot.items() if isinstance(value, bool)},
            "base_backup": str(args.base_backup),
            "base_verification": base_verification,
            "old_private_history": "preserved in verified encrypted base",
            "password_stdin_format": "keychain-value-nul-terminated",
            "keychain_service": base["keychain_service"],
            "keychain_account": base["keychain_account"],
            "production_services_unchanged": True,
            "off_device_copy": False,
            "external_recovery_dependencies": [
                "TLS private keys and certificate provisioning",
                "domain and DNS configuration",
            ],
        }
        report = folder / "BACKUP-REPORT.json"
        report.write_text(json.dumps(receipt, indent=2))
        os.chmod(report, 0o600)
        print(
            json.dumps(
                {
                    "ok": bool(not args.verify_boot or boot["ok"]),
                    "encrypted_archive_verified": True,
                    "application_restore_verified": boot["ok"],
                    "release_gate_ready": receipt["release_gate_ready"],
                    "report": str(report),
                    "receipt_sha256": digest(report),
                    "verification": verified,
                    "encrypted": True,
                    "unmounted": True,
                }
            )
        )
        if args.verify_boot and not boot["ok"]:
            raise SystemExit(2)
    finally:
        if mounted:
            _hdi("detach", mount)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Underlying commands may mention private paths; don't echo their output.
        print(json.dumps({"ok": False, "error_type": type(error).__name__}))
        raise SystemExit(1)

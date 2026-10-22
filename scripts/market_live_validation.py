#!/usr/bin/env python3
"""Fixed Linux supervisor for the credential-free market probe.

Invoked by the reviewed serialized controller, never as a deployment command.
No credential/configuration reader, model, SSH, proxy or generic URL exists.
The inherited candidate is imported only in the dedicated non-root process,
after real root-collected PID/UID/cgroup/mount facts and network-denial controls.
Offline tests are not real Linux/BPF/market acceptance receipts.
"""

from __future__ import annotations

import base64
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import socket
import stat
import sys
import time
from pathlib import Path

PROBE_SHA256 = "904617183a1d9c1c99dca2a07d38a26c4165246385979fc038dfb61a0b0ebbab"
OPERATIONS = Path("/var/lib/prism-operations/integration")
RELEASES = Path("/opt/prism/releases")
PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
HOSTS = {"qt.gtimg.cn", "money.finance.sina.com.cn"}
CHECKS = {
    "unauthenticated_api_denied",
    "quote_real_provider",
    "kline_real_provider",
    "quote_owned_capability_cache_readback",
    "kline_owned_capability_cache_readback",
    "quote_retained_stale_after_injected_failure",
    "kline_retained_stale_after_injected_failure",
    "no_cache_failure_explicit_503",
}
BOOT_CHECKS = {
    "production_home_denied",
    "production_proc_denied",
    "candidate_write_denied",
    "run_readonly",
    "production_8088_denied",
    "vibe_8899_denied",
    "metadata_denied",
    "private_ip_denied",
    "other_public_denied",
    "synthetic_empty_data",
    "staging_denied",
}
ERRORS = {
    "request_rejected",
    "source_rejected",
    "dns_rejected",
    "runtime_rejected",
    "account_rejected",
    "account_busy",
    "workspace_reused",
    "unit_rejected",
    "kernel_rejected",
    "private_run_rejected",
    "proof_rejected",
    "worker_failed",
    "result_rejected",
    "cleanup_failed",
    "supervision_failed",
    "market_acceptance_incomplete",
    "operator_stop",
}

WORKER_SOURCE = r"""
import asyncio, contextlib, errno, hashlib, importlib.util, json, os, pwd, re, socket, stat, sys, time
from pathlib import Path

def encoded(value):return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def write(path,value):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,"wb") as stream:stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
def source(path,expected):
 for part in (path,*path.parents):
  info=part.lstat()
  if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise RuntimeError("source_rejected")
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,"rb") as stream:
  info=os.fstat(stream.fileno())
  if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>131072:raise RuntimeError("source_rejected")
  raw=stream.read(131073)
 if hashlib.sha256(raw).hexdigest()!=expected:raise RuntimeError("source_rejected")
 return raw
def denied(path,create=False):
 try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW if create else os.O_RDONLY|os.O_NOFOLLOW,0o600)
 except OSError as error:return error.errno in ({errno.EACCES,errno.EPERM,errno.EROFS} if create else {errno.EACCES,errno.EPERM,errno.ENOENT})
 os.close(fd)
 if create:os.unlink(path)
 return False
def connect_denied(host,port):
 with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as channel:
  channel.settimeout(.5);code=channel.connect_ex((host,port))
 return code in {errno.EACCES,errno.EPERM}
def main():
 config=json.loads(sys.argv[1]);work=Path(config["work"]);candidate=Path('/opt/prism/releases')/config['candidate']
 if sys.platform!='linux' or not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode or Path(sys.executable)!=candidate/'.venv/bin/python':raise RuntimeError("runtime_rejected")
 account=pwd.getpwnam('prism-integration')
 if os.getuid()==0 or os.getuid()!=account.pw_uid or account.pw_shell!='/usr/sbin/nologin' or any(group!=account.pw_gid for group in os.getgroups()):raise RuntimeError("account_rejected")
 source(work/'worker.py',config['worker_sha256']);source(work/'probe.py',config['probe_sha256'])
 if any(re.search(r'(?i)(?:password|secret|token|api.?key|authorization)',key) for key in os.environ):raise RuntimeError('runtime_rejected')
 checks={
 'production_home_denied':denied('/home/ubuntu/stock-analyzer/.env'),
 'production_proc_denied':denied('/proc/'+str(config['production_pid'])+'/environ'),
 'candidate_write_denied':denied(candidate/('.market-canary-'+config['nonce']),True),
 'run_readonly':denied(Path('/run')/('prism-market-denied-'+config['nonce']),True),
 'production_8088_denied':connect_denied('127.0.0.1',8088),
 'vibe_8899_denied':connect_denied('127.0.0.1',8899),
 'metadata_denied':connect_denied('169.254.169.254',80),
 'private_ip_denied':connect_denied('10.0.0.1',443),
 'other_public_denied':connect_denied(config['negative_public'],443),
 'staging_denied':denied('/var/lib/prism-operations/staging'),
 'synthetic_empty_data':not any((work/'data').iterdir()),
 }
 binding={key:config[key] for key in ('candidate','release_id','nonce','probe_sha256','worker_sha256','production_pid','issued_at')};binding['pid']=os.getpid()
 proof={'schema':1,'binding':binding,'checked_at':int(time.time()),'checks':checks}
 write(work/'output/bootstrap.json',proof)
 if not all(checks.values()):raise RuntimeError('proof_rejected')
 deadline=time.monotonic()+20
 while not (work/'root-allow.json').exists() and time.monotonic()<deadline:time.sleep(.05)
 fd=os.open(work/'root-allow.json',os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as stream:
  info=os.fstat(stream.fileno())
  if info.st_uid!=0 or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o444 or info.st_size>16384:raise RuntimeError('proof_rejected')
  allow=json.loads(stream.read(16385))
 if set(allow)!={'schema','binding','bootstrap_sha256','allowed_at','actual_kernel_verified'} or allow['schema']!=1 or allow['binding']!=binding or allow['bootstrap_sha256']!=hashlib.sha256(encoded(proof)).hexdigest() or allow['actual_kernel_verified'] is not True or not config['issued_at']<=allow['allowed_at']<=time.time()<=config['issued_at']+60:raise RuntimeError('proof_rejected')
 raw=source(work/'probe.py',config['probe_sha256'])
 module=importlib.util.module_from_spec(importlib.util.spec_from_loader('_approved_market_probe',loader=None));module.__file__=str(work/'probe.py')
 exec(compile(raw,module.__file__,'exec'),module.__dict__)
 module.MAC_REPO=candidate;module.MAC_SITES=candidate/'.venv/lib/python3.12/site-packages'
 result={'schema':1,'binding':binding,'checked_at':int(time.time()),'passed':False,'market_provider_verified':False,'production_verified':False,'research_quality_verified':False,'live_model_verified':False,'native_search_verified':False,'native_fetch_verified':False,'error_class':None,'checks':{}}
 try:
  fd=os.open(work/'output/private.log',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'w') as log:
   os.dup2(log.fileno(),1);os.dup2(log.fileno(),2)
   with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
    modules=module.candidate_modules(work);transport=module.FixedTransport(config['pins'])
    import urllib.request
    original=urllib.request.urlopen;urllib.request.urlopen=transport.urlopen
    try:result.update(asyncio.run(module.exercise(modules,transport)))
    finally:urllib.request.urlopen=original
  result['market_provider_verified']=all(result['checks'].get(key) is True for key in ('quote_real_provider','kline_real_provider'))
  result['passed']=all(result['checks'].values()) and not result['kline_provenance_missing'] and transport.counts=={'quote':1,'kline':1}
  if not result['passed']:result['error_class']='market_acceptance_incomplete'
 except Exception:result['error_class']='worker_failed'
 result['checked_at']=int(time.time());write(work/'output/market-result.json',result)
 raise SystemExit(0 if result['passed'] else 1)
try:main()
except Exception:raise SystemExit(1)
"""
WORKER_SHA256 = hashlib.sha256(WORKER_SOURCE.encode()).hexdigest()


class ValidationError(RuntimeError):
    """Only fixed safe diagnostic categories."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def write(path, raw, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def locked(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValidationError("source_rejected")


def validate_request(request):
    if (
        type(request) is not dict
        or set(request) != {"schema", "release_id", "candidate", "production_pid", "probe_source"}
        or type(request.get("schema")) is not int
        or request["schema"] != 1
        or not isinstance(request.get("release_id"), str)
        or not re.fullmatch("[a-z0-9][a-z0-9-]{7,63}", request["release_id"])
        or not isinstance(request.get("candidate"), str)
        or not re.fullmatch("[0-9a-f]{40}", request["candidate"])
        or type(request.get("production_pid")) is not int
        or request["production_pid"] <= 1
        or type(request.get("probe_source")) is not dict
        or set(request["probe_source"]) != {"sha256", "content"}
        or request["probe_source"]["sha256"] != PROBE_SHA256
    ):
        raise ValidationError("request_rejected")
    try:
        value = request["probe_source"]["content"]
        if not isinstance(value, str) or len(value) > 180000:
            raise ValueError
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValidationError("source_rejected") from None
    if hashlib.sha256(raw).hexdigest() != PROBE_SHA256:
        raise ValidationError("source_rejected")
    return raw


def network_policy(resolve=socket.getaddrinfo):
    pins = {}
    for host in sorted(HOSTS):
        values = [item[4][0] for item in resolve(host, 443, type=socket.SOCK_STREAM)]
        if not values or len(values) > 32:
            raise ValidationError("dns_rejected")
        peers = set()
        for value in values:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                raise ValidationError("dns_rejected") from None
            if (
                not address.is_global
                or address.is_reserved
                or address.is_multicast
                or isinstance(address, ipaddress.IPv6Address)
                and (
                    address.ipv4_mapped
                    or address.sixtofour
                    or address.teredo
                    or address.is_site_local
                    or address in ipaddress.ip_network("64:ff9b::/96")
                )
            ):
                raise ValidationError("dns_rejected")
            peers.add(str(address))
        pins[host] = [sorted(peers, key=lambda value: (":" in value, value))[0]]
    allowed = {value for values in pins.values() for value in values}
    negative = next(value for value in ("9.9.9.9", "8.8.4.4", "1.0.0.1") if value not in allowed)
    return pins, negative


def uids(process):
    row = next(
        (line for line in (process / "status").read_text().splitlines() if line.startswith("Uid:")), ""
    )
    values = row.split()[1:]
    if len(values) != 4 or any(not value.isdigit() for value in values):
        raise ValidationError("kernel_rejected")
    return tuple(int(value) for value in values)


def startticks(process):
    fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise ValidationError("kernel_rejected")
    return int(fields[19])


def private_run(process):
    rows = []
    for line in (process / "mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) > 9 and fields[4] == "/run":
            rows.append(("ro" in fields[5].split(","), fields[fields.index("-") + 1] == "tmpfs"))
    if rows != [(True, True)]:
        raise ValidationError("private_run_rejected")
    runtime = process / "root/run"
    descriptor = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info, host, current = os.fstat(descriptor), Path("/run").stat(), runtime.stat()
        if (
            info.st_uid != 0
            or info.st_mode & 0o022
            or (info.st_dev, info.st_ino) == (host.st_dev, host.st_ino)
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
            or not os.fstatvfs(descriptor).f_flag & os.ST_RDONLY
        ):
            raise ValidationError("private_run_rejected")
    finally:
        os.close(descriptor)


def read_private(path, uid):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 32768
        ):
            raise ValidationError("result_rejected")
        raw = stream.read(32769)
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValidationError("result_rejected")
    return value, hashlib.sha256(raw).hexdigest()


class Validation:
    def __init__(self, request, *, boundary, run, record):
        self.request, self.source = request, validate_request(request)
        self.boundary, self.run, self.record = boundary, run, record
        self.nonce, self.issued = secrets.token_hex(16), int(time.time())
        self.work = OPERATIONS / request["release_id"] / "market"
        self.candidate = RELEASES / request["candidate"]
        self.unit = "prism-market-probe-" + request["candidate"][:12] + "-" + self.nonce + ".service"
        self.pid, self.invocation, self.ticks, self.launched = None, None, None, False
        self.launch_attempted, self.instance = False, None
        self.workspace_created = False
        self.original = None
        self.pins, self.negative = None, None
        self.report = {
            "schema": 1,
            "probe": "credential_free_market_linux",
            "candidate": request["candidate"],
            "release_id": request["release_id"],
            "started_at": self.issued,
            "passed": False,
            "actual_linux_collection": False,
            "original_runtime_unchanged": False,
            "market_provider_verified": False,
            "production_verified": False,
            "research_quality_verified": False,
            "live_model_verified": False,
            "native_search_verified": False,
            "native_fetch_verified": False,
            "error_class": None,
            "unit_stopped": False,
            "scope": "market_public_https_only",
        }

    def stage(self, label):
        self.boundary("market_" + label)
        self.record(
            {
                "stage": label,
                "candidate": self.request["candidate"],
                "release_id": self.request["release_id"],
                "at": int(time.time()),
            }
        )

    def production(self):
        raw = self.run(
            [
                "/usr/bin/systemctl",
                "show",
                "--property=MainPID,InvocationID,WorkingDirectory",
                "stock-app.service",
            ],
            timeout=5,
        )
        state = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines() if "=" in line)
        if (
            state.get("MainPID") != str(self.request["production_pid"])
            or state.get("WorkingDirectory") != "/home/ubuntu/stock-analyzer"
            or not re.fullmatch("[0-9a-f]{32}", state.get("InvocationID", ""))
        ):
            raise ValidationError("runtime_rejected")
        return state["InvocationID"], startticks(PROC / str(self.request["production_pid"]))

    def idle(self):
        for path in PROC.iterdir():
            if path.name.isdigit():
                try:
                    if self.account.pw_uid in uids(path):
                        raise ValidationError("account_busy")
                except FileNotFoundError:
                    pass

    def workspace(self):
        self.account = pwd.getpwnam("prism-integration")
        others = [pwd.getpwnam(name) for name in ("ubuntu", "prism-stage")]
        if (
            self.account.pw_uid == 0
            or self.account.pw_gid == 0
            or any(
                self.account.pw_uid == item.pw_uid or self.account.pw_gid == item.pw_gid for item in others
            )
            or self.account.pw_shell != "/usr/sbin/nologin"
            or grp.getgrgid(self.account.pw_gid).gr_name != "prism-integration"
            or any("prism-integration" in group.gr_mem for group in grp.getgrall())
        ):
            raise ValidationError("account_rejected")
        self.idle()
        locked(self.candidate)
        revision = self.candidate / ".deploy-revision"
        locked(revision)
        revision_info = revision.stat()
        if (
            not stat.S_ISREG(revision_info.st_mode)
            or revision_info.st_nlink != 1
            or revision_info.st_size > 128
            or revision.read_text().strip() != self.request["candidate"]
        ):
            raise ValidationError("source_rejected")
        if self.work.exists() or self.work.is_symlink():
            raise ValidationError("workspace_reused")
        self.stage("workspace")
        for path in (OPERATIONS, self.work.parent):
            if not path.exists():
                path.mkdir(mode=0o755)
            locked(path)
        self.work.mkdir(mode=0o755)
        self.workspace_created = True
        for name in ("home", "data", "tmp", "output"):
            path = self.work / name
            path.mkdir(mode=0o700)
            os.chown(path, self.account.pw_uid, self.account.pw_gid)
        write(self.work / "probe.py", self.source, 0o444)
        write(self.work / "worker.py", WORKER_SOURCE.encode(), 0o444)

    def command(self):
        config = {key: self.request[key] for key in ("candidate", "release_id", "production_pid")}
        config.update(
            work=str(self.work),
            nonce=self.nonce,
            issued_at=self.issued,
            probe_sha256=PROBE_SHA256,
            worker_sha256=WORKER_SHA256,
            pins=self.pins,
            negative_public=self.negative,
        )
        addresses = sorted({value for values in self.pins.values() for value in values})
        properties = {
            "Type": "exec",
            "RemainAfterExit": "yes",
            "User": "prism-integration",
            "Group": "prism-integration",
            "SupplementaryGroups": "",
            "WorkingDirectory": str(self.work),
            "PrivateNetwork": "no",
            "ProtectHome": "yes",
            "ProtectProc": "invisible",
            "ProcSubset": "pid",
            "ProtectSystem": "strict",
            "NoNewPrivileges": "yes",
            "PrivateTmp": "yes",
            "TemporaryFileSystem": "/run:ro",
            "InaccessiblePaths": "-/var/lib/prism-operations/staging",
            "ReadWritePaths": " ".join(str(self.work / name) for name in ("home", "data", "tmp", "output")),
            "IPAddressDeny": "any",
            "IPAddressAllow": " ".join(addresses),
            "LimitCORE": "0",
            "MemoryMax": "768M",
            "TasksMax": "32",
            "CPUQuota": "100%",
            "UMask": "0077",
            "RuntimeMaxSec": "60",
            "KillMode": "control-group",
            "CapabilityBoundingSet": "",
            "AmbientCapabilities": "",
            "StandardOutput": "null",
            "StandardError": "null",
        }
        return [
            "/usr/bin/systemd-run",
            "--quiet",
            "--unit=" + self.unit,
            *("--property=" + key + "=" + value for key, value in properties.items()),
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "HOME=" + str(self.work / "home"),
            "TMPDIR=" + str(self.work / "tmp"),
            "LANG=C.UTF-8",
            "TZ=Asia/Shanghai",
            "PYTHONDONTWRITEBYTECODE=1",
            str(self.candidate / ".venv/bin/python"),
            "-I",
            "-B",
            "-S",
            str(self.work / "worker.py"),
            encoded(config).decode(),
        ]

    def show(self):
        names = "MainPID,ExecMainPID,InvocationID,ActiveState,ExecMainStatus,Result,User,Group,ControlGroup,NoNewPrivileges,ProtectSystem,ProtectHome,PrivateNetwork,KillMode,RuntimeMaxUSec,IPAddressDeny,IPAddressAllow,LimitCORE"
        raw = self.run(["/usr/bin/systemctl", "show", "--property=" + names, self.unit], timeout=5)
        return dict(line.split("=", 1) for line in raw.decode("ascii").splitlines() if "=" in line)

    def inventory_command(self):
        return ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend", self.unit]

    def acquire_instance(self, state):
        """Bind this launched instance before any cleanup may target its unit."""
        group = "/system.slice/" + self.unit
        if state.get("ExecMainPID") != str(self.pid) or state.get("ControlGroup") != group:
            raise ValidationError("unit_rejected")
        self.identity()  # Actual UID, executable, source, namespace, cgroup and startticks.
        self.instance = {
            "pid": self.pid,
            "invocation": self.invocation,
            "ticks": self.ticks,
            "control_group": group,
        }
        self.launched = True

    def same_instance_for_cleanup(self):
        if self.instance is None:
            return False
        original, state = self.instance, self.show()
        if (
            state.get("InvocationID") != original["invocation"]
            or state.get("ExecMainPID") != str(original["pid"])
            or state.get("ControlGroup") not in {"", original["control_group"]}
        ):
            return False
        if state.get("MainPID") == str(original["pid"]):
            if (self.pid, self.invocation, self.ticks) != (
                original["pid"],
                original["invocation"],
                original["ticks"],
            ):
                return False
            self.identity()
            return True
        # A retained exited invocation is still ours. A reused PID, a live
        # different main or an unrecognized cgroup cannot authorize stop.
        return state.get("MainPID") == "0" and not (PROC / str(original["pid"])).exists()

    def identity(self):
        state = self.show()
        fixed = {
            "MainPID": str(self.pid),
            "ExecMainPID": str(self.pid),
            "InvocationID": self.invocation,
            "User": "prism-integration",
            "Group": "prism-integration",
            "NoNewPrivileges": "yes",
            "ProtectSystem": "strict",
            "ProtectHome": "yes",
            "PrivateNetwork": "no",
            "KillMode": "control-group",
            "LimitCORE": "0",
            "ControlGroup": "/system.slice/" + self.unit,
        }
        expected_allow = {
            str(ipaddress.ip_network(value)) for values in self.pins.values() for value in values
        }
        if (
            any(state.get(key) != value for key, value in fixed.items())
            or state.get("RuntimeMaxUSec") not in {"1min", "60s", "60000000"}
            or {
                str(ipaddress.ip_network(value, strict=False))
                for value in state.get("IPAddressDeny", "").split()
            }
            != {"0.0.0.0/0", "::/0"}
            or {
                str(ipaddress.ip_network(value, strict=False))
                for value in state.get("IPAddressAllow", "").split()
            }
            != expected_allow
        ):
            raise ValidationError("unit_rejected")
        process = PROC / str(self.pid)
        expected_exe, actual_exe = (self.candidate / ".venv/bin/python").stat(), (process / "exe").stat()
        for name, expected_sha in (("probe.py", PROBE_SHA256), ("worker.py", WORKER_SHA256)):
            path = self.work / name
            locked(path)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o444
                    or info.st_size > 131072
                    or hashlib.sha256(stream.read(131073)).hexdigest() != expected_sha
                ):
                    raise ValidationError("source_rejected")
        status = (process / "status").read_text().splitlines()
        gid_row = next((line for line in status if line.startswith("Gid:")), "").split()[1:]
        group_row = next((line for line in status if line.startswith("Groups:")), "").split()[1:]
        if (
            uids(process) != (self.account.pw_uid,) * 4
            or (expected_exe.st_dev, expected_exe.st_ino) != (actual_exe.st_dev, actual_exe.st_ino)
            or gid_row != [str(self.account.pw_gid)] * 4
            or any(value != str(self.account.pw_gid) for value in group_row)
            or startticks(process) != self.ticks
            or (process / "ns/net").stat().st_ino != (PROC / "1/ns/net").stat().st_ino
            or (process / "cgroup").read_text().strip() != "0::" + fixed["ControlGroup"]
        ):
            raise ValidationError("kernel_rejected")
        private_run(process)
        return {
            "pid": self.pid,
            "startticks": self.ticks,
            "invocation": self.invocation,
            "uids": [self.account.pw_uid] * 4,
            "host_network_namespace": True,
            "network_namespace_inode": (process / "ns/net").stat().st_ino,
            "private_readonly_run": True,
        }

    def proof(self):
        proof, digest = read_private(self.work / "output/bootstrap.json", self.account.pw_uid)
        binding = {key: self.request[key] for key in ("candidate", "release_id", "production_pid")}
        binding.update(
            pid=self.pid,
            nonce=self.nonce,
            issued_at=self.issued,
            probe_sha256=PROBE_SHA256,
            worker_sha256=WORKER_SHA256,
        )
        if (
            set(proof) != {"schema", "binding", "checked_at", "checks"}
            or type(proof.get("schema")) is not int
            or proof["schema"] != 1
            or proof.get("binding") != binding
            or type(proof.get("checked_at")) is not int
            or not self.issued <= proof["checked_at"] <= time.time() <= self.issued + 60
            or type(proof.get("checks")) is not dict
            or set(proof["checks"]) != BOOT_CHECKS
            or any(type(value) is not bool for value in proof["checks"].values())
        ):
            raise ValidationError("proof_rejected")
        self.report["os_denial_controls"] = dict(proof["checks"])
        if not all(proof["checks"].values()):
            raise ValidationError("proof_rejected")
        return proof, digest

    def result(self):
        result, digest = read_private(self.work / "output/market-result.json", self.account.pw_uid)
        proof, _ = self.proof()
        fixed = {
            "schema": 1,
            "binding": proof["binding"],
            "production_verified": False,
            "research_quality_verified": False,
            "live_model_verified": False,
            "native_search_verified": False,
            "native_fetch_verified": False,
        }
        allowed = {
            *fixed,
            "checked_at",
            "passed",
            "market_provider_verified",
            "error_class",
            "checks",
            "request_counts",
            "https",
            "cached_result_sha256",
            "kline_provenance_missing",
            "kline_adapter_is_tencent_actual_endpoint_is_sina",
            "failure_injection_disclosed",
            "fetched_at_is_not_trade_date",
        }
        if (
            set(result) != allowed
            or any(
                type(result.get(key)) is not type(value) or result[key] != value
                for key, value in fixed.items()
            )
            or type(result.get("checks")) is not dict
            or set(result["checks"]) != CHECKS
            or any(type(value) is not bool for value in result["checks"].values())
            or result.get("request_counts") != {"quote": 1, "kline": 1}
            or any(type(value) is not int for value in result["request_counts"].values())
            or type(result.get("checked_at")) is not int
            or not self.issued <= result["checked_at"] <= time.time() <= self.issued + 65
            or type(result.get("passed")) is not bool
            or type(result.get("market_provider_verified")) is not bool
            or result.get("error_class") not in {None, "worker_failed", "market_acceptance_incomplete"}
        ):
            raise ValidationError("result_rejected")
        for key in ("https", "cached_result_sha256"):
            if type(result.get(key)) is not dict or not set(result[key]).issubset({"quote", "kline"}):
                raise ValidationError("result_rejected")
        for value in result["https"].values():
            if (
                type(value) is not dict
                or set(value) != {"https_verified", "redirect_followed", "response_sha256", "response_bytes"}
                or value.get("https_verified") is not True
                or value.get("redirect_followed") is not False
                or not re.fullmatch("[0-9a-f]{64}", value.get("response_sha256", ""))
                or type(value.get("response_bytes")) is not int
                or not 0 < value["response_bytes"] <= 524288
            ):
                raise ValidationError("result_rejected")
        if (
            any(
                not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value)
                for value in result["cached_result_sha256"].values()
            )
            or type(result.get("kline_provenance_missing")) is not list
            or any(
                value not in {"source", "retrieved_at", "data_timestamp"}
                for value in result["kline_provenance_missing"]
            )
            or any(
                result.get(key) is not True
                for key in (
                    "kline_adapter_is_tencent_actual_endpoint_is_sina",
                    "failure_injection_disclosed",
                    "fetched_at_is_not_trade_date",
                )
            )
        ):
            raise ValidationError("result_rejected")
        self.report.update(
            worker_result_sha256=digest,
            worker_result=result,
            market_provider_verified=result["market_provider_verified"],
        )
        accepted = (
            all(result["checks"].values())
            and not result["kline_provenance_missing"]
            and set(result["https"]) == {"quote", "kline"}
            and set(result["cached_result_sha256"]) == {"quote", "kline"}
        )
        if result["passed"] != accepted or result["market_provider_verified"] != (
            result["checks"]["quote_real_provider"] and result["checks"]["kline_real_provider"]
        ):
            raise ValidationError("result_rejected")
        return accepted

    def empty(self):
        base = CGROUP / "system.slice" / self.unit
        if base.is_symlink():
            raise ValidationError("cleanup_failed")
        if not base.exists():
            return True
        for path in (base, *base.rglob("*")):
            if path.is_symlink():
                raise ValidationError("cleanup_failed")
            if path.is_dir() and (path / "cgroup.procs").read_text().strip():
                return False
        return True

    def execute(self):
        try:
            if sys.platform != "linux" or os.getuid() != 0:
                raise ValidationError("runtime_rejected")
            self.report["actual_linux_collection"] = True
            self.original = self.production()
            self.pins, self.negative = network_policy()
            self.workspace()
            self.stage("start")
            inventory = self.run(self.inventory_command(), timeout=5)
            if type(inventory) is not bytes or len(inventory) > 65536 or inventory.strip():
                raise ValidationError("unit_rejected")
            self.launch_attempted = True
            self.run(self.command(), timeout=15)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = self.show()
                if state.get("MainPID", "").isdigit() and int(state["MainPID"]) > 1:
                    self.pid, self.invocation = int(state["MainPID"]), state.get("InvocationID")
                    if not isinstance(self.invocation, str) or not re.fullmatch(
                        "[0-9a-f]{32}", self.invocation
                    ):
                        raise ValidationError("unit_rejected")
                    self.ticks = startticks(PROC / str(self.pid))
                    self.acquire_instance(state)
                    break
                time.sleep(0.05)
            if self.pid is None:
                raise ValidationError("unit_rejected")
            while not (self.work / "output/bootstrap.json").exists() and time.monotonic() < deadline:
                self.boundary("market_wait_proof")
                time.sleep(0.05)
            proof, digest = self.proof()
            identity = self.identity()
            self.stage("allow_public_http")
            if self.production() != self.original or self.identity() != identity or self.proof()[1] != digest:
                raise ValidationError("kernel_rejected")
            write(
                self.work / "root-allow.json",
                encoded(
                    {
                        "schema": 1,
                        "binding": proof["binding"],
                        "bootstrap_sha256": digest,
                        "allowed_at": int(time.time()),
                        "actual_kernel_verified": True,
                    }
                ),
                0o444,
            )
            self.report.update(identity=identity, bootstrap_sha256=digest, os_denial_controls=proof["checks"])
            while time.time() <= self.issued + 65:
                self.boundary("market_wait_result")
                state = self.show()
                if state.get("MainPID") == "0":
                    break
                if state.get("MainPID") != str(self.pid) or state.get("InvocationID") != self.invocation:
                    raise ValidationError("unit_rejected")
                time.sleep(0.1)
            if state.get("MainPID") != "0":
                raise ValidationError("worker_failed")
            accepted = self.result()
            if (
                state.get("ExecMainStatus") not in {"0", "1"}
                or state.get("ExecMainPID") != str(self.pid)
                or state.get("InvocationID") != self.invocation
                or self.production() != self.original
            ):
                raise ValidationError("runtime_rejected")
            self.report["passed"] = (
                accepted and state.get("ExecMainStatus") == "0" and state.get("Result") == "success"
            )
            if not self.report["passed"]:
                self.report["error_class"] = "market_acceptance_incomplete"
        except Exception as error:
            detail = error.args[0] if isinstance(error, ValidationError) and len(error.args) == 1 else None
            self.report["error_class"] = (
                detail if isinstance(detail, str) and detail in ERRORS else "supervision_failed"
            )
            self.report["passed"] = False
        finally:
            if self.launch_attempted:
                try:
                    if not self.launched or not self.same_instance_for_cleanup():
                        # Observation is allowed after uncertain launch, stop is not.
                        if self.instance is None:
                            self.show()
                        raise ValidationError("cleanup_failed")
                    self.run(["/usr/bin/systemctl", "stop", self.unit], timeout=15)
                    if not self.empty():
                        raise ValidationError("cleanup_failed")
                    self.idle()
                    self.report["unit_stopped"] = True
                except Exception:
                    self.report.update(passed=False, error_class="cleanup_failed")
            if self.original is not None:
                try:
                    self.report["original_runtime_unchanged"] = self.production() == self.original
                except Exception:
                    self.report["original_runtime_unchanged"] = False
                if not self.report["original_runtime_unchanged"]:
                    self.report.update(passed=False, error_class="runtime_rejected")
            self.report["finished_at"] = int(time.time())
            if self.workspace_created:
                write(self.work / "validation-result.json", encoded(self.report))
            self.record(dict(self.report))
        return self.report


def run_validation(request, *, boundary, run, record):
    """Trusted callbacks are controller-owned; request carries public source only."""
    return Validation(request, boundary=boundary, run=run, record=record).execute()

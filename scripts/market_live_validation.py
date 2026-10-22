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
import types
from pathlib import Path

NETWORK_GATE_SHA256 = "da13fbd0110493c4746126fa00fc45f7393ee77a773b5c27a642eaec124f7134"
UNIT_REFERENCE_SHA256 = "b160660e92a3cd98e11b37dc21855b9292150b137752c9300f0c234d5a78a4be"
UNIT_REFERENCE_ERRORS = frozenset(
    {
        "reference_platform_rejected",
        "reference_library_rejected",
        "reference_bus_rejected",
        "reference_identity_rejected",
        "reference_refs_rejected",
        "reference_state_rejected",
        "reference_call_failed",
        "reference_close_failed",
    }
)
NETWORK_DIAGNOSTIC_STAGES = frozenset(
    {
        "not_started",
        "construct",
        "open_cgroup",
        "identity",
        "query_direct",
        "query_effective",
        "load",
        "info",
        "attach",
        "verify",
        "prepare_stop",
        "stop_identity",
        "offline_query",
        "detach",
        "close_owned",
        "object_release",
        "partial_cleanup",
        "complete",
        "unknown",
    }
)
EXTRA_NEGATIVES = {"ipv6_loopback", "ipv4_mapped_public", "udp_provider", "udp6_loopback"}
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
    "network_gate_rejected",
}

NETWORK_PERMIT_READER = r"""
def self_ticks():
    return int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
def extra_network_denials(pin):
    values={}
    for name,family,kind,target in (
        ('ipv6_loopback',socket.AF_INET6,socket.SOCK_STREAM,('::1',443,0,0)),
        ('ipv4_mapped_public',socket.AF_INET6,socket.SOCK_STREAM,('::ffff:'+pin,443,0,0)),
        ('udp_provider',socket.AF_INET,socket.SOCK_DGRAM,(pin,443)),
        ('udp6_loopback',socket.AF_INET6,socket.SOCK_DGRAM,('::1',53,0,0)),
    ):
        try:
            with socket.socket(family,kind) as probe:
                probe.settimeout(.5)
                if kind==socket.SOCK_STREAM:code=probe.connect_ex(target)
                else:
                    probe.sendto(b'',target);code=0
        except OSError as error:code=error.errno
        if type(code) is not int or not 0<=code<=4095:raise RuntimeError('network_canary_rejected')
        values[name]=code
    return values
def network_permit(work,binding,config,expected_sha=None):
    path=work/'network-allow.json';deadline=time.monotonic()+25
    while not path.exists() and time.monotonic()<deadline:time.sleep(.05)
    for part in (path,*path.parents):
        info=part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise RuntimeError('network_permit_rejected')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o444 or info.st_size>16384:raise RuntimeError('network_permit_rejected')
        raw=stream.read(16385)
    if expected_sha is not None and hashlib.sha256(raw).hexdigest()!=expected_sha:raise RuntimeError('network_permit_rejected')
    value=json.loads(raw);identity=value.get('identity',{});receipt=value.get('programs',[])
    unit=('prism-live-broker' if config['network_role']=='live' else 'prism-market-probe')+'-'+config['candidate'][:12]+'-'+config['nonce']+'.service'
    group=Path('/sys/fs/cgroup/system.slice')/unit;cg=group.stat()
    if set(value)!={'schema','binding','identity','programs','helper_sha256','provider_ipv4','allowed_at','preflight_only'} or type(value['schema']) is not int or value['schema']!=1 or value['binding']!=binding or value['helper_sha256']!=config['network_gate_sha256'] or value['provider_ipv4']!=config['network_pins'] or type(value['preflight_only']) is not bool or value['preflight_only'] is not config['preflight_only']:raise RuntimeError('network_permit_rejected')
    if set(identity)!={'unit','invocation_id','pid','startticks','uid','cgroup_inode','cgroup_device'} or identity['unit']!=unit or not isinstance(identity['invocation_id'],str) or not re.fullmatch('[0-9a-f]{32}',identity['invocation_id']) or any(type(identity[key]) is not int for key in ('pid','startticks','uid','cgroup_inode','cgroup_device')) or (identity['pid'],identity['startticks'],identity['uid'],identity['cgroup_inode'],identity['cgroup_device'])!=(os.getpid(),self_ticks(),os.getuid(),cg.st_ino,cg.st_dev) or Path('/proc/self/cgroup').read_text().strip()!='0::/system.slice/'+unit:raise RuntimeError('network_permit_rejected')
    if type(value['allowed_at']) is not int or not config['issued_at']<=value['allowed_at']<=time.time()<=config['issued_at']+180 or type(receipt) is not list or len(receipt)!=4:raise RuntimeError('network_permit_rejected')
    for row,hook in zip(receipt,(10,11,14,15)):
        if type(row) is not list or len(row)!=4 or type(row[0]) is not int or row[0]!=hook or type(row[1]) is not int or not 0<row[1]<2**32 or not isinstance(row[2],str) or not re.fullmatch('[0-9a-f]{16}',row[2]) or not isinstance(row[3],str) or not re.fullmatch('[0-9a-f]{64}',row[3]):raise RuntimeError('network_permit_rejected')
    if len({row[1] for row in receipt})!=4:raise RuntimeError('network_permit_rejected')
    return value
"""

WORKER_SOURCE = (
    r"""
import asyncio, contextlib, errno, hashlib, importlib.util, json, os, pwd, re, socket, ssl, stat, sys, time
from pathlib import Path
"""
    + NETWORK_PERMIT_READER
    + r"""

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
NETWORK_ERRNO={}
def connect_denied(name,host,port):
 with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as channel:
  channel.settimeout(.5);code=channel.connect_ex((host,port))
 NETWORK_ERRNO[name]=code
 return code in {errno.EACCES,errno.EPERM}
def main():
 config=json.loads(sys.argv[1]);work=Path(config["work"]);candidate=Path('/opt/prism/releases')/config['candidate']
 if sys.platform!='linux' or not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode or Path(sys.executable)!=candidate/'.venv/bin/python':raise RuntimeError("runtime_rejected")
 account=pwd.getpwnam('prism-integration')
 if os.getuid()==0 or os.getuid()!=account.pw_uid or account.pw_shell!='/usr/sbin/nologin' or any(group!=account.pw_gid for group in os.getgroups()):raise RuntimeError("account_rejected")
 source(work/'worker.py',config['worker_sha256']);source(work/'probe.py',config['probe_sha256'])
 if any(re.search(r'(?i)(?:password|secret|token|api.?key|authorization)',key) for key in os.environ):raise RuntimeError('runtime_rejected')
 binding={key:config[key] for key in ('candidate','release_id','nonce','probe_sha256','worker_sha256','production_pid','issued_at')};binding['pid']=os.getpid()
 write(work/'output/hello.json',{'schema':1,'kind':'hello','binding':binding,'startticks':self_ticks(),'checked_at':int(time.time())})
 network_permit(work,binding,config)
 checks={
 'production_home_denied':denied('/home/ubuntu/stock-analyzer/.env'),
 'production_proc_denied':denied('/proc/'+str(config['production_pid'])+'/environ'),
 'candidate_write_denied':denied(candidate/('.market-canary-'+config['nonce']),True),
 'run_readonly':denied(Path('/run')/('prism-market-denied-'+config['nonce']),True),
 'production_8088_denied':connect_denied('production_8088_denied','127.0.0.1',8088),
 'vibe_8899_denied':connect_denied('vibe_8899_denied','127.0.0.1',8899),
 'metadata_denied':connect_denied('metadata_denied','169.254.169.254',80),
 'private_ip_denied':connect_denied('private_ip_denied','10.0.0.1',443),
 'other_public_denied':connect_denied('other_public_denied',config['negative_public'],443),
 'staging_denied':denied('/var/lib/prism-operations/staging'),
 'synthetic_empty_data':not any((work/'data').iterdir()),
 }
 additional=extra_network_denials(config['network_pins'][0])
 positive={}
 if not all(checks.values()) or any(code not in {1,13} for code in additional.values()):
  write(work/'output/bootstrap.json',{'schema':1,'binding':binding,'checked_at':int(time.time()),'checks':checks,'positive_tls':{},'additional_network_errno':additional,'negative_errno':NETWORK_ERRNO});raise RuntimeError('proof_rejected')
 for host,pins in sorted(config['pins'].items()):
  pin=pins[0]
  with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as channel:
   channel.settimeout(5);channel.connect((pin,443));context=ssl.create_default_context()
   if context.verify_mode!=ssl.CERT_REQUIRED or not context.check_hostname:raise RuntimeError('tls_rejected')
   context.minimum_version=ssl.TLSVersion.TLSv1_2
   with context.wrap_socket(channel,server_hostname=host) as secured:
    if secured.getpeername()[0]!=pin:raise RuntimeError('tls_rejected')
    positive[host]={'peer':pin,'tls_verified':True}
 proof={'schema':1,'binding':binding,'checked_at':int(time.time()),'checks':checks,'positive_tls':positive,'additional_network_errno':additional,'negative_errno':NETWORK_ERRNO}
 write(work/'output/bootstrap.json',proof)
 if not all(checks.values()):raise RuntimeError('proof_rejected')
 if config['preflight_only']:
  deadline=time.monotonic()+25
  while not (work/'preflight-finish.json').exists() and time.monotonic()<deadline:time.sleep(.05)
  raise SystemExit(0)
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
 deadline=time.monotonic()+15
 while not (work/'result-ack.json').exists() and time.monotonic()<deadline:time.sleep(.05)
 fd=os.open(work/'result-ack.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 with os.fdopen(fd,'rb') as stream:
  info=os.fstat(stream.fileno())
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o444 or info.st_size>16384:raise RuntimeError('proof_rejected')
  ack=json.loads(stream.read(16385))
 if ack!={'schema':1,'binding':binding,'result_sha256':hashlib.sha256(encoded(result)).hexdigest()}:raise RuntimeError('proof_rejected')
 raise SystemExit(0 if result['passed'] else 1)
try:main()
except Exception:raise SystemExit(1)
"""
)
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


def load_network_gate(path):
    """Load the frozen helper from a trusted sealed path, including dataclasses.

    The path is a controller argument, never request input. Restore sys.modules
    even on failure: old sealed loaders do not register dynamically exec'd code.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValidationError("network_gate_rejected")
    locked(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_size > 65536:
            raise ValidationError("network_gate_rejected")
        raw = stream.read(65537)
    if hashlib.sha256(raw).hexdigest() != NETWORK_GATE_SHA256:
        raise ValidationError("network_gate_rejected")
    name = "_prism_owned_network_gate_" + NETWORK_GATE_SHA256
    module = types.ModuleType(name)
    module.__file__ = str(path)
    previous = sys.modules.get(name)
    try:
        sys.modules[name] = module
        exec(compile(raw, str(path), "exec"), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def load_unit_reference(path):
    """The reference implementation is separately sealed, never candidate code."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValidationError("network_gate_rejected")
    locked(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_size > 65536:
            raise ValidationError("network_gate_rejected")
        raw = stream.read(65537)

        def stable(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_uid,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if stable(info) != stable(os.fstat(stream.fileno())) or stable(info) != stable(path.lstat()):
            raise ValidationError("network_gate_rejected")
    if hashlib.sha256(raw).hexdigest() != UNIT_REFERENCE_SHA256:
        raise ValidationError("network_gate_rejected")
    name = "_prism_owned_unit_reference_" + UNIT_REFERENCE_SHA256
    module = types.ModuleType(name)
    module.__file__ = str(path)
    previous = sys.modules.get(name)
    try:
        sys.modules[name] = module
        exec(compile(raw, str(path), "exec"), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


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
        ipv4 = sorted(value for value in peers if ":" not in value)
        if not ipv4:
            raise ValidationError("dns_rejected")
        pins[host] = [ipv4[0]]
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
    def __init__(self, request, *, boundary, run, record, network_gate_path=None, preflight_only=False):
        if type(preflight_only) is not bool:
            raise ValidationError("request_rejected")
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
        self.preflight_only = preflight_only
        self.network_gate_path, self.network_gate = network_gate_path, None
        self.network_programs = None
        self.network_module = self.network_stop_proof = None
        self.unit_references, self.reference_identities, self.reference_unique_names = {}, {}, {}
        self.reference_releases = {}
        self.reference_roles = ("market",)
        self.network_diagnostic_failed = False
        self.network_cleanup_complete = False
        self.report = {
            "schema": 1,
            "probe": "credential_free_market_network_preflight"
            if preflight_only
            else "credential_free_market_linux",
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
            "scope": "pinned_tls_handshake_only" if preflight_only else "market_public_https_only",
            "preflight_only": preflight_only,
            "network_gate_verified": False,
            "network_gate_verified_before_cleanup": False,
            "network_gate_cleaned": False,
            "network_gate_diagnostic": {"stage": "not_started", "command": None, "errno": None},
            "network_gate_stop_proof_sha256": None,
            "network_gate_cleanup_proof_sha256": None,
            "network_gate_cleanup_mode": None,
            "network_gate_objects_released": False,
            "unit_reference_acquire_sha256": None,
            "unit_reference_stop_sha256": None,
            "unit_reference_release_sha256": None,
            "unit_reference_released": False,
            "unit_reference_error_class": None,
            "negative_errno": {},
            "additional_network_errno": {},
            "cleanup_error_class": None,
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
            preflight_only=self.preflight_only,
            network_gate_sha256=NETWORK_GATE_SHA256,
            network_role="market",
            network_pins=sorted({value for values in self.pins.values() for value in values}),
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
            set(proof)
            != {
                "schema",
                "binding",
                "checked_at",
                "checks",
                "positive_tls",
                "additional_network_errno",
                "negative_errno",
            }
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
        additional = proof.get("additional_network_errno")
        negatives = proof.get("negative_errno")
        expected_negative = {
            "production_8088_denied",
            "vibe_8899_denied",
            "metadata_denied",
            "private_ip_denied",
            "other_public_denied",
        }
        if (
            type(additional) is not dict
            or set(additional) != EXTRA_NEGATIVES
            or type(negatives) is not dict
            or set(negatives) != expected_negative
            or any(
                type(code) is not int or not 0 <= code <= 4095
                for code in [*additional.values(), *negatives.values()]
            )
        ):
            raise ValidationError("proof_rejected")
        self.report["additional_network_errno"] = dict(additional)
        self.report["negative_errno"] = dict(negatives)
        if not all(proof["checks"].values()) or any(
            code not in {1, 13} for code in [*additional.values(), *negatives.values()]
        ):
            raise ValidationError("proof_rejected")
        positive = proof.get("positive_tls")
        if type(positive) is not dict or set(positive) != HOSTS:
            raise ValidationError("proof_rejected")
        for host, facts in positive.items():
            if (
                type(facts) is not dict
                or set(facts) != {"peer", "tls_verified"}
                or type(facts.get("peer")) is not str
                or facts["peer"] not in self.pins[host]
                or facts.get("tls_verified") is not True
            ):
                raise ValidationError("proof_rejected")
        return proof, digest

    def install_network_gate(self):
        self.stage("network_attach")
        identity = self.identity()
        hello, _ = read_private(self.work / "output/hello.json", self.account.pw_uid)
        binding = {key: self.request[key] for key in ("candidate", "release_id", "production_pid")}
        binding.update(
            pid=self.pid,
            nonce=self.nonce,
            issued_at=self.issued,
            probe_sha256=PROBE_SHA256,
            worker_sha256=WORKER_SHA256,
        )
        if (
            set(hello) != {"schema", "kind", "binding", "startticks", "checked_at"}
            or type(hello.get("schema")) is not int
            or hello["schema"] != 1
            or hello.get("kind") != "hello"
            or hello.get("binding") != binding
            or type(hello.get("startticks")) is not int
            or hello["startticks"] != self.ticks
            or type(hello.get("checked_at")) is not int
            or not self.issued <= hello["checked_at"] <= time.time() <= self.issued + 60
        ):
            raise ValidationError("network_gate_rejected")
        module = load_network_gate(self.network_gate_path)
        self.network_module = module
        info = (CGROUP / "system.slice" / self.unit).stat()
        pins = sorted({value for values in self.pins.values() for value in values})
        descriptor = module.ControllerDescriptor(
            role="market",
            candidate_prefix=self.request["candidate"][:12],
            nonce=self.nonce,
            invocation_id=self.invocation,
            pid=self.pid,
            startticks=self.ticks,
            uid=self.account.pw_uid,
            cgroup_inode=info.st_ino,
            cgroup_device=info.st_dev,
            provider_ipv4=tuple(pins),
        )
        if descriptor.unit != self.unit or self.identity() != identity:
            raise ValidationError("network_gate_rejected")
        try:
            self.network_gate = module.OwnedNetworkGate(descriptor)
            self.acquire_unit_reference(descriptor)
            self.network_programs = self.network_gate.install()
            self.verify_network_gate()
        except Exception as error:
            self.network_diagnostic(error)
            raise ValidationError("network_gate_rejected") from None
        observed = {
            name: getattr(descriptor, name)
            for name in ("invocation_id", "pid", "startticks", "uid", "cgroup_inode", "cgroup_device")
        }
        observed["unit"] = descriptor.unit
        permit = {
            "schema": 1,
            "binding": binding,
            "identity": observed,
            "programs": self.network_programs,
            "helper_sha256": NETWORK_GATE_SHA256,
            "provider_ipv4": pins,
            "allowed_at": int(time.time()),
            "preflight_only": self.preflight_only,
        }
        self.stage("network_permit")
        self.verify_network_gate()
        write(self.work / "network-allow.json", encoded(permit), 0o444)

    def verify_network_gate(self):
        try:
            self.verify_unit_reference()
            if self.network_gate is None or self.network_gate.verify() != self.network_programs:
                raise ValidationError("network_gate_rejected")
            self.verify_unit_reference()
        except Exception as error:
            self.network_diagnostic(error)
            raise ValidationError("network_gate_rejected") from None

    def network_diagnostic(self, error=None):
        try:
            value = (
                self.network_gate.diagnostic()
                if self.network_gate is not None
                else self.network_module.safe_diagnostic(error)
                if self.network_module is not None
                else {"stage": "unknown", "command": None, "errno": None}
            )
            valid = (
                type(value) is dict
                and set(value) == {"stage", "command", "errno"}
                and type(value["stage"]) is str
                and value["stage"] in NETWORK_DIAGNOSTIC_STAGES
                and (
                    value["command"] is None
                    or type(value["command"]) is int
                    and value["command"] in {5, 8, 9, 13, 15, 16}
                )
                and (value["errno"] is None or type(value["errno"]) is int and 0 <= value["errno"] <= 4095)
            )
            if not valid:
                raise ValidationError("network_gate_rejected")
        except Exception:
            value = {"stage": "unknown", "command": None, "errno": None}
        # A successful cleanup must not erase an earlier failed phase. Prefer
        # the first bounded syscall errno if a later failure supplies one.
        if error is None and self.network_diagnostic_failed:
            return
        if error is not None:
            self.network_diagnostic_failed = True
        if self.report["network_gate_diagnostic"]["errno"] is None:
            self.report["network_gate_diagnostic"] = dict(value)

    def network_proof(self, kind, document):
        if type(document) is not dict or type(document.get("schema")) is not int or document["schema"] != 1:
            raise ValidationError("network_gate_rejected")
        raw = encoded(document)
        if len(raw) > 16384:
            raise ValidationError("network_gate_rejected")
        write(self.work / ("network-gate-" + kind + ".json"), raw, 0o600)
        directory = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return hashlib.sha256(raw).hexdigest()

    def reference_failure(self, error):
        category = getattr(error, "category", None)
        if type(category) is not str or category not in UNIT_REFERENCE_ERRORS:
            category = "reference_state_rejected"
        if self.report["unit_reference_error_class"] is None:
            self.report["unit_reference_error_class"] = category

    def reference_proof(self, proof, state, role):
        identity = self.reference_identities.get(role)
        if (
            type(identity) is not dict
            or type(proof) is not dict
            or set(proof)
            != {"schema", "unit", "invocation_id", "pid", "startticks", "uid", "unique_name", "state"}
            or type(proof["schema"]) is not int
            or proof["schema"] != 1
            or any(
                type(proof[key]) is not type(value) or proof[key] != value for key, value in identity.items()
            )
            or proof["state"] != state
            or type(proof["unique_name"]) is not str
            or len(proof["unique_name"]) > 255
            or not re.fullmatch(r":[0-9]+\.[0-9]+", proof["unique_name"])
            or role in self.reference_unique_names
            and proof["unique_name"] != self.reference_unique_names[role]
        ):
            raise ValidationError("network_gate_rejected")
        return proof

    def write_reference_proof(self, kind, proof):
        allowed = {"acquire", "stop", "release", *("release-" + role for role in self.reference_roles)}
        if kind not in allowed:
            raise ValidationError("network_gate_rejected")
        raw = encoded(proof)
        if len(raw) > 16384:
            raise ValidationError("network_gate_rejected")
        write(self.work / ("unit-reference-" + kind + ".json"), raw, 0o600)
        directory = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return hashlib.sha256(raw).hexdigest()

    def reference_bundle(self, proofs):
        if set(proofs) != set(self.reference_roles):
            raise ValidationError("network_gate_rejected")
        return {
            "schema": 1,
            "references": [{"role": role, "proof": proofs[role]} for role in self.reference_roles],
        }

    def acquire_unit_reference(self, descriptor):
        try:
            if self.unit_references or self.report["unit_reference_acquire_sha256"] is not None:
                raise ValidationError("network_gate_rejected")
            module = load_unit_reference(self.network_gate_path.with_name("systemd_unit_reference.py"))
            expected = {
                self.reference_roles[0]: module.ExpectedIdentity(
                    **{
                        name: getattr(descriptor, name)
                        for name in (
                            "role",
                            "candidate_prefix",
                            "nonce",
                            "invocation_id",
                            "pid",
                            "startticks",
                            "uid",
                        )
                    }
                )
            }

            proofs = {}
            for role in self.reference_roles:
                item = expected[role]
                self.reference_identities[role] = {
                    "unit": item.unit,
                    **{name: getattr(item, name) for name in ("invocation_id", "pid", "startticks", "uid")},
                }
                self.unit_references[role] = module.UnitReference(item)
                proof = self.reference_proof(self.unit_references[role].acquire(), "acquired", role)
                self.reference_unique_names[role] = proof["unique_name"]
                proofs[role] = proof
            self.report["unit_reference_acquire_sha256"] = self.write_reference_proof(
                "acquire", self.reference_bundle(proofs)
            )
        except Exception as error:
            self.reference_failure(error)
            raise ValidationError("network_gate_rejected") from None

    def verify_unit_reference(self, *, stopped=False, role=None):
        try:
            role = self.reference_roles[0] if role is None else role
            if (
                role not in self.reference_roles
                or role not in self.unit_references
                or self.report["unit_reference_acquire_sha256"] is None
            ):
                raise ValidationError("network_gate_rejected")
            return self.reference_proof(
                self.unit_references[role].verify(stopped=stopped),
                "verified_stopped" if stopped else "verified_running",
                role,
            )
        except Exception as error:
            self.reference_failure(error)
            raise ValidationError("network_gate_rejected") from None

    def reference_snapshot(self):
        return self.reference_bundle(
            {role: self.verify_unit_reference(role=role) for role in self.reference_roles}
        )

    def release_unit_reference(self, role=None):
        try:
            role = self.reference_roles[0] if role is None else role
            self.verify_unit_reference(stopped=True, role=role)
            if (
                not self.report["network_gate_objects_released"]
                or self.report["unit_reference_stop_sha256"] is None
                or role in self.reference_releases
            ):
                raise ValidationError("network_gate_rejected")
            proof = self.reference_proof(self.unit_references[role].release(), "released", role)
            self.write_reference_proof("release-" + role, proof)
            self.reference_releases[role] = proof
            if set(self.reference_releases) == set(self.reference_roles):
                self.report["unit_reference_release_sha256"] = self.write_reference_proof(
                    "release", self.reference_bundle(self.reference_releases)
                )
                self.report["unit_reference_released"] = True
                if self.network_cleanup_complete:
                    self.report["network_gate_cleaned"] = True
        except Exception as error:
            self.reference_failure(error)
            raise ValidationError("network_gate_rejected") from None

    def close_unit_reference(self):
        failed = None
        for role in self.reference_roles:
            if role not in self.unit_references:
                continue
            try:
                self.unit_references[role].close()
            except Exception as error:
                self.reference_failure(error)
                failed = error
        if failed is not None:
            raise ValidationError("network_gate_rejected") from None

    def prepare_network_stop(self):
        if self.report["network_gate_stop_proof_sha256"] is not None:
            return
        try:
            reference = self.reference_snapshot()
            document = self.network_gate.prepare_stop()
            if self.reference_snapshot() != reference:
                raise ValidationError("network_gate_rejected")
            if (
                type(document) is not dict
                or set(document) != {"schema", "profile", "identity", "programs", "verified_at_ns"}
                or document.get("profile") not in {"linux-6.8", "linux-5.15-source-audit"}
                or type(document.get("identity")) is not dict
                or document.get("programs") != self.network_programs
                or type(document.get("verified_at_ns")) is not int
                or document["verified_at_ns"] <= 0
            ):
                raise ValidationError("network_gate_rejected")
            digest = self.network_proof("stop", document)
            self.network_stop_proof = document
            self.report["network_gate_stop_proof_sha256"] = digest
            self.report["unit_reference_stop_sha256"] = self.write_reference_proof("stop", reference)
        except Exception as error:
            self.network_diagnostic(error)
            raise ValidationError("network_gate_rejected") from None

    def cleanup_network_gate(self):
        try:
            self.verify_unit_reference(stopped=True)
            document = self.network_gate.cleanup()
            if (
                type(document) is not dict
                or set(document)
                != {
                    "schema",
                    "mode",
                    "complete",
                    "objects_released",
                    "program_count",
                    "programs",
                    "offline_queries",
                    "detached_hooks",
                    "stop_verified_at_ns",
                    "profile",
                }
                or document.get("mode") not in {"retained_empty", "collected_offline", "partial_closed"}
                or document.get("profile") not in {"linux-6.8", "linux-5.15-source-audit"}
                or type(document.get("complete")) is not bool
                or type(document.get("objects_released")) is not bool
                or type(document.get("program_count")) is not int
                or not 0 <= document["program_count"] <= 4
                or type(document.get("programs")) is not list
                or len(document["programs"]) != document["program_count"]
                or type(document.get("offline_queries")) is not list
                or len(document["offline_queries"]) not in {0, 8}
                or type(document.get("detached_hooks")) is not list
                or len(document["detached_hooks"]) > 4
                or document["objects_released"]
                and not document["complete"]
                or document["complete"]
                and (
                    document["program_count"] != 4
                    or self.network_stop_proof is None
                    or document["stop_verified_at_ns"] != self.network_stop_proof["verified_at_ns"]
                    or document["profile"] != self.network_stop_proof["profile"]
                )
            ):
                raise ValidationError("network_gate_rejected")
            digest = self.network_proof("cleanup", document)
            self.report.update(
                network_gate_cleanup_proof_sha256=digest,
                network_gate_cleanup_mode=document["mode"],
                network_gate_objects_released=document["objects_released"],
            )
            self.network_diagnostic()
            if not (
                document["complete"]
                and document["objects_released"]
                and document["mode"] in {"retained_empty", "collected_offline"}
                and self.report["network_gate_stop_proof_sha256"] is not None
            ):
                raise ValidationError("network_gate_rejected")
            self.release_unit_reference()
            self.network_cleanup_complete = True
            self.report["network_gate_cleaned"] = self.report["unit_reference_released"]
        except Exception as error:
            self.network_diagnostic(error)
            raise ValidationError("network_gate_rejected") from None

    def result(self):
        self.verify_network_gate()
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
            while not (self.work / "output/hello.json").exists() and time.monotonic() < deadline:
                self.boundary("market_wait_hello")
                time.sleep(0.05)
            self.install_network_gate()
            deadline = min(time.monotonic() + 25, time.monotonic() + max(0, self.issued + 55 - time.time()))
            while not (self.work / "output/bootstrap.json").exists() and time.monotonic() < deadline:
                self.boundary("market_wait_proof")
                time.sleep(0.05)
            proof, digest = self.proof()
            identity = self.identity()
            self.verify_network_gate()
            self.report.update(network_gate_verified=True, network_gate_verified_before_cleanup=True)
            if self.preflight_only:
                if (
                    self.production() != self.original
                    or self.identity() != identity
                    or self.proof()[1] != digest
                ):
                    raise ValidationError("kernel_rejected")
                self.verify_network_gate()
                self.report.update(passed=True, identity=identity, bootstrap_sha256=digest)
                return self.report
            self.stage("allow_public_http")
            if self.production() != self.original or self.identity() != identity or self.proof()[1] != digest:
                raise ValidationError("kernel_rejected")
            self.verify_network_gate()
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
            while not (self.work / "output/market-result.json").exists() and time.time() <= self.issued + 65:
                self.boundary("market_wait_result")
                state = self.show()
                if state.get("MainPID") == "0":
                    raise ValidationError("worker_failed")
                if state.get("MainPID") != str(self.pid) or state.get("InvocationID") != self.invocation:
                    raise ValidationError("unit_rejected")
                time.sleep(0.1)
            self.identity()
            accepted = self.result()
            self.verify_network_gate()
            self.prepare_network_stop()
            write(
                self.work / "result-ack.json",
                encoded(
                    {
                        "schema": 1,
                        "binding": proof["binding"],
                        "result_sha256": self.report["worker_result_sha256"],
                    }
                ),
                0o444,
            )
            while time.time() <= self.issued + 65:
                self.boundary("market_wait_exit")
                state = self.show()
                if state.get("MainPID") == "0":
                    break
                if state.get("MainPID") != str(self.pid) or state.get("InvocationID") != self.invocation:
                    raise ValidationError("unit_rejected")
                time.sleep(0.05)
            if state.get("MainPID") != "0":
                raise ValidationError("worker_failed")
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
                    gate_failed = False
                    if (
                        self.network_gate is not None
                        and self.report["network_gate_stop_proof_sha256"] is None
                    ):
                        try:
                            self.prepare_network_stop()
                        except Exception:
                            gate_failed = True
                    self.run(["/usr/bin/systemctl", "stop", self.unit], timeout=15)
                    if not self.empty():
                        raise ValidationError("cleanup_failed")
                    self.idle()
                    self.report["unit_stopped"] = True
                    if self.network_gate is not None:
                        try:
                            self.cleanup_network_gate()
                        except Exception:
                            gate_failed = True
                    if gate_failed:
                        raise ValidationError("cleanup_failed")
                except Exception:
                    self.report.update(
                        passed=False, cleanup_error_class="cleanup_failed", network_gate_verified=False
                    )
                    if self.report["error_class"] is None:
                        self.report["error_class"] = "cleanup_failed"
            try:
                self.close_unit_reference()
            except Exception:
                self.report.update(
                    passed=False,
                    cleanup_error_class="cleanup_failed",
                    network_gate_verified=False,
                    network_gate_cleaned=False,
                )
                if self.report["error_class"] is None:
                    self.report["error_class"] = "cleanup_failed"
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


def run_validation(request, *, boundary, run, record, network_gate_path=None):
    """Trusted callbacks are controller-owned; request carries public source only."""
    return Validation(
        request,
        boundary=boundary,
        run=run,
        record=record,
        network_gate_path=network_gate_path,
    ).execute()


def run_network_preflight(request, *, boundary, run, record, network_gate_path):
    """Fixed diagnostic: direct pinned TLS only, before any candidate import/HTTP."""
    return Validation(
        request,
        boundary=boundary,
        run=run,
        record=record,
        network_gate_path=network_gate_path,
        preflight_only=True,
    ).execute()

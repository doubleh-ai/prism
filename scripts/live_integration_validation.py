#!/usr/bin/env python3
"""Fixed-target root supervisor for bounded live integration, not deployment.

Imported only after the external controller verifies this helper's exact SHA.
``boundary(stage)`` must enforce the independent operator/stop/source gates;
``run(argv, timeout=...)`` runs fixed argv without a shell; ``record(dict)``
persists only safe journal records. ``key_provider()`` belongs to the trusted
root controller and returns only its allowlisted DeepSeek key. This module
never reads an env/credential file or puts a key in argv, env, disk or records.

Actual Linux/systemd/BPF/UID/mount/network witnesses are required. Mock tests
are never live evidence. Both transient units are synthetic and expire within
180s; cleanup never stops the production or staging service.
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
import struct
import sys
import time
import types
from pathlib import Path

OPERATIONS = Path("/var/lib/prism-operations/integration")
RELEASES = Path("/opt/prism/releases")
TOOLS = Path("/opt/prism/tools/claude")
PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
RESOLV = Path("/etc/resolv.conf")
NETWORK_GATE_SHA256 = "7a0cf487d6456d73412a873c89d1e08c2a4a5643c663529b2c22ae5341f4c22c"
EXTRA_NEGATIVES = {"ipv6_loopback", "ipv4_mapped_public", "udp_provider", "udp6_loopback"}
RESOLVE_CODE = 'import json,socket;print(json.dumps(sorted({row[4][0] for row in socket.getaddrinfo("api.deepseek.com",443,type=socket.SOCK_STREAM)})))'
SOURCE_PINS = {
    "worker": "f7f21d576417d45195c8fcf91f25f181728dafb7f30acea1077d4c75f75a75a4",
    "transport": "e92bf2120df61b9b14aeea1b1e147bf11218785f54ab50d84f7e12a33cc80e76",
    "policy": "c6fa205466800636fe6fbe557eb8f625785567a129b405ebb4935b7b65e325d9",
}
BOOT_CHECKS = {
    "production_home_denied",
    "production_proc_denied",
    "production_8088_denied",
    "vibe_8899_denied",
    "metadata_denied",
    "private_network_denied",
    "direct_public_egress_denied",
    "candidate_write_denied",
    "config_write_denied",
    "staging_directory_denied",
    "synthetic_empty_data",
}
WITNESSES = {
    "dedicated_worker_uid",
    "dedicated_broker_uid",
    "worker_private_network_namespace",
    "production_8088_denied",
    "vibe_8899_denied",
    "metadata_denied",
    "private_network_denied",
    "production_home_denied",
    "production_proc_denied",
    "candidate_write_denied",
    "config_write_denied",
    "egress_allowlist_enforced",
    "public_dns_pinned",
    "upstream_tls_verified",
    "synthetic_empty_data",
}
NEGATIVE_TARGETS = {
    "production": ("127.0.0.1", 8088),
    "vibe": ("127.0.0.1", 8899),
    "metadata": ("169.254.169.254", 80),
    "private": ("10.255.255.1", 443),
}
BROKER_FILESYSTEM_CHECKS = {
    "production_home_denied",
    "production_proc_denied",
    "config_write_denied",
    "staging_directory_denied",
}
SAFE_ERRORS = {
    "request_rejected",
    "root_linux_required",
    "source_rejected",
    "candidate_rejected",
    "account_rejected",
    "uid_busy",
    "workspace_exists",
    "workspace_rejected",
    "dns_rejected",
    "unit_rejected",
    "kernel_rejected",
    "private_run_rejected",
    "bootstrap_rejected",
    "ipc_rejected",
    "broker_preflight_rejected",
    "key_rejected",
    "worker_result_rejected",
    "worker_failed",
    "broker_failed",
    "cleanup_failed",
    "supervision_failed",
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

# No site or candidate import; exact sealed transport/policy only. Real key is
# requested over this one root-owned, peer-verified UDS after the root permit.
BROKER_BOOTSTRAP = (
    r"""
import contextlib, ctypes, errno, hashlib, importlib.util, ipaddress, json, os, resource, socket, stat, struct, sys, time
from pathlib import Path
"""
    + NETWORK_PERMIT_READER
    + r"""

def packed(value):
    raw=json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    if len(raw)>16384: raise RuntimeError("ipc_rejected")
    return struct.pack("!I",len(raw))+raw
def receive(channel):
    def exact(count):
        data=b""
        while len(data)<count:
            value=channel.recv(count-len(data))
            if not value: raise RuntimeError("ipc_rejected")
            data+=value
        return data
    size=struct.unpack("!I",exact(4))[0]
    if not 0<size<=16384: raise RuntimeError("ipc_rejected")
    value=json.loads(exact(size))
    if type(value) is not dict: raise RuntimeError("ipc_rejected")
    return value
def root_source(path,expected):
    for part in (path,*path.parents):
        info=part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022: raise RuntimeError("source_rejected")
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    with os.fdopen(fd,"rb") as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>131072: raise RuntimeError("source_rejected")
        raw=stream.read(131073)
    if hashlib.sha256(raw).hexdigest()!=expected: raise RuntimeError("source_rejected")
    return raw
def denied_open(path,write=False):
    try:
        fd=os.open(path,os.O_CLOEXEC|os.O_NOFOLLOW|(os.O_WRONLY if write else os.O_RDONLY))
    except OSError as error:
        return error.errno in ({errno.EACCES,errno.EPERM,errno.EROFS} if write else {errno.EACCES,errno.EPERM,errno.ENOENT})
    os.close(fd)
    return False
def seal_memory():
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    if resource.getrlimit(resource.RLIMIT_CORE)!=(0,0): raise RuntimeError("broker_memory_unsealed")
    call=ctypes.CDLL(None,use_errno=True).prctl
    call.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong];call.restype=ctypes.c_int
    if call(4,0,0,0,0)!=0 or call(3,0,0,0,0)!=0 or resource.getrlimit(resource.RLIMIT_CORE)!=(0,0): raise RuntimeError("broker_memory_unsealed")
    return True
def main():
    config=json.loads(sys.argv[1]); work=Path(config["work"])
    if sys.platform!="linux" or os.getuid()!=config["broker_uid"] or os.getuid()==0 or not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode: raise RuntimeError("broker_identity_rejected")
    memory_sealed=seal_memory()
    filesystem={"production_home_denied":denied_open("/home/ubuntu/stock-analyzer/.env"),"production_proc_denied":denied_open("/proc/"+str(config["production_pid"])+"/environ"),"config_write_denied":denied_open("/home/ubuntu/stock-analyzer/.env",write=True),"staging_directory_denied":denied_open("/var/lib/prism-operations/staging")}
    if any(value is not True for value in filesystem.values()): raise RuntimeError("broker_filesystem_rejected")
    source=work/"transport.py"; raw=root_source(source,config["transport_sha256"])
    module=importlib.util.module_from_spec(importlib.util.spec_from_loader("_prism_live_transport",loader=None));module.__file__=str(source)
    exec(compile(raw,str(source),"exec"),module.__dict__)
    path=work/"credential.sock";info=path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=0 or info.st_gid!=config["broker_gid"] or stat.S_IMODE(info.st_mode)!=0o660: raise RuntimeError("ipc_rejected")
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as channel:
        channel.settimeout(25);channel.connect(str(path))
        pid,uid,gid=struct.unpack("3i",channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if (pid,uid)!=(config["root_pid"],0): raise RuntimeError("ipc_rejected")
        channel.sendall(packed({"schema":1,"kind":"hello","candidate":config['candidate'],"nonce":config["nonce"],"pid":os.getpid(),"startticks":self_ticks(),"bootstrap_sha256":config['bootstrap_sha256'],"transport_sha256":config['transport_sha256']}))
        launch=receive(channel)
        if set(launch)!={"schema","kind","binding","negative_targets","provider_pins","network_permit_sha256"} or launch["schema"]!=1 or launch["kind"]!="binding": raise RuntimeError("ipc_rejected")
        binding=launch["binding"]
        if binding["broker_pid"]!=os.getpid() or binding["nonce"]!=config["nonce"]: raise RuntimeError("ipc_rejected")
        config['network_pins']=launch['provider_pins']
        network_permit(work,binding,config,launch['network_permit_sha256'])
        broker=module.Broker(work,binding); observed=set();original=broker.upstream.connect
        def connect(deadline):
            secured=original(deadline);peer=secured.getpeername()[0]
            if peer not in launch["provider_pins"]:
                secured.close();raise RuntimeError("provider_peer_rejected")
            observed.add(peer);return secured
        broker.upstream.connect=connect
        def gatekeeper(identity,facts):
            negative={}
            for name,target in launch["negative_targets"].items():
                with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.5);negative[name]=probe.connect_ex(tuple(target))
            negative.update(extra_network_denials(launch["provider_pins"][0]))
            channel.sendall(packed({"schema":1,"kind":"preflight","binding":identity,"facts":{**facts,"broker_memory_sealed":memory_sealed},"negative_errno":negative,"positive_peers":sorted(observed),"filesystem_checks":filesystem}))
            reply=receive(channel)
            if reply!={"schema":1,"kind":"allowed","nonce":config["nonce"]}: raise RuntimeError("ipc_rejected")
        def key_provider():
            channel.sendall(packed({"schema":1,"kind":"key","binding":binding}))
            reply=receive(channel)
            if set(reply)!={"schema","kind","key"} or reply["schema"]!=1 or reply["kind"]!="key" or type(reply["key"]) is not str: raise RuntimeError("ipc_rejected")
            return reply["key"]
        if config['preflight_only']:
            facts=broker.upstream.preflight(min(broker.deadline,time.monotonic()+25))
            gatekeeper(binding,facts)
            if receive(channel)!={'schema':1,'kind':'preflight-finish','nonce':config['nonce']}:raise RuntimeError('ipc_rejected')
            return
        with module.serve_broker(broker,config["bridge_gid"]):
            broker.authorize(gatekeeper=gatekeeper,key_provider=key_provider)
            channel.sendall(packed({"schema":1,"kind":"authorized","binding":binding}))
            channel.settimeout(max(0.1,config["issued_at"]+178-time.time()))
            if receive(channel)!={"schema":1,"kind":"finish","nonce":config["nonce"]}: raise RuntimeError("ipc_rejected")
            channel.sendall(packed({"schema":1,"kind":"summary","binding":binding,"summary":broker.summary()}))
            if receive(channel)!={'schema':1,'kind':'result-ack','nonce':config['nonce']}:raise RuntimeError('ipc_rejected')
try:
    main()
except Exception:
    raise SystemExit(1)
"""
)
BROKER_BOOTSTRAP_SHA256 = hashlib.sha256(BROKER_BOOTSTRAP.encode()).hexdigest()


class ValidationError(RuntimeError):
    """Fixed safe category; never an exception, key or provider response string."""


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def decode(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_):
        raise ValueError

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValidationError("ipc_rejected") from None


def exact(channel, count):
    value = b""
    while len(value) < count:
        chunk = channel.recv(count - len(value))
        if not chunk:
            raise ValidationError("ipc_rejected")
        value += chunk
    return value


def receive(channel):
    size = struct.unpack("!I", exact(channel, 4))[0]
    if not 0 < size <= 16384:
        raise ValidationError("ipc_rejected")
    value = decode(exact(channel, size))
    if not isinstance(value, dict):
        raise ValidationError("ipc_rejected")
    return value


def send(channel, value):
    raw = encoded(value)
    if not 0 < len(raw) <= 16384:
        raise ValidationError("ipc_rejected")
    channel.sendall(struct.pack("!I", len(raw)) + raw)


def peer(channel, pid, uid):
    if not hasattr(socket, "SO_PEERCRED"):
        raise ValidationError("ipc_rejected")
    actual = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if actual[:2] != (pid, uid):
        raise ValidationError("ipc_rejected")


def root_locked(path):
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
    root_locked(path)
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


def write(path, raw, mode=0o644):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def read_private(path, uid, *, maximum=16384):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise ValidationError("worker_result_rejected")
        raw = stream.read(maximum + 1)
    value = decode(raw)
    if not isinstance(value, dict):
        raise ValidationError("worker_result_rejected")
    return value, hashlib.sha256(raw).hexdigest()


def validate_request(request):
    if (
        not isinstance(request, dict)
        or set(request) != {"schema", "release_id", "candidate", "cli_sha256", "production_pid", "sources"}
        or type(request.get("schema")) is not int
        or request["schema"] != 1
        or not isinstance(request.get("release_id"), str)
        or not re.fullmatch("[a-z0-9][a-z0-9-]{7,39}", request["release_id"])
        or not isinstance(request.get("candidate"), str)
        or not re.fullmatch("[0-9a-f]{40}", request["candidate"])
        or not isinstance(request.get("cli_sha256"), str)
        or not re.fullmatch("[0-9a-f]{64}", request["cli_sha256"])
        or type(request.get("production_pid")) is not int
        or request["production_pid"] <= 1
        or not isinstance(request.get("sources"), dict)
        or set(request["sources"]) != set(SOURCE_PINS)
    ):
        raise ValidationError("request_rejected")
    sources = {}
    for name, value in request["sources"].items():
        try:
            if not isinstance(value, str) or len(value) > 180000:
                raise ValueError
            raw = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            raise ValidationError("source_rejected") from None
        if not 0 < len(raw) <= 131072 or hashlib.sha256(raw).hexdigest() != SOURCE_PINS[name]:
            raise ValidationError("source_rejected")
        sources[name] = raw
    return sources


def public_ip(value):
    if not isinstance(value, str) or "%" in value:
        raise ValidationError("dns_rejected")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValidationError("dns_rejected") from None
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
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
    return str(address)


def network_policy(resolve=None):
    addresses = (
        resolve()
        if resolve is not None
        else [row[4][0] for row in socket.getaddrinfo("api.deepseek.com", 443, type=socket.SOCK_STREAM)]
    )
    if not isinstance(addresses, list) or not 0 < len(addresses) <= 32:
        raise ValidationError("dns_rejected")
    checked = {public_ip(value) for value in addresses}
    pins = sorted(value for value in checked if ":" not in value)
    if not pins or len(pins) > 8:
        raise ValidationError("dns_rejected")
    with RESOLV.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValidationError("dns_rejected")
    try:
        text = raw.decode("ascii")
    except UnicodeError:
        raise ValidationError("dns_rejected") from None
    resolvers = set()
    for line in text.splitlines():
        fields = line.partition("#")[0].split()
        if fields[:1] == ["nameserver"]:
            if len(fields) != 2:
                raise ValidationError("dns_rejected")
            try:
                address = ipaddress.ip_address(fields[1])
            except ValueError:
                raise ValidationError("dns_rejected") from None
            if str(address) != "127.0.0.53":
                raise ValidationError("dns_rejected")
            resolvers.add(str(address))
    if resolvers != {"127.0.0.53"}:
        raise ValidationError("dns_rejected")
    off_public = next(
        value
        for value in ("9.9.9.9", "8.8.4.4", "208.67.222.222", "1.0.0.1")
        if value not in set(pins) | resolvers
    )
    return pins, sorted(resolvers), {**NEGATIVE_TARGETS, "disallowed_public": (off_public, 443)}


def uids(process):
    text = (process / "status").read_text()
    row = next((line for line in text.splitlines() if line.startswith("Uid:")), "")
    values = row.split()[1:]
    if len(values) != 4 or any(not value.isdigit() for value in values):
        raise ValidationError("kernel_rejected")
    return tuple(int(value) for value in values)


def startticks(process):
    values = (process / "stat").read_text().rsplit(")", 1)[1].split()
    if len(values) < 20 or not values[19].isdigit():
        raise ValidationError("kernel_rejected")
    return int(values[19])


def core_disabled(process):
    # Read the real process limit, independently of systemd's desired setting.
    rows = [
        line.split()
        for line in (process / "limits").read_text().splitlines()
        if line.startswith("Max core file size")
    ]
    return len(rows) == 1 and rows[0] == ["Max", "core", "file", "size", "0", "0", "bytes"]


def private_run(process):
    rows = []
    for line in (process / "mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) > 9 and fields[4] == "/run":
            separator = fields.index("-")
            rows.append((fields[5].split(","), fields[separator + 1]))
    if len(rows) != 1 or "ro" not in rows[0][0] or rows[0][1] != "tmpfs":
        raise ValidationError("private_run_rejected")
    # /proc/<pid>/root is an intentional kernel link. The final /run must be a
    # separate, root-owned read-only mount, not merely a declared unit flag.
    runtime = process / "root/run"
    descriptor = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        private, current, host = os.fstat(descriptor), runtime.stat(), Path("/run").stat()
        filesystem = os.fstatvfs(descriptor)
        if (
            not stat.S_ISDIR(private.st_mode)
            or private.st_uid != 0
            or private.st_mode & 0o022
            or (private.st_dev, private.st_ino) == (host.st_dev, host.st_ino)
            or (private.st_dev, private.st_ino) != (current.st_dev, current.st_ino)
            or not filesystem.f_flag & os.ST_RDONLY
        ):
            raise ValidationError("private_run_rejected")
    finally:
        os.close(descriptor)


class Validation:
    def __init__(
        self, request, *, boundary, run, record, key_provider, network_gate_path=None, preflight_only=False
    ):
        if type(preflight_only) is not bool:
            raise ValidationError("request_rejected")
        self.request = request
        self.sources = validate_request(request)
        self.boundary, self._run, self.record, self.key_provider = boundary, run, record, key_provider
        self.preflight_only = preflight_only
        if preflight_only:
            self.key_provider = None
        self.network_gate_path, self.network_gate = network_gate_path, None
        self.network_programs = None
        self.nonce, self.issued = secrets.token_hex(16), int(time.time())
        self.work = OPERATIONS / request["release_id"] / "live"
        self.candidate = RELEASES / request["candidate"]
        self.cli = TOOLS / request["cli_sha256"] / "claude"
        stem = request["candidate"][:12] + "-" + self.nonce
        self.units = {role: "prism-live-" + role + "-" + stem + ".service" for role in ("worker", "broker")}
        self.launched = set()
        self.invocations = {}
        self.accounts = {}
        self.binding = None
        self.kernel = None
        self.proof_sha = None
        self.workspace_created = False
        self.key_attempted = False
        self.network_allow = set()
        self.deadline = time.monotonic() + 180
        self.report = {
            "schema": 1,
            "action": "integration_network_preflight" if preflight_only else "integration_live",
            "candidate": request["candidate"],
            "release_id": request["release_id"],
            "source_sha256": dict(SOURCE_PINS),
            "started_at": self.issued,
            "finished_at": None,
            "stage": "request_validated",
            "passed": False,
            "error_class": None,
            "live_model_verified": False,
            "native_search_verified": False,
            "owned_mcp_verified": False,
            "server_clock_verified": False,
            "native_fetch_verified": False,
            "market_provider_verified": False,
            "research_quality_verified": False,
            "production_verified": False,
            "units_stopped": False,
            "cleanup_error_class": None,
            "broker_negative_errno": {},
            "broker_filesystem_checks": {},
            "broker_preflight_facts": {},
            "broker_memory_sealed": False,
            "provider_peer_count": 0,
            "bootstrap_proof_sha256": None,
            "worker_result_sha256": None,
            "blocked_provider_header_names": [],
            "official_source_count": 0,
            "broker_summary_sha256": None,
            "preflight_only": preflight_only,
            "network_gate_verified": False,
            "network_gate_verified_before_cleanup": False,
            "network_gate_cleaned": False,
            "key_acquisition_attempted": False,
            "requests": dict.fromkeys(("messages", "count_tokens", "quote", "kline", "public_fetch"), 0),
        }

    def stage(self, value):
        if time.monotonic() >= self.deadline:
            raise ValidationError("supervision_failed")
        self.report["stage"] = value
        self.boundary("live_" + value)
        self.record(
            {
                "schema": 1,
                "action": self.report["action"],
                "candidate": self.request["candidate"],
                "release_id": self.request["release_id"],
                "stage": value,
                "at": int(time.time()),
                "passed": False,
                "production_verified": False,
            }
        )

    def run(self, argv, *, timeout):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValidationError("supervision_failed")
        return self._run(argv, timeout=min(timeout, remaining))

    def source_identity(self):
        for name, expected in SOURCE_PINS.items():
            path = self.work / (name + ".py")
            root_locked(path)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 131072:
                    raise ValidationError("source_rejected")
                raw = stream.read(131073)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValidationError("source_rejected")

    def candidate_identity(self):
        for path in (self.candidate, self.candidate / ".venv", self.cli):
            root_locked(path)
        for name, expected in (
            (".deploy-revision", self.request["candidate"]),
            (".cli-revision", self.request["cli_sha256"]),
        ):
            path = self.candidate / name
            root_locked(path)
            if (
                not path.is_file()
                or path.stat().st_nlink != 1
                or path.stat().st_size > 128
                or path.read_text().strip() != expected
            ):
                raise ValidationError("candidate_rejected")
        info = self.cli.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o755:
            raise ValidationError("candidate_rejected")
        with self.cli.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise ValidationError("candidate_rejected")
            stream.seek(0)
            if hashlib.file_digest(stream, "sha256").hexdigest() != self.request["cli_sha256"]:
                raise ValidationError("candidate_rejected")

    def accounts_ready(self):
        self.stage("accounts")
        for role, name in (("worker", "prism-integration"), ("broker", "prism-integration-broker")):
            try:
                account = pwd.getpwnam(name)
            except KeyError:
                self.run(
                    [
                        "/usr/sbin/useradd",
                        "--system",
                        "--user-group",
                        "--no-create-home",
                        "--shell",
                        "/usr/sbin/nologin",
                        name,
                    ],
                    timeout=15,
                )
                account = pwd.getpwnam(name)
            if (
                account.pw_uid == 0
                or account.pw_gid == 0
                or account.pw_shell != "/usr/sbin/nologin"
                or grp.getgrgid(account.pw_gid).gr_name != name
                or any(name in group.gr_mem for group in grp.getgrall())
            ):
                raise ValidationError("account_rejected")
            self.accounts[role] = account
        others = [pwd.getpwnam(name) for name in ("ubuntu", "prism-stage")]
        all_accounts = [*self.accounts.values(), *others]
        if len({value.pw_uid for value in all_accounts}) != len(all_accounts) or len(
            {value.pw_gid for value in all_accounts}
        ) != len(all_accounts):
            raise ValidationError("account_rejected")
        try:
            group = grp.getgrnam("prism-integration-bridge")
        except KeyError:
            self.run(["/usr/sbin/groupadd", "--system", "prism-integration-bridge"], timeout=15)
            group = grp.getgrnam("prism-integration-bridge")
        if group.gr_gid == 0 or group.gr_gid in {account.pw_gid for account in all_accounts} or group.gr_mem:
            raise ValidationError("account_rejected")
        self.bridge_gid = group.gr_gid
        for process in PROC.iterdir():
            if not process.name.isdigit():
                continue
            try:
                values = uids(process)
            except FileNotFoundError:
                continue
            if set(values) & {account.pw_uid for account in self.accounts.values()}:
                raise ValidationError("uid_busy")

    def workspace(self):
        self.stage("workspace")
        if self.work.exists():
            raise ValidationError("workspace_exists")
        for path in (OPERATIONS, self.work.parent):
            if not path.exists():
                path.mkdir(mode=0o755)
                os.chmod(path, 0o755)
            root_locked(path)
            if stat.S_IMODE(path.stat().st_mode) != 0o755:
                raise ValidationError("workspace_rejected")
        self.work.mkdir(mode=0o755)
        self.workspace_created = True
        os.chmod(self.work, 0o755)
        for name in ("output", "home", "tmp", "data"):
            path = self.work / name
            path.mkdir(mode=0o700)
            os.chown(path, self.accounts["worker"].pw_uid, self.accounts["worker"].pw_gid)
        broker = self.work / "broker"
        broker.mkdir(mode=0o750)
        os.chmod(broker, 0o750)
        os.chown(broker, self.accounts["broker"].pw_uid, self.bridge_gid)
        (broker / "home").mkdir(mode=0o700)
        os.chown(broker / "home", self.accounts["broker"].pw_uid, self.accounts["broker"].pw_gid)
        for name, raw in self.sources.items():
            write(self.work / (name + ".py"), raw)

    def unit_command(self, role, pins, resolvers):
        account = self.accounts[role]
        args = [
            "/usr/bin/systemd-run",
            "--unit=" + self.units[role],
            "--service-type=exec",
            "--no-block",
            "--quiet",
            "--property=RemainAfterExit=yes",
            "--property=User=" + account.pw_name,
            "--property=Group=" + account.pw_name,
            "--property=SupplementaryGroups=prism-integration-bridge",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes",
            "--property=ProtectProc=invisible",
            "--property=ProcSubset=pid",
            "--property=PrivateTmp=yes",
            "--property=PrivateDevices=yes",
            "--property=TemporaryFileSystem=/run:ro",
            "--property=UMask=0077",
            "--property=RuntimeMaxSec=180",
            "--property=KillMode=control-group",
            "--property=MemoryMax=768M",
            "--property=TasksMax=48",
            "--property=LimitCORE=0",
            "--property=CPUQuota=100%",
            "--property=RestrictSUIDSGID=yes",
            "--property=CapabilityBoundingSet=",
            "--property=AmbientCapabilities=",
            "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "--property=InaccessiblePaths=-/var/lib/prism-operations/staging",
            "--property=StandardOutput=null",
            "--property=StandardError=null",
        ]
        if role == "worker":
            args += [
                "--property=PrivateNetwork=yes",
                "--property=ReadWritePaths="
                + " ".join(str(self.work / name) for name in ("output", "home", "tmp", "data")),
            ]
            env = ["HOME=" + str(self.work / "home"), "TMPDIR=" + str(self.work / "tmp")]
            command = [
                str(self.candidate / ".venv/bin/python"),
                "-I",
                "-B",
                "-S",
                str(self.work / "worker.py"),
                "--candidate",
                self.request["candidate"],
                "--cli-sha256",
                self.request["cli_sha256"],
                "--probe-sha256",
                SOURCE_PINS["worker"],
                "--transport-sha256",
                SOURCE_PINS["transport"],
                "--nonce",
                self.nonce,
                "--workdir",
                str(self.work),
                "--broker-pid",
                str(self.pids["broker"]),
                "--production-pid",
                str(self.request["production_pid"]),
                "--issued-at",
                str(self.issued),
                "--bridge-gid",
                str(self.bridge_gid),
            ]
        else:
            args += [
                "--property=IPAddressDeny=any",
                "--property=IPAddressAllow=" + " ".join([*pins, *resolvers]),
                "--property=ReadWritePaths=" + str(self.work / "broker"),
            ]
            if Path("/run/systemd/resolve").is_dir():
                args.append("--property=BindReadOnlyPaths=/run/systemd/resolve")
            env = ["HOME=" + str(self.work / "broker/home")]
            config = {
                "work": str(self.work),
                "transport_sha256": SOURCE_PINS["transport"],
                "root_pid": os.getpid(),
                "nonce": self.nonce,
                "broker_uid": account.pw_uid,
                "broker_gid": account.pw_gid,
                "bridge_gid": self.bridge_gid,
                "issued_at": self.issued,
                "production_pid": self.request["production_pid"],
                "candidate": self.request["candidate"],
                "bootstrap_sha256": BROKER_BOOTSTRAP_SHA256,
                "network_gate_sha256": NETWORK_GATE_SHA256,
                "network_role": "live",
                "preflight_only": self.preflight_only,
            }
            command = ["/usr/bin/python3", "-I", "-B", "-S", "-c", BROKER_BOOTSTRAP, encoded(config).decode()]
        return [
            *args,
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8",
            "TZ=Asia/Shanghai",
            "PYTHONDONTWRITEBYTECODE=1",
            *env,
            *command,
        ]

    def show(self, role, *, cleanup=False):
        raw = (self._run if cleanup else self.run)(
            [
                "/usr/bin/systemctl",
                "show",
                "--property=MainPID,ExecMainPID,InvocationID,ExecMainStatus,Result,User,Group,ControlGroup,NoNewPrivileges,ProtectSystem,ProtectHome,PrivateNetwork,KillMode,RuntimeMaxUSec,IPAddressDeny,IPAddressAllow,LimitCORE",
                self.units[role],
            ],
            timeout=5,
        )
        try:
            result = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines() if "=" in line)
        except (ValueError, UnicodeError):
            raise ValidationError("unit_rejected") from None
        return result

    def start(self, role, pins, resolvers):
        self.stage(role + "_start")
        existing = self.run(
            ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend", self.units[role]],
            timeout=5,
        )
        if existing.strip():
            raise ValidationError("unit_rejected")
        self.launched.add(role)
        self.run(self.unit_command(role, pins, resolvers), timeout=15)
        deadline = min(self.deadline, time.monotonic() + 10)
        while True:
            state = self.show(role)
            value = state.get("MainPID", "")
            if value.isdigit() and int(value) > 1:
                invocation = state.get("InvocationID", "")
                if not re.fullmatch("[0-9a-f]{32}", invocation) or state.get("ExecMainPID") != value:
                    raise ValidationError("unit_rejected")
                self.invocations[role] = (invocation, int(value), startticks(PROC / value))
                return int(value)
            if time.monotonic() >= deadline:
                raise ValidationError("unit_rejected")
            time.sleep(0.05)

    def identity(self, role):
        state = self.show(role)
        pid = self.pids[role]
        account = self.accounts[role]
        if (
            state.get("MainPID") != str(pid)
            or state.get("ExecMainPID") != str(pid)
            or state.get("InvocationID") != self.invocations[role][0]
            or startticks(PROC / str(pid)) != self.invocations[role][2]
            or state.get("User") != account.pw_name
            or state.get("Group") != account.pw_name
            or state.get("NoNewPrivileges") != "yes"
            or state.get("ProtectSystem") != "strict"
            or state.get("ProtectHome") != "yes"
            or state.get("KillMode") != "control-group"
            or state.get("RuntimeMaxUSec") not in {"3min", "180s", "180000000"}
            or state.get("PrivateNetwork") != ("yes" if role == "worker" else "no")
            or state.get("LimitCORE") != "0"
            or state.get("ControlGroup") != "/system.slice/" + self.units[role]
        ):
            raise ValidationError("unit_rejected")
        if role == "broker":
            try:
                deny = {
                    str(ipaddress.ip_network(value, strict=False))
                    for value in state.get("IPAddressDeny", "").split()
                }
                allow = {
                    str(ipaddress.ip_network(value, strict=False))
                    for value in state.get("IPAddressAllow", "").split()
                }
            except ValueError:
                raise ValidationError("unit_rejected") from None
            expected = {str(ipaddress.ip_network(value)) for value in self.network_allow}
            if deny != {"0.0.0.0/0", "::/0"} or allow != expected:
                raise ValidationError("unit_rejected")
        process = PROC / str(pid)
        if uids(process) != (account.pw_uid,) * 4 or not core_disabled(process):
            raise ValidationError("kernel_rejected")
        cgroup = (process / "cgroup").read_text().strip()
        if cgroup != "0::" + state["ControlGroup"]:
            raise ValidationError("kernel_rejected")
        private_run(process)
        return {
            "uids": list(uids(process)),
            "netns_inode": (process / "ns/net").stat().st_ino,
            "startticks": startticks(process),
        }

    def kernel_identity(self):
        production_pid = self.run(
            ["/usr/bin/systemctl", "show", "--value", "--property=MainPID", "stock-app.service"], timeout=5
        ).strip()
        if production_pid != str(self.request["production_pid"]).encode():
            raise ValidationError("kernel_rejected")
        worker, broker = self.identity("worker"), self.identity("broker")
        host = (PROC / "1/ns/net").stat().st_ino
        production = (PROC / str(self.request["production_pid"]) / "ns/net").stat().st_ino
        if (
            worker["netns_inode"] in {host, broker["netns_inode"], production}
            or broker["netns_inode"] != host
        ):
            raise ValidationError("kernel_rejected")
        stage_uid = pwd.getpwnam("prism-stage").pw_uid
        for process in PROC.iterdir():
            if process.name.isdigit():
                try:
                    if (
                        stage_uid in uids(process)
                        and (process / "ns/net").stat().st_ino == worker["netns_inode"]
                    ):
                        raise ValidationError("kernel_rejected")
                except FileNotFoundError:
                    continue
        return {
            "worker_uids": worker["uids"],
            "broker_uids": broker["uids"],
            "worker_netns_inode": worker["netns_inode"],
            "broker_netns_inode": broker["netns_inode"],
            "host_netns_inode": host,
            "worker_startticks": worker["startticks"],
            "broker_startticks": broker["startticks"],
        }

    def proof(self):
        proof, sha = read_private(self.work / "output/bootstrap-proof.json", self.accounts["worker"].pw_uid)
        if (
            set(proof)
            != {"schema", "binding", "production_pid", "issued_at", "checked_at", "sources", "checks"}
            or type(proof.get("schema")) is not int
            or proof["schema"] != 1
            or proof.get("binding") != self.binding
            or type(proof.get("production_pid")) is not int
            or proof["production_pid"] != self.request["production_pid"]
            or proof.get("issued_at") != self.issued
            or type(proof.get("checked_at")) is not int
            or not self.issued <= proof["checked_at"] <= time.time() <= self.issued + 120
            or proof.get("sources") != SOURCE_PINS
            or not isinstance(proof.get("checks"), dict)
            or set(proof["checks"]) != BOOT_CHECKS
            or any(value is not True for value in proof["checks"].values())
        ):
            raise ValidationError("bootstrap_rejected")
        return proof, sha

    def install_network_gate(self, pins):
        self.stage("network_attach")
        self.kernel = self.kernel_identity()
        module = load_network_gate(self.network_gate_path)
        group = CGROUP / "system.slice" / self.units["broker"]
        info = group.stat()
        saved = self.invocations["broker"]
        descriptor = module.ControllerDescriptor(
            role="live",
            candidate_prefix=self.request["candidate"][:12],
            nonce=self.nonce,
            invocation_id=saved[0],
            pid=self.pids["broker"],
            startticks=saved[2],
            uid=self.accounts["broker"].pw_uid,
            cgroup_inode=info.st_ino,
            cgroup_device=info.st_dev,
            provider_ipv4=tuple(pins),
        )
        if descriptor.unit != self.units["broker"] or self.kernel_identity() != self.kernel:
            raise ValidationError("network_gate_rejected")
        self.network_gate = module.OwnedNetworkGate(descriptor)
        self.network_programs = self.network_gate.install()
        self.verify_network_gate()
        identity = {
            name: getattr(descriptor, name)
            for name in ("invocation_id", "pid", "startticks", "uid", "cgroup_inode", "cgroup_device")
        }
        identity["unit"] = descriptor.unit
        permit = {
            "schema": 1,
            "binding": self.binding,
            "identity": identity,
            "programs": self.network_programs,
            "helper_sha256": NETWORK_GATE_SHA256,
            "provider_ipv4": pins,
            "allowed_at": int(time.time()),
            "preflight_only": self.preflight_only,
        }
        raw = encoded(permit)
        self.stage("network_permit")
        self.verify_network_gate()
        write(self.work / "network-allow.json", raw, 0o444)
        return hashlib.sha256(raw).hexdigest()

    def verify_network_gate(self):
        try:
            if self.network_gate is None or self.network_gate.verify() != self.network_programs:
                raise ValidationError("network_gate_rejected")
        except Exception:
            raise ValidationError("network_gate_rejected") from None

    def preflight(self, value, pins):
        # Only typed, bounded diagnostics survive a failed gate. Never retain
        # raw IPC, host addresses, provider text or arbitrary exception detail.
        self.report.update(
            broker_negative_errno={},
            broker_filesystem_checks={},
            broker_preflight_facts={},
            broker_memory_sealed=False,
            provider_peer_count=0,
        )
        expected = {
            "schema",
            "kind",
            "binding",
            "facts",
            "negative_errno",
            "positive_peers",
            "filesystem_checks",
        }
        if (
            type(value) is not dict
            or set(value) != expected
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("kind") != "preflight"
            or value.get("binding") != self.binding
            or type(value.get("facts")) is not dict
            or set(value["facts"]) != {"public_dns_pinned", "upstream_tls_verified", "broker_memory_sealed"}
            or any(type(flag) is not bool for flag in value["facts"].values())
            or type(value.get("filesystem_checks")) is not dict
            or set(value["filesystem_checks"]) != BROKER_FILESYSTEM_CHECKS
            or any(type(flag) is not bool for flag in value["filesystem_checks"].values())
            or type(value.get("negative_errno")) is not dict
            or set(value["negative_errno"]) != {*NEGATIVE_TARGETS, "disallowed_public", *EXTRA_NEGATIVES}
            or any(
                type(number) is not int or not 0 <= number <= 4095
                for number in value["negative_errno"].values()
            )
            or type(value.get("positive_peers")) is not list
            or len(value["positive_peers"]) > 16
            or any(type(item) is not str or item not in pins for item in value["positive_peers"])
            or len(value["positive_peers"]) != len(set(value["positive_peers"]))
        ):
            raise ValidationError("broker_preflight_rejected")
        self.report["broker_negative_errno"] = dict(value["negative_errno"])
        self.report["broker_filesystem_checks"] = dict(value["filesystem_checks"])
        self.report["broker_preflight_facts"] = dict(value["facts"])
        self.report["broker_memory_sealed"] = value["facts"]["broker_memory_sealed"]
        self.report["provider_peer_count"] = len(set(value["positive_peers"]))
        if (
            any(flag is not True for flag in value["facts"].values())
            or any(flag is not True for flag in value["filesystem_checks"].values())
            or any(number not in {1, 13} for number in value["negative_errno"].values())
            or not value["positive_peers"]
        ):
            raise ValidationError("broker_preflight_rejected")

    def allow(self):
        if self.preflight_only:
            raise ValidationError("key_rejected")
        self.stage("allow")
        self.candidate_identity()
        self.source_identity()
        self.kernel = self.kernel_identity()
        proof, self.proof_sha = self.proof()
        if self.kernel_identity() != self.kernel or self.proof()[1] != self.proof_sha:
            raise ValidationError("bootstrap_rejected")
        checked = int(time.time())
        self.verify_network_gate()
        value = {
            "schema": 1,
            "binding": self.binding,
            "policy_sha256": SOURCE_PINS["policy"],
            "broker_memory_sealed": self.report["broker_memory_sealed"],
            "bootstrap_proof_sha256": self.proof_sha,
            "allowed_at": checked,
            "sources": dict(SOURCE_PINS),
            "kernel": self.kernel,
            "witness": {
                "binding": self.binding,
                "checked_at": checked,
                "checks": dict.fromkeys(WITNESSES, True),
            },
        }
        write(self.work / "root-allow.json", encoded(value), 0o444)
        self.report["bootstrap_proof_sha256"] = self.proof_sha

    def release_key(self, channel):
        if self.preflight_only:
            raise ValidationError("key_rejected")
        self.stage("key_release")
        if self.key_attempted:
            raise ValidationError("key_rejected")
        peer(channel, self.pids["broker"], self.accounts["broker"].pw_uid)
        if self.kernel_identity() != self.kernel or self.proof()[1] != self.proof_sha:
            raise ValidationError("kernel_rejected")
        self.candidate_identity()
        self.source_identity()
        self.verify_network_gate()
        self.key_attempted = True
        self.report["key_acquisition_attempted"] = True
        key = self.key_provider()
        if not isinstance(key, str) or not 16 <= len(key) <= 4096 or not re.fullmatch("[!-~]+", key):
            raise ValidationError("key_rejected")
        self.stage("key_send")
        peer(channel, self.pids["broker"], self.accounts["broker"].pw_uid)
        if self.kernel_identity() != self.kernel or self.proof()[1] != self.proof_sha:
            raise ValidationError("kernel_rejected")
        self.candidate_identity()
        self.source_identity()
        self.verify_network_gate()
        send(channel, {"schema": 1, "kind": "key", "key": key})

    def result(self):
        self.verify_network_gate()
        value, sha = read_private(self.work / "output/live-result.json", self.accounts["worker"].pw_uid)
        fields = {
            "schema",
            "probe",
            "binding",
            "checked_at",
            "passed",
            "live_model_verified",
            "native_search_verified",
            "native_fetch_verified",
            "market_provider_verified",
            "production_verified",
            "research_quality_verified",
            "checks",
            "error_class",
            "stage",
            "blocked_provider_header_names",
            "probe_mutations",
        }
        if (
            set(value) != fields
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("probe") != "bounded_live_integration"
            or value.get("binding") != self.binding
            or type(value.get("checked_at")) is not int
            or not self.issued <= value["checked_at"] <= time.time() <= self.issued + 185
            or any(
                type(value.get(key)) is not bool
                for key in (
                    "passed",
                    "live_model_verified",
                    "native_search_verified",
                    "native_fetch_verified",
                    "market_provider_verified",
                    "production_verified",
                    "research_quality_verified",
                )
            )
            or any(
                value[key] is not False
                for key in (
                    "native_fetch_verified",
                    "market_provider_verified",
                    "production_verified",
                    "research_quality_verified",
                )
            )
            or value.get("error_class")
            not in {None, "bounded_live_probe_failed", "live_acceptance_incomplete"}
            or value.get("stage")
            not in {
                "candidate_import",
                "bridge_start",
                "public_qa",
                "result_validation",
                "acceptance_incomplete",
                "complete",
            }
            or not isinstance(value.get("blocked_provider_header_names"), list)
            or any(
                not isinstance(name, str) or not re.fullmatch("[a-z][a-z0-9-]{0,63}", name)
                for name in value["blocked_provider_header_names"]
            )
        ):
            raise ValidationError("worker_result_rejected")
        self.report["worker_result_sha256"] = sha
        self.report["blocked_provider_header_names"] = value["blocked_provider_header_names"]
        if not value["passed"]:
            raise ValidationError("worker_failed")
        if (
            value["live_model_verified"] is not True
            or value["native_search_verified"] is not True
            or value["error_class"] is not None
            or value["stage"] != "complete"
            or value["blocked_provider_header_names"]
            or value.get("probe_mutations")
            != [
                "broker origin and synthetic token",
                "bounded turns",
                "two read-only MCP tools",
                "synthetic data and HOME",
            ]
        ):
            raise ValidationError("worker_result_rejected")
        checks = value["checks"]
        if (
            not isinstance(checks, dict)
            or set(checks)
            != {
                "phases",
                "owned_mcp_roundtrip_verified",
                "foreign_owner_http_404_verified",
                "real_runner_invocations",
                "private_event_bytes",
            }
            or checks.get("owned_mcp_roundtrip_verified") is not True
            or checks.get("foreign_owner_http_404_verified") is not True
            or type(checks.get("real_runner_invocations")) is not int
            or checks["real_runner_invocations"] != 2
            or type(checks.get("private_event_bytes")) is not int
            or not 0 < checks["private_event_bytes"] <= 1048576
            or not isinstance(checks.get("phases"), list)
            or len(checks["phases"]) != 2
        ):
            raise ValidationError("worker_result_rejected")
        phase_fields = {
            "model_completed",
            "chat_http_verified",
            "chat_sse_sha256",
            "chat_sse_ledger_match",
            "server_clock_verified",
            "model_date_echo_verified",
            "native_search_frames_observed",
            "search_call_count",
            "server_search_frames_observed",
            "official_source_urls",
            "source_ledger_persisted",
            "server_search_unhandled_by_candidate",
            "run_id",
        }
        for index, phase in enumerate(checks["phases"]):
            if (
                not isinstance(phase, dict)
                or set(phase) != phase_fields
                or not isinstance(phase.get("run_id"), str)
                or not re.fullmatch("[0-9a-f]{32}", phase["run_id"])
                or not isinstance(phase.get("chat_sse_sha256"), str)
                or not re.fullmatch("[0-9a-f]{64}", phase["chat_sse_sha256"])
                or any(
                    type(phase.get(name)) is not bool
                    for name in phase_fields
                    - {"chat_sse_sha256", "search_call_count", "official_source_urls", "run_id"}
                )
                or any(
                    phase[name] is not True
                    for name in ("model_completed", "chat_http_verified", "server_clock_verified")
                )
                or type(phase.get("search_call_count")) is not int
                or phase["search_call_count"] not in {0, 1}
                or not isinstance(phase.get("official_source_urls"), list)
                or len(phase["official_source_urls"]) > 16
                or index == 0
                and phase.get("model_date_echo_verified") is not True
                or index == 1
                and (
                    phase.get("search_call_count") != 1
                    or phase.get("native_search_frames_observed") is not True
                    or phase.get("source_ledger_persisted") is not True
                    or phase.get("chat_sse_ledger_match") is not True
                    or phase.get("server_search_unhandled_by_candidate") is not False
                    or not phase["official_source_urls"]
                )
            ):
                raise ValidationError("worker_result_rejected")
            for url in phase["official_source_urls"]:
                from urllib.parse import urlsplit

                if (
                    not isinstance(url, str)
                    or len(url) > 2048
                    or any(ord(character) < 33 for character in url)
                ):
                    raise ValidationError("worker_result_rejected")
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname not in {"www.sse.com.cn", "static.sse.com.cn"}
                    or parsed.username
                    or parsed.password
                    or parsed.port not in {None, 443}
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValidationError("worker_result_rejected")
        self.report.update(
            live_model_verified=True,
            native_search_verified=True,
            owned_mcp_verified=True,
            server_clock_verified=True,
            official_source_count=len(checks["phases"][1]["official_source_urls"]),
        )

    def broker_result(self, summary):
        if (
            set(summary) != {"schema", "kind", "binding", "summary"}
            or type(summary.get("schema")) is not int
            or summary["schema"] != 1
            or summary.get("kind") != "summary"
            or summary.get("binding") != self.binding
        ):
            raise ValidationError("broker_failed")
        data = summary["summary"]
        if (
            not isinstance(data, dict)
            or set(data)
            != {
                "requests",
                "request_bytes",
                "response_bytes",
                "completed",
                "failed",
                "unknown_header_names",
                "native_fetch_verified",
                "research_quality_verified",
            }
            or type(data.get("failed")) is not int
            or data["failed"] != 0
            or data.get("native_fetch_verified") is not False
            or data.get("research_quality_verified") is not False
            or not isinstance(data.get("requests"), dict)
            or set(data["requests"]) != {"messages", "count_tokens", "quote", "kline", "public_fetch"}
            or any(type(number) is not int or number < 0 for number in data["requests"].values())
            or not 2 <= data["requests"]["messages"] <= 6
            or not 0 <= data["requests"]["count_tokens"] <= 2
            or any(data["requests"][name] for name in ("quote", "kline", "public_fetch"))
            or type(data.get("completed")) is not int
            or data["completed"] != sum(data["requests"].values())
            or type(data.get("request_bytes")) is not int
            or not 0 < data["request_bytes"] <= 2097152
            or type(data.get("response_bytes")) is not int
            or not 0 < data["response_bytes"] <= 8388608
            or data.get("unknown_header_names") != []
        ):
            raise ValidationError("broker_failed")
        self.report["broker_summary_sha256"] = hashlib.sha256(encoded(data)).hexdigest()
        self.report["requests"] = data["requests"]

    def completed(self, role):
        deadline = min(self.deadline, time.monotonic() + 10)
        while True:
            self.boundary("live_wait_" + role)
            state = self.show(role)
            if state.get("MainPID") == "0":
                if state.get("ExecMainStatus") != "0" or state.get("Result") != "success":
                    raise ValidationError(role + "_failed")
                return
            if state.get("MainPID") != str(self.pids[role]) or time.monotonic() >= deadline:
                raise ValidationError(role + "_failed")
            time.sleep(0.05)

    def cleanup(self):
        failed = False
        for role in sorted(self.launched):
            unit = self.units[role]
            try:
                saved = self.invocations.get(role)
                state = self.show(role, cleanup=True)
                if (
                    saved is None
                    or state.get("InvocationID") != saved[0]
                    or state.get("ExecMainPID") != str(saved[1])
                    or state.get("MainPID") not in {"0", str(saved[1])}
                ):
                    raise ValidationError("cleanup_failed")
                if state.get("MainPID") != "0" and startticks(PROC / str(saved[1])) != saved[2]:
                    raise ValidationError("cleanup_failed")
                self._run(["/usr/bin/systemctl", "stop", unit], timeout=30)
                raw = self._run(["/usr/bin/systemctl", "show", "--property=MainPID", unit], timeout=5)
                if raw.strip() != b"MainPID=0":
                    raise ValidationError("cleanup_failed")
                group = CGROUP / "system.slice" / unit
                if group.exists():
                    events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
                    if events.get("populated") != "0" or (group / "cgroup.procs").read_text().strip():
                        raise ValidationError("cleanup_failed")
            except Exception:
                failed = True
        self.report["units_stopped"] = not failed
        if self.network_gate is not None:
            try:
                self.network_gate.cleanup()
                self.report["network_gate_cleaned"] = True
            except Exception:
                failed = True
        if failed:
            self.report["passed"] = False
            self.report["network_gate_verified"] = False
            self.report["cleanup_error_class"] = "cleanup_failed"
            if self.report["error_class"] is None:
                self.report["error_class"] = "cleanup_failed"

    def execute(self):
        listener = channel = None
        try:
            if sys.platform != "linux" or os.geteuid() != 0:
                raise ValidationError("root_linux_required")
            root_locked(Path(__file__))
            self.candidate_identity()
            self.accounts_ready()
            pins, resolvers, targets = network_policy(
                lambda: decode(
                    self.run(["/usr/bin/python3", "-I", "-B", "-S", "-c", RESOLVE_CODE], timeout=8)
                )
            )
            self.network_allow = {*pins, *resolvers}
            self.workspace()
            path = self.work / "credential.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            os.chmod(path, 0o660)
            os.chown(path, 0, self.accounts["broker"].pw_gid)
            listener.listen(1)
            listener.settimeout(25)
            self.pids = {}
            self.pids["broker"] = self.start("broker", pins, resolvers)
            self.pids["worker"] = self.start("worker", pins, resolvers)
            self.binding = {
                "candidate": self.request["candidate"],
                "cli_sha256": self.request["cli_sha256"],
                "probe_sha256": SOURCE_PINS["worker"],
                "nonce": self.nonce,
                "worker_pid": self.pids["worker"],
                "broker_pid": self.pids["broker"],
            }
            channel, _ = listener.accept()
            channel.settimeout(25)
            peer(channel, self.pids["broker"], self.accounts["broker"].pw_uid)
            hello = receive(channel)
            if (
                hello
                != {
                    "schema": 1,
                    "kind": "hello",
                    "candidate": self.request["candidate"],
                    "nonce": self.nonce,
                    "pid": self.pids["broker"],
                    "startticks": self.invocations["broker"][2],
                    "bootstrap_sha256": BROKER_BOOTSTRAP_SHA256,
                    "transport_sha256": SOURCE_PINS["transport"],
                }
                or type(hello.get("schema")) is not int
                or type(hello.get("pid")) is not int
                or type(hello.get("startticks")) is not int
            ):
                raise ValidationError("ipc_rejected")
            permit_sha = self.install_network_gate(pins)
            self.stage("network_launch")
            self.verify_network_gate()
            send(
                channel,
                {
                    "schema": 1,
                    "kind": "binding",
                    "binding": self.binding,
                    "negative_targets": targets,
                    "provider_pins": pins,
                    "network_permit_sha256": permit_sha,
                },
            )
            self.stage("preflight")
            self.preflight(receive(channel), pins)
            self.verify_network_gate()
            self.report["network_gate_verified_before_cleanup"] = True
            self.report["network_gate_verified"] = True
            deadline = min(self.deadline, time.monotonic() + 15)
            while not (self.work / "output/bootstrap-proof.json").exists():
                self.boundary("live_wait_bootstrap")
                if time.monotonic() >= deadline:
                    raise ValidationError("bootstrap_rejected")
                time.sleep(0.05)
            if self.preflight_only:
                self.kernel_identity()
                self.proof()
                self.verify_network_gate()
                send(channel, {"schema": 1, "kind": "allowed", "nonce": self.nonce})
                self.verify_network_gate()
                send(channel, {"schema": 1, "kind": "preflight-finish", "nonce": self.nonce})
                self.report["passed"] = True
                self.report["stage"] = "network_preflight_complete"
                return self.report
            self.allow()
            send(channel, {"schema": 1, "kind": "allowed", "nonce": self.nonce})
            if receive(channel) != {"schema": 1, "kind": "key", "binding": self.binding}:
                raise ValidationError("ipc_rejected")
            self.release_key(channel)
            if receive(channel) != {"schema": 1, "kind": "authorized", "binding": self.binding}:
                raise ValidationError("ipc_rejected")
            self.stage("public_qa")
            while True:
                self.boundary("live_wait_qa")
                state = self.show("worker")
                if state.get("MainPID") == "0":
                    if state.get("ExecMainStatus") != "0" or state.get("Result") != "success":
                        raise ValidationError("worker_failed")
                    break
                if state.get("MainPID") != str(self.pids["worker"]) or time.monotonic() >= self.deadline:
                    raise ValidationError("worker_failed")
                time.sleep(0.1)
            self.stage("result_validation")
            self.result()
            self.stage("broker_finish")
            self.verify_network_gate()
            send(channel, {"schema": 1, "kind": "finish", "nonce": self.nonce})
            self.broker_result(receive(channel))
            self.verify_network_gate()
            send(channel, {"schema": 1, "kind": "result-ack", "nonce": self.nonce})
            self.completed("broker")
            self.stage("verification")
            self.report["passed"] = True
            self.report["stage"] = "complete"
        except Exception as error:
            self.report["passed"] = False
            detail = error.args[0] if isinstance(error, ValidationError) and len(error.args) == 1 else None
            self.report["error_class"] = (
                detail if isinstance(detail, str) and detail in SAFE_ERRORS else "supervision_failed"
            )
        finally:
            if channel is not None:
                channel.close()
            if listener is not None:
                listener.close()
            self.cleanup()
            self.report["finished_at"] = int(time.time())
            if self.workspace_created:
                try:
                    write(self.work / "validation-result.json", encoded(self.report), 0o600)
                except Exception:
                    self.report["passed"] = False
                    self.report["error_class"] = "supervision_failed"
            self.record(dict(self.report))
        return self.report


def run_validation(request, *, boundary, run, record, key_provider, network_gate_path=None):
    """One attempt; callbacks are trusted controller functions, never request code."""
    return Validation(
        request,
        boundary=boundary,
        run=run,
        record=record,
        key_provider=key_provider,
        network_gate_path=network_gate_path,
    ).execute()


def run_network_preflight(request, *, boundary, run, record, network_gate_path):
    """Fixed credential-free diagnostic: TLS handshake only; never authorizes models."""
    return Validation(
        request,
        boundary=boundary,
        run=run,
        record=record,
        key_provider=None,
        network_gate_path=network_gate_path,
        preflight_only=True,
    ).execute()

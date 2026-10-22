"""Finite stop experiment for a prepared candidate's actual Python environment.

The reviewed root controller loads this standard-library supervisor only. The
fixed worker imports Uvicorn/uvloop solely as an isolated DynamicUser after an
actual kernel gate. This experiment never imports the application, reads its
configuration, stops production, or proves production tasks are drained.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
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

OPERATIONS = Path("/var/lib/prism-operations")
RELEASES = Path("/opt/prism/releases")
SOURCES = Path("/opt/prism/validation/new-runtime-stop")
PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
SYSTEM_PYTHON_SHA256 = "e1efa562c2cc2e35521a5c9c9b9939921001ff8ca9708a13ef15ace68cc2ccd7"
APPLICATION_SOURCES = {
    "candidate": {
        "analysis_engine": "40196d29c6a8d199bf29163aa7cc008e37b9055a0762526202f1360de0e3bcf7",
        "database": "83b7eab6a62f9586e9695bb580e3b1008832e8038acc3c735f9cdfda3b70538f",
        "config": "ff48f688f4136e882f1465ba3d6edbae4b4b2a427188ec7df20c427b31682f3e",
    },
    "recovery": {
        "analysis_engine": "83786c7b143406f93b663c6993a1a7c8e697fee327a457fa23b6342637aaf90b",
        "database": "83b7eab6a62f9586e9695bb580e3b1008832e8038acc3c735f9cdfda3b70538f",
        "config": "b5cc72b713ea6d9184191f8abfb7cf74898fa28e1b68e75cae082114e8c6a33c",
    },
}
POLICY = {
    "KillSignal": "2",
    "KillMode": "process",
    "SendSIGKILL": "no",
    "SendSIGHUP": "no",
    "TimeoutStopUSec": "infinity",
    "Restart": "no",
}
EXTRA_POLICY = {
    "ExecStop": "",
    "ExecStopPost": "",
    "WatchdogUSec": "0",
    "RuntimeMaxUSec": "infinity",
    "NRestarts": "0",
    "RemainAfterExit": "yes",
}
PROPERTIES = (
    "LoadState,ActiveState,SubState,MainPID,ExecMainPID,InvocationID,ControlGroup,"
    "ExecMainCode,ExecMainStatus,ExecMainExitTimestampMonotonic,Result,Transient,DynamicUser,"
    + ",".join((*POLICY, *(key for key in EXTRA_POLICY if key not in {"ExecStop", "ExecStopPost"}), "Job"))
)
EMPTY_EXEC_ARRAY = {"type": "a(sasbttttuii)", "data": []}
OBSERVER_PROPERTIES = (
    "LoadState,ActiveState,SubState,MainPID,ExecMainPID,InvocationID,ControlGroup,"
    "ExecMainCode,ExecMainStatus,Result,Transient,DynamicUser,RemainAfterExit,"
    "Wants,PartOf,BindsTo,PropagatesStopTo,Job"
)
REQUEST_KEYS = {
    "commit",
    "release_id",
    "attempt",
    "role",
    "candidate",
    "archive_sha256",
    "environment_sha256",
}
INVENTORY = [
    "/usr/bin/systemctl",
    "list-units",
    "--all",
    "--plain",
    "--no-legend",
    "prism-new-runtime-stop-*.service",
]
ERRORS = {
    "request_rejected",
    "root_linux_required",
    "source_rejected",
    "candidate_rejected",
    "environment_changed",
    "workspace_exists",
    "unit_rejected",
    "kernel_rejected",
    "bootstrap_rejected",
    "canary_rejected",
    "marker_rejected",
    "census_rejected",
    "stop_policy_rejected",
    "job_property_rejected",
    "stop_rejected",
    "stop_already_consumed",
    "result_rejected",
    "cleanup_unconfirmed",
    "record_rejected",
    "operator_stop",
    "supervision_failed",
}


class StopError(RuntimeError):
    """Fixed safe category only; never persist an arbitrary exception."""


def reject(code):
    raise StopError("new_runtime_stop_" + code)


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


WORKER_SOURCE = r"""
import asyncio, errno, hashlib, importlib, importlib.metadata, inspect, json, os, pwd, socket, stat, sys, threading, time, types
from pathlib import Path

def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()
def digest(raw):return hashlib.sha256(raw).hexdigest()
def emit(root,name,value):
    path=root/name;pending=root/(name+'.pending')
    if path.exists() or path.is_symlink():raise RuntimeError('duplicate_marker')
    fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
    os.rename(pending,path)
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
    return digest(encoded(value))
def read_gate(root,name,binding):
    path=root/name
    try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:return False
    with os.fdopen(fd,'rb') as stream:
        a=os.fstat(stream.fileno());raw=stream.read(16385);b=os.fstat(stream.fileno())
    if not stat.S_ISREG(a.st_mode) or a.st_uid!=0 or a.st_nlink!=1 or stat.S_IMODE(a.st_mode)!=0o444 or len(raw)>16384 or (a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns,a.st_ctime_ns)!=(b.st_dev,b.st_ino,b.st_size,b.st_mtime_ns,b.st_ctime_ns):raise RuntimeError('gate_rejected')
    return json.loads(raw)==binding

def census(state,phase):
    future=state.get('future');thread=state.get('thread')
    aid=state.get('analysis_id');entry=state['engine']._store.get(aid,{}) if aid else {}
    return {'schema':1,'binding_sha256':state['binding'],'nonce':state['nonce'],'pid':os.getpid(),
        'at_us':time.monotonic_ns()//1000,'phase':phase,'future_id':id(future) if future else 0,
        'future_done':bool(future and future.done()),'future_cancelled':bool(future and future.cancelled()),
        'analysis_id':aid,'analysis_status':entry.get('status','unknown'),
        'executor_thread_id':thread.ident if thread else 0,'executor_thread_alive':bool(thread and thread.is_alive()),
        'executor_finished':state['executor_finished'],'persistence_saved':state['persistence_saved'],
        'server_unwound':state['server_unwound'],'uvloop_running':state['uvloop_running'],
        'asyncio_pending_task_count':len(asyncio.all_tasks(state['loop'])) if state.get('loop') else 0,
        'live_thread_ids':sorted(t.ident for t in threading.enumerate() if t.ident and t.is_alive())}

def graph_fixture(state):
    class Graph:
        def __init__(self,*,debug,config,selected_analysts=None):self.graph=self
        def prepare_graph_run(self,ticker,date):return {'messages':[]},{},None
        def stream(self,initial,**kwargs):
            state['thread']=threading.current_thread()
            end=time.monotonic()+30
            while not state.get('analysis_id') or state.get('future') is None:
                if time.monotonic()>=end:raise RuntimeError('observation_timeout')
                time.sleep(.001)
            state['before_sha']=emit(state['root'],'census-before.json',census(state,'before'))
            during=False
            while time.monotonic()<end:
                if (state['root']/'abort.json').exists():raise RuntimeError('aborted')
                if state['server_unwound'] and not during:
                    state['during_sha']=emit(state['root'],'census-during.json',census(state,'during'));during=True
                if read_gate(state['root'],'release-work.json',{'schema':1,'binding_sha256':state['binding'],'nonce':state['nonce'],'phase':'release_work'}):break
                time.sleep(.01)
            else:raise RuntimeError('work_gate_expired')
            yield {'final_trade_decision':'PRISM_SYNTHETIC_STOP_RESULT_197','signal':'hold'}
        def finalize_graph_run(self,ticker,date,final):return 'hold'
        def close_graph_run(self):state['executor_finished']=True
    return Graph

def denied(path,write=False):
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW if write else os.O_RDONLY|os.O_NOFOLLOW,0o600)
    except OSError as error:return error.errno
    os.close(fd)
    if write:os.unlink(path)
    return 0

def main():
    config=json.loads(sys.argv[1]);root=Path.cwd();candidate=Path('/opt/prism/releases')/config['candidate']
    if sys.platform!='linux' or sys.version_info[:3]!=(3,12,3) or not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode or Path(sys.executable)!=candidate/'.venv/bin/python':raise RuntimeError('runtime_rejected')
    forbidden={0}
    for name in ('ubuntu','prism-stage','prism-integration','prism-integration-broker'):
        try:forbidden.add(pwd.getpwnam(name).pw_uid)
        except KeyError:pass
    info=root.lstat()
    if os.geteuid() in forbidden or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700 or root!=Path(config['runtime']):raise RuntimeError('identity_rejected')
    os.environ.clear();os.environ.update({'PATH':'/usr/bin:/bin','HOME':str(root),'TMPDIR':str(root),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','STOCK_DATA_DIR':str(root/'data'),'STOCK_LOG_FILE':str(root/'app.log'),'ASTOCK_PROJECT':str(root/'engine-fixture'),'ASTOCK_CACHE_TTL_SECONDS':'3600','CLAUDE_BIN':str(root/'disabled-claude')})
    binding=config['binding_sha256'];nonce=config['nonce']
    emit(root,'bootstrap-ready.json',{'schema':1,'binding_sha256':binding,'nonce':nonce,'pid':os.getpid(),'candidate_imported':False,'uvicorn_imported':False,'uvloop_imported':False})
    gate={'schema':1,'binding_sha256':binding,'nonce':nonce,'phase':'allow_canaries'}
    end=time.monotonic()+12
    while not read_gate(root,'allow-canaries.json',gate):
        if time.monotonic()>=end:raise RuntimeError('bootstrap_timeout')
        time.sleep(.01)
    try:channel=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    except OSError as error:inet_errno=error.errno
    else:channel.close();inet_errno=0
    checks={'production_read_errno':denied('/home/ubuntu/stock-analyzer/.env'),
        'other_process_read_errno':denied('/proc/1/environ'),
        'production_write_errno':denied('/home/ubuntu/stock-analyzer/.new-runtime-stop-'+nonce,True),
        'candidate_write_errno':denied(candidate/('.new-runtime-stop-'+nonce),True),
        'private_run_write_errno':denied(Path('/run')/('new-runtime-stop-'+nonce),True),
        'inet_socket_errno':inet_errno,
        'systemd_socket_errno':None,'dbus_socket_errno':None}
    for key,path in (('systemd_socket_errno','/run/systemd/private'),('dbus_socket_errno','/run/dbus/system_bus_socket')):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as channel:
            checks[key]=channel.connect_ex(path)
    emit(root,'canaries.json',{'schema':1,'binding_sha256':binding,'nonce':nonce,'pid':os.getpid(),'checks':checks})
    denied_codes={errno.EACCES,errno.EPERM,errno.EROFS,errno.ENOENT}
    if any(code not in denied_codes for name,code in checks.items() if name!='inet_socket_errno') or inet_errno not in {errno.EACCES,errno.EPERM,errno.EAFNOSUPPORT}:raise RuntimeError('canary_failed')
    gate={'schema':1,'binding_sha256':binding,'nonce':nonce,'phase':'allow_import'}
    end=time.monotonic()+12
    while not read_gate(root,'allow-import.json',gate):
        if time.monotonic()>=end:raise RuntimeError('import_timeout')
        time.sleep(.01)
    if any(name in sys.modules for name in ('sitecustomize','usercustomize','patch_ssl')):raise RuntimeError('startup_hook_rejected')
    sites=candidate/'.venv/lib/python3.12/site-packages';sys.path.insert(0,str(sites))
    import uvicorn,uvloop
    binary=importlib.import_module('uvloop.loop')
    if importlib.metadata.version('uvicorn')!='0.34.3' or importlib.metadata.version('uvloop')!='0.23.0' or any(not Path(m.__file__).resolve().is_relative_to(sites) for m in (uvicorn,uvloop,binary)):raise RuntimeError('environment_rejected')
    sys.path.insert(0,str(candidate))
    from server import analysis_engine as engine, database, config as app_config
    if Path(app_config.DATA_DIR)!=root/'data' or Path(database.DB_PATH)!=root/'data/chat.db' or Path(app_config.CLAUDE_BIN)!=root/'disabled-claude' or (root/'disabled-claude').exists():raise RuntimeError('database_path_rejected')
    modules={'analysis_engine':engine,'database':database,'config':app_config}
    source_sha={name:digest(Path(module.__file__).read_bytes()) for name,module in modules.items()}
    if source_sha!=config['application_sources']:raise RuntimeError('application_source_rejected')
    if any(Path(module.__file__).resolve()!=candidate/'server'/('config.py' if name=='config' else name+'.py') for name,module in modules.items()):raise RuntimeError('source_path_rejected')
    functions=(engine.start_analysis,engine._persist_analysis,engine._save_cached_analysis,engine.get_analysis_record,engine._load_cached_analysis)
    if any(function.__module__!='server.analysis_engine' or Path(inspect.getsourcefile(function)).resolve()!=candidate/'server/analysis_engine.py' for function in functions):raise RuntimeError('function_source_rejected')
    database.init_db()
    state={'root':root,'binding':binding,'nonce':nonce,'future':None,'analysis_id':None,'thread':None,
        'engine':engine,'executor_finished':False,'persistence_saved':False,'server_unwound':False,'uvloop_running':False,
        'before_sha':None,'during_sha':None,'result_sha':None,'owner':'newstop_'+nonce[:16]}
    # Only external provider calls are fixed synthetic boundaries. Actual
    # start_analysis/_run, event delivery, cache and database functions stay intact.
    Graph=graph_fixture(state)
    for name in ('tradingagents','tradingagents.graph','tradingagents.graph.trading_graph','tradingagents.default_config'):
        module=types.ModuleType(name);module.__path__=[];sys.modules[name]=module
    sys.modules['tradingagents.graph.trading_graph'].TradingAgentsGraph=Graph
    sys.modules['tradingagents.default_config'].DEFAULT_CONFIG={}
    if config['role']=='candidate':
        from server.services import auxiliary_model
        auxiliary_model.run_text=lambda *args,**kwargs:''
    async def app(scope,receive,send):
        if scope['type']!='http' or scope['path']!='/start' or scope['method']!='POST' or state['analysis_id'] is not None:raise RuntimeError('fixed_request_rejected')
        loop=asyncio.get_running_loop();state['loop']=loop;state['uvloop_running']=type(loop).__module__.startswith('uvloop')
        def observe(frame,event,arg):
            if frame.f_code is engine.start_analysis.__code__ and event=='return':
                future=frame.f_locals.get('future')
                if isinstance(future,asyncio.Future):state['future']=future
        old=sys.getprofile();sys.setprofile(observe)
        try:state['analysis_id']=await engine.start_analysis('600519',name='public synthetic stop',date='2026-10-22',username=state['owner'],mode='fast',force_refresh=True)
        finally:sys.setprofile(old)
        if state['future'] is None:raise RuntimeError('actual_executor_not_observed')
        while state['before_sha'] is None:await asyncio.sleep(.001)
        body=encoded({'schema':1,'binding_sha256':binding,'nonce':nonce,'started':True})
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':body})
    class Server(uvicorn.Server):
        async def serve(self,*args,**kwargs):
            try:return await super().serve(*args,**kwargs)
            finally:state['server_unwound']=True
    server=Server(uvicorn.Config(app,uds=str(root/'server.sock'),loop='uvloop',http='h11',ws='none',lifespan='off',log_level='warning',timeout_graceful_shutdown=None))
    def deadline():
        end=time.monotonic()+45
        while time.monotonic()<end:
            if (root/'abort.json').exists():break
            time.sleep(.05)
        server.should_exit=True
    threading.Thread(target=deadline,name='prism-new-stop-deadline',daemon=True).start()
    try:server.run()
    except KeyboardInterrupt:pass
    aid=state['analysis_id'];future=state['future'];entry=engine._store.get(aid)
    if not entry or entry.get('status')!='done' or not future.done() or not state['executor_finished'] or state['thread'].is_alive() or not state['during_sha'] or not state['uvloop_running']:raise RuntimeError('executor_not_completed')
    engine._store.pop(aid)
    record=engine.get_analysis_record(aid,state['owner'])
    foreign=engine.get_analysis_record(aid,'foreign_'+nonce[:16])
    cached=engine._load_cached_analysis(state['owner'],'600519','2026-10-22','fast')
    if not record or record['status']!='done' or record['report']['final_decision']!='PRISM_SYNTHETIC_STOP_RESULT_197' or record['signal']!='hold' or foreign is not None or not cached or cached['report']['final_decision']!='PRISM_SYNTHETIC_STOP_RESULT_197':raise RuntimeError('actual_database_result_missing')
    with database.get_db() as db:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('database_integrity_rejected')
        checkpoint=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint[0]!=0:raise RuntimeError('database_checkpoint_rejected')
    database.reset_connection()
    fd=os.open(database.DB_PATH,os.O_RDONLY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
    fd=os.open(database.DB_PATH.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
    result={'schema':1,'kind':'analysis_executor_database','binding_sha256':binding,'nonce':nonce,'analysis_id':aid,
        'owner':state['owner'],'status':'done','signal':'hold','report_marker':'PRISM_SYNTHETIC_STOP_RESULT_197',
        'foreign_owner_denied':True,'cache_readback':True,'database_sha256':digest(database.DB_PATH.read_bytes()),'source_modules':source_sha}
    state['result_sha']=emit(root,'result.json',result);state['persistence_saved']=True
    # Preserve the actual terminal status while observing the database path.
    engine._store[aid]=entry
    after=census(state,'after');after_sha=emit(root,'census-after.json',after)
    emit(root,'finished.json',{'schema':1,'binding_sha256':binding,'nonce':nonce,'pid':os.getpid(),
        'python_version':'3.12.3','uvicorn_version':'0.34.3','uvloop_version':'0.23.0',
        'uvicorn_sha256':digest(Path(uvicorn.__file__).read_bytes()),'uvloop_binary_sha256':digest(Path(binary.__file__).read_bytes()),
        'before_sha256':state['before_sha'],'during_sha256':state['during_sha'],'after_sha256':after_sha,'result_sha256':state['result_sha']})
try:
    if __name__=='__main__':main()
except Exception:raise SystemExit(1)
"""
WORKER_SHA256 = digest(WORKER_SOURCE.encode())


def valid_request(request):
    if (
        type(request) is not dict
        or set(request) != REQUEST_KEYS
        or any(type(request[k]) is not str for k in REQUEST_KEYS)
        or not re.fullmatch("[0-9a-f]{40}", request["commit"])
        or not re.fullmatch("[0-9a-f]{40}", request["candidate"])
        or not re.fullmatch("[a-z0-9][a-z0-9-]{7,63}", request["release_id"])
        or not re.fullmatch("[a-z0-9][a-z0-9-]{7,39}", request["attempt"])
        or request["role"] not in {"candidate", "recovery"}
        or any(not re.fullmatch("[0-9a-f]{64}", request[k]) for k in ("archive_sha256", "environment_sha256"))
    ):
        reject("request_rejected")
    return dict(request)


def locked(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            reject("source_rejected")


def read(path, *, uid=0, mode=None, limit=131072):
    if path.is_symlink():
        reject("marker_rejected")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        a = os.fstat(stream.fileno())
        raw = stream.read(limit + 1)
        b = os.fstat(stream.fileno())

        def keys(x):
            return (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)

        if (
            not stat.S_ISREG(a.st_mode)
            or uid is not None
            and a.st_uid != uid
            or a.st_nlink != 1
            or len(raw) > limit
            or mode is not None
            and stat.S_IMODE(a.st_mode) != mode
            or keys(a) != keys(b)
            or keys(a) != keys(path.lstat())
        ):
            reject("marker_rejected")
    return raw


def read_json(path, **kwargs):
    try:
        value = parse_json(read(path, **kwargs))
    except (ValueError, UnicodeError):
        reject("marker_rejected")
    if type(value) is not dict:
        reject("marker_rejected")
    return value


def parse_json(raw):
    def pairs(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = item
        return value

    def constant(_):
        raise ValueError("nonfinite_number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def write(path, raw, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_hash(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        a = os.fstat(stream.fileno())
        value = hashlib.file_digest(stream, "sha256").hexdigest()
        b = os.fstat(stream.fileno())
    if (
        not stat.S_ISREG(a.st_mode)
        or a.st_uid != 0
        or a.st_mode & 0o022
        or (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns)
        != (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns)
    ):
        reject("source_rejected")
    return value


def tree_hash(root, *, source=False):
    """Same environment inventory encoding as the formal release controller."""
    locked(root)
    items = []
    for directory, folders, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        if source and parent == root:
            folders[:] = [name for name in folders if name != ".venv"]
        for item in [*(parent / name for name in folders), *(parent / name for name in files)]:
            relative = item.relative_to(root).as_posix()
            info = item.lstat()
            if item.name in {".env", "secret_key"}:
                reject("source_rejected")
            if stat.S_ISLNK(info.st_mode):
                items.append([relative, "link", os.readlink(item)])
            elif stat.S_ISDIR(info.st_mode):
                if info.st_uid != 0 or info.st_mode & 0o022:
                    reject("source_rejected")
                items.append([relative, "directory", stat.S_IMODE(info.st_mode)])
            elif stat.S_ISREG(info.st_mode):
                items.append([relative, info.st_size, stat.S_IMODE(info.st_mode), file_hash(item)])
            else:
                reject("source_rejected")
            if len(items) > 100000:
                reject("source_rejected")
    items.sort(key=lambda row: row[0])
    return digest(encoded(items))


def descriptor(request, *, nonce=None):
    request = valid_request(request)
    nonce = nonce or secrets.token_hex(16)
    if not re.fullmatch("[0-9a-f]{32}", nonce):
        reject("request_rejected")
    name = "prism-new-runtime-stop-" + nonce + "-" + request["role"]
    runtime = Path("/run") / name
    storage = name + "-storage"
    return {
        "request": request,
        "nonce": nonce,
        "binding_sha256": digest(encoded(request)),
        "unit": name + ".service",
        "observer": name + "-ref.service",
        "runtime": str(runtime),
        "storage": storage,
        "alias": "/run/" + storage,
        "host_runtime": "/run/private/" + storage,
        "control_group": "/system.slice/" + name + ".service",
    }


def command(plan, worker):
    candidate = RELEASES / plan["request"]["candidate"]
    properties = {
        "Type": "exec",
        "Slice": "system.slice",
        "DynamicUser": "yes",
        "RuntimeDirectory": plan["storage"],
        "RuntimeDirectoryMode": "0700",
        "RuntimeDirectoryPreserve": "yes",
        "WorkingDirectory": plan["runtime"],
        "PrivateNetwork": "yes",
        "ProtectHome": "yes",
        "ProtectSystem": "strict",
        "ProtectProc": "invisible",
        "ProcSubset": "pid",
        "NoNewPrivileges": "yes",
        "PrivateDevices": "yes",
        "ProtectControlGroups": "yes",
        "ProtectKernelTunables": "yes",
        "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes",
        "RestrictSUIDSGID": "yes",
        "RestrictNamespaces": "yes",
        "RestrictAddressFamilies": "AF_UNIX",
        "IPAddressDeny": "any",
        "CapabilityBoundingSet": "",
        "AmbientCapabilities": "",
        "TemporaryFileSystem": "/run:ro /run/systemd:ro,mode=000 /run/dbus:ro,mode=000",
        "BindPaths": plan["host_runtime"] + ":" + plan["runtime"],
        "ReadWritePaths": plan["runtime"],
        "ReadOnlyPaths": str(candidate) + " " + str(worker.parent),
        "InaccessiblePaths": "/home /root /var/lib",
        "UMask": "0077",
        "MemoryMax": "512M",
        "CPUQuota": "100%",
        "TasksMax": "32",
        "LimitCORE": "0",
        "KillSignal": "SIGINT",
        "KillMode": "process",
        "SendSIGKILL": "no",
        "SendSIGHUP": "no",
        "TimeoutStopSec": "infinity",
        "Restart": "no",
        "RuntimeMaxSec": "infinity",
        "RemainAfterExit": "yes",
        "StandardOutput": "null",
        "StandardError": "null",
    }
    config = {
        **plan["request"],
        "nonce": plan["nonce"],
        "binding_sha256": plan["binding_sha256"],
        "runtime": plan["runtime"],
        "application_sources": APPLICATION_SOURCES[plan["request"]["role"]],
    }
    return [
        "/usr/bin/systemd-run",
        "--quiet",
        "--unit=" + plan["unit"],
        *("--property=" + k + "=" + v for k, v in sorted(properties.items())),
        "--",
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        str(candidate / ".venv/bin/python"),
        "-I",
        "-B",
        "-S",
        str(worker),
        encoded(config).decode(),
    ]


def validate_job(value, printed=None):
    if (
        type(value) is not dict
        or set(value) != {"type", "data"}
        or value["type"] != "(uo)"
        or type(value["data"]) is not list
        or len(value["data"]) != 2
        or type(value["data"][0]) is not int
        or not 0 <= value["data"][0] < 2**32
        or type(value["data"][1]) is not str
    ):
        reject("job_property_rejected")
    number, path = value["data"]
    if path != ("/" if number == 0 else "/org/freedesktop/systemd1/job/" + str(number)):
        reject("job_property_rejected")
    if printed is not None and (type(printed) is not str or printed not in {"", str(number)}):
        reject("job_property_rejected")
    return value


def job_command(unit):
    if type(unit) is not str or not re.fullmatch(
        "prism-new-runtime-stop-[0-9a-f]{32}-(?:candidate|recovery)(?:-ref)?\\.service", unit
    ):
        reject("unit_rejected")
    component = "".join(
        character if character.isascii() and character.isalnum() else "_" + format(ord(character), "02x")
        for character in unit
    )
    return [
        "/usr/bin/busctl",
        "--system",
        "--json=short",
        "get-property",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1/unit/" + component,
        "org.freedesktop.systemd1.Unit",
        "Job",
    ]


def typed_job(run, unit, printed):
    raw = run(job_command(unit), timeout=10)
    if type(raw) is not bytes or len(raw) > 4096:
        reject("job_property_rejected")

    def pairs(rows):
        value = dict(rows)
        if len(value) != len(rows):
            raise ValueError("duplicate_key")
        return value

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        reject("job_property_rejected")
    return validate_job(value, printed)


def exec_array_commands(unit):
    if type(unit) is not str or not re.fullmatch(
        r"prism-new-runtime-stop-[0-9a-f]{32}-(?:candidate|recovery)\.service", unit
    ):
        reject("unit_rejected")
    component = "".join(
        character if character.isascii() and character.isalnum() else "_" + format(ord(character), "02x")
        for character in unit
    )
    return [
        [
            "/usr/bin/busctl",
            "--system",
            "--json=short",
            "get-property",
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1/unit/" + component,
            "org.freedesktop.systemd1.Service",
            key,
        ]
        for key in ("ExecStop", "ExecStopPost")
    ]


def empty_exec_arrays(run, unit):
    observed = {}
    for command in exec_array_commands(unit):
        raw = run(command, timeout=10)
        if type(raw) is not bytes or len(raw) > 4096:
            reject("stop_policy_rejected")
        try:
            value = parse_json(raw)
        except (ValueError, UnicodeError):
            reject("stop_policy_rejected")
        if encoded(value) != encoded(EMPTY_EXEC_ARRAY):
            reject("stop_policy_rejected")
        observed[command[-1]] = value
    return observed


def observer_command(plan):
    properties = {
        "Type": "oneshot",
        "Slice": "system.slice",
        "DynamicUser": "yes",
        "PrivateNetwork": "yes",
        "ProtectHome": "yes",
        "ProtectSystem": "strict",
        "NoNewPrivileges": "yes",
        "WorkingDirectory": "/",
        "RestrictAddressFamilies": "AF_UNIX",
        "IPAddressDeny": "any",
        "CapabilityBoundingSet": "",
        "AmbientCapabilities": "",
        "TemporaryFileSystem": "/run:ro /run/systemd:ro,mode=000 /run/dbus:ro,mode=000",
        "InaccessiblePaths": "/home /root /var/lib",
        "RemainAfterExit": "yes",
        "Wants": plan["unit"],
        "StandardOutput": "null",
        "StandardError": "null",
    }
    return [
        "/usr/bin/systemd-run",
        "--quiet",
        "--unit=" + plan["observer"],
        *("--property=" + k + "=" + v for k, v in sorted(properties.items())),
        "--",
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "/usr/bin/true",
    ]


def show(run, unit, *, observer=False, evidence=None, jobs=None):
    names = OBSERVER_PROPERTIES if observer else PROPERTIES
    raw = run(["/usr/bin/systemctl", "show", "--all", "--property=" + names, unit], timeout=10)
    if type(raw) is not bytes or len(raw) > 32768:
        reject("unit_rejected")
    try:
        rows = [line.split("=", 1) for line in raw.decode("ascii").splitlines()]
    except (ValueError, UnicodeError):
        reject("unit_rejected")
    if any(len(row) != 2 for row in rows):
        reject("unit_rejected")
    result = dict(rows)
    if len(rows) != len(result) or set(result) != set(names.split(",")):
        reject("unit_rejected")
    if result["LoadState"] == "loaded":
        job = typed_job(run, unit, result["Job"])
        if jobs is not None:
            jobs[unit] = job
        result["Job"] = str(job["data"][0])
    if not observer and result["LoadState"] == "loaded":
        observed = empty_exec_arrays(run, unit)
        if evidence is not None:
            evidence.clear()
            evidence.update(observed)
        result.update({key: "" for key in observed})
    return result


def empty_group(path):
    if path.is_symlink():
        reject("kernel_rejected")
    if not path.exists():
        return True
    for directory, folders, _ in os.walk(path, followlinks=False):
        root = Path(directory)
        if any((root / name).is_symlink() for name in folders):
            reject("kernel_rejected")
        raw = read(root / "cgroup.procs", limit=8192)
        if raw.strip():
            return False
    return True


def process_stat(pid):
    raw = read(PROC / str(pid) / "stat", uid=None, limit=8192)
    try:
        ticks = int(raw.rsplit(b")", 1)[1].split()[19])
        if int(raw.split(b" ", 1)[0]) != pid or ticks <= 0:
            raise ValueError
    except (ValueError, IndexError):
        reject("kernel_rejected")
    return ticks


def host_runtime(plan):
    root = Path(plan["host_runtime"])
    private = root.parent
    base = private.parent
    alias = Path(plan["alias"])
    for path, mode in ((base, 0o755), (private, 0o700)):
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != mode
        ):
            reject("kernel_rejected")
    link = alias.lstat()
    info = root.lstat()
    if (
        not stat.S_ISLNK(link.st_mode)
        or link.st_uid != 0
        or link.st_gid != 0
        or link.st_nlink != 1
        or os.readlink(alias) != "private/" + root.name
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid <= 0
        or info.st_gid != info.st_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        reject("kernel_rejected")
    actual = alias.stat()
    if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
        reject("kernel_rejected")
    return root


def kernel(plan, state, worker):
    try:
        pid = int(state["MainPID"])
    except (ValueError, KeyError):
        reject("kernel_rejected")
    if (
        pid <= 1
        or state["ExecMainPID"] != str(pid)
        or state["ControlGroup"] != plan["control_group"]
        or state["LoadState"] != "loaded"
        or state["Transient"] != "yes"
        or state["DynamicUser"] != "yes"
        or not re.fullmatch("[0-9a-f]{32}", state["InvocationID"])
    ):
        reject("kernel_rejected")
    process = PROC / str(pid)
    raw = read(process / "status", uid=None, limit=131072)
    fields = dict(line.split(b":", 1) for line in raw.splitlines() if b":" in line)
    try:
        uids = [int(x) for x in fields[b"Uid"].split()]
        gids = [int(x) for x in fields[b"Gid"].split()]
    except (ValueError, KeyError):
        reject("kernel_rejected")
    forbidden = {0}
    for name in ("ubuntu", "prism-stage", "prism-integration", "prism-integration-broker"):
        with contextlib.suppress(KeyError):
            forbidden.add(pwd.getpwnam(name).pw_uid)
    if (
        len(uids) != 4
        or len(set(uids)) != 1
        or uids[0] in forbidden
        or len(gids) != 4
        or len(set(gids)) != 1
        or fields.get(b"NoNewPrivs", b"").strip() != b"1"
        or any(
            int(fields.get(name, b"-1"), 16) != 0
            for name in (b"CapEff", b"CapPrm", b"CapBnd", b"CapInh", b"CapAmb")
        )
    ):
        reject("kernel_rejected")
    expected = (RELEASES / plan["request"]["candidate"] / ".venv/bin/python").stat()
    actual = (process / "exe").stat()
    if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
        reject("kernel_rejected")
    ticks = process_stat(pid)
    net = (process / "ns/net").stat().st_ino
    host = (PROC / "self/ns/net").stat().st_ino
    mnt = (process / "ns/mnt").stat().st_ino
    hostmnt = (PROC / "self/ns/mnt").stat().st_ino
    group = read(process / "cgroup", uid=None, limit=8192)
    if net == host or mnt == hostmnt or group.strip() != ("0::" + plan["control_group"]).encode():
        reject("kernel_rejected")
    group_root = CGROUP / plan["control_group"].lstrip("/")
    members = read(group_root / "cgroup.procs", limit=8192).split()
    if members != [str(pid).encode()]:
        reject("kernel_rejected")
    mounts = {}
    for row in read(process / "mountinfo", uid=None, limit=1048576).decode().splitlines():
        head, sep, tail = row.partition(" - ")
        parts = head.split()
        rest = tail.split()
        if not sep or len(parts) < 6 or len(rest) < 3:
            reject("kernel_rejected")
        mounts[parts[4]] = {"options": parts[5].split(","), "fstype": rest[0]}
    proof = {}
    for point in ("/run", "/run/systemd", "/run/dbus"):
        item = mounts.get(point, {})
        if "ro" not in item.get("options", []) or item.get("fstype") != "tmpfs":
            reject("kernel_rejected")
        child = (process / "root" / point.lstrip("/")).stat()
        host_info = Path(point).stat()
        if (child.st_dev, child.st_ino) == (host_info.st_dev, host_info.st_ino) or child.st_uid != 0:
            reject("kernel_rejected")
        if point != "/run" and stat.S_IMODE(child.st_mode) != 0:
            reject("kernel_rejected")
        proof[point] = {
            "device": child.st_dev,
            "inode": child.st_ino,
            "mode": stat.S_IMODE(child.st_mode),
            "host_device": host_info.st_dev,
            "host_inode": host_info.st_ino,
            "fstype": "tmpfs",
            "readonly": True,
        }
    readonly = {}
    for point in ("/home", str(RELEASES / plan["request"]["candidate"]), str(worker.parent)):
        candidates = [
            (key, item)
            for key, item in mounts.items()
            if point == key or point.startswith(key.rstrip("/") + "/")
        ]
        if not candidates or "ro" not in max(candidates, key=lambda pair: len(pair[0]))[1]["options"]:
            reject("kernel_rejected")
        mountpoint, item = max(candidates, key=lambda pair: len(pair[0]))
        readonly[point] = {"mountpoint": mountpoint, "options": item["options"], "fstype": item["fstype"]}
    runtime = (process / "root" / plan["runtime"].lstrip("/")).stat()
    outside_runtime = host_runtime(plan).stat()
    if (
        (runtime.st_dev, runtime.st_ino) != (outside_runtime.st_dev, outside_runtime.st_ino)
        or runtime.st_uid != uids[0]
        or stat.S_IMODE(runtime.st_mode) != 0o700
    ):
        reject("kernel_rejected")
    if process_stat(pid) != ticks:
        reject("kernel_rejected")
    return {
        "pid": pid,
        "start_ticks": ticks,
        "invocation_id": state["InvocationID"],
        "uids": uids,
        "gids": gids,
        "network_namespace_inode": net,
        "host_network_namespace_inode": host,
        "mount_namespace_inode": mnt,
        "host_mount_namespace_inode": hostmnt,
        "exe_device": actual.st_dev,
        "exe_inode": actual.st_ino,
        "control_group": plan["control_group"],
        "members": [pid],
        "runtime_device": runtime.st_dev,
        "runtime_inode": runtime.st_ino,
        "mounts": proof,
        "readonly_paths": readonly,
        "no_new_privs": 1,
        "capabilities": {
            name.decode(): int(fields[name], 16)
            for name in (b"CapEff", b"CapPrm", b"CapBnd", b"CapInh", b"CapAmb")
        },
    }


CENSUS_KEYS = {
    "schema",
    "binding_sha256",
    "nonce",
    "pid",
    "at_us",
    "phase",
    "future_id",
    "future_done",
    "future_cancelled",
    "analysis_id",
    "analysis_status",
    "executor_thread_id",
    "executor_thread_alive",
    "executor_finished",
    "persistence_saved",
    "server_unwound",
    "uvloop_running",
    "asyncio_pending_task_count",
    "live_thread_ids",
}


def validate_census(value, plan, pid, phase):
    fixed = {
        "schema": 1,
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "pid": pid,
        "phase": phase,
    }
    if (
        type(value) is not dict
        or set(value) != CENSUS_KEYS
        or any(type(value.get(k)) is not type(v) or value[k] != v for k, v in fixed.items())
        or any(
            type(value[k]) is not int or not 0 < value[k] < 2**64
            for k in ("at_us", "future_id", "executor_thread_id")
        )
        or type(value["asyncio_pending_task_count"]) is not int
        or not 0 <= value["asyncio_pending_task_count"] <= 128
        or any(
            type(value[k]) is not bool
            for k in (
                "future_done",
                "future_cancelled",
                "executor_thread_alive",
                "executor_finished",
                "persistence_saved",
                "server_unwound",
                "uvloop_running",
            )
        )
        or type(value["analysis_id"]) is not str
        or not re.fullmatch("[0-9a-f]{32}", value["analysis_id"])
        or type(value["analysis_status"]) is not str
        or value["analysis_status"] != ("done" if phase == "after" else "running")
        or type(value["live_thread_ids"]) is not list
        or len(value["live_thread_ids"]) > 32
        or any(type(n) is not int or not 0 < n < 2**64 for n in value["live_thread_ids"])
        or value["live_thread_ids"] != sorted(set(value["live_thread_ids"]))
        or not value["uvloop_running"]
    ):
        reject("census_rejected")
    after = phase == "after"
    if (
        value["future_done"] is not after
        and not value["future_cancelled"]
        or value["executor_finished"] is not after
        or value["persistence_saved"] is not after
        or value["executor_thread_alive"] is after
        or value["server_unwound"] is (phase == "before")
        or (value["asyncio_pending_task_count"] == 0) is not after
        or ((value["executor_thread_id"] in value["live_thread_ids"]) is not (not after))
    ):
        reject("census_rejected")
    return value


def validate_result(value, plan, before, sources):
    if (
        type(value) is not dict
        or type(value.get("database_sha256")) is not str
        or not re.fullmatch("[0-9a-f]{64}", value["database_sha256"])
    ):
        reject("result_rejected")
    expected = {
        "schema": 1,
        "kind": "analysis_executor_database",
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "analysis_id": before["analysis_id"],
        "owner": "newstop_" + plan["nonce"][:16],
        "status": "done",
        "signal": "hold",
        "report_marker": "PRISM_SYNTHETIC_STOP_RESULT_197",
        "foreign_owner_denied": True,
        "cache_readback": True,
        "database_sha256": value["database_sha256"],
        "source_modules": {
            name: sources[name + "_sha256"] for name in ("analysis_engine", "database", "config")
        },
    }
    if encoded(value) != encoded(expected):
        reject("result_rejected")
    return value


def validate_canaries(value, plan, pid):
    keys = {
        "production_read_errno",
        "other_process_read_errno",
        "production_write_errno",
        "candidate_write_errno",
        "private_run_write_errno",
        "inet_socket_errno",
        "systemd_socket_errno",
        "dbus_socket_errno",
    }
    if (
        type(value) is not dict
        or set(value) != {"schema", "binding_sha256", "nonce", "pid", "checks"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["binding_sha256"] != plan["binding_sha256"]
        or value["nonce"] != plan["nonce"]
        or type(value["pid"]) is not int
        or value["pid"] != pid
        or type(value["checks"]) is not dict
        or set(value["checks"]) != keys
        or any(type(n) is not int for n in value["checks"].values())
        or value["checks"]["inet_socket_errno"] not in {1, 13, 97}
        or any(n not in {1, 2, 13, 30} for k, n in value["checks"].items() if k != "inet_socket_errno")
    ):
        reject("canary_rejected")
    return value


def group_evidence(plan):
    if not (CGROUP / "cgroup.controllers").is_file():
        reject("kernel_rejected")
    root = CGROUP / plan["control_group"].lstrip("/")
    if root.is_symlink():
        reject("kernel_rejected")
    groups = []
    if root.exists():
        for directory, folders, _ in os.walk(root, followlinks=False):
            path = Path(directory)
            if any((path / name).is_symlink() for name in folders):
                reject("kernel_rejected")
            procs = read(path / "cgroup.procs", limit=8192).split()
            events = dict(line.split() for line in read(path / "cgroup.events", limit=8192).splitlines())
            if procs or events.get(b"populated") != b"0":
                reject("cleanup_unconfirmed")
            groups.append({"relative": path.relative_to(root).as_posix(), "members": [], "populated": 0})
    return {"cgroup_v2": True, "root_exists": root.exists(), "groups": groups}


def source_identity(request):
    release = RELEASES / request["candidate"]
    locked(release)
    revision = read(release / ".deploy-revision", mode=0o644, limit=128).decode().strip()
    if revision != request["candidate"]:
        reject("candidate_rejected")
    environment = tree_hash(release / ".venv")
    if environment != request["environment_sha256"]:
        reject("environment_changed")
    modules = {
        name: file_hash(release / "server" / (name + ".py")) for name in APPLICATION_SOURCES[request["role"]]
    }
    if modules != APPLICATION_SOURCES[request["role"]]:
        reject("source_rejected")
    system = file_hash(Path("/usr/bin/python3.12"))
    if system != SYSTEM_PYTHON_SHA256:
        reject("source_rejected")
    packages = release / ".venv/lib/python3.12/site-packages"
    binaries = list((packages / "uvloop").glob("loop.*.so"))
    if len(binaries) != 1:
        reject("source_rejected")
    return {
        "environment_sha256": environment,
        "candidate_source_sha256": tree_hash(release, source=True),
        "system_python_sha256": system,
        "uvicorn_sha256": file_hash(packages / "uvicorn/__init__.py"),
        "uvloop_binary_sha256": file_hash(binaries[0]),
        **{name + "_sha256": value for name, value in modules.items()},
    }


def install_worker():
    path = SOURCES / WORKER_SHA256
    for parent in (SOURCES.parent, SOURCES):
        if not parent.exists():
            parent.mkdir(mode=0o755)
        locked(parent)
    if not path.exists():
        path.mkdir(mode=0o700)
        write(path / "worker.py", WORKER_SOURCE.encode(), 0o444)
        path.chmod(0o555)
    locked(path)
    if stat.S_IMODE(path.stat().st_mode) != 0o555 or {p.name for p in path.iterdir()} != {"worker.py"}:
        reject("source_rejected")
    if digest(read(path / "worker.py", mode=0o444)) != WORKER_SHA256:
        reject("source_rejected")
    return path / "worker.py"


class Validation:
    def __init__(self, request, *, boundary, run, record):
        self.request = valid_request(request)
        self.plan = descriptor(request)
        self.boundary, self._run = boundary, run
        self.record_root = Path(record)
        if self.record_root != OPERATIONS / request["release_id"]:
            reject("record_rejected")
        self.directory = self.record_root / "new-runtime-stop" / request["attempt"]
        self.worker = None
        self.own = None
        self.observer = None
        self.before = None
        self.during = None
        self.after = None
        self.started = False
        self.stop_consumed = False
        self.observer_started = False
        self.observer_stop_consumed = False
        self.events = 0
        self.tail = "0" * 64
        self.fd = None
        self.last_event = None
        self.census = []
        self.kernel_before = None
        self.sources = None
        self.abort_written = False
        self.http_start_consumed = False
        self.exec_arrays = {}
        self.jobs = {}

    def event(self, event, details):
        if self.fd is None or self.events >= 64:
            reject("record_rejected")
        row = {"sequence": self.events + 1, "previous_sha256": self.tail, "event": event, "details": details}
        raw = encoded(row) + b"\n"
        if len(raw) > 32768:
            reject("record_rejected")
        view = memoryview(raw)
        while view:
            n = os.write(self.fd, view)
            if n <= 0:
                reject("record_rejected")
            view = view[n:]
        os.fsync(self.fd)
        self.events += 1
        self.tail = digest(raw)
        self.last_event = event

    def run(self, argv, *, timeout):
        plan = self.plan
        if type(timeout) not in {int, float} or not 0 < timeout <= 15:
            reject("unit_rejected")
        valid = argv == INVENTORY and not self.started
        if self.started and argv in exec_array_commands(plan["unit"]):
            valid = True
        if (self.started and argv == job_command(plan["unit"])) or (
            self.observer_started and argv == job_command(plan["observer"])
        ):
            valid = True
        for unit, is_observer in ((plan["unit"], False), (plan["observer"], True)):
            if argv == ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend", unit]:
                valid = True
            if argv == [
                "/usr/bin/systemctl",
                "show",
                "--all",
                "--property=" + (OBSERVER_PROPERTIES if is_observer else PROPERTIES),
                unit,
            ]:
                valid = True
        if (
            self.worker
            and argv == command(plan, self.worker)
            and not self.started
            and self.last_event == "start_intent"
        ):
            valid = True
            self.started = True
        if (
            argv == observer_command(plan)
            and not self.observer_started
            and self.last_event == "observer_start_intent"
        ):
            valid = True
            self.observer_started = True
        if argv == ["/usr/bin/systemctl", "stop", "--no-block", plan["unit"]]:
            if self.stop_consumed or self.last_event != "stop_committed":
                reject("stop_already_consumed")
            valid = True
            self.stop_consumed = True
        if argv == ["/usr/bin/systemctl", "stop", "--no-block", plan["observer"]]:
            if self.observer_stop_consumed or self.last_event != "observer_release_intent":
                reject("stop_already_consumed")
            valid = True
            self.observer_stop_consumed = True
        if not valid:
            reject("unit_rejected")
        result = self._run(argv, timeout=timeout)
        if type(result) is not bytes or len(result) > 32768:
            reject("unit_rejected")
        return result

    def poll(self, callback, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = callback()
            if value:
                return value
            time.sleep(0.02)
        reject("supervision_failed")

    def marker(self, name):
        path = host_runtime(self.plan) / name
        if not path.exists():
            return None
        if self.own is None:
            reject("kernel_rejected")
        return read_json(path, uid=self.own["uids"][0], mode=0o600)

    def gate(self, name, phase):
        value = {
            "schema": 1,
            "binding_sha256": self.plan["binding_sha256"],
            "nonce": self.plan["nonce"],
            "phase": phase,
        }
        write(host_runtime(self.plan) / name, encoded(value), 0o444)

    def census_marker(self, phase):
        value = self.poll(lambda: self.marker("census-" + phase + ".json"), seconds=10)
        value = validate_census(value, self.plan, self.own["pid"], phase)
        if self.census and (
            value["future_id"] != self.census[0]["future_id"]
            or value["analysis_id"] != self.census[0]["analysis_id"]
            or value["executor_thread_id"] != self.census[0]["executor_thread_id"]
            or value["at_us"] <= self.census[-1]["at_us"]
        ):
            reject("census_rejected")
        self.census.append(value)
        self.event("census_" + phase, value)
        return value

    def observer_state(self):
        state = show(self.run, self.plan["observer"], observer=True, jobs=self.jobs)
        group = CGROUP / ("system.slice/" + self.plan["observer"])
        if (
            state["LoadState"] != "loaded"
            or state["ActiveState"] != "active"
            or state["SubState"] != "exited"
            or state["MainPID"] != "0"
            or state["ExecMainCode"] != "1"
            or state["ExecMainStatus"] != "0"
            or state["Result"] != "success"
            or state["Wants"].split() != [self.plan["unit"]]
            or any(state[k] for k in ("PartOf", "BindsTo", "PropagatesStopTo"))
            or state["DynamicUser"] != "yes"
            or state["RemainAfterExit"] != "yes"
            or state["Job"] != "0"
            or state["Transient"] != "yes"
            or not re.fullmatch("[0-9a-f]{32}", state["InvocationID"])
            or state["ControlGroup"] not in {"", "/system.slice/" + self.plan["observer"]}
            or not empty_group(group)
        ):
            reject("unit_rejected")
        return {
            "invocation_id": state["InvocationID"],
            "exec_main_pid": int(state["ExecMainPID"]),
            "control_group": "/system.slice/" + self.plan["observer"],
            "empty": True,
            "state": state,
            "job": self.jobs[self.plan["observer"]],
        }

    def own_state(self, *, waiting=False, allow_pending=False):
        state = show(self.run, self.plan["unit"], evidence=self.exec_arrays, jobs=self.jobs)
        if any(state[k] != v for k, v in {**POLICY, **EXTRA_POLICY}.items()):
            reject("stop_policy_rejected")
        if self.own is not None and (
            state["InvocationID"] != self.own["invocation_id"]
            or state["ExecMainPID"] != str(self.own["pid"])
            or state["MainPID"] not in {"0", str(self.own["pid"])}
        ):
            reject("unit_rejected")
        if not waiting and not allow_pending and state["Job"] != "0":
            reject("stop_rejected")
        if waiting and (
            state["ActiveState"] != "deactivating"
            or state["SubState"] != "stop-sigterm"
            or not state["Job"].isdigit()
            or int(state["Job"]) <= 0
            or kernel(self.plan, state, self.worker) != self.own
        ):
            reject("stop_rejected")
        return state

    def request_start(self):
        path = Path(self.plan["host_runtime"]) / "server.sock"
        if not path.exists():
            return False
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.own["uids"][0]:
            reject("result_rejected")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection = http.client.HTTPConnection("localhost", timeout=3)
        connection.auto_open = 0
        try:
            sock.settimeout(3)
            try:
                sock.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                return False
            connection.sock = sock
            if self.http_start_consumed:
                reject("result_rejected")
            self.http_start_consumed = True
            connection.request("POST", "/start", body=b"")
            response = connection.getresponse()
            body = response.read(16385)
            if response.status != 200 or len(body) > 16384:
                reject("result_rejected")
            if json.loads(body) != {
                "schema": 1,
                "binding_sha256": self.plan["binding_sha256"],
                "nonce": self.plan["nonce"],
                "started": True,
            }:
                reject("result_rejected")
            return True
        finally:
            connection.close()
            sock.close()

    def release_observer(self):
        if self.observer is None:
            return
        if self.observer_state() != self.observer or not empty_group(cgroup_root(self.plan)):
            reject("cleanup_unconfirmed")
        self.boundary("new_runtime_stop_release_observer")
        self.event("observer_release_intent", self.observer)
        self.run(["/usr/bin/systemctl", "stop", "--no-block", self.plan["observer"]], timeout=10)

        def gone():
            state = show(self.run, self.plan["observer"], observer=True, jobs=self.jobs)
            if state["LoadState"] == "not-found":
                return state
            if state["InvocationID"] != self.observer["invocation_id"]:
                reject("cleanup_unconfirmed")
            return (
                state
                if state["MainPID"] == "0" and state["ActiveState"] == "inactive" and state["Job"] == "0"
                else None
            )

        state = self.poll(gone)
        group = group_evidence({"control_group": "/system.slice/" + self.plan["observer"]})
        self.event(
            "observer_released",
            {
                "state": state,
                "cgroup": group,
                "job": None if state["LoadState"] == "not-found" else self.jobs[self.plan["observer"]],
            },
        )

    def execute(self):
        if sys.platform != "linux" or os.geteuid() != 0:
            reject("root_linux_required")
        locked(self.record_root)
        locked(Path(__file__))
        if self.directory.exists() or self.directory.is_symlink():
            reject("workspace_exists")
        self.sources = source_identity(self.request)
        self.worker = install_worker()
        self.boundary("new_runtime_stop_workspace")
        parent = self.directory.parent
        if not parent.exists():
            parent.mkdir(mode=0o700)
        locked(parent)
        if stat.S_IMODE(parent.stat().st_mode) != 0o700:
            reject("record_rejected")
        self.directory.mkdir(mode=0o700)
        boot = read(PROC / "sys/kernel/random/boot_id", limit=128).decode().strip()
        identity = {
            "schema": 1,
            "request": self.request,
            "plan": self.plan,
            "sources": self.sources,
            "helper_sha256": file_hash(Path(__file__)),
            "worker_sha256": WORKER_SHA256,
            "boot_id": boot,
        }
        write(self.directory / "identity.json", encoded(identity))
        self.fd = os.open(
            self.directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        passed = False
        error = None
        cleaned = False
        try:
            self.event("sources_verified", identity)
            if self.run(INVENTORY, timeout=10).strip():
                reject("unit_rejected")
            for unit in (self.plan["unit"], self.plan["observer"]):
                if self.run(
                    ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend", unit], timeout=10
                ).strip():
                    reject("unit_rejected")
            if any(
                Path(self.plan[k]).exists() or Path(self.plan[k]).is_symlink()
                for k in ("runtime", "host_runtime", "alias")
            ):
                reject("workspace_exists")
            self.boundary("new_runtime_stop_start")
            self.event("start_intent", {"nonce": self.plan["nonce"], "unit": self.plan["unit"]})
            self.run(command(self.plan, self.worker), timeout=15)

            def ready():
                state = show(self.run, self.plan["unit"])
                return state if state["MainPID"].isdigit() and int(state["MainPID"]) > 1 else None

            state = self.poll(ready)
            self.own = kernel(self.plan, state, self.worker)
            bootstrap = self.poll(lambda: self.marker("bootstrap-ready.json"))
            if bootstrap != {
                "schema": 1,
                "binding_sha256": self.plan["binding_sha256"],
                "nonce": self.plan["nonce"],
                "pid": self.own["pid"],
                "candidate_imported": False,
                "uvicorn_imported": False,
                "uvloop_imported": False,
            }:
                reject("bootstrap_rejected")
            self.boundary("new_runtime_stop_allow_canaries")
            state = self.own_state()
            if kernel(self.plan, state, self.worker) != self.own:
                reject("kernel_rejected")
            self.event("policy_verified", state)
            self.event("exec_arrays_verified", self.exec_arrays)
            self.event("typed_job_verified", self.jobs[self.plan["unit"]])
            self.event(
                "kernel_bootstrap",
                {
                    "identity": self.own,
                    "bootstrap": bootstrap,
                    "bootstrap_sha256": digest(encoded(bootstrap)),
                },
            )
            self.gate("allow-canaries.json", "allow_canaries")
            canaries = validate_canaries(
                self.poll(lambda: self.marker("canaries.json")), self.plan, self.own["pid"]
            )
            self.event("canaries_verified", canaries)
            self.boundary("new_runtime_stop_allow_import")
            if kernel(self.plan, self.own_state(), self.worker) != self.own:
                reject("kernel_rejected")
            self.gate("allow-import.json", "allow_import")
            self.poll(self.request_start, seconds=12)
            before = self.census_marker("before")
            self.boundary("new_runtime_stop_observer")
            self.event("observer_start_intent", {"unit": self.plan["observer"]})
            self.run(observer_command(self.plan), timeout=15)
            self.observer = self.poll(self.observer_state)
            self.event("observer_ready", self.observer)
            self.boundary("new_runtime_stop_prepare")
            if kernel(self.plan, self.own_state(), self.worker) != self.own:
                reject("kernel_rejected")
            if self.marker("result.json") or self.marker("census-during.json"):
                reject("census_rejected")
            stop = {
                "pid": self.own["pid"],
                "start_ticks": self.own["start_ticks"],
                "invocation_id": self.own["invocation_id"],
                "at_us": time.monotonic_ns() // 1000,
            }
            self.event("stop_intent", stop)
            self.boundary("new_runtime_stop_dispatch")
            if kernel(self.plan, self.own_state(), self.worker) != self.own:
                reject("kernel_rejected")
            self.event("stop_committed", stop)
            self.run(["/usr/bin/systemctl", "stop", "--no-block", self.plan["unit"]], timeout=10)
            during = self.census_marker("during")
            state = self.own_state(waiting=True)
            if during["at_us"] <= stop["at_us"] or self.observer_state() != self.observer:
                reject("stop_rejected")
            self.event(
                "stop_waiting",
                {
                    "job": int(state["Job"]),
                    "typed_job": self.jobs[self.plan["unit"]],
                    "pid": self.own["pid"],
                    "invocation_id": self.own["invocation_id"],
                    "active_state": state["ActiveState"],
                    "sub_state": state["SubState"],
                    "identity": kernel(self.plan, state, self.worker),
                },
            )
            self.boundary("new_runtime_stop_release_work")
            release = time.monotonic_ns() // 1000
            self.event("work_release_intent", {"at_us": release, "during_sha256": digest(encoded(during))})
            self.gate("release-work.json", "release_work")
            after = self.census_marker("after")

            def exited():
                state = self.own_state(allow_pending=True)
                return state if state["MainPID"] == "0" and state["Job"] == "0" else None

            state = self.poll(exited, seconds=12)
            finished = self.poll(lambda: self.marker("finished.json"))
            result = self.marker("result.json")
            expected_result = validate_result(result, self.plan, before, self.sources)
            if (
                digest(
                    read(
                        host_runtime(self.plan) / "data/chat.db",
                        uid=self.own["uids"][0],
                        mode=0o600,
                        limit=32 * 1024 * 1024,
                    )
                )
                != result["database_sha256"]
            ):
                reject("result_rejected")
            fixed = {
                "schema": 1,
                "binding_sha256": self.plan["binding_sha256"],
                "nonce": self.plan["nonce"],
                "pid": self.own["pid"],
                "python_version": "3.12.3",
                "uvicorn_version": "0.34.3",
                "uvloop_version": "0.23.0",
                "uvicorn_sha256": self.sources["uvicorn_sha256"],
                "uvloop_binary_sha256": self.sources["uvloop_binary_sha256"],
                "before_sha256": digest(encoded(before)),
                "during_sha256": digest(encoded(during)),
                "after_sha256": digest(encoded(after)),
                "result_sha256": digest(encoded(expected_result)),
            }
            if finished != fixed or result != expected_result or not release < after["at_us"]:
                reject("result_rejected")
            group = group_evidence(self.plan)
            exit_us = int(state["ExecMainExitTimestampMonotonic"])
            if (
                state["ExecMainCode"] != "1"
                or state["ExecMainStatus"] != "0"
                or state["Result"] != "success"
                or state["ActiveState"] != "inactive"
                or state["SubState"] != "dead"
                or exit_us < after["at_us"]
            ):
                reject("result_rejected")
            self.event("result_preserved", {"finished": finished, "result": result, "release_at_us": release})
            self.event(
                "exit_observed",
                {
                    "pid": self.own["pid"],
                    "invocation_id": self.own["invocation_id"],
                    "exit_code": 1,
                    "exit_status": 0,
                    "job": self.jobs[self.plan["unit"]],
                    "result": "success",
                    "exit_at_us": exit_us,
                    "cgroup": group,
                    "state": state,
                },
            )
            self.release_observer()
            cleaned = True
            if source_identity(self.request) != self.sources:
                reject("environment_changed")
            self.event("sources_reverified", self.sources)
            passed = True
        except Exception as exc:
            code = str(exc).removeprefix("new_runtime_stop_") if type(exc) is StopError else None
            error = code if code in ERRORS else "supervision_failed"
        finally:
            if not passed and self.own is not None:
                try:
                    if kernel(self.plan, self.own_state(), self.worker) == self.own:
                        self.event("abort_intent", {"pid": self.own["pid"]})
                        write(
                            Path(self.plan["host_runtime"]) / "abort.json", encoded({"aborted": True}), 0o444
                        )
                except Exception:
                    pass
            summary = {
                "schema": 1,
                "request": self.request,
                "actual_linux": True,
                "passed": passed,
                "error_class": error,
                "operation": "systemctl_stop_no_block",
                "python_version": "3.12.3",
                "uvicorn_version": "0.34.3",
                "uvloop_version": "0.23.0",
                "clean_exit_zero": passed,
                "recursive_cgroup_empty": passed,
                "pending_results_preserved": passed,
                "task_census_sha256": digest(encoded(self.census)) if len(self.census) == 3 else None,
                "stop_properties_sha256": digest(encoded(POLICY)),
                "helper_sha256": identity["helper_sha256"],
                "worker_sha256": WORKER_SHA256,
                "completed_at_us": time.monotonic_ns() // 1000,
                "cleanup_observed": cleaned,
                "production_verified": False,
                "production_tasks_drained": False,
                "proof_scope": "analysis_executor_database",
            }
            self.event("completed" if passed else "failed", summary)
            receipt = {
                "schema": 1,
                "identity_sha256": digest(encoded(identity)),
                "summary": summary,
                "events": self.events,
                "last_event_sha256": self.tail,
            }
            os.close(self.fd)
            self.fd = None
            write(self.directory / "receipt.json", encoded(receipt))
        if not passed:
            reject(error or "supervision_failed")
        return summary


def cgroup_root(plan):
    return CGROUP / plan["control_group"].lstrip("/")


def run_validation(request, *, boundary, run, record):
    return Validation(request, boundary=boundary, run=run, record=record).execute()


SUCCESS_EVENTS = (
    "sources_verified",
    "start_intent",
    "policy_verified",
    "exec_arrays_verified",
    "typed_job_verified",
    "kernel_bootstrap",
    "canaries_verified",
    "census_before",
    "observer_start_intent",
    "observer_ready",
    "stop_intent",
    "stop_committed",
    "census_during",
    "stop_waiting",
    "work_release_intent",
    "census_after",
    "result_preserved",
    "exit_observed",
    "observer_release_intent",
    "observer_released",
    "sources_reverified",
    "completed",
)


def exact(value, expected, code="record_rejected"):
    if encoded(value) != encoded(expected):
        reject(code)


def positive(value):
    return type(value) is int and 0 < value < 2**64


def kernel_evidence(value, plan, worker):
    keys = {
        "pid",
        "start_ticks",
        "invocation_id",
        "uids",
        "gids",
        "network_namespace_inode",
        "host_network_namespace_inode",
        "mount_namespace_inode",
        "host_mount_namespace_inode",
        "exe_device",
        "exe_inode",
        "control_group",
        "members",
        "runtime_device",
        "runtime_inode",
        "mounts",
        "readonly_paths",
        "no_new_privs",
        "capabilities",
    }
    if type(value) is not dict or set(value) != keys:
        reject("record_rejected")
    numbers = keys - {
        "invocation_id",
        "uids",
        "gids",
        "control_group",
        "members",
        "mounts",
        "readonly_paths",
        "no_new_privs",
        "capabilities",
    }
    if (
        any(not positive(value[k]) for k in numbers)
        or type(value["invocation_id"]) is not str
        or not re.fullmatch("[0-9a-f]{32}", value["invocation_id"])
        or value["control_group"] != plan["control_group"]
        or value["network_namespace_inode"] == value["host_network_namespace_inode"]
        or value["mount_namespace_inode"] == value["host_mount_namespace_inode"]
    ):
        reject("record_rejected")
    for key in ("uids", "gids"):
        row = value[key]
        if type(row) is not list or len(row) != 4 or any(not positive(n) for n in row) or len(set(row)) != 1:
            reject("record_rejected")
    forbidden = {0}
    for name in ("ubuntu", "prism-stage", "prism-integration", "prism-integration-broker"):
        with contextlib.suppress(KeyError):
            forbidden.add(pwd.getpwnam(name).pw_uid)
    if value["uids"][0] in forbidden:
        reject("record_rejected")
    exact(value["members"], [value["pid"]])
    exact(value["no_new_privs"], 1)
    exact(value["capabilities"], {k: 0 for k in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb")})
    mounts = value["mounts"]
    if type(mounts) is not dict or set(mounts) != {"/run", "/run/systemd", "/run/dbus"}:
        reject("record_rejected")
    for point, item in mounts.items():
        if (
            type(item) is not dict
            or set(item) != {"device", "inode", "mode", "host_device", "host_inode", "fstype", "readonly"}
            or any(not positive(item[k]) for k in ("device", "inode", "host_device", "host_inode"))
            or type(item["mode"]) is not int
            or not 0 <= item["mode"] <= 0o777
            or point != "/run"
            and item["mode"] != 0
            or (item["device"], item["inode"]) == (item["host_device"], item["host_inode"])
            or item["fstype"] != "tmpfs"
            or item["readonly"] is not True
        ):
            reject("record_rejected")
    readonly = value["readonly_paths"]
    expected = {"/home", str(RELEASES / plan["request"]["candidate"]), str(worker.parent)}
    if type(readonly) is not dict or set(readonly) != expected:
        reject("record_rejected")
    for point, item in readonly.items():
        if (
            type(item) is not dict
            or set(item) != {"mountpoint", "options", "fstype"}
            or type(item["mountpoint"]) is not str
            or not item["mountpoint"].startswith("/")
            or not (point == item["mountpoint"] or point.startswith(item["mountpoint"].rstrip("/") + "/"))
            or type(item["options"]) is not list
            or len(item["options"]) > 32
            or any(type(option) is not str or len(option) > 128 for option in item["options"])
            or "ro" not in item["options"]
            or "rw" in item["options"]
            or type(item["fstype"]) is not str
            or not re.fullmatch("[a-zA-Z0-9_.-]{1,32}", item["fstype"])
        ):
            reject("record_rejected")
    return value


def policy_evidence(state, plan, own, *, phase):
    if (
        type(state) is not dict
        or set(state) != set(PROPERTIES.split(",")) | {"ExecStop", "ExecStopPost"}
        or any(type(v) is not str for v in state.values())
        or any(state[k] != v for k, v in {**POLICY, **EXTRA_POLICY}.items())
        or state["LoadState"] != "loaded"
        or state["Transient"] != "yes"
        or state["DynamicUser"] != "yes"
        or state["InvocationID"] != own["invocation_id"]
        or state["ExecMainPID"] != str(own["pid"])
        or state["ControlGroup"]
        not in ({"", plan["control_group"]} if phase == "after" else {plan["control_group"]})
    ):
        reject("record_rejected")
    if phase == "before":
        if (
            state["ActiveState"] != "active"
            or state["SubState"] != "running"
            or state["MainPID"] != str(own["pid"])
            or state["Job"] != "0"
        ):
            reject("record_rejected")
    else:
        if (
            state["ActiveState"] != "inactive"
            or state["SubState"] != "dead"
            or state["MainPID"] != "0"
            or state["ExecMainCode"] != "1"
            or state["ExecMainStatus"] != "0"
            or state["Result"] != "success"
            or state["Job"] != "0"
            or not state["ExecMainExitTimestampMonotonic"].isdigit()
            or int(state["ExecMainExitTimestampMonotonic"]) <= 0
        ):
            reject("record_rejected")


def empty_evidence(value):
    if (
        type(value) is not dict
        or set(value) != {"cgroup_v2", "root_exists", "groups"}
        or value["cgroup_v2"] is not True
        or type(value["root_exists"]) is not bool
        or type(value["groups"]) is not list
        or len(value["groups"]) > 128
        or value["root_exists"] is not bool(value["groups"])
    ):
        reject("record_rejected")
    names = set()
    for item in value["groups"]:
        if (
            type(item) is not dict
            or set(item) != {"relative", "members", "populated"}
            or type(item["relative"]) is not str
            or item["relative"] in names
            or item["relative"] != "."
            and not re.fullmatch("[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*", item["relative"])
        ):
            reject("record_rejected")
        exact(item["members"], [])
        exact(item["populated"], 0)
        names.add(item["relative"])
    if value["root_exists"] and "." not in names:
        reject("record_rejected")


def observer_evidence(value, plan):
    if (
        type(value) is not dict
        or set(value) != {"invocation_id", "exec_main_pid", "control_group", "empty", "state", "job"}
        or type(value["invocation_id"]) is not str
        or not re.fullmatch("[0-9a-f]{32}", value["invocation_id"])
        or not positive(value["exec_main_pid"])
        or value["control_group"] != "/system.slice/" + plan["observer"]
        or value["empty"] is not True
    ):
        reject("record_rejected")
    exact(validate_job(value["job"]), {"type": "(uo)", "data": [0, "/"]})
    state = value["state"]
    if (
        type(state) is not dict
        or set(state) != set(OBSERVER_PROPERTIES.split(","))
        or any(type(v) is not str for v in state.values())
        or state["InvocationID"] != value["invocation_id"]
        or state["ExecMainPID"] != str(value["exec_main_pid"])
        or state["LoadState"] != "loaded"
        or state["Transient"] != "yes"
        or state["DynamicUser"] != "yes"
        or state["ActiveState"] != "active"
        or state["SubState"] != "exited"
        or state["MainPID"] != "0"
        or state["ExecMainCode"] != "1"
        or state["ExecMainStatus"] != "0"
        or state["Result"] != "success"
        or state["RemainAfterExit"] != "yes"
        or state["Job"] != "0"
        or state["Wants"].split() != [plan["unit"]]
        or state["ControlGroup"] not in {"", value["control_group"]}
        or any(state[k] for k in ("PartOf", "BindsTo", "PropagatesStopTo"))
    ):
        reject("record_rejected")


def replay(identity, raw, receipt, request):
    """Verify root-supervised facts in their fixed order, never summary booleans alone."""
    if type(identity) is not dict or set(identity) != {
        "schema",
        "request",
        "plan",
        "sources",
        "helper_sha256",
        "worker_sha256",
        "boot_id",
    }:
        reject("record_rejected")
    exact(identity["schema"], 1)
    exact(identity["request"], request)
    plan = identity["plan"]
    if type(plan) is not dict or type(plan.get("nonce")) is not str:
        reject("record_rejected")
    exact(plan, descriptor(request, nonce=plan["nonce"]))
    if (
        type(identity["boot_id"]) is not str
        or not re.fullmatch("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity["boot_id"])
        or identity["worker_sha256"] != WORKER_SHA256
        or identity["helper_sha256"] != file_hash(Path(__file__))
    ):
        reject("record_rejected")
    sources = identity["sources"]
    if (
        type(sources) is not dict
        or set(sources)
        != {
            "environment_sha256",
            "candidate_source_sha256",
            "system_python_sha256",
            "uvicorn_sha256",
            "uvloop_binary_sha256",
            "analysis_engine_sha256",
            "database_sha256",
            "config_sha256",
        }
        or any(type(v) is not str or not re.fullmatch("[0-9a-f]{64}", v) for v in sources.values())
        or sources["environment_sha256"] != request["environment_sha256"]
        or sources["system_python_sha256"] != SYSTEM_PYTHON_SHA256
        or any(
            sources[name + "_sha256"] != value for name, value in APPLICATION_SOURCES[request["role"]].items()
        )
    ):
        reject("record_rejected")
    if type(raw) is not bytes or len(raw) > 2 * 1024 * 1024 or not raw.endswith(b"\n"):
        reject("record_rejected")
    rows = raw.splitlines(keepends=True)
    if len(rows) != len(SUCCESS_EVENTS):
        reject("record_rejected")
    details = {}
    tail = "0" * 64
    for index, (line, name) in enumerate(zip(rows, SUCCESS_EVENTS, strict=True), 1):
        try:
            row = parse_json(line)
        except (ValueError, UnicodeError):
            reject("record_rejected")
        if (
            type(row) is not dict
            or set(row) != {"sequence", "previous_sha256", "event", "details"}
            or type(row["sequence"]) is not int
            or row["sequence"] != index
            or row["previous_sha256"] != tail
            or row["event"] != name
            or type(row["details"]) is not dict
            or encoded(row) + b"\n" != line
        ):
            reject("record_rejected")
        details[name] = row["details"]
        tail = digest(line)
    if type(receipt) is not dict or set(receipt) != {
        "schema",
        "identity_sha256",
        "summary",
        "events",
        "last_event_sha256",
    }:
        reject("record_rejected")
    exact(
        receipt,
        {
            "schema": 1,
            "identity_sha256": digest(encoded(identity)),
            "summary": details["completed"],
            "events": len(rows),
            "last_event_sha256": tail,
        },
    )
    exact(details["sources_verified"], identity)
    exact(details["start_intent"], {"nonce": plan["nonce"], "unit": plan["unit"]})
    bootstrap = details["kernel_bootstrap"]
    if set(bootstrap) != {"identity", "bootstrap", "bootstrap_sha256"}:
        reject("record_rejected")
    worker = SOURCES / WORKER_SHA256 / "worker.py"
    own = kernel_evidence(bootstrap["identity"], plan, worker)
    pid = own["pid"]
    expected_bootstrap = {
        "schema": 1,
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "pid": pid,
        "candidate_imported": False,
        "uvicorn_imported": False,
        "uvloop_imported": False,
    }
    exact(bootstrap["bootstrap"], expected_bootstrap)
    exact(bootstrap["bootstrap_sha256"], digest(encoded(expected_bootstrap)))
    policy_evidence(details["policy_verified"], plan, own, phase="before")
    exact(details["exec_arrays_verified"], {key: EMPTY_EXEC_ARRAY for key in ("ExecStop", "ExecStopPost")})
    exact(validate_job(details["typed_job_verified"]), {"type": "(uo)", "data": [0, "/"]})
    validate_canaries(details["canaries_verified"], plan, pid)
    census = [
        validate_census(details["census_" + phase], plan, pid, phase)
        for phase in ("before", "during", "after")
    ]
    before, during, after = census
    if (
        any(
            row["future_id"] != before["future_id"]
            or row["analysis_id"] != before["analysis_id"]
            or row["executor_thread_id"] != before["executor_thread_id"]
            for row in census
        )
        or not before["at_us"] < during["at_us"] < after["at_us"]
    ):
        reject("record_rejected")
    exact(details["observer_start_intent"], {"unit": plan["observer"]})
    observer = details["observer_ready"]
    observer_evidence(observer, plan)
    stop = details["stop_intent"]
    if type(stop.get("at_us")) is not int or not before["at_us"] < stop["at_us"] < during["at_us"]:
        reject("record_rejected")
    exact(
        stop,
        {
            "pid": pid,
            "start_ticks": own["start_ticks"],
            "invocation_id": own["invocation_id"],
            "at_us": stop["at_us"],
        },
    )
    exact(details["stop_committed"], stop)
    waiting = details["stop_waiting"]
    if not positive(waiting.get("job")):
        reject("record_rejected")
    exact(
        waiting,
        {
            "job": waiting["job"],
            "typed_job": validate_job(waiting.get("typed_job"), str(waiting["job"])),
            "pid": pid,
            "invocation_id": own["invocation_id"],
            "active_state": "deactivating",
            "sub_state": "stop-sigterm",
            "identity": own,
        },
    )
    release = details["work_release_intent"]
    if not positive(release.get("at_us")) or not during["at_us"] < release["at_us"] < after["at_us"]:
        reject("record_rejected")
    exact(release, {"at_us": release["at_us"], "during_sha256": digest(encoded(during))})
    result = validate_result(details["result_preserved"].get("result"), plan, before, sources)
    finished = {
        "schema": 1,
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "pid": pid,
        "python_version": "3.12.3",
        "uvicorn_version": "0.34.3",
        "uvloop_version": "0.23.0",
        "uvicorn_sha256": sources["uvicorn_sha256"],
        "uvloop_binary_sha256": sources["uvloop_binary_sha256"],
        "before_sha256": digest(encoded(before)),
        "during_sha256": digest(encoded(during)),
        "after_sha256": digest(encoded(after)),
        "result_sha256": digest(encoded(result)),
    }
    exact(
        details["result_preserved"],
        {"finished": finished, "result": result, "release_at_us": release["at_us"]},
    )
    exited = details["exit_observed"]
    if not positive(exited.get("exit_at_us")) or exited["exit_at_us"] < after["at_us"]:
        reject("record_rejected")
    policy_evidence(exited.get("state"), plan, own, phase="after")
    exact(validate_job(exited.get("job")), {"type": "(uo)", "data": [0, "/"]})
    empty_evidence(exited.get("cgroup"))
    exact(
        exited,
        {
            "pid": pid,
            "invocation_id": own["invocation_id"],
            "exit_code": 1,
            "exit_status": 0,
            "job": exited["job"],
            "result": "success",
            "exit_at_us": exited["exit_at_us"],
            "cgroup": exited["cgroup"],
            "state": exited["state"],
        },
    )
    if int(exited["state"]["ExecMainExitTimestampMonotonic"]) != exited["exit_at_us"]:
        reject("record_rejected")
    exact(details["observer_release_intent"], observer)
    released = details["observer_released"]
    if set(released) != {"state", "cgroup", "job"}:
        reject("record_rejected")
    empty_evidence(released["cgroup"])
    state = released["state"]
    if (
        type(state) is not dict
        or set(state) != set(OBSERVER_PROPERTIES.split(","))
        or any(type(v) is not str for v in state.values())
        or state["LoadState"] != "not-found"
        and not (
            state["InvocationID"] == observer["invocation_id"]
            and state["MainPID"] == "0"
            and state["ActiveState"] == "inactive"
            and state["Job"] == "0"
        )
    ):
        reject("record_rejected")
    if state["LoadState"] == "not-found":
        exact(released["job"], None)
    else:
        exact(validate_job(released["job"]), {"type": "(uo)", "data": [0, "/"]})
    exact(details["sources_reverified"], sources)
    summary = details["completed"]
    if not positive(summary.get("completed_at_us")) or summary["completed_at_us"] < exited["exit_at_us"]:
        reject("record_rejected")
    exact(
        summary,
        {
            "schema": 1,
            "request": request,
            "actual_linux": True,
            "passed": True,
            "error_class": None,
            "operation": "systemctl_stop_no_block",
            "python_version": "3.12.3",
            "uvicorn_version": "0.34.3",
            "uvloop_version": "0.23.0",
            "clean_exit_zero": True,
            "recursive_cgroup_empty": True,
            "pending_results_preserved": True,
            "task_census_sha256": digest(encoded(census)),
            "stop_properties_sha256": digest(encoded(POLICY)),
            "helper_sha256": identity["helper_sha256"],
            "worker_sha256": WORKER_SHA256,
            "completed_at_us": summary["completed_at_us"],
            "cleanup_observed": True,
            "production_verified": False,
            "production_tasks_drained": False,
            "proof_scope": "analysis_executor_database",
        },
    )
    return plan, own, census, details


def read_accepted(request, *, role, candidate, archive_sha256, environment_sha256, record, run):
    """Read only an accepted root chain bound to the same boot, source and venv.

    The reviewed role adapter additionally revalidates the prepared archive/tree
    before and after this call. No caller bool or alternate receipt path exists.
    """
    request = valid_request(request)
    if sys.platform != "linux" or os.geteuid() != 0:
        reject("root_linux_required")
    exact(
        {k: request[k] for k in ("role", "candidate", "archive_sha256", "environment_sha256")},
        {
            "role": role,
            "candidate": candidate,
            "archive_sha256": archive_sha256,
            "environment_sha256": environment_sha256,
        },
    )
    root = Path(record)
    if root != OPERATIONS / request["release_id"]:
        reject("record_rejected")
    directory = root / "new-runtime-stop" / request["attempt"]
    locked(Path(__file__))
    locked(directory)
    if (
        stat.S_IMODE(directory.stat().st_mode) != 0o700
        or stat.S_IMODE(directory.parent.stat().st_mode) != 0o700
        or {item.name for item in directory.iterdir()} != {"identity.json", "events.jsonl", "receipt.json"}
    ):
        reject("record_rejected")
    identity = read_json(directory / "identity.json", mode=0o600)
    receipt = read_json(directory / "receipt.json", mode=0o600)
    raw = read(directory / "events.jsonl", mode=0o600, limit=2 * 1024 * 1024)
    plan, own, census, details = replay(identity, raw, receipt, request)
    if read(PROC / "sys/kernel/random/boot_id", limit=128).decode().strip() != identity["boot_id"]:
        reject("record_rejected")
    if source_identity(request) != identity["sources"]:
        reject("environment_changed")
    worker = SOURCES / WORKER_SHA256 / "worker.py"
    locked(worker)
    if digest(read(worker, mode=0o444)) != WORKER_SHA256:
        reject("source_rejected")
    allowed = [
        INVENTORY,
        *exec_array_commands(plan["unit"]),
        job_command(plan["unit"]),
        job_command(plan["observer"]),
        *(
            ["/usr/bin/systemctl", "show", "--all", "--property=" + names, unit]
            for names, unit in ((PROPERTIES, plan["unit"]), (OBSERVER_PROPERTIES, plan["observer"]))
        ),
    ]

    def readonly(argv, *, timeout):
        if argv not in allowed or type(timeout) not in {int, float} or not 0 < timeout <= 10:
            reject("unit_rejected")
        result = run(argv, timeout=timeout)
        if type(result) is not bytes or len(result) > 32768:
            reject("unit_rejected")
        return result

    # A manager may retain this experiment's inactive units briefly after its
    # empty observer exits. Verify them below; never ignore any unrelated unit.
    inventory = readonly(INVENTORY, timeout=10)
    try:
        rows = inventory.decode("ascii").splitlines()
    except UnicodeError:
        reject("unit_rejected")
    if any(len(row.split()) < 4 or row.split()[0] not in {plan["unit"], plan["observer"]} for row in rows):
        reject("cleanup_unconfirmed")
    for unit, observer in ((plan["unit"], False), (plan["observer"], True)):
        state = show(readonly, unit, observer=observer)
        invocation = own["invocation_id"] if not observer else None
        if state["LoadState"] != "not-found" and (
            state["MainPID"] != "0"
            or state["ActiveState"] != "inactive"
            or state["Job"] != "0"
            or invocation is not None
            and state["InvocationID"] != invocation
        ):
            reject("cleanup_unconfirmed")
        group_evidence({"control_group": "/system.slice/" + unit})
    try:
        if process_stat(own["pid"]) == own["start_ticks"]:
            reject("cleanup_unconfirmed")
    except FileNotFoundError:
        pass
    # Public synthetic witness files remain private; compare every actual byte.
    runtime = host_runtime(plan)
    if (runtime.stat().st_dev, runtime.stat().st_ino) != (own["runtime_device"], own["runtime_inode"]):
        reject("record_rejected")
    for name, value in (
        (f"census-{phase}.json", row)
        for phase, row in zip(("before", "during", "after"), census, strict=True)
    ):
        exact(read_json(runtime / name, uid=own["uids"][0], mode=0o600), value)
    exact(
        read_json(runtime / "bootstrap-ready.json", uid=own["uids"][0], mode=0o600),
        details["kernel_bootstrap"]["bootstrap"],
    )
    exact(
        read_json(runtime / "canaries.json", uid=own["uids"][0], mode=0o600),
        details["canaries_verified"],
    )
    for name, phase in (
        ("allow-canaries.json", "allow_canaries"),
        ("allow-import.json", "allow_import"),
        ("release-work.json", "release_work"),
    ):
        exact(
            read_json(runtime / name, mode=0o444),
            {"schema": 1, "binding_sha256": plan["binding_sha256"], "nonce": plan["nonce"], "phase": phase},
        )
    exact(
        read_json(runtime / "finished.json", uid=own["uids"][0], mode=0o600),
        details["result_preserved"]["finished"],
    )
    exact(
        read_json(runtime / "result.json", uid=own["uids"][0], mode=0o600),
        details["result_preserved"]["result"],
    )
    if (
        digest(read(runtime / "data/chat.db", uid=own["uids"][0], mode=0o600, limit=32 * 1024 * 1024))
        != details["result_preserved"]["result"]["database_sha256"]
    ):
        reject("result_rejected")
    return {
        "sha256": digest(encoded(receipt)),
        "candidate": candidate,
        "archive_sha256": archive_sha256,
        "environment_sha256": environment_sha256,
        "stop_properties_sha256": digest(encoded(POLICY)),
        "actual_linux": True,
        "operation": "systemctl_stop_no_block",
        "uvloop_version": "0.23.0",
        "clean_exit_zero": True,
        "recursive_cgroup_empty": True,
        "pending_results_preserved": True,
        "task_census_sha256": digest(encoded(census)),
    }

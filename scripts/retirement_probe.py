"""Fixed Linux retirement fixture and descriptor, never an operations entry point.

A reviewed root controller must verify/copy the pinned data-only dependency bundle,
launch the descriptor's isolated synthetic units and collect actual systemd/kernel
observations. This module never invokes systemctl, signals a process, imports the
old environment as root, or reads application databases/configuration. Unit tests
and a descriptor do not constitute Linux acceptance or production quiescence.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import sys
from contextlib import suppress
from pathlib import Path

OLD_ENV_SHA256 = "a395e2170c27c264e5c8f3503902732aec06a8905baf8653908abb6b45b5b072"
UVICORN_SHA256 = "29dd92d146abcf0272b7d6076734e084c3f1c5155ad7321503c0aacba02e4fd0"
UVLOOP_SHA256 = "8d67497ee731a56033f091471d500e3b0a277a641af09ca7da06794ed3e6f76f"
SOURCE_ROOT = Path("/home/ubuntu/stock-venv/lib/python3.12/site-packages")
BUNDLE_ROOT = Path("/opt/prism/validation")
RUNTIME_ROOT = Path("/run")
CGROUP_ROOT = Path("/sys/fs/cgroup")
FORBIDDEN_USERS = ("ubuntu", "prism-stage", "prism-integration")
VERSIONS = {"uvicorn": "0.34.3", "uvloop": "0.22.1", "click": "8.4.1", "h11": "0.16.0"}
SHA = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
RELEASE = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
NONCE = re.compile(r"[0-9a-f]{32}")
CASES = (
    ("int-executor", "executor", "SIGINT", "executor_preserved"),
    ("int-post", "post_complete", "SIGINT", "executor_preserved"),
    ("int-nested", "nested_thread", "SIGINT", "executor_preserved"),
    ("int-async", "async_task", "SIGINT", "async_cancel_counterexample"),
    ("int-daemon", "daemon_thread", "SIGINT", "daemon_counterexample"),
    ("int-child", "orphan_child", "SIGINT", "child_counterexample"),
    ("term-control", "executor", "SIGTERM", "sigterm_counterexample"),
)
# Public package bytes observed in the bound old environment. No .pth, startup
# hook, bytecode, executable script from bin/, editable hook or app code is copied.
PACKAGE_FILES = (
    ("click/__init__.py", 4634, "1487767d7092241df27960fe7b6b8e37e9818456b661cf4cbd71a61eef0e5c6d"),
    ("click/_compat.py", 18910, "5cad70a2a0523d16f528d0305b056c48c7c49bc6657c3c010a371e5b34869846"),
    ("click/_termui_impl.py", 30411, "54dc96636ee99b4254ab5d962bffe924d631794a03c9f370604804eafcff7273"),
    ("click/_textwrap.py", 6270, "ed9d0ded59a7fbae933524d4c29e8e5c96dc5174666044fd87d736b9c0fca104"),
    ("click/_utils.py", 996, "782642b7026db180f941892435627c318ffc001223cbc31cce030cc95637dab4"),
    ("click/_winconsole.py", 8543, "292c5f35b3256116ba18e26e08b82c8363dbddd56480934fa8b24f69ef8f6b5d"),
    ("click/core.py", 137917, "090689d002f0e33cb2569e84ee03fa6bf1bcf1e8ae5c4f6607316720943e8a06"),
    ("click/decorators.py", 18467, "c6047cbd90029615959338ad3f6da4ea311315184aada961f35cef03af42b1dd"),
    ("click/exceptions.py", 11294, "1498ad0dde8c4992e3e82b4a416a644331e77ec530dd4ca9c43534b7be9b9f47"),
    ("click/formatting.py", 10370, "afa9062e93f21740af6bab924f0499ffc38d444ee3a7ddc8d42d0955161012a1"),
    ("click/globals.py", 1923, "80cf8d87a0383341c1fd2824685e4ce2770618c0c773f7e51d7bbdfe88781845"),
    ("click/parser.py", 19052, "a09f9f53fde6bf1ba022e36d1da0804d9e7a9601261d18208c9892a84818ae30"),
    ("click/shell_completion.py", 21748, "4317856f745aa4d48ff19c2f2b6043548da3ac4292d516c969eb8532b1c90275"),
    ("click/termui.py", 33000, "85f959fed2bb3f8594bff79ffd155544bee7d2744f8530f4c0dd2990713b843a"),
    ("click/testing.py", 25717, "d24fefe41cbfe593b631c8c14fb953a90e7b5bc409b96d3fe52c7b77664b29e7"),
    ("click/types.py", 42783, "522544c74ca842dad63b6c07db6360304e0b7c3c4c3508bc8f5aad26ea58229c"),
    ("click/utils.py", 20386, "62c4cdac28447f00d7977f424c2ff25a743e655919c588c51124427c4802958e"),
    ("h11/__init__.py", 1507, "88ed4ace448ee36c99e9f7e0f953206f1fd9553586518d349d16045a7facde46"),
    ("h11/_abnf.py", 4815, "c9b8b1af4c6cba99e403a18503232e6ee5c5e9373595bfe117cf743600ac7cd7"),
    ("h11/_connection.py", 26863, "93d61155fea4a19a9bb6d056dfac5259a26959d6706bec50554f401c4a3d0ee2"),
    ("h11/_events.py", 11792, "23deda5e86a5d56bbb7642f9e3c04034150290e21b7bec790a2a1803d201635e"),
    ("h11/_headers.py", 10412, "3fb0fe9413711f074b64f2e29a6630acf1bef59923125bef25925d6408123fee"),
    ("h11/_readers.py", 8590, "6b8472a4e454082dddd2afe4c4fb8120cee34c3f222ede57f754c7d05b1db8de"),
    ("h11/_receivebuffer.py", 5252, "c6bb29b1db0d81617145f41c4d7c51f11add8d15d74cad08a39710616a49d56b"),
    ("h11/_state.py", 13231, "ff92c6fc1191f0509c1507813c7f93307826ffe07e1147160a74287fff578c51"),
    ("h11/_util.py", 4888, "2d69248d7c89685940cba2eddfdc3bdd44ad925153e68bdcbe8d13918ed162e9"),
    ("h11/_version.py", 686, "1954ac6cf48f0dc3ae17aa6d7c88979d52686849b7ca05db327aa5aff1a26a1c"),
    ("h11/_writers.py", 5081, "a052a6e8fb637877db8f844b5fb541eca0dcd60218e778171b7fc747d96d9930"),
    ("uvicorn/__init__.py", 147, "e0bc04a7b8e82234c91950a0cc1e064130281411f80180f63be10f098c56b7a3"),
    ("uvicorn/__main__.py", 62, "0d08b3cba9ca3f4cb084fa6708782644360831f72a64a544ccd2164198eab554"),
    ("uvicorn/_subprocess.py", 2766, "1db7d19ec0a45f283bc425950165b35d04de5a5bcb29f4e5205e707af141911e"),
    ("uvicorn/_types.py", 7775, "e4570fbef21f78ab090e31a14eb71e0eff139b0cd823cc8f17b997b174d63943"),
    ("uvicorn/config.py", 20887, "27ab5d061c1e70d8142475f409b2181c9f36f1297758ac9eadc7fa4214b31a63"),
    ("uvicorn/importer.py", 1128, "9d1b74410deaa62dbae3e9ff991d25e790b675d33c9e8c133734f58ec59a6a6f"),
    ("uvicorn/lifespan/__init__.py", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("uvicorn/lifespan/off.py", 332, "9df23aa87014a3ff3e0445cc04aa0743dc146ec5eb3da5cb09b0d24b4bca4abf"),
    ("uvicorn/lifespan/on.py", 5184, "d4a62e14d37240e3488ed10784a640269f8e3a8908ca38fbe30a4608606fe259"),
    ("uvicorn/logging.py", 4235, "f9e084e273899856ed07da9f349b8454d174635dcb1941eabc5cde9984f43da4"),
    ("uvicorn/loops/__init__.py", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("uvicorn/loops/asyncio.py", 301, "a8f9d02d3da1b5991c186fe77274f2ad21f7f23124aa383c82ec0fd25035e3a0"),
    ("uvicorn/loops/auto.py", 400, "05656ad7c71ef52a054e8df3e73356d88536f39d2edad46b73a5b22bb89db1d2"),
    ("uvicorn/loops/uvloop.py", 148, "2b84326d8571b4af42d9e9838433d40a40574785cc4f939fbfd04f14fa17d289"),
    ("uvicorn/main.py", 17234, "e53173b9bd946d29382ce43f51e4373d92e24d11b9d4a210d2c498f5265430df"),
    ("uvicorn/middleware/__init__.py", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("uvicorn/middleware/asgi2.py", 394, "610ad0366dd17a116db379b3937938cb0f1a0fc120b63d2d452dcde3962441ad"),
    (
        "uvicorn/middleware/message_logger.py",
        2859,
        "2071195129c535a305505770b593b702e1404e761c4a8afe815b4e8db0b3b7c3",
    ),
    (
        "uvicorn/middleware/proxy_headers.py",
        5790,
        "7f554301cfa2a4f1dd353b8b347c1809e0e0617a022ff5630fa8434d45d94ff5",
    ),
    ("uvicorn/middleware/wsgi.py", 7105, "37a7d6c8e9c7a1e1db51ebd7d260d814d988e25b0cbfb634aa777b6377c954be"),
    ("uvicorn/protocols/__init__.py", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    (
        "uvicorn/protocols/http/__init__.py",
        0,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
    (
        "uvicorn/protocols/http/auto.py",
        403,
        "61f5c6cb359369a136a7f8e44cf5ab2425ecc446909c2dcd2b4f86ed68269e5b",
    ),
    (
        "uvicorn/protocols/http/flow_control.py",
        1701,
        "d39d16560df512f3ce907c329c2a0c3f5cd7165fef3b753876eae5739bf2a785",
    ),
    (
        "uvicorn/protocols/http/h11_impl.py",
        20694,
        "e1bf8ab302b9ec505a44f7875e52bfa32f2928ceaf1b585a00b0887911fed5b0",
    ),
    (
        "uvicorn/protocols/http/httptools_impl.py",
        21805,
        "b6e4010a20faadfe4341ec90c0755ce39af11fda2ea233299178bd286e8d4419",
    ),
    ("uvicorn/protocols/utils.py", 1849, "ac28d82dde3fbb03de6646d15d0e9b7827f1c88fe8629bc90b0cf78c418d3a21"),
    (
        "uvicorn/protocols/websockets/__init__.py",
        0,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
    (
        "uvicorn/protocols/websockets/auto.py",
        581,
        "487fca57fdefc11f3fa0675ad86ac716ddfc546f88c18d2e7e3bc73aee15034a",
    ),
    (
        "uvicorn/protocols/websockets/websockets_impl.py",
        15546,
        "aa89d0271cfdc0c28cc01d919d865ecde3fe5df980b310af35e1a0bb8b02dcb7",
    ),
    (
        "uvicorn/protocols/websockets/wsproto_impl.py",
        15366,
        "bb64cacb34540a64294b57b813f5fa528bbd408ba829934d3661d4d48ce458f5",
    ),
    ("uvicorn/server.py", 12879, "29dd92d146abcf0272b7d6076734e084c3f1c5155ad7321503c0aacba02e4fd0"),
    (
        "uvicorn/supervisors/__init__.py",
        507,
        "c13f1e38422a4f5c96420cad66dbf9b5a58cba5ef1a13218d319b59b8a323d3c",
    ),
    (
        "uvicorn/supervisors/basereload.py",
        4036,
        "3005d243772464fc2acc9f149fdc83864dd6d32c8002b42d64bb8e1343889d27",
    ),
    (
        "uvicorn/supervisors/multiprocess.py",
        7507,
        "3a9b745ef3948f50c83176306f83a59096717a1fd18f07859d39833d822d38dc",
    ),
    (
        "uvicorn/supervisors/statreload.py",
        1566,
        "b986e59a8c4cdc86cf6ef303cebe406f0dbe5b291097c3714d3cde2df572be75",
    ),
    (
        "uvicorn/supervisors/watchfilesreload.py",
        2859,
        "5bce986dbd04e5274c618b961c9ddba40157770e59babbcb45d15cbcb9d81080",
    ),
    ("uvicorn/workers.py", 3895, "eca1a01806a9c4690679751c046ba73a348d748f51e38282a164c6100a8075fb"),
    ("uvloop/__init__.py", 7558, "1db6e9f617c96dfacef84b5ca5d362900177752e41130690e5f54b8ea1949f83"),
    ("uvloop/_noop.py", 86, "4830094e25a113b8372b2b6d6e33dd962bfe5217ae6acfad297e3fcbf9ef3bf4"),
    ("uvloop/_testbase.py", 15570, "b116631d1fa7307bf8519db7024482b6012cbe0a3cb8ea8372114d3855628918"),
    ("uvloop/_version.py", 576, "883ca08b9ff23a16c9d34b1c3d4d88216a6b1d597d839210781edb7c15213d06"),
    ("uvloop/includes/__init__.py", 361, "f8e519eb3afa3a9770efc3cab07622d40b8fef8129ed7072c72a1160eb91b602"),
    (
        "uvloop/loop.cpython-312-x86_64-linux-gnu.so",
        15952168,
        "8d67497ee731a56033f091471d500e3b0a277a641af09ca7da06794ed3e6f76f",
    ),
)


class RetirementProbeError(RuntimeError):
    """A fixed safe error code, without inherited exception text."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def bindings(request):
    if type(request) is not dict or set(request) != {"commit", "release_id", "base_environment_sha256"}:
        raise RetirementProbeError("retirement_request_rejected")
    for key, pattern in (("commit", COMMIT), ("release_id", RELEASE), ("base_environment_sha256", SHA)):
        if not isinstance(request[key], str) or not pattern.fullmatch(request[key]):
            raise RetirementProbeError("retirement_identity_rejected")
    if request["base_environment_sha256"] != OLD_ENV_SHA256:
        raise RetirementProbeError("retirement_base_environment_changed")
    return dict(request)


# The payload is reviewed fixed code, run only under the descriptor's non-root
# UID. Only four pinned public libraries are put on sys.path, after UID/Linux/hash
# checks. No site.main(), old interpreter, env file, candidate or user code runs.
FIXTURE_SOURCE = r"""
import asyncio
import concurrent.futures
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path


def main(argv):
    if len(argv) != 5 or argv[0] not in {"serve", "child"}:
        raise SystemExit(2)
    mode, profile, nonce, binding_sha, bundle_sha = argv
    if profile not in {"executor", "post_complete", "nested_thread", "async_task", "daemon_thread", "orphan_child"}:
        raise SystemExit(2)
    if not re.fullmatch("[0-9a-f]{32}", nonce) or not all(re.fullmatch("[0-9a-f]{64}", x) for x in (binding_sha, bundle_sha)):
        raise SystemExit(2)
    root = Path.cwd()
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise SystemExit(2)
    if not re.fullmatch("prism-retirement-probe-"+nonce+"-[a-z-]+", root.name) or root.parent != Path("/run"):
        raise SystemExit(2)
    forbidden = {0}
    for name in ("ubuntu", "prism-stage", "prism-integration"):
        try:
            forbidden.add(pwd.getpwnam(name).pw_uid)
        except KeyError:
            pass
    if os.geteuid() in forbidden or root.is_symlink() or root.stat().st_uid != os.geteuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise SystemExit(2)
    os.umask(0o077)
    os.environ.clear()
    os.environ.update({"PATH":"/usr/bin:/bin", "HOME":str(root), "TMPDIR":str(root), "LANG":"C.UTF-8"})

    def mark(name, **fields):
        value={"case_profile":profile,"nonce":nonce,"binding_sha256":binding_sha,"at_ns":time.monotonic_ns(),**fields}
        final=root/(name+".json")
        if final.exists() or final.is_symlink():
            raise RuntimeError("duplicate_fixture_marker")
        pending=root/(name+".pending")
        fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        with os.fdopen(fd,"w") as stream:
            json.dump(value,stream,sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        # Fixed marker names are each published once by their fixed phase;
        # polling never observes a partially written JSON marker.
        os.rename(pending,final)

    def wait_gate():
        deadline=time.monotonic()+20
        while not (root/"release-work").exists():
            if time.monotonic()>=deadline:
                mark("fixture_gate_expired")
                return False
            time.sleep(.01)
        return True

    if mode == "child":
        mark("child_started",pid=os.getpid())
        if wait_gate():
            time.sleep(.05)
            mark("child_saved")
        mark("child_finished")
        return

    vendor=Path("/opt/prism/validation")/bundle_sha/"vendor"
    def pinned(relative, expected):
        path=vendor/relative
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        with os.fdopen(fd,"rb") as stream:
            st=os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o222 or st.st_nlink!=1:
                raise SystemExit(2)
            if hashlib.file_digest(stream,"sha256").hexdigest()!=expected:
                raise SystemExit(2)
    pinned("uvicorn/server.py","29dd92d146abcf0272b7d6076734e084c3f1c5155ad7321503c0aacba02e4fd0")
    pinned("uvloop/loop.cpython-312-x86_64-linux-gnu.so","8d67497ee731a56033f091471d500e3b0a277a641af09ca7da06794ed3e6f76f")
    sys.path.insert(0,str(vendor))
    import uvicorn
    import uvloop
    import h11
    if (uvicorn.__version__,uvloop.__version__,h11.__version__) != ("0.34.3","0.22.1","0.16.0"):
        raise SystemExit(2)
    mark("fixture_ready",pid=os.getpid(),uid_nonroot=True,versions_bound=True)

    def work():
        mark("executor_started")
        if profile=="post_complete":
            mark("analysis_saved")
            mark("post_started")
            if not wait_gate():
                return
        elif profile=="nested_thread":
            def nested():
                mark("nested_started")
                if wait_gate():
                    mark("nested_saved")
            thread=threading.Thread(target=nested,daemon=False)
            thread.start()
            thread.join()
            mark("nested_joined")
        elif profile=="orphan_child":
            subprocess.Popen(["/usr/bin/python3","-I","-S","-B",str(Path(__file__).resolve()),"child",profile,nonce,binding_sha,bundle_sha],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=dict(os.environ))
            mark("executor_finished")
            return
        elif not wait_gate():
            return
        if profile!="post_complete":
            mark("analysis_saved")
            mark("post_started")
        time.sleep(.05)
        mark("post_saved")
        mark("executor_finished")

    async def async_work():
        mark("async_started")
        try:
            while not (root/"release-work").exists():
                await asyncio.sleep(.01)
            mark("async_saved")
        except asyncio.CancelledError:
            mark("async_cancelled")
            raise

    def daemon_work():
        mark("daemon_started")
        if wait_gate():
            mark("daemon_saved")

    async def app(scope, receive, send):
        if scope["type"]!="http" or scope.get("path")!="/fixture":
            raise RuntimeError("fixed_fixture_route_required")
        if profile=="async_task":
            asyncio.create_task(async_work())
        elif profile=="daemon_thread":
            threading.Thread(target=daemon_work,daemon=True).start()
        else:
            asyncio.get_running_loop().run_in_executor(None,work)
        await send({"type":"http.response.start","status":200,"headers":[(b"content-type",b"text/event-stream")]})
        await send({"type":"http.response.body","body":b"data: started\n\n","more_body":True})
        if profile=="post_complete":
            while not (root/"analysis_saved.json").exists():
                await asyncio.sleep(.01)
            await send({"type":"http.response.body","body":b"data: done\n\n","more_body":False})
            mark("stream_finished")
            return
        while True:
            if (await receive())["type"]=="http.disconnect":
                mark("client_disconnected")
                mark("stream_finished")
                return

    uvicorn.run(app,uds=str(root/"server.sock"),loop="uvloop",http="h11",ws="none",lifespan="off",log_level="warning",timeout_graceful_shutdown=None)


if __name__=="__main__":
    main(sys.argv[1:])
"""


def package_manifest():
    return [{"path": path, "size": size, "sha256": digest} for path, size, digest in PACKAGE_FILES]


def bundle_identity():
    return sha256(
        encoded(
            {
                "schema": 1,
                "base_environment_sha256": OLD_ENV_SHA256,
                "packages": package_manifest(),
                "fixture_sha256": sha256(FIXTURE_SOURCE.encode()),
                "python_minor": "3.12",
                "versions": VERSIONS,
            }
        )
    )


def descriptor(request, *, nonce=None):
    identity = bindings(request)
    token = secrets.token_hex(16) if nonce is None else nonce
    if not isinstance(token, str) or not NONCE.fullmatch(token):
        raise RetirementProbeError("retirement_nonce_rejected")
    bundle = bundle_identity()
    bound = sha256(encoded(identity))
    immutable = BUNDLE_ROOT / bundle
    cases = []
    for case_id, profile, signal_name, expectation in CASES:
        unit_base = "prism-retirement-probe-" + token + "-" + case_id
        runtime = RUNTIME_ROOT / unit_base
        properties = {
            "Type": "exec",
            "Slice": "system.slice",
            "DynamicUser": "yes",
            "RuntimeDirectory": unit_base,
            "RuntimeDirectoryMode": "0700",
            "RuntimeDirectoryPreserve": "yes",
            "WorkingDirectory": str(runtime),
            "PrivateNetwork": "yes",
            "PrivateTmp": "yes",
            "ProtectHome": "yes",
            "ProtectSystem": "strict",
            "ProtectProc": "invisible",
            "ProcSubset": "pid",
            "NoNewPrivileges": "yes",
            "RestrictSUIDSGID": "yes",
            "RestrictNamespaces": "yes",
            "PrivateDevices": "yes",
            "ProtectControlGroups": "yes",
            "ProtectKernelTunables": "yes",
            "ProtectKernelModules": "yes",
            "ProtectKernelLogs": "yes",
            "ProtectClock": "yes",
            "LockPersonality": "yes",
            "RestrictRealtime": "yes",
            "RestrictAddressFamilies": "AF_UNIX",
            "IPAddressDeny": "any",
            "ReadOnlyPaths": str(immutable),
            "ReadWritePaths": str(runtime),
            # Mask existing ancestors as well as future children; optional '-'
            # paths would expose a later-created production directory.
            "InaccessiblePaths": "/home /root /var/lib /etc /opt/prism/releases",
            "NoExecPaths": "/run /tmp /var/tmp /home /root",
            "UMask": "0077",
            "CapabilityBoundingSet": "",
            "AmbientCapabilities": "",
            "MemoryMax": "256M",
            "CPUQuota": "50%",
            "TasksMax": "32",
            "LimitNOFILE": "128",
            "Restart": "no",
            "KillMode": "process",
            "KillSignal": signal_name,
            "SendSIGKILL": "no",
            "SendSIGHUP": "no",
            "TimeoutStopSec": "infinity",
            "RuntimeMaxSec": "infinity",
            "StandardOutput": "null",
            "StandardError": "null",
        }
        command = [
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8",
            "HOME=" + str(runtime),
            "TMPDIR=" + str(runtime),
            "/usr/bin/python3",
            "-I",
            "-S",
            "-B",
            str(immutable / "fixture.py"),
            "serve",
            profile,
            token,
            bound,
            bundle,
        ]
        cases.append(
            {
                "case_id": case_id,
                "profile": profile,
                "expectation": expectation,
                "unit": unit_base + ".service",
                "runtime_directory": str(runtime),
                "socket_path": str(runtime / "server.sock"),
                "control_group": "/system.slice/" + unit_base + ".service",
                "signal": signal_name,
                "properties": properties,
                "command": command,
                "start_phase_marker": {
                    "post_complete": "post_started",
                    "nested_thread": "nested_started",
                    "async_task": "async_started",
                    "daemon_thread": "daemon_started",
                    "orphan_child": "child_started",
                }.get(profile, "executor_started"),
                "stop_once_only": True,
                "release_gate_after_signal": "release-work",
                "observe_seconds": 5,
                "natural_cleanup_seconds": 25,
            }
        )
    return {
        "schema": 1,
        "kind": "isolated_synthetic_retirement_validation",
        "bindings": identity,
        "binding_sha256": bound,
        "nonce": token,
        "bundle_sha256": bundle,
        "source_root": str(SOURCE_ROOT),
        "bundle_directory": str(immutable),
        "bundle_files": package_manifest(),
        "fixture_sha256": sha256(FIXTURE_SOURCE.encode()),
        "target_owner_uid": 0,
        "target_file_mode": "0444",
        "target_directory_mode": "0555",
        "cases": cases,
        "production_quiescence_proven": False,
        "root_parent_required": True,
        "actual_linux_acceptance": False,
    }


def _case(plan, case_id):
    expected = descriptor(plan["bindings"], nonce=plan["nonce"])
    if plan != expected:
        raise RetirementProbeError("retirement_descriptor_changed")
    for case in expected["cases"]:
        if case["case_id"] == case_id:
            return case
    raise RetirementProbeError("retirement_case_rejected")


def _read_regular(path, maximum):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise RetirementProbeError("retirement_file_open_rejected") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise RetirementProbeError("retirement_regular_file_required")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(maximum + 1)
        after = os.fstat(fd)

        def identify(s):
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)

        if len(raw) > maximum or identify(before) != identify(after):
            raise RetirementProbeError("retirement_file_changed")
        return raw, before
    finally:
        os.close(fd)


def verify_bundle(plan):
    _require_linux()
    _case(plan, CASES[0][0])
    root = Path(plan["bundle_directory"])
    for entry in (root, *root.parents):
        info = entry.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RetirementProbeError("retirement_bundle_ancestor_rejected")
    expected = {"vendor/" + p: (size, digest) for p, size, digest in PACKAGE_FILES}
    expected["fixture.py"] = (len(FIXTURE_SOURCE.encode()), sha256(FIXTURE_SOURCE.encode()))
    actual = set()
    for directory, folders, files in os.walk(root, followlinks=False):
        path = Path(directory)
        for entry in [path] + [path / name for name in folders]:
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o555:
                raise RetirementProbeError("retirement_bundle_directory_rejected")
        for name in files:
            candidate = path / name
            relative = candidate.relative_to(root).as_posix()
            if relative not in expected:
                raise RetirementProbeError("retirement_unexpected_bundle_file")
            size, digest = expected[relative]
            raw, info = _read_regular(candidate, 32 * 1024 * 1024)
            if (
                info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o444
                or len(raw) != size
                or sha256(raw) != digest
            ):
                raise RetirementProbeError("retirement_bundle_content_rejected")
            actual.add(relative)
    if actual != set(expected):
        raise RetirementProbeError("retirement_bundle_incomplete")
    return True


def _require_linux():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RetirementProbeError("retirement_linux_required")


def recursive_cgroup_state(plan, case_id, *, stopped=False, previously_observed=False):
    """Read real cgroup v2 files, never argv/environment or fabricated PID lists.

    Missing groups count as empty only after a previously observed fixed group
    and a root-parent-confirmed stopped invocation. The caller must also bind
    actual systemd exit identity; absence alone cannot establish clean exit.
    """
    _require_linux()
    case = _case(plan, case_id)
    base = CGROUP_ROOT / case["control_group"].lstrip("/")
    if not (CGROUP_ROOT / "cgroup.controllers").is_file():
        raise RetirementProbeError("retirement_cgroup_v2_required")
    if not base.exists():
        accepted = stopped is True and previously_observed is True
        return {"observed": accepted, "exists": False, "empty": accepted, "member_count": 0, "members": []}
    members = set()
    for directory, folders, _ in os.walk(base, followlinks=False):
        path = Path(directory)
        for entry in [path] + [path / name for name in folders]:
            if entry.is_symlink() or not entry.is_dir():
                raise RetirementProbeError("retirement_cgroup_link_rejected")
        raw, _ = _read_regular(path / "cgroup.procs", 256 * 1024)
        for token in raw.split():
            if not re.fullmatch(rb"[1-9][0-9]{0,9}", token):
                raise RetirementProbeError("retirement_cgroup_member_rejected")
            members.add(int(token))
    return {
        "observed": True,
        "exists": True,
        "empty": not members,
        "member_count": len(members),
        "members": sorted(members),
    }


def linux_process_identity(plan, case_id, pid):
    """Root parent captures live fixture identity and actual namespace separation."""
    _require_linux()
    case = _case(plan, case_id)
    if type(pid) is not int or pid <= 1:
        raise RetirementProbeError("retirement_pid_rejected")
    state = recursive_cgroup_state(plan, case_id)
    if pid not in state["members"]:
        raise RetirementProbeError("retirement_process_outside_fixture")
    proc = Path("/proc") / str(pid)
    raw, _ = _read_regular(proc / "stat", 8192)
    start_ticks = _start_ticks(raw, pid)
    cgroups, _ = _read_regular(proc / "cgroup", 8192)
    membership = ("0::" + case["control_group"]).encode() in cgroups.splitlines()
    forbidden = {0}
    for user in FORBIDDEN_USERS:
        with suppress(KeyError):
            forbidden.add(pwd.getpwnam(user).pw_uid)
    uid = proc.stat().st_uid
    network_private = os.stat(proc / "ns/net").st_ino != os.stat("/proc/self/ns/net").st_ino
    again, _ = _read_regular(proc / "stat", 8192)
    if _start_ticks(again, pid) != start_ticks:
        raise RetirementProbeError("retirement_process_changed")
    return {
        "pid": pid,
        "start_ticks": start_ticks,
        "uid_isolated": uid not in forbidden,
        "private_network": network_private,
        "cgroup_bound": membership and pid in state["members"],
        "cgroup": state,
    }


def _start_ticks(raw, pid):
    try:
        ticks = int(raw.rsplit(b")", 1)[1].split()[19])
        if raw.split(b" ", 1)[0] != str(pid).encode() or ticks <= 0:
            raise ValueError()
        return ticks
    except (IndexError, ValueError):
        raise RetirementProbeError("retirement_process_stat_rejected") from None


def read_markers(plan, case_id):
    """Return only fixed marker booleans after binding their synthetic identities."""
    case = _case(plan, case_id)
    root = Path(case["runtime_directory"])
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise RetirementProbeError("retirement_workspace_rejected")
    owner = root.stat().st_uid
    forbidden = {0}
    for user in FORBIDDEN_USERS:
        with suppress(KeyError):
            forbidden.add(pwd.getpwnam(user).pw_uid)
    if owner in forbidden:
        raise RetirementProbeError("retirement_workspace_owner_rejected")
    allowed = {
        "fixture_ready",
        "executor_started",
        "analysis_saved",
        "post_started",
        "post_saved",
        "executor_finished",
        "stream_finished",
        "client_disconnected",
        "nested_started",
        "nested_saved",
        "nested_joined",
        "child_started",
        "child_saved",
        "child_finished",
        "async_started",
        "async_saved",
        "async_cancelled",
        "daemon_started",
        "daemon_saved",
        "fixture_gate_expired",
    }
    result = {name: False for name in allowed}
    for name in allowed:
        path = root / (name + ".json")
        if path.is_symlink():
            raise RetirementProbeError("retirement_marker_link_rejected")
        if not path.exists():
            continue
        raw, info = _read_regular(path, 4096)
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != owner:
            raise RetirementProbeError("retirement_marker_mode_rejected")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RetirementProbeError("retirement_marker_json_rejected") from None
        if (
            not isinstance(value, dict)
            or value.get("nonce") != plan["nonce"]
            or value.get("binding_sha256") != plan["binding_sha256"]
            or value.get("case_profile") != case["profile"]
            or type(value.get("at_ns")) is not int
            or value["at_ns"] <= 0
        ):
            raise RetirementProbeError("retirement_marker_identity_rejected")
        if name == "fixture_ready" and (
            value.get("uid_nonroot") is not True or value.get("versions_bound") is not True
        ):
            raise RetirementProbeError("retirement_fixture_proof_rejected")
        result[name] = True
    return result


def evaluate_case(plan, case_id, observation):
    """Evaluate root-collected facts; no user-supplied observation is an authority.

    A passed counterexample means the validator correctly witnessed unsafe
    behavior; it never authorizes retiring production. Unit-test observations
    must not be persisted as real Linux receipts by the root controller.
    """
    case = _case(plan, case_id)
    required = {
        "linux",
        "source_bound",
        "uid_isolated",
        "private_network",
        "cgroup_bound",
        "same_invocation",
        "exit_after_signal",
        "inactive",
        "result_success",
        "exec_main_code",
        "exec_main_status",
        "signals_sent",
        "deadline_expired",
        "cgroup_observed",
        "cgroup_empty",
        "cgroup_member_count",
        "cleanup_cgroup_empty",
        "markers",
        "markers_before_signal",
        "gate_closed_before_signal",
    }
    if type(observation) is not dict or set(observation) != required:
        raise RetirementProbeError("retirement_observation_rejected")
    numbers = {"exec_main_code", "exec_main_status", "signals_sent", "cgroup_member_count"}
    for key in required - numbers - {"markers", "markers_before_signal"}:
        if type(observation[key]) is not bool:
            raise RetirementProbeError("retirement_boolean_required")
    for key in numbers:
        if type(observation[key]) is not int:
            raise RetirementProbeError("retirement_integer_required")
    if observation["cgroup_member_count"] < 0 or observation["cgroup_empty"] != (
        observation["cgroup_member_count"] == 0
    ):
        raise RetirementProbeError("retirement_cgroup_count_rejected")
    markers = observation["markers"]
    if type(markers) is not dict or any(type(v) is not bool for v in markers.values()):
        raise RetirementProbeError("retirement_markers_rejected")
    before = observation["markers_before_signal"]
    if type(before) is not dict or any(type(v) is not bool for v in before.values()):
        raise RetirementProbeError("retirement_markers_rejected")
    preconditions = all(
        observation[k]
        for k in (
            "linux",
            "source_bound",
            "uid_isolated",
            "private_network",
            "cgroup_bound",
            "same_invocation",
            "exit_after_signal",
            "inactive",
            "cgroup_observed",
            "cleanup_cgroup_empty",
        )
    )
    preconditions &= observation["signals_sent"] == 1 and not observation["deadline_expired"]
    preconditions &= markers.get("fixture_ready") is True and markers.get("stream_finished") is True
    preconditions &= not markers.get("fixture_gate_expired", False)
    unfinished_marker = {
        "async_task": "async_saved",
        "daemon_thread": "daemon_saved",
        "orphan_child": "child_finished",
    }.get(case["profile"], "post_saved")
    preconditions &= (
        observation["gate_closed_before_signal"]
        and before.get("fixture_ready") is True
        and before.get("stream_finished") is True
        and before.get(case["start_phase_marker"]) is True
        and not before.get(unfinished_marker, False)
        and not before.get("fixture_gate_expired", False)
    )
    clean = (
        observation["exec_main_code"] == 1
        and observation["exec_main_status"] == 0
        and observation["result_success"]
    )
    complete = all(markers.get(k) is True for k in ("analysis_saved", "post_saved", "executor_finished"))
    expectation = case["expectation"]
    if expectation == "executor_preserved":
        matched = clean and observation["cgroup_empty"] and complete
        if case["profile"] == "nested_thread":
            matched &= markers.get("nested_saved") is True and markers.get("nested_joined") is True
    elif expectation == "async_cancel_counterexample":
        matched = (
            clean
            and observation["cgroup_empty"]
            and markers.get("async_cancelled") is True
            and not markers.get("async_saved", False)
        )
    elif expectation == "daemon_counterexample":
        matched = (
            clean
            and observation["cgroup_empty"]
            and markers.get("daemon_started") is True
            and not markers.get("daemon_saved", False)
        )
    elif expectation == "child_counterexample":
        matched = (
            clean
            and observation["cgroup_member_count"] > 0
            and markers.get("child_started") is True
            and markers.get("child_finished") is True
        )
    else:
        matched = (
            observation["exec_main_code"] == 2 and observation["exec_main_status"] == 15 and not complete
        )
    passed = bool(preconditions and matched)
    return {
        "case_id": case_id,
        "passed": passed,
        "clean_exit_zero": bool(clean),
        "executor_results_preserved": bool(complete),
        "expected_counterexample": expectation != "executor_preserved",
        "production_quiescence_proven": False,
        "task_census_still_required": True,
    }


def summarize(plan, results):
    _case(plan, CASES[0][0])
    if (
        not isinstance(results, list)
        or len(results) != len(CASES)
        or any(type(r) is not dict or type(r.get("case_id")) is not str for r in results)
        or {r["case_id"] for r in results} != {c[0] for c in CASES}
    ):
        raise RetirementProbeError("retirement_matrix_incomplete")
    # Only exact evaluator outputs are accepted; no arbitrary keys or strings
    # from stdout/stderr/exception messages reach this public safe result.
    keys = {
        "case_id",
        "passed",
        "clean_exit_zero",
        "executor_results_preserved",
        "expected_counterexample",
        "production_quiescence_proven",
        "task_census_still_required",
    }
    if any(
        type(r) is not dict
        or set(r) != keys
        or any(type(v) is not bool for k, v in r.items() if k != "case_id")
        or r["production_quiescence_proven"]
        or not r["task_census_still_required"]
        for r in results
    ):
        raise RetirementProbeError("retirement_result_rejected")
    return {
        "schema": 1,
        "bindings": plan["bindings"],
        "bundle_sha256": plan["bundle_sha256"],
        "retirement_validation_passed": all(r["passed"] for r in results),
        "cases": results,
        "production_quiescence_proven": False,
        "task_census_still_required": True,
        "actual_linux_acceptance_requires_root_collection": True,
    }

"""Finite PDF text extraction in an owned, no-credential Python child.

The caller retains its auxiliary work lease until the child has exited and is
reaped. This is a document parser, never a CLI/model Agent or shell executor.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 32768
_WORKER = r"""
import io,json,resource,socket,sys
resource.setrlimit(resource.RLIMIT_CPU,(2,2))
resource.setrlimit(resource.RLIMIT_FSIZE,(0,0))
resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
if sys.platform.startswith('linux'):
    resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
def denied(*args,**kwargs): raise ValueError('network forbidden')
socket.socket.connect=denied
socket.socket.connect_ex=denied
sys.path.insert(0,sys.argv[1])
try:
    from pypdf import PdfReader
    raw=sys.stdin.buffer.read(8*1024*1024+1)
    if len(raw)>8*1024*1024 or not raw.startswith(b'%PDF-'): raise ValueError()
    reader=PdfReader(io.BytesIO(raw),strict=True,root_object_recovery_limit=2000,xref_stream_maximum_entries=50000)
    if reader.is_encrypted:
        print(json.dumps({'status':'pdf_encrypted'}));raise SystemExit(0)
    total=len(reader.pages)
    if total>2000: raise ValueError()
    first,count=int(sys.argv[2]),int(sys.argv[3])
    if first>total:
        print(json.dumps({'status':'pdf_page_out_of_range','page_count':total}));raise SystemExit(0)
    parts=[];size=0
    for number in range(first-1,min(total,first-1+count)):
        text=reader.pages[number].extract_text() or ''
        remaining=32768-size
        parts.append({'page':number+1,'text':text[:remaining]})
        size+=len(parts[-1]['text'])
        if size>=32768:break
    value={'status':'ok' if any(p['text'].strip() for p in parts) else 'pdf_no_text',
           'pages':parts,'page_count':total,'truncated':first>1 or first+len(parts)-1<total or size>=32768}
    print(json.dumps(value,ensure_ascii=True))
except Exception:
    print(json.dumps({'status':'pdf_invalid'}))
"""


def extract_pdf(raw, *, timeout_seconds=5, first_page=1, page_count=10):
    if (
        type(raw) is not bytes
        or not 0 < len(raw) <= MAX_PDF_BYTES
        or type(first_page) is not int
        or not 1 <= first_page <= 2000
        or type(page_count) is not int
        or not 1 <= page_count <= 20
        or type(timeout_seconds) not in (int, float)
        or not 0 < timeout_seconds <= 5
    ):
        return {"status": "pdf_invalid"}
    spec = importlib.util.find_spec("pypdf")
    if spec is None or spec.origin is None:
        return {"status": "pdf_unavailable"}
    packages = str(Path(spec.origin).resolve().parent.parent)
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-S", "-c", _WORKER, packages, str(first_page), str(page_count)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        close_fds=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(raw, timeout=timeout_seconds)
        if process.returncode != 0 or len(output) > 256 * 1024:
            return {"status": "pdf_invalid"}
        value = json.loads(output)
        if type(value) is not dict or value.get("status") not in {
            "ok",
            "pdf_invalid",
            "pdf_encrypted",
            "pdf_no_text",
            "pdf_page_out_of_range",
        }:
            return {"status": "pdf_invalid"}
        return value
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return {"status": "pdf_timeout"}
    except (OSError, ValueError):
        return {"status": "pdf_invalid"}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()

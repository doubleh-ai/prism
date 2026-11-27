"""Exercise the shipped public site configuration with a synthetic upstream."""

import http.client
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_public_admin_boundary():
    nginx = shutil.which("nginx")
    if not nginx:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("nginx is required for the ingress regression")
        pytest.skip("nginx is unavailable")
    forwarded = []

    class Upstream(socketserver.StreamRequestHandler):
        def handle(self):
            method, path, _ = self.rfile.readline().decode().split()
            forwarded.append((method, path))
            status = (
                "401 Unauthorized" if path == "/api/admin" or path.startswith("/api/admin/") else "200 OK"
            )
            self.wfile.write(f"HTTP/1.0 {status}\r\nContent-Length: 0\r\n\r\n".encode())

    # Short paths keep Unix listeners portable on macOS and Linux.
    with (
        tempfile.TemporaryDirectory(dir="/tmp") as tmp,
        socketserver.TCPServer(("127.0.0.1", 0), Upstream) as upstream,
    ):
        root = Path(tmp)
        sock = root / "edge.sock"
        port = upstream.server_address[1]
        site = (CONFIG / "nginx-site.conf").read_text()
        site = site.replace("listen 80;", f"listen unix:{sock};").replace(
            "127.0.0.1:8088", f"127.0.0.1:{port}"
        )
        (root / "snippets").symlink_to(CONFIG / "snippets")
        (root / "nginx.conf").write_text(
            "daemon off; master_process off; pid nginx.pid; error_log stderr; events {}\n"
            "http { access_log off; client_body_temp_path body; proxy_temp_path proxy; "
            f"fastcgi_temp_path fastcgi; uwsgi_temp_path uwsgi; scgi_temp_path scgi; {site} }}"
        )
        server = threading.Thread(target=upstream.serve_forever)
        server.start()
        try:
            with subprocess.Popen([nginx, "-e", "stderr", "-p", tmp, "-c", str(root / "nginx.conf")]) as edge:
                try:
                    for _ in range(100):
                        if sock.exists() or edge.poll() is not None:
                            break
                        time.sleep(0.02)
                    assert edge.poll() is None and sock.exists()

                    def request(path, method="GET"):
                        return subprocess.check_output(
                            [
                                "curl",
                                "-q",
                                "-sS",
                                "--noproxy",
                                "*",
                                "--unix-socket",
                                str(sock),
                                "--max-time",
                                "3",
                                "-X",
                                method,
                                "-o",
                                os.devnull,
                                "-w",
                                "%{http_code}",
                                "http://localhost" + path,
                            ],
                            text=True,
                        )

                    for path in ("/api/admin", "/api/admin/", "/api/admin/health?view=1"):
                        assert request(path) == "404"
                    assert request("/api/admin/users", "POST") == "404"
                    assert forwarded == []
                    for path in ("/login", "/atlas/", "/api/atlas/health", "/api/administrator"):
                        assert request(path) == "200"
                    # The destination of the SSH tunnel still requires application authentication.
                    client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    try:
                        client.request("GET", "/api/admin/health")
                        assert client.getresponse().status == 401
                    finally:
                        client.close()
                finally:
                    edge.terminate()
        finally:
            upstream.shutdown()
            server.join()

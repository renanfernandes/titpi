#!/usr/bin/env python3
"""Preview the local dashboard using read-only data from an existing TitPi."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="http://10.0.0.80:8080")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    upstream = urlsplit(args.upstream)
    if upstream.scheme not in ("http", "https") or not upstream.netloc:
        parser.error("--upstream must be an HTTP or HTTPS origin")
    if upstream.username or upstream.password or upstream.path not in ("", "/") or upstream.query or upstream.fragment:
        parser.error("--upstream must not include credentials, paths, or query parameters")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    origin = args.upstream.rstrip("/")
    template = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"

    class PreviewHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/dashboard.html"):
                content = template.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
                return
            if not path.startswith(("/api/", "/photo/", "/video/")):
                self.send_error(404)
                return
            try:
                request = Request(origin + self.path)
                with urlopen(request, timeout=15) as response:
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.headers.get("Content-Type", "application/octet-stream"))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while chunk := response.read(65536):
                        self.wfile.write(chunk)
            except HTTPError as error:
                self.send_error(error.code, "Upstream request failed")
            except (URLError, TimeoutError):
                self.send_error(502, "TitPi is unavailable")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            self.send_error(405, "Read-only preview: changes are disabled")

        do_PATCH = do_POST
        do_DELETE = do_POST

        def log_message(self, format, *values):
            pass

    try:
        with ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler) as server:
            print(f"Read-only dashboard preview: http://127.0.0.1:{args.port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("Preview stopped.", flush=True)
    except OSError as error:
        parser.exit(1, f"Could not start preview: {error.strerror}\n")


if __name__ == "__main__":
    main()
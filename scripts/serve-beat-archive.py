#!/usr/bin/env python3
# serve-beat-archive.py
# Read-only HTTP file server for beat-archive/, so send-*-docs.py scripts can
# link to video files too large to email as attachments (see file_url() in
# those scripts and VIDEO_LINK_BASE_URL below).
#
# Serves only files that already exist under beat-archive/ by exact relative
# path — no directory listing, no path traversal outside the root.
#
# Usage:
#   python3 scripts/serve-beat-archive.py [--port 8843]
#
# For persistence, install as a systemd user service — see
# scripts/beat-archive-server.service and the setup notes below it.

import argparse
import http.server
import os
import socketserver
import sys
import urllib.parse

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT     = os.path.join(REPO_DIR, "beat-archive")


class BeatArchiveHandler(http.server.BaseHTTPRequestHandler):
    server_version = "BeatArchiveServer/1.0"

    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)

    def _serve(self, send_body):
        rel = urllib.parse.unquote(self.path.lstrip("/").split("?", 1)[0])
        fpath = os.path.realpath(os.path.join(ROOT, rel))

        if os.path.commonpath([fpath, ROOT]) != ROOT:
            return self._error(403, "Forbidden")
        if not os.path.isfile(fpath):
            return self._error(404, "Not found")

        ctype, _ = {
            ".mp4": ("video/mp4", None), ".mkv": ("video/x-matroska", None),
            ".webm": ("video/webm", None), ".m4a": ("audio/mp4", None),
            ".mp3": ("audio/mpeg", None), ".mov": ("video/quicktime", None),
            ".pdf": ("application/pdf", None),
        }.get(os.path.splitext(fpath)[1].lower(), ("application/octet-stream", None))

        size = os.path.getsize(fpath)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'inline; filename="{os.path.basename(fpath)}"')
        self.end_headers()

        if send_body:
            with open(fpath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    try:
                        self.wfile.write(chunk)
                    except BrokenPipeError:
                        break

    def _error(self, code, message):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8843)
    args = parser.parse_args()

    if not os.path.isdir(ROOT):
        sys.exit(f"ERROR: {ROOT} does not exist")

    server = ThreadingHTTPServer(("0.0.0.0", args.port), BeatArchiveHandler)
    print(f"Serving {ROOT} on 0.0.0.0:{args.port} (read-only, no directory listing)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

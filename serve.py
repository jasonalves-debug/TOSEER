#!/usr/bin/env python3
"""
Tiny static file server for the Toronto Live Emergency Map.
Serves this directory (index.html + data/incidents.json) with CORS open,
so the page works whether you open it via this server's address or embed
it somewhere else later.

No dependencies — stdlib only.

Usage:
    python3 serve.py [port]      # default port 8080

Leave it running (see README.md for keeping it up persistently via
systemd or `screen`/`tmux`).
"""

import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
os.chdir(os.path.dirname(os.path.abspath(__file__)))


class CORSRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        # quieter default logging — comment this out if you want full access logs
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), CORSRequestHandler)
    print(f"Serving {os.getcwd()}")
    print(f"  Open this in your browser: http://localhost:{PORT}/")
    print(f"  (0.0.0.0 above just means it's listening on all network interfaces —")
    print(f"   that's for other devices on your LAN; don't put 0.0.0.0 in the browser itself)")
    print(f"  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

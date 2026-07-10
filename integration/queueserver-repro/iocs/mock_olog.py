#!/usr/bin/env python3
"""Minimal mock Olog server for the IOS queueserver demo.

The IOS profile's startup subscribes an Olog logbook callback to the RunEngine
(nslsii.configure_olog), which POSTs a logbook entry to the Olog server on every
run start. Production runs a real Olog service (deployed by the NSLS-II
ansible-epics-tools roles); without one, the callback's HTTP call fails and the
run cannot complete. This server implements just enough of the pyOlog REST API
(list logbooks/tags/properties, create logbook, post log entry + attachment) to
let those calls succeed, so plans run end to end.

It stores nothing; every entry is accepted and assigned an incrementing id.
Point the client at it with a ~/.pyOlog.conf whose `url` is this server's
address, e.g. url = http://localhost:8181

Usage:  python mock_olog.py [--port 8181] [--host 127.0.0.1]
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The logbook the IOS profile's callback writes to (LOGBOOKS in 00-startup.py).
DEFAULT_LOGBOOKS = ["Data Acquisition", "test"]


def _logbook(name):
    return {"name": name, "owner": "olog"}


def _log_entry(entry_id, logbooks):
    now_ms = int(time.time() * 1000)
    return {
        "id": entry_id,
        "description": "",
        "owner": "olog",
        "level": "Info",
        "createdDate": now_ms,
        "modifiedDate": now_ms,
        "logbooks": [_logbook(n) for n in logbooks] or [_logbook("Data Acquisition")],
        "tags": [],
        "properties": [],
    }


class Handler(BaseHTTPRequestHandler):
    _next_id = 1

    def log_message(self, *args):  # quiet
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/resources/logbooks"):
            self._send({"logbook": [_logbook(n) for n in DEFAULT_LOGBOOKS]})
        elif self.path.startswith("/resources/tags"):
            self._send({"tag": []})
        elif self.path.startswith("/resources/properties"):
            self._send({"property": []})
        else:
            self._send({})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        self._read_body()
        if self.path.startswith("/resources/logs"):
            entry_id = Handler._next_id
            Handler._next_id += 1
            # Newer pyOlog reads resp.json()['log'][0]; older reads [0]. Return
            # the newer shape (this client uses it).
            self._send({"log": [_log_entry(entry_id, DEFAULT_LOGBOOKS)]})
        elif self.path.startswith("/resources/attachments"):
            self._send({})
        else:
            self._send({})

    def do_PUT(self):
        self._read_body()
        # createLogbook / createTag / createProperty — echo a plausible object.
        if self.path.startswith("/resources/logbooks"):
            name = self.path.rsplit("/", 1)[-1] or "Data Acquisition"
            self._send(_logbook(name))
        else:
            self._send({})


def main():
    ap = argparse.ArgumentParser(description="Mock Olog server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8181)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock-olog listening on http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

"""A stand-in for the event gateway the reconciler reports to (`VECTOR_BASE_URL`).

The real one is Vector's HTTP source in front of a message bus. This accepts the same request
and writes one line per event to stdout, so a deployment can run end to end, and be read from
its logs, before any of that exists:

    POST /sensors/<app>   {"payload": {"event_name": ..., "user_id": ..., ...}}   -> 200

Unlike Vector it answers 400 to a body it cannot use, which is the more useful behaviour for a
test double. Standard library only. Listens on `$PORT` (default 8080).
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 1024 * 1024
PREFIX = "/sensors/"


class Handler(BaseHTTPRequestHandler):
    server_version = "gateway-stub/1"

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/healthz"):
            self._answer(200, {"status": "ok"})
        else:
            self._answer(404, {"error": "not found"})

    def do_POST(self):
        app = self.path[len(PREFIX):] if self.path.startswith(PREFIX) else ""
        if not app or "/" in app:
            return self._answer(404, {"error": "POST /sensors/<app>"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._answer(400, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            return self._answer(413, {"error": "body too large"})
        try:
            payload = json.loads(self.rfile.read(length))["payload"]
            if not isinstance(payload, dict):
                raise TypeError("payload is not an object")
        except (ValueError, KeyError, TypeError) as e:
            return self._answer(400, {"error": f"expected {{\"payload\": {{...}}}}: {e}"})
        print(json.dumps({"app": app, **payload}, sort_keys=True), flush=True)
        self._answer(200, {"ok": True})

    def log_message(self, format, *args):
        # The event line above is the log. Health checks would otherwise drown it.
        pass


def serve(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> int:
    port = int(os.environ.get("PORT", "8080"))
    server = serve(port)
    print(f"gateway stub listening on :{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

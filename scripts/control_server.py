"""HTTP for the operator control surface. Deliberately the thinnest layer that could work.

`operations/control.py` is transport-free and holds every decision: what is authorized, what is
asymmetric, what a refusal says. This file decodes a request and encodes a response. Keeping it
this thin is the point -- there is nothing here worth testing, so the tests live where the rules
are and nothing important hides in a request handler.

**Why this is a separate process from the dashboard.** `dashboard.service` serves generated HTML
and holds no credentials, which is what makes a file server on the LAN a reasonable thing to run.
A control surface cannot have that property: acting on the kill switch means reaching the ledger
and the broker. So the credentialed listener is confined to *this* -- two POST endpoints and one
small page, no file serving, no path traversal surface, no directory listing. The large attack
surface stays credential-free and the credentialed surface stays small.

It also serves its own control page, rather than adding buttons to the dashboard. That avoids a
cross-origin POST between two ports and, more usefully, keeps the page that can stop trading a
place you navigate to deliberately rather than a button beside a chart.

Usage:
    QUANTBOT_CONTROL_TOKEN=... uv run python scripts/control_server.py [PORT]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantbot.operations.control import (  # noqa: E402
    CLEAR,
    ENGAGE,
    ControlRefused,
    OperatorControl,
)
from quantbot.operations.kill_switch import KillSwitchController  # noqa: E402
from quantbot.operations.readiness import measure_readiness  # noqa: E402
from quantbot.storage import Database  # noqa: E402

TOKEN_ENV = "QUANTBOT_CONTROL_TOKEN"
TOKEN_HEADER = "X-Elrond-Token"

#: Bodies are two short fields. Anything larger is not a control request, and reading an
#: unbounded body from an unauthenticated caller is how a small server becomes a memory problem.
MAX_BODY_BYTES = 4096

PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Elrond control</title>
<style>
 body{font:15px/1.5 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;
      background:#12151a;color:#e6e6e6}
 h1{font-size:1.2rem} label{display:block;margin:1rem 0 .3rem}
 input{width:100%;padding:.5rem;background:#1b1f26;color:#e6e6e6;border:1px solid #2c323c;
       border-radius:4px;font:inherit}
 button{padding:.6rem 1rem;margin:1rem .5rem 0 0;border:0;border-radius:4px;font:inherit;
        cursor:pointer}
 .stop{background:#7f1d1d;color:#fff} .go{background:#1f3a2e;color:#cfe9dc}
 pre{background:#1b1f26;padding:1rem;border-radius:4px;white-space:pre-wrap;margin-top:1.5rem}
 .note{color:#9aa4b2;font-size:.85rem}
</style>
<h1>Elrond kill switch</h1>
<p class="note">Engaging always works. Clearing is verified against the broker, the ledger and
data freshness, and refuses if the conditions do not hold.</p>
<label>Control token</label><input id="tok" type="password" autocomplete="off">
<label>Reason</label><input id="why" placeholder="why are you doing this">
<button class="stop" onclick="go('engage')">Engage (stop trading)</button>
<button class="go" onclick="go('clear')">Clear (resume)</button>
<pre id="out">ready</pre>
<script>
async function go(action){
  const out = document.getElementById('out');
  out.textContent = 'working...';
  try{
    const r = await fetch('/kill-switch/' + action, {
      method:'POST',
      headers:{'Content-Type':'application/json','X-Elrond-Token':document.getElementById('tok').value},
      body: JSON.stringify({reason: document.getElementById('why').value})
    });
    out.textContent = r.status + '\\n' + JSON.stringify(await r.json(), null, 2);
  }catch(e){ out.textContent = 'request failed: ' + e; }
}
</script>
"""


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "elrond-control/1"
    control: OperatorControl

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path != "/":
            self._send(404, {"error": "not found"})
            return
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The page holds no state and must not be cached into staleness.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        actions = {"/kill-switch/engage": ENGAGE, "/kill-switch/clear": CLEAR}
        action = actions.get(self.path)
        if action is None:
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "bad request"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "body too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            reason = str(payload.get("reason", ""))
        except (ValueError, TypeError):
            self._send(400, {"error": "bad request"})
            return

        try:
            result = asyncio.run(
                self.control.handle(
                    action, reason=reason, presented_token=self.headers.get(TOKEN_HEADER)
                )
            )
        except ControlRefused as refused:
            self._send(refused.status, {"error": str(refused)})
            return
        except Exception:  # noqa: BLE001 - never leak an internal failure to the network
            # The message could carry a path, a query or a credential. The operator reads the
            # journal; the caller gets a number.
            self.log_error("control request failed", exc_info=True)
            self._send(500, {"error": "internal error"})
            return
        self._send(result.status, dict(result.payload))

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # Default logging writes the request line, and a request line is attacker-controlled.
        # The method and status are enough to see what is happening.
        sys.stderr.write(f"{self.command} -> {args[1] if len(args) > 1 else '?'}\n")


def main() -> int:
    token = os.environ.get(TOKEN_ENV, "")
    if not token.strip():
        # Refused at startup rather than serving an open control surface. A default token would
        # be the same as no token, and worse for being invisible.
        print(f"{TOKEN_ENV} is not set; refusing to expose an unguarded control surface")
        return 2
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
    database = Database(os.environ.get("QUANTBOT_DB_PATH", "quantbot.db"))

    async def measure():
        # No broker adapter here on purpose: this process should not hold trading credentials
        # just to answer a health question. `measure_readiness` reports BROKER as *unprobed*,
        # which blocks a clear and says so -- see the deployment notes, which explain how to
        # give it one when you want browser clears to be able to succeed.
        return await measure_readiness(
            database,
            None,
            paper_mode=True,
            expected_account_id=os.environ.get("EXPECTED_ACCOUNT_ID"),
            risk_caps_valid=True,
            now=datetime.now(UTC),
        )

    ControlHandler.control = OperatorControl(
        KillSwitchController(database),
        measure,
        token=token,
        clock=lambda: datetime.now(UTC),
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), ControlHandler)  # noqa: S104 - LAN by design
    print(f"control surface on :{port} (engage is unconditional; clear is verified)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

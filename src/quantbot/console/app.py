"""The operator console's read-only surface (#82 slice 2).

Server-rendered from `console.state`, which reads authoritative storage. Nothing here decides
anything: no business logic lives in a template, and there is no mutation endpoint to find,
because slice 2's job is to render the system truthfully before slice 3 lets anyone change it.

**Two audiences, one source.** `/api/...` returns the same `ConsoleState` the pages render, so a
future frontend, a script or an operator with `curl` all see identical values. A page that
computed something the API did not would be a second implementation of the truth.

**Bound to loopback by default.** #82 asks for a boundary that is not implicitly trusted merely
because it is local, and the honest first step is a listener that is not reachable from the
network at all. Widening it is a deployment decision, and the default refuses more.

**No secrets, no account identifiers.** The configuration registry redacts by type before this
module ever sees a value, and the health panel reads the account id only as a lookup key -- it is
never rendered and never serialised.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from quantbot.console.state import ConsoleState, read_console_state
from quantbot.storage import Database

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: Loopback, deliberately. See the module docstring.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

#: The pages the console offers, in navigation order. Declared once so the shell and the router
#: cannot disagree about what exists.
NAVIGATION: tuple[tuple[str, str], ...] = (
    ("/", "Operations"),
    ("/risk", "Risk"),
    ("/research", "Research"),
    ("/market-data", "Market data"),
    ("/capacity", "Capacity"),
    ("/config", "Configuration"),
)


def create_app(database: Database) -> FastAPI:
    """Build the console over one already-open store.

    The database is injected rather than opened here, for the reason #86 established: acquiring
    a connection must not be how a store gets created, and a web process that opened its own
    would be a second place that could do it at a mistyped path.
    """
    app = FastAPI(
        title="Elrond operator console",
        version="0.2",
        description=(
            "Read-only. Renders authoritative state and changes nothing. Paper trading only; "
            "no surface here can enable live trading."
        ),
    )

    def snapshot() -> ConsoleState:
        with database.transaction() as session:
            return read_console_state(session, now=datetime.now(UTC))

    def page(request: Request, template: str, active: str) -> HTMLResponse:
        state = snapshot()
        return TEMPLATES.TemplateResponse(
            request=request,
            name=template,
            context={
                "state": state,
                "navigation": NAVIGATION,
                "active": active,
                "attention": state.attention,
            },
        )

    @app.get("/api/state")
    def api_state() -> ConsoleState:
        """Everything the pages render, as JSON. The same object, not a parallel one."""
        return snapshot()

    @app.get("/", response_class=HTMLResponse)
    def operations(request: Request) -> HTMLResponse:
        return page(request, "operations.html", "/")

    @app.get("/risk", response_class=HTMLResponse)
    def risk(request: Request) -> HTMLResponse:
        return page(request, "risk.html", "/risk")

    @app.get("/research", response_class=HTMLResponse)
    def research(request: Request) -> HTMLResponse:
        return page(request, "research.html", "/research")

    @app.get("/market-data", response_class=HTMLResponse)
    def market_data(request: Request) -> HTMLResponse:
        return page(request, "market_data.html", "/market-data")

    @app.get("/capacity", response_class=HTMLResponse)
    def capacity(request: Request) -> HTMLResponse:
        return page(request, "capacity.html", "/capacity")

    @app.get("/config", response_class=HTMLResponse)
    def config(request: Request) -> HTMLResponse:
        return page(request, "config.html", "/config")

    return app


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "NAVIGATION", "create_app"]

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

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from quantbot.console.control import (
    ConfigProposal,
    ProposalRefused,
    apply_proposal,
    field_documentation,
    proposable_fields,
    rollback_to,
    validate_proposal,
)
from quantbot.console.glossary import GLOSSARY, anchor, terms_in
from quantbot.console.state import (
    ConsoleState,
    ReportsPanel,
    RuntimePanel,
    read_console_state,
)
from quantbot.operations.kill_switch import (
    KillSwitchClearBlocked,
    KillSwitchController,
    MeasuredReadiness,
)
from quantbot.risk.policy_versions import (
    PolicyVersion,
    active_policy_version,
    initial_version,
    recorded_policy_version,
)
from quantbot.storage import Database, StorageRepository

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: Loopback, deliberately. See the module docstring.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

#: The pages the console offers, in navigation order. Declared once so the shell and the router
#: cannot disagree about what exists.
NAVIGATION: tuple[tuple[str, str], ...] = (
    ("/", "Operations"),
    ("/session", "Market session"),
    ("/risk", "Risk"),
    ("/research", "Research"),
    ("/market-data", "Market data"),
    ("/capacity", "Capacity"),
    ("/discovery", "Discovery"),
    ("/config", "Configuration"),
    ("/config/policy", "Risk policy"),
    ("/reports", "Reports"),
    ("/runtime", "Runtime"),
    ("/kill-switch", "Kill switch"),
    ("/glossary", "Glossary"),
)


def create_app(
    database: Database,
    *,
    measure: Callable[[], Awaitable[MeasuredReadiness]] | None = None,
    runtime: RuntimePanel | None = None,
    reports: ReportsPanel | None = None,
    reconcile: Callable[[], Awaitable[Mapping[str, object]]] | None = None,
) -> FastAPI:
    """Build the console over one already-open store.

    The database is injected rather than opened here, for the reason #86 established: acquiring
    a connection must not be how a store gets created, and a web process that opened its own
    would be a second place that could do it at a mistyped path.

    `measure` probes the world and returns readiness. `None` when the console was started
    somewhere the broker is not configured, which makes clearing the kill switch impossible
    rather than merely unlikely -- and says so, instead of offering a control that would refuse
    for a reason nobody could read.

    `reconcile` is a **narrow callable**, not a `Runtime`. That distinction is the whole design:
    `_reconcile` in `runtime.py` takes the entire object graph -- broker, execution coordinator,
    recovery, market data -- and handing that to a web process would put the execution path
    inside it. The console's safety story is that its route table is an allowlist and order
    submission is not on it; holding a `Runtime` would make the capability *present* while no
    route used it, and "nothing calls it" is a far weaker guarantee than "this process cannot".

    So the console receives one function that does one thing, exactly as `measure` does.
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
            return read_console_state(
                session, now=datetime.now(UTC), runtime=runtime, reports=reports
            )

    def page_with_notice(request: Request, notice: str) -> HTMLResponse:
        """Operations, with something the operator has to read.

        Rendered in place rather than redirected: a redirect to a page showing unchanged state
        is indistinguishable from success, which is the same trap the policy apply path avoids.
        """
        state = snapshot()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="operations.html",
            context={
                "state": state,
                "navigation": NAVIGATION,
                "active": "/",
                "attention": state.attention,
                "notice": notice,
            },
        )

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

    @app.get("/discovery", response_class=HTMLResponse)
    def discovery(request: Request) -> HTMLResponse:
        return page(request, "discovery.html", "/discovery")

    # --- the kill switch (#82) ------------------------------------------------------------------
    #
    # Engaging is always available and needs no argument beyond a reason: stopping is never the
    # dangerous direction, and a control that made it hard would be worse than useless in the
    # moment somebody reaches for it.
    #
    # **Clearing cannot be faked from here.** `KillSwitchController.clear` accepts only a
    # `MeasuredReadiness`, and only `measure_readiness()` can mint one -- the token is private to
    # its module. So this surface cannot assert that it is safe to resume; it can only ask, and
    # relay the answer. That property predates this console and is why adding the button is safe:
    # the gate is the measurement, not the caller.
    #
    # The refusal is the useful part. A blocked clear names every condition that was unmet *and*
    # every condition nobody could probe, because an unprobed condition is not a satisfied one.

    def kill_switch_page(
        request: Request,
        *,
        readiness: MeasuredReadiness | None = None,
        refusal: str | None = None,
        blocking: tuple[str, ...] = (),
    ) -> HTMLResponse:
        state = snapshot()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="kill_switch.html",
            context={
                "state": state,
                "navigation": NAVIGATION,
                "active": "/kill-switch",
                "attention": state.attention,
                "can_measure": measure is not None,
                "readiness": readiness,
                "refusal": refusal,
                "blocking": blocking,
            },
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports_view(request: Request) -> HTMLResponse:
        """What has been written to the reports directory. A listing, not a viewer."""
        return page(request, "reports.html", "/reports")

    @app.get("/session", response_class=HTMLResponse)
    def session_view(request: Request) -> HTMLResponse:
        """What the market-hours loop has been doing, and what it deliberately cannot do (#143)."""
        return page(request, "session.html", "/session")

    @app.get("/runtime", response_class=HTMLResponse)
    def runtime_view(request: Request) -> HTMLResponse:
        """What this machine measured about itself, and what that permits (#38, #136)."""
        return page(request, "runtime.html", "/runtime")

    @app.get("/glossary", response_class=HTMLResponse)
    def glossary(request: Request) -> HTMLResponse:
        """Plain-language definitions for the jargon this console uses.

        A setting an operator cannot interpret is one they will either not touch or touch
        wrongly, and most of the vocabulary here is meaningless without an explanation nobody
        was offering.
        """
        state = snapshot()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="glossary.html",
            context={
                "state": state,
                "navigation": NAVIGATION,
                "active": "/glossary",
                "attention": state.attention,
                "glossary": GLOSSARY,
                "anchor": anchor,
            },
        )

    @app.get("/kill-switch", response_class=HTMLResponse)
    def kill_switch(request: Request) -> HTMLResponse:
        return kill_switch_page(request)

    @app.post("/kill-switch/engage", response_model=None)
    async def kill_switch_engage(request: Request) -> RedirectResponse | HTMLResponse:
        """Stop trading. Never gated, because stopping is never the dangerous direction."""
        form = await read_form(request)
        reason = str(form.get("reason", "")).strip()
        if not reason:
            return kill_switch_page(
                request,
                refusal=(
                    "a kill switch needs a reason; an engagement nobody can explain is one "
                    "nobody can safely clear later"
                ),
            )
        KillSwitchController(database).engage(reason=reason, updated_at=datetime.now(UTC))
        return RedirectResponse("/kill-switch", status_code=303)

    @app.post("/kill-switch/clear", response_model=None)
    async def kill_switch_clear(request: Request) -> RedirectResponse | HTMLResponse:
        """Resume trading, but only against readiness this process actually measured."""
        form = await read_form(request)
        reason = str(form.get("reason", "")).strip()
        if not reason:
            return kill_switch_page(
                request, refusal="clearing the kill switch needs a reason"
            )
        if measure is None:
            return kill_switch_page(
                request,
                refusal=(
                    "this console cannot measure readiness, so it cannot clear the kill switch; "
                    "run it where the broker is configured, or clear from the CLI there"
                ),
            )
        measured = await measure()
        try:
            KillSwitchController(database).clear(
                reason=reason, measured=measured, updated_at=datetime.now(UTC)
            )
        except KillSwitchClearBlocked:
            return kill_switch_page(
                request,
                readiness=measured,
                refusal="the conditions for resuming are not met",
                blocking=tuple(measured.blocking),
            )
        return RedirectResponse("/kill-switch", status_code=303)

    @app.post("/operations/reconcile", response_model=None)
    async def operations_reconcile(request: Request) -> RedirectResponse | HTMLResponse:
        """Compare the broker's ledger against ours, and record what it found.

        A safe operational action in the specific sense that matters: it submits no order,
        cancels nothing and moves no position. It reads the broker, adopts its authoritative
        fills, and records a reconciliation -- the same work the CLI does, through the same code.

        Deliberately the *first* such action. The operator's system has been stopped since a
        reconciliation failure whose underlying condition later cleared, and being able to ask
        "is it reconciled now" without a terminal is the difference between that being obvious
        and being archaeology.
        """
        if reconcile is None:
            return page_with_notice(
                request,
                "this console cannot reconcile: it was started without a broker, and a surface "
                "that cannot reach the broker must not offer to compare against it",
            )
        try:
            await reconcile()
        except Exception as error:  # noqa: BLE001 -- reported, never silently swallowed
            return page_with_notice(request, f"reconciliation failed: {error!r}"[:400])
        return RedirectResponse("/", status_code=303)

    @app.get("/config", response_class=HTMLResponse)
    def config(request: Request) -> HTMLResponse:
        return page(request, "config.html", "/config")

    # --- the one mutable surface (#82 slice 6) -------------------------------------------------
    #
    # Everything above renders and changes nothing. These four routes are the exception, and the
    # boundary test is now an allowlist naming exactly them, so a POST added anywhere else still
    # fails the suite. That is the property the blanket read-only assertion was protecting, kept
    # rather than relaxed.
    #
    # **A console apply records PROJECT_DEFAULT, never OPERATOR.** The temptation is to treat a
    # form submission as the operator acting, and for a person typing in a browser that is true.
    # But this surface cannot tell a person from a script POSTing to the same local port, so
    # recording OPERATOR would be a provenance claim it cannot substantiate -- and #72 is
    # explicit that a default must never be rendered as approval. Weigh the two errors: a false
    # OPERATOR puts a human's name on a threshold no human chose, while a false PROJECT_DEFAULT
    # only understates authority Hutch can still assert separately. `authority` therefore stays
    # unreachable from code, exactly as `ratified_by` does.

    def rebuild(row: object) -> PolicyVersion:
        """A stored row back into a version, decoding the two JSON columns."""
        mapping = dict(row)  # type: ignore[call-overload]
        return PolicyVersion.model_validate(
            {
                **mapping,
                "body": json.loads(str(mapping["body"])),
                "changes": json.loads(str(mapping["changes"])),
            }
        )

    def shown_version(repository: StorageRepository) -> tuple[PolicyVersion, bool]:
        """What to render, without writing anything.

        Deliberately not `active_policy_version`, which records the shipped defaults as version 1
        when the store is empty. That is the right behaviour for the runtime -- it genuinely is
        running under them -- but the wrong behaviour for a page: rendering a form must not
        append to the history, and a preview that wrote would have changed the rule before the
        operator finished reading what the change was.

        With nothing recorded the shipped defaults are shown unrecorded, and the page says so.
        The runtime writes version 1 when it starts, and an apply from here writes it first.
        """
        recorded = recorded_policy_version(repository)
        if recorded is not None:
            return recorded, True
        return initial_version(now=datetime.now(UTC)), False

    def policy_page(
        request: Request,
        *,
        changes: object = None,
        refusal: str | None = None,
        pending: dict[str, str] | None = None,
    ) -> HTMLResponse:
        with database.transaction() as session:
            repository = StorageRepository(session)
            head, is_recorded = shown_version(repository)
            history = list(reversed(repository.list_policy_versions()))
        state = snapshot()
        return TEMPLATES.TemplateResponse(
            request=request,
            name="policy.html",
            context={
                "state": state,
                "navigation": NAVIGATION,
                "active": "/config/policy",
                "attention": state.attention,
                "head": head,
                "head_recorded": is_recorded,
                "policy": head.policy(),
                "fields": sorted(proposable_fields()),
                "documentation": field_documentation(),
                # Which glossary entries each description leans on, so a reader who does not
                # know a word can find out without leaving the page they are working on.
                "glossary_links": {
                    key: terms_in(f"{title} {description}")
                    for key, (title, description) in field_documentation().items()
                },
                "anchor": anchor,
                "history": history,
                "changes": changes,
                "refusal": refusal,
                "pending": pending or {},
            },
        )

    async def read_form(request: Request) -> dict[str, str]:
        """Parse a urlencoded form body.

        Deliberately not `request.form()`, which requires `python-multipart` even for a payload
        that has no parts. An HTML form posts `application/x-www-form-urlencoded` by default and
        `parse_qsl` reads exactly that, so the dependency would buy nothing -- and this console
        should not accept file uploads in any case, which not having a multipart parser makes
        true by construction rather than by policy.
        """
        body = (await request.body()).decode("utf-8", errors="replace")
        return dict(parse_qsl(body, keep_blank_values=True))

    def submitted(form: Mapping[str, object], *, drop: set[str]) -> dict[str, str]:
        """Only the fields the operator actually filled in.

        An empty input is a field left alone, not a proposal to set an empty value. The
        distinction is load-bearing: `validate_proposal` refuses a no-op, so passing every field
        through would refuse every partial edit with a message about the fields nobody touched.
        """
        return {
            key: str(value).strip()
            for key, value in form.items()
            if key not in drop and str(value).strip()
        }

    @app.get("/config/policy", response_class=HTMLResponse)
    def policy(request: Request) -> HTMLResponse:
        return policy_page(request)

    @app.post("/config/policy/preview", response_class=HTMLResponse)
    async def policy_preview(request: Request) -> HTMLResponse:
        """Validate and diff without writing.

        The step that makes this a control plane rather than a form that does things: the
        operator sees the exact successor before anything is minted.
        """
        form = await read_form(request)
        updates = submitted(form, drop={"reason"})
        reason = str(form.get("reason", "")).strip()
        with database.transaction() as session:
            head, _ = shown_version(StorageRepository(session))
        try:
            changes = validate_proposal(
                ConfigProposal(
                    base_identity=head.identity,
                    updates=updates,
                    actor="console",
                    reason=reason or "no reason given",
                ),
                head=head,
            )
        except (ProposalRefused, ValueError) as error:
            return policy_page(request, refusal=str(error), pending=updates)
        return policy_page(request, changes=changes, pending={**updates, "reason": reason})

    @app.post("/config/policy/apply", response_model=None)
    async def policy_apply(request: Request) -> RedirectResponse | HTMLResponse:
        """Mint the successor. Refuses rather than merges if the head moved since the preview."""
        form = await read_form(request)
        updates = submitted(form, drop={"reason", "base_identity"})
        reason = str(form.get("reason", "")).strip()
        try:
            proposal = ConfigProposal(
                base_identity=str(form.get("base_identity", "")),
                updates=updates,
                actor="console",
                reason=reason or "no reason given",
            )
            with database.transaction() as session:
                repository = StorageRepository(session)
                head, _ = active_policy_version(repository, now=datetime.now(UTC))
                successor = apply_proposal(proposal, head=head, now=datetime.now(UTC))
                repository.record_policy_version(successor.as_row())
        except (ProposalRefused, ValueError) as error:
            return policy_page(request, refusal=str(error), pending=updates)
        return RedirectResponse("/config/policy", status_code=303)

    @app.post("/config/policy/rollback", response_model=None)
    async def policy_rollback(request: Request) -> RedirectResponse | HTMLResponse:
        """Restore an earlier body by minting a *new* version, never by rewinding.

        The form is read off the request rather than declared with `Form(...)`, which would pull
        in `python-multipart` for a payload that is plain urlencoded. Starlette parses that
        natively, so the dependency would buy nothing.
        """
        form = await read_form(request)
        target_identity = str(form.get("target_identity", ""))
        reason = str(form.get("reason", ""))
        try:
            with database.transaction() as session:
                repository = StorageRepository(session)
                head, _ = active_policy_version(repository, now=datetime.now(UTC))
                target = next(
                    (
                        rebuild(row)
                        for row in repository.list_policy_versions()
                        if row["identity"] == target_identity
                    ),
                    None,
                )
                if target is None:
                    return policy_page(
                        request,
                        refusal=(
                            f"no recorded version {target_identity!r} to restore; the history "
                            "cannot roll back to something it does not contain"
                        ),
                    )
                repository.record_policy_version(
                    rollback_to(
                        target,
                        head=head,
                        actor="console",
                        reason=reason.strip() or "no reason given",
                        now=datetime.now(UTC),
                    ).as_row()
                )
        except (ProposalRefused, ValueError) as error:
            return policy_page(request, refusal=str(error))
        return RedirectResponse("/config/policy", status_code=303)

    return app


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "NAVIGATION", "create_app"]

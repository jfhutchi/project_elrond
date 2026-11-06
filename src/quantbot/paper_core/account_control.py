"""Explicit local account control; durable request charges never authorize a trade."""

import hashlib
import json
import math
import os
import platform
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlparse

from pydantic import Field, model_validator

from quantbot.brokers.base import BrokerModel, BrokerProtocolError, BrokerRequestError
from quantbot.research.manifest import content_id
from quantbot.storage.lock import SingleWriterLock

if TYPE_CHECKING:
    from quantbot.paper_core.entry_policy import EntryPolicy
    from quantbot.paper_core.store import CoreStore


class AccountRequestControl(BrokerModel):
    control_path: str
    account_scope: str = Field(min_length=64, max_length=64)
    host_identity: str = Field(min_length=64, max_length=64)
    host_epoch: str = Field(min_length=64, max_length=64)
    entry_generation: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def absolute_control(self) -> Self:
        if not Path(self.control_path).is_absolute():
            raise ValueError("absolute initialized account control path required")
        return self


@lru_cache(maxsize=1)
def observed_host_identity() -> str:
    return hashlib.sha256(("ELROND_ACCOUNT_HOST_V1:" + platform.node()).encode()).hexdigest()


@lru_cache(maxsize=1)
def observed_host_epoch() -> str:
    if platform.system() == "Windows":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance "
                "Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        boot = result.stdout.strip()
        if not boot:
            raise ValueError("host boot epoch unavailable")
    else:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return hashlib.sha256((observed_host_identity() + ":" + boot).encode()).hexdigest()


ROLE_LIMITS = {"guardian": 120, "trader": 40, "outcomes": 20}
TOTAL_LIMIT = 180
WINDOW = 60.0
_priority: ContextVar[str] = ContextVar("elrond_request_priority", default="RECOVERY")


@contextmanager
def request_priority(purpose: str) -> Iterator[None]:
    token = _priority.set(purpose)
    try:
        yield
    finally:
        _priority.reset(token)


def initialize_account_control(control_path: Path) -> None:
    if not control_path.is_absolute() or control_path.exists() or not control_path.parent.is_dir():
        raise ValueError("new absolute account control path required")
    with sqlite3.connect(control_path) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE schema_info(version INTEGER NOT NULL CHECK(version=1));
            INSERT INTO schema_info VALUES(1);
            CREATE TABLE scopes(scope TEXT PRIMARY KEY,epoch TEXT NOT NULL,last_mono REAL NOT NULL,
                cooldown REAL NOT NULL,blocked REAL NOT NULL,observed_limit INTEGER NOT NULL,
                limit_until REAL NOT NULL);
            CREATE TABLE charges(id INTEGER PRIMARY KEY,scope TEXT NOT NULL,epoch TEXT NOT NULL,
                role TEXT NOT NULL,purpose TEXT NOT NULL,at REAL NOT NULL,
                consumed INTEGER NOT NULL);
            CREATE INDEX charges_window ON charges(scope,epoch,at,role);
            CREATE TABLE issuers(scope TEXT NOT NULL,epoch TEXT NOT NULL,issuer TEXT NOT NULL,
                last_mono REAL NOT NULL,PRIMARY KEY(scope,epoch,issuer));
            CREATE TABLE refusals(id INTEGER PRIMARY KEY,scope TEXT NOT NULL,epoch TEXT NOT NULL,
                role TEXT NOT NULL,purpose TEXT NOT NULL,at REAL NOT NULL,reason TEXT NOT NULL);
            CREATE TABLE entry_bindings(scope TEXT PRIMARY KEY,generation INTEGER NOT NULL,
                store_identity TEXT NOT NULL,release_id TEXT NOT NULL,payload TEXT NOT NULL);
        """)


def require_resource_control(
    policy: "EntryPolicy | None", config: AccountRequestControl | None
) -> None:
    if policy is not None and policy.contract == "RESOURCE_BOUNDED_PAPER_V1" and config is None:
        raise ValueError("RESOURCE_PAPER_ACCOUNT_CONTROL_REQUIRED")


def _entry_lock(config: AccountRequestControl) -> SingleWriterLock:
    if observed_host_identity() != config.host_identity:
        raise ValueError("ACCOUNT_ENTRY_HOST_MISMATCH")
    return SingleWriterLock(config.control_path + "." + config.account_scope + ".entries.lock")


def bind_entry_account(
    config: AccountRequestControl,
    *,
    store_identity: str,
    release_id: str,
    expected_generation: int,
    evidence_id: str,
) -> str:
    """Explicit sidecar CAS. Caller must supply the audited fenced/flat ledger proof."""
    budget = RequestBudget(Path(config.control_path), config.account_scope, config.host_epoch)
    document = dict(
        kind="ACCOUNT_ENTRY_BINDING_V1",
        account_scope=config.account_scope,
        host_identity=config.host_identity,
        generation=config.entry_generation,
        store_identity=store_identity,
        release_id=release_id,
        evidence_id=evidence_id,
    )
    with _entry_lock(config), budget._transaction() as c:
        prior = c.execute(
            "SELECT * FROM entry_bindings WHERE scope=?", (config.account_scope,)
        ).fetchone()
        if prior and json.loads(prior["payload"]) == document:
            return content_id(
                document
            )  # Recover an interrupted same-identity bind, never increment.
        generation = int(prior["generation"]) if prior else 0
        if generation != expected_generation or config.entry_generation != generation + 1:
            raise ValueError("ACCOUNT_ENTRY_BINDING_STALE_GENERATION")
        c.execute(
            "INSERT INTO entry_bindings VALUES(?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
            "generation=excluded.generation,store_identity=excluded.store_identity,"
            "release_id=excluded.release_id,payload=excluded.payload",
            (
                config.account_scope,
                config.entry_generation,
                store_identity,
                release_id,
                json.dumps(document, sort_keys=True),
            ),
        )
    return content_id(document)


class AccountEntryLease:
    def __init__(self, config: AccountRequestControl, store_identity: str, release_id: str):
        self.config, self.store_identity, self.release_id = config, store_identity, release_id
        self.lock = _entry_lock(config)
        self.binding_id: str | None = None
        self.document: dict[str, Any] | None = None

    def acquire(self) -> None:
        self.lock.acquire()
        try:
            budget = RequestBudget(
                Path(self.config.control_path), self.config.account_scope, self.config.host_epoch
            )
            with budget._transaction() as c:
                row = c.execute(
                    "SELECT * FROM entry_bindings WHERE scope=?", (self.config.account_scope,)
                ).fetchone()
                if row is None or (row["generation"], row["store_identity"], row["release_id"]) != (
                    self.config.entry_generation,
                    self.store_identity,
                    self.release_id,
                ):
                    raise ValueError("ACCOUNT_ENTRY_BINDING_MISMATCH")
                self.document = json.loads(row["payload"])
                self.binding_id = content_id(self.document)
        except BaseException:
            self.lock.release()
            raise

    @property
    def is_current(self) -> bool:
        # A supported generation change requires this very OS lock, so it cannot revoke us.
        return self.lock.is_acquired and self.binding_id is not None

    def release(self) -> None:
        self.lock.release()


def entry_lease_objections(store: "CoreStore") -> tuple[str, ...]:
    control = getattr(store, "runtime_account_control", None)
    if control is None or not store.parallel_entries:
        return ()  # Explicit offline coordinator fixtures and unchanged legacy runtime.
    lease = getattr(store, "runtime_account_lease", None)
    if (
        not isinstance(lease, AccountEntryLease)
        or not lease.is_current
        or (
            lease.release_id != store.runtime_release_id
            or lease.store_identity != store.database.store_identity
            or lease.config != control
        )
    ):
        return ("CORE_ACCOUNT_ENTRY_LEASE_UNAVAILABLE",)
    return ()


def read_control_status(config: AccountRequestControl) -> dict[str, Any]:
    """Last recorded windows, without charging, resetting clocks or granting readiness."""
    result: dict[str, Any] = dict(
        scope="LAST_RECORDED_REQUEST_WINDOWS_NOT_LIVE_READINESS",
        role_limits=ROLE_LIMITS,
        total_limit=TOTAL_LIMIT,
        window_seconds=WINDOW,
        guardian_noncritical_limit=36,
        guardian_protection_reserve=84,
        entry_generation=config.entry_generation,
        namespaces={},
    )
    try:
        with sqlite3.connect(Path(config.control_path).as_uri() + "?mode=ro", uri=True) as c:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA query_only=ON")
            c.execute("BEGIN")
            for namespace in ("trading", "market"):
                row = c.execute(
                    "SELECT * FROM scopes WHERE scope=?", (config.account_scope + ":" + namespace,)
                ).fetchone()
                if row is None:
                    result["namespaces"][namespace] = dict(state="NOT_SAMPLED")
                    continue
                counts = dict(
                    c.execute(
                        "SELECT role,count(*) FROM charges WHERE scope=? AND epoch=? AND at>? "
                        "GROUP BY role",
                        (row["scope"], row["epoch"], row["last_mono"] - WINDOW),
                    ).fetchall()
                )
                result["namespaces"][namespace] = dict(
                    state="RECORDED",
                    as_of_monotonic=row["last_mono"],
                    host_epoch=row["epoch"],
                    role_used=counts,
                    total_used=sum(counts.values()),
                    observed_limit=row["observed_limit"],
                    limit_until_monotonic=row["limit_until"],
                    cooldown_until_monotonic=row["cooldown"],
                    blocked_until_monotonic=row["blocked"],
                )
    except (sqlite3.Error, ValueError):
        result["state"] = "ACCOUNT_CONTROL_UNAVAILABLE"
    return result


class RequestBudget:
    def __init__(
        self,
        control_path: Path,
        account_scope: str,
        host_epoch: str,
        *,
        initial_host_epoch: str | None = None,
        issuer_id: str | None = None,
    ) -> None:
        if (
            not control_path.is_absolute()
            or not control_path.is_file()
            or control_path.is_symlink()
            or not account_scope
            or not host_epoch
        ):
            raise ValueError("explicitly initialized account control required")
        self.path, self.scope, self.epoch = control_path, account_scope, host_epoch
        self.initial_epoch = initial_host_epoch or host_epoch
        self.issuer = issuer_id or str(os.getpid())
        try:
            with self._transaction() as c:
                if c.execute("SELECT version FROM schema_info").fetchone()[0] != 1:
                    raise ValueError("unsupported initialized account control")
        except sqlite3.Error as error:
            raise ValueError("explicitly initialized account control required") from error

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def _state(self, c: sqlite3.Connection, now: float) -> sqlite3.Row:
        if not math.isfinite(now) or now < 0:
            raise ValueError("finite nonnegative host monotonic time required")
        row = c.execute("SELECT * FROM scopes WHERE scope=?", (self.scope,)).fetchone()
        issuer = c.execute(
            "SELECT last_mono FROM issuers WHERE scope=? AND epoch=? AND issuer=?",
            (self.scope, self.epoch, self.issuer),
        ).fetchone()
        if row is None:
            initial = now + WINDOW if self.initial_epoch != self.epoch else now
            c.execute(
                "INSERT INTO scopes VALUES(?,?,?,?,?,?,?)",
                (self.scope, self.epoch, now, initial, initial, TOTAL_LIMIT, now),
            )
        elif (
            row["epoch"] != self.epoch
            or issuer is not None
            and now < issuer["last_mono"]
            or issuer is None
            and now < row["last_mono"] - 5
        ):
            # Old-domain charges remain retained. A reboot/regression earns no instant reset.
            c.execute(
                "UPDATE scopes SET "
                "epoch=?,last_mono=?,cooldown=?,blocked=?,observed_limit=?,limit_until=? "
                "WHERE scope=?",
                (
                    self.epoch,
                    now,
                    now + WINDOW,
                    now + WINDOW,
                    TOTAL_LIMIT,
                    now + WINDOW,
                    self.scope,
                ),
            )
        else:
            c.execute(
                "UPDATE scopes SET last_mono=? WHERE scope=?",
                (max(now, row["last_mono"]), self.scope),
            )
        c.execute(
            "INSERT INTO issuers VALUES(?,?,?,?) ON CONFLICT(scope,epoch,issuer) DO UPDATE "
            "SET last_mono=excluded.last_mono",
            (self.scope, self.epoch, self.issuer, now),
        )
        result = c.execute("SELECT * FROM scopes WHERE scope=?", (self.scope,)).fetchone()
        assert isinstance(result, sqlite3.Row)
        return result

    def _counts(
        self, c: sqlite3.Connection, now: float, *, exclude_identity: int | None = None
    ) -> dict[str, int]:
        return dict(
            c.execute(
                "SELECT role,count(*) FROM charges WHERE scope=? AND epoch=? AND at>? AND id!=? "
                "GROUP BY role",
                (self.scope, self.epoch, now - WINDOW, exclude_identity or -1),
            ).fetchall()
        )

    def acquire(self, role: str, *, purpose: str, now_monotonic: float) -> bool:
        return (
            self.reserve(role, purpose=purpose, now_monotonic=now_monotonic, consumed=True)
            is not None
        )

    def reserve(
        self,
        role: str,
        *,
        purpose: str,
        now_monotonic: float,
        consumed: bool = False,
        reuse_identity: int | None = None,
    ) -> int | None:
        if role not in ROLE_LIMITS or not purpose:
            raise ValueError("explicit known role and purpose required")
        with self._transaction() as c:
            row = self._state(c, now_monotonic)
            if reuse_identity is not None:
                original = c.execute(
                    "SELECT purpose FROM charges WHERE id=? AND scope=? AND epoch=? AND role=? "
                    "AND consumed=0",
                    (reuse_identity, self.scope, self.epoch, role),
                ).fetchone()
                if original is None or consumed:
                    return None
                # A retry retains its original request priority and prepaid identity.
                purpose = original["purpose"]
            counts = self._counts(c, now_monotonic, exclude_identity=reuse_identity)
            ceiling = row["observed_limit"] if now_monotonic < row["limit_until"] else TOTAL_LIMIT
            noncritical = c.execute(
                "SELECT count(*) FROM charges WHERE scope=? AND epoch=? AND "
                "role='guardian' AND at>? AND purpose NOT LIKE 'OWNED_EXIT%' AND purpose "
                "NOT LIKE 'CLOCK%' AND id!=?",
                (self.scope, self.epoch, now_monotonic - WINDOW, reuse_identity or -1),
            ).fetchone()[0]
            reason = (
                "HOST_EPOCH_COOLDOWN"
                if now_monotonic < row["cooldown"]
                else "PROVIDER_RETRY_DEADLINE"
                if now_monotonic < row["blocked"]
                else "SHARED_REQUEST_BUDGET"
                if sum(counts.values()) >= min(TOTAL_LIMIT, ceiling)
                else "OWNED_PROTECTION_REQUEST_RESERVE"
                if role == "guardian"
                and not purpose.startswith(("OWNED_EXIT", "CLOCK"))
                and noncritical >= 36
                else "ROLE_REQUEST_BUDGET"
                if counts.get(role, 0) >= ROLE_LIMITS[role]
                else None
            )
            if reason:
                c.execute(
                    "INSERT INTO refusals(scope,epoch,role,purpose,at,reason) VALUES(?,?,?,?,?,?)",
                    (self.scope, self.epoch, role, purpose, now_monotonic, reason),
                )
                return None
            if reuse_identity is not None:
                c.execute("UPDATE charges SET at=? WHERE id=?", (now_monotonic, reuse_identity))
                return reuse_identity
            cursor = c.execute(
                "INSERT INTO charges(scope,epoch,role,purpose,at,consumed) VALUES(?,?,?,?,?,?)",
                (self.scope, self.epoch, role, purpose, now_monotonic, int(consumed)),
            )
            assert cursor.lastrowid is not None
            return int(cursor.lastrowid)

    def consume(self, identity: int, role: str, *, now_monotonic: float) -> bool:
        """Move the prepaid request to actual I/O time, rechecking its rolling window."""
        with self._transaction() as c:
            state = self._state(c, now_monotonic)
            original = c.execute(
                "SELECT * FROM charges WHERE id=? AND scope=? AND epoch=? AND role=? AND "
                "consumed=0",
                (identity, self.scope, self.epoch, role),
            ).fetchone()
            if original is None or now_monotonic < max(state["cooldown"], state["blocked"]):
                return False
            counts = dict(
                c.execute(
                    "SELECT role,count(*) FROM charges WHERE scope=? AND epoch=? AND at>? "
                    "AND id!=? GROUP BY role",
                    (self.scope, self.epoch, now_monotonic - WINDOW, identity),
                ).fetchall()
            )
            limit = state["observed_limit"] if now_monotonic < state["limit_until"] else TOTAL_LIMIT
            if (
                sum(counts.values()) >= min(TOTAL_LIMIT, limit)
                or counts.get(role, 0) >= ROLE_LIMITS[role]
            ):
                return False
            c.execute("UPDATE charges SET at=?,consumed=1 WHERE id=?", (now_monotonic, identity))
            return True

    def status(self, *, now_monotonic: float) -> dict[str, object]:
        with self._transaction() as c:
            row = self._state(c, now_monotonic)
            counts = self._counts(c, now_monotonic)
            return dict(
                used=sum(counts.values()),
                role_used=counts,
                role_limits=ROLE_LIMITS,
                total_limit=TOTAL_LIMIT,
                window_seconds=WINDOW,
                host_epoch=self.epoch,
                guardian_noncritical_limit=36,
                guardian_protection_reserve=84,
                cooldown_until=row["cooldown"],
                blocked_until=row["blocked"],
                observed_limit=row["observed_limit"],
                limit_until=row["limit_until"],
            )

    def observe(
        self,
        *,
        status_code: int,
        headers: Mapping[str, str],
        now_monotonic: float,
        now_utc: datetime,
    ) -> None:
        h = {k.lower(): v for k, v in headers.items()}
        if now_utc.tzinfo is None:
            raise ValueError("aware provider-response receipt time required")
        with self._transaction() as c:
            row = self._state(c, now_monotonic)
            limit = int(row["observed_limit"])
            until = float(row["limit_until"])
            blocked = float(row["blocked"])
            try:
                raw = int(h.get("x-ratelimit-limit", str(TOTAL_LIMIT)))
                if raw > 0 and raw < TOTAL_LIMIT:
                    limit = min(limit, raw) if now_monotonic < until else raw
                    until = max(until, now_monotonic + WINDOW)
                if status_code == 429 or h.get("x-ratelimit-remaining") == "0":
                    delay = WINDOW
                    if "retry-after" in h:
                        try:
                            delay = float(h["retry-after"])
                        except ValueError:
                            delay = (
                                parsedate_to_datetime(h["retry-after"]) - now_utc
                            ).total_seconds()
                    if "x-ratelimit-reset" in h:
                        delay = max(delay, float(h["x-ratelimit-reset"]) - now_utc.timestamp())
                    if not math.isfinite(delay):
                        raise ValueError("invalid retry deadline")
                    blocked = max(blocked, now_monotonic + max(0.0, delay))
            except (ValueError, TypeError, OverflowError):
                if status_code == 429:
                    blocked = max(blocked, now_monotonic + WINDOW)
            c.execute(
                "UPDATE scopes SET observed_limit=?,limit_until=?,blocked=? WHERE scope=?",
                (limit, until, blocked, self.scope),
            )


class LocalRequestBudgetRefusal(BrokerRequestError):
    def __init__(self, method: str, client_order_id: str | None = None):
        super().__init__("LOCAL_BROKER_REQUEST_BUDGET", status_code=429)
        self.method, self.client_order_id = method, client_order_id


class BudgetedTransport:
    """Both existing transports charge before awaiting I/O; headers contain no new authority."""

    def __init__(
        self, transport: Any, config: AccountRequestControl, role: str, clocks: Any
    ) -> None:
        if observed_host_identity() != config.host_identity or role not in ROLE_LIMITS:
            raise ValueError("ACCOUNT_REQUEST_CONTROL_HOST_OR_ROLE_MISMATCH")
        self.transport, self.config, self.role, self.clocks = transport, config, role, clocks
        self._send_permits: dict[str, int] = {}
        epoch = observed_host_epoch()
        self.budgets = {
            name: RequestBudget(
                Path(config.control_path),
                config.account_scope + ":" + name,
                epoch,
                initial_host_epoch=config.host_epoch,
                issuer_id=getattr(clocks, "process_id", None),
            )
            for name in ("trading", "market")
        }

    def reserve_send(self, client_order_id: str) -> None:
        identity = self.budgets["trading"].reserve(
            self.role,
            purpose=_priority.get() + " POST_RESERVATION " + client_order_id,
            now_monotonic=self.clocks.sample()[1],
            reuse_identity=self._send_permits.get(client_order_id),
        )
        if identity is None:
            raise LocalRequestBudgetRefusal("POST_RESERVATION", client_order_id)
        self._send_permits[client_order_id] = identity

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "paper-api.alpaca.markets",
            "data.alpaca.markets",
        }:
            raise BrokerProtocolError("ACCOUNT_REQUEST_CONTROL_ENDPOINT_MISMATCH")
        budget = self.budgets[
            "trading" if parsed.hostname == "paper-api.alpaca.markets" else "market"
        ]
        if method == "POST":
            client = str(kwargs.get("json_body", {}).get("client_order_id", ""))
            identity = self._send_permits.get(client)
            if identity is None or not budget.consume(
                identity, self.role, now_monotonic=self.clocks.sample()[1]
            ):
                raise LocalRequestBudgetRefusal("POST", client)
            self._send_permits.pop(client)
        elif not budget.acquire(
            self.role,
            purpose=("CLOCK" if parsed.path in {"/v2/clock", "/v2/calendar"} else _priority.get())
            + " "
            + method
            + " "
            + parsed.path,
            now_monotonic=self.clocks.sample()[1],
        ):
            raise LocalRequestBudgetRefusal(method)
        response = await self.transport.request(method, url, **kwargs)
        wall, mono = self.clocks.sample()
        budget.observe(
            status_code=response.status_code,
            headers=response.headers,
            now_monotonic=mono,
            now_utc=wall,
        )
        return response

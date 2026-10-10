"""PAPER close capture: broker GETs and a dedicated immutable performance ledger.

No migrations, orders or reconstructed historical closes. The CLI prepares a real
close-window reconciliation using the existing shared operational writer lock.
The first timely reconciled snapshot seeds a baseline without counting a profit day.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from quantbot.brokers import AlpacaPaperBrokerAdapter, HttpxBrokerTransport
from quantbot.brokers.base import PaperPerformanceAccount
from quantbot.config import Settings
from quantbot.domain import BrokerEnvironment, TradingMode
from quantbot.market_data import AlpacaMarketCalendar, HttpxMarketDataTransport
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.operations.close_reference import (
    has_reference,
    reference_mode_enabled,
    retain_reference,
)
from quantbot.operations.daily_performance import CloseEvidence, CloseLedger, evaluate_day
from quantbot.operations.performance_flows import external_flow as external_flow
from quantbot.storage import SingleWriterLock


def initialize(directory: Path, operational: Path) -> None:
    if not directory.is_absolute() or not operational.is_absolute():
        raise ValueError('absolute targets required')
    paths = [directory / 'baselines.db', directory / 'closes.db']
    if operational.resolve() in {p.resolve() for p in paths}:
        raise ValueError('performance store aliases operational store')
    if any(path.exists() for path in paths):
        raise ValueError('refusing existing performance stores')
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(paths[0])) as c, c:
        c.execute('CREATE TABLE paper_close_baselines_v1 '
                  '(session_date TEXT PRIMARY KEY, close_at TEXT NOT NULL, '
                  'captured_at TEXT NOT NULL, equity TEXT NOT NULL, epoch TEXT NOT NULL, '
                  'evidence_json TEXT NOT NULL)')
    CloseLedger(paths[1], create=True)


def _write(directory: Path, name: str, document: Mapping[str, Any]) -> None:
    path = directory / name
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(document, sort_keys=True), encoding='utf-8')
    temporary.replace(path)


def _retain_validation(directory: Path, document: Mapping[str, Any]) -> None:
    """Immutable diagnostic proof; never grants or reconstructs daily accounting."""
    payload = json.dumps(document, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    receipts = directory / 'validation-receipts'
    receipts.mkdir(exist_ok=True)
    path = receipts / (digest + '.json')
    try:
        with path.open('x', encoding='utf-8') as handle:
            handle.write(payload)
    except FileExistsError:
        if path.read_text(encoding='utf-8') != payload:
            raise ValueError('conflicting close validation receipt') from None


def _reconciled(operational: Path, account: PaperPerformanceAccount,
                *, close_at: datetime, captured_at: datetime) -> tuple[bool, str, dict[str, str]]:
    """A timely completed reconciliation and matching durable account, one RO snapshot."""
    with closing(sqlite3.connect(operational.resolve().as_uri() + '?mode=ro',
                                uri=True, timeout=5)) as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        store = c.execute('SELECT store_identity FROM schema_version WHERE id=1').fetchone()
        reconciliation = c.execute('SELECT reconciliation_id,status,completed_at '
                                   'FROM reconciliation_runs ORDER BY completed_at DESC LIMIT 1'
                                   ).fetchone()
        snapshot = c.execute('SELECT captured_at,equity,cash,currency FROM account_snapshots '
                             'WHERE account_id=? ORDER BY captured_at DESC LIMIT 1',
                             (account.account.account_id,)).fetchone()
        if store is None:
            raise ValueError('missing durable account lineage')
        epoch = hashlib.sha256(json.dumps([store[0], account.account.account_id,
                                          account.created_at.isoformat()]).encode()).hexdigest()
        if not reconciliation or not snapshot or reconciliation[1] != 'RECONCILED':
            return False, epoch, {}
        if c.execute('SELECT COUNT(*) FROM reconciliation_diffs WHERE reconciliation_id=?',
                     (reconciliation[0],)).fetchone()[0] != 0:
            return False, epoch, {}
        for timestamp in (reconciliation[2], snapshot[0]):
            at = datetime.fromisoformat(timestamp)
            if at.tzinfo is None or not close_at <= at <= captured_at or (
                captured_at - at > timedelta(minutes=2)
            ):
                return False, epoch, {}
        return (Decimal(snapshot[1]) == account.account.equity
                and Decimal(snapshot[2]) == account.account.cash
                and snapshot[3] == account.account.currency == 'USD'), epoch, {
                    'id': reconciliation[0], 'completed_at': reconciliation[2],
                    'snapshot_hash': hashlib.sha256(json.dumps(snapshot).encode()).hexdigest(),
                }


async def capture_close(
    directory: Path, operational: Path, broker: AlpacaPaperBrokerAdapter,
    calendar: AlpacaMarketCalendar, *, expected_account_id: str,
    now: datetime, clock: Callable[[], datetime] | None = None,
    prepare_reconciliation: Callable[[], Awaitable[None]] | None = None,
    broker_close_reference: bool = False,
) -> dict[str, str]:
    """One bounded pass. Outside close window never requests account/activities."""
    if now.tzinfo is None or not directory.is_absolute() or not operational.is_absolute():
        raise ValueError('aware time and absolute configured paths required')
    if broker.environment != 'PAPER' or broker.base_url != 'https://paper-api.alpaca.markets':
        raise ValueError('close capture has no live route')
    if not expected_account_id:
        raise ValueError('close capture requires pinned account')
    baselines = directory / 'baselines.db'
    closes = directory / 'closes.db'
    if any(not path.is_file() for path in (baselines, closes)):
        raise ValueError('explicit performance initialization required')
    if operational.resolve() in {baselines.resolve(), closes.resolve()}:
        raise ValueError('performance store aliases operational store')
    today = now.astimezone(XNYS_TIMEZONE).date()
    with SingleWriterLock(directory / 'capture.lock'):
        with closing(sqlite3.connect(baselines.as_uri() + '?mode=ro', uri=True)) as c:
            oldest = c.execute(
                'SELECT MIN(session_date) FROM paper_close_baselines_v1').fetchone()[0]
        start = min(today - timedelta(days=40), datetime.fromisoformat(oldest).date()
                    if oldest else today)
        end = today + timedelta(days=62)
        sessions = await calendar.get_sessions(start, end)
        _write(directory, 'calendar.json', dict(calendar='XNYS', start=start.isoformat(),
               end=end.isoformat(), sessions=[dict(session_date=s.session_date.isoformat(),
               open_at=s.open_at.isoformat(), close_at=s.close_at.isoformat()) for s in sessions]))
        current = next((s for s in sessions if s.session_date == today), None)

        def status(state: str, reason: str) -> dict[str, str]:
            document = dict(session_date=today.isoformat(), status=state, reason=reason)
            _write(directory, 'capture-status.json', document)
            return document

        if current is None:
            return status('PENDING', 'SESSION_PENDING')
        if now < current.close_at:
            return status('PENDING', 'SESSION_PENDING')
        with closing(sqlite3.connect(closes.as_uri() + '?mode=ro', uri=True)) as c:
            if c.execute('SELECT 1 FROM paper_daily_closes_v1 WHERE session_date=?',
                         (today.isoformat(),)).fetchone():
                return dict(session_date=today.isoformat(), status='FINAL', reason='ALREADY_FINAL')
        canonical_mode = reference_mode_enabled(directory)
        if canonical_mode and not broker_close_reference:
            return status('INVALID', 'CAPTURE_UNAVAILABLE')
        with closing(sqlite3.connect(baselines.as_uri() + '?mode=ro', uri=True)) as c:
            existing = c.execute('SELECT evidence_json FROM paper_close_baselines_v1 '
                                 'WHERE session_date=?', (today.isoformat(),)).fetchone()
        if existing:
            evidence = _decode_evidence(existing[0])
            if evidence.close_at != current.close_at or evidence.session_date != today:
                return status('INVALID', 'CAPTURE_UNAVAILABLE')
            day = evaluate_day(evidence)
            if day.status == 'FINAL':
                if broker_close_reference:
                    with closing(sqlite3.connect(baselines.as_uri() + '?mode=ro', uri=True)) as c:
                        retained_epoch = c.execute(
                            'SELECT epoch FROM paper_close_baselines_v1 WHERE session_date=?',
                            (today.isoformat(),)).fetchone()[0]
                    if not has_reference(directory, evidence, epoch=retained_epoch):
                        return status('INVALID', 'CAPTURE_UNAVAILABLE')
                CloseLedger(closes).finalize(evidence)
                return dict(session_date=today.isoformat(), status='FINAL', reason='CAPTURED')
            return status(day.status, 'BASELINE_MISSING' if day.status == 'MISSING'
                          else 'CAPTURE_UNAVAILABLE')
        if now > current.close_at + timedelta(minutes=5):
            return status('INVALID', 'CLOSE_NOT_CAPTURED')
        prior_sessions = [s for s in sessions if s.session_date < today]
        if not prior_sessions:
            return status('MISSING', 'CALENDAR_UNAVAILABLE')
        previous_session = prior_sessions[-1]
        if prepare_reconciliation is not None:
            await prepare_reconciliation()
        account = await broker.get_performance_account()
        captured_at = clock() if clock is not None else now
        if (captured_at.tzinfo is None or captured_at < now
                or captured_at > current.close_at + timedelta(minutes=5)):
            return status('INVALID', 'CLOSE_NOT_CAPTURED')
        if account.account.account_id != expected_account_id:
            return status('INVALID', 'ACCOUNT_EPOCH_CHANGED')
        reconciled, epoch, reconciliation = _reconciled(
            operational, account, close_at=current.close_at, captured_at=captured_at)
        if not reconciled:
            return status('INVALID', 'RECONCILIATION_UNVERIFIED')
        with closing(sqlite3.connect(baselines.as_uri() + '?mode=ro', uri=True)) as c:
            baseline = c.execute('SELECT equity,epoch,captured_at,evidence_json '
                                 'FROM paper_close_baselines_v1 '
                                 'WHERE session_date=?',
                                 (previous_session.session_date.isoformat(),)).fetchone()
        history = None
        close_equity = account.account.equity
        history_mark_matches = True
        if broker_close_reference:
            history = await broker.get_performance_close_history(
                start=previous_session.close_at, end=current.close_at)
            if history.start != previous_session.close_at or history.end != current.close_at:
                return status('INVALID', 'CAPTURE_UNAVAILABLE')
            # Exact session closes only; no nearest point or timestamp reinterpretation.
            close_equity = history.equity_at(current.close_at)
            prior_mark = history.points().get(previous_session.close_at)
            confirmed = await broker.get_performance_account()
            captured_at = clock() if clock is not None else now
            if (captured_at.tzinfo is None or captured_at < now
                    or captured_at > current.close_at + timedelta(minutes=5)):
                return status('INVALID', 'CLOSE_NOT_CAPTURED')
            if confirmed != account:
                return status('INVALID', 'RECONCILIATION_UNVERIFIED')
            reconciled, confirmed_epoch, reconciliation = _reconciled(
                operational, confirmed, close_at=current.close_at, captured_at=captured_at)
            if not reconciled or confirmed_epoch != epoch:
                return status('INVALID', 'RECONCILIATION_UNVERIFIED')
            # Reconciliation reflects the close holdings only if no later economic change occurred.
            if captured_at > current.close_at and await broker.get_performance_activities(
                    after=current.close_at, until=captured_at):
                return status('INVALID', 'RECONCILIATION_UNVERIFIED')
            if baseline:
                prior_evidence = _decode_evidence(baseline[3])
                if (prior_evidence.session_date != previous_session.session_date
                        or prior_evidence.close_at != previous_session.close_at
                        or str(prior_evidence.close_equity) != baseline[0]):
                    return status('INVALID', 'CAPTURE_UNAVAILABLE')
                if not has_reference(directory, prior_evidence, epoch=baseline[1]):
                    baseline = None  # A legacy capture is not a canonical broker-close baseline.
                else:
                    history_mark_matches = prior_mark == Decimal(baseline[0])
        epoch_matches = bool(baseline and baseline[1] == epoch)
        mark_matches = bool(baseline and history_mark_matches
                            and Decimal(baseline[0]) == account.previous_close_equity)
        same_epoch = epoch_matches and mark_matches
        flows_verified = False
        flow = Decimal(0)
        rows: Sequence[Mapping[str, object]] | None = None
        if same_epoch:
            rows = await broker.get_performance_activities(
                after=previous_session.close_at if history else datetime.fromisoformat(baseline[2]),
                until=current.close_at if history else captured_at)
            try:
                flow = external_flow(rows)
                flows_verified = True
            except (ValueError, KeyError, ArithmeticError):
                pass
        if history is not None:
            completed_at = clock() if clock is not None else now
            if (completed_at.tzinfo is None or completed_at < captured_at
                    or completed_at > current.close_at + timedelta(minutes=5)):
                return status('INVALID', 'CLOSE_NOT_CAPTURED')
            still_reconciled, final_epoch, _ = _reconciled(
                operational, account, close_at=current.close_at, captured_at=completed_at)
            if not still_reconciled or final_epoch != epoch:
                return status('INVALID', 'RECONCILIATION_UNVERIFIED')
        evidence = CloseEvidence(
            session_date=today, close_at=current.close_at, captured_at=captured_at,
            previous_close_equity=Decimal(baseline[0]) if baseline else None,
            previous_session_date=previous_session.session_date if baseline else None,
            expected_previous_session_date=previous_session.session_date,
            close_equity=close_equity, net_external_flow=flow,
            flows_verified=flows_verified, same_account_epoch=same_epoch, reconciled=reconciled,
        )
        day = evaluate_day(evidence)
        if history is not None:
            retain_reference(directory, evidence, epoch=epoch, history=history,
                             reconciliation=reconciliation, activities=rows)
        reasons = (['BASELINE_MISSING'] if not baseline else
                   (['ACCOUNT_EPOCH_CHANGED'] if not epoch_matches else [])
                   + (['PRIOR_CLOSE_MARK_UNVERIFIED'] if not mark_matches else []))
        _retain_validation(directory, dict(
            session_date=today.isoformat(), recorded_at=captured_at.isoformat(),
            epoch_matches=epoch_matches, prior_close_mark_matches=mark_matches,
            reasons=reasons, grants_accounting_finalization=False,
            evidence_hash=hashlib.sha256(json.dumps(
                asdict(evidence), default=str, sort_keys=True).encode()).hexdigest(),
        ))
        # Seed even the first/reset close, but never label that comparison profitable.
        document = json.dumps(asdict(evidence), default=str, sort_keys=True)
        record = (today.isoformat(), current.close_at.isoformat(), captured_at.isoformat(),
                  str(evidence.close_equity), epoch, document)
        with closing(sqlite3.connect(baselines, timeout=5)) as c, c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT session_date,close_at,captured_at,equity,epoch,evidence_json '
                            'FROM paper_close_baselines_v1 WHERE session_date=?',
                            (today.isoformat(),)).fetchone()
            if old and old != record:
                return status('INVALID', 'CAPTURE_UNAVAILABLE')
            c.execute('INSERT OR IGNORE INTO paper_close_baselines_v1 VALUES (?,?,?,?,?,?)', record)
        if day.status == 'FINAL':
            CloseLedger(closes).finalize(evidence)
            return dict(session_date=today.isoformat(), status='FINAL', reason='CAPTURED')
        return status(day.status, 'BASELINE_MISSING' if not baseline else
                      'ACCOUNT_EPOCH_CHANGED' if not epoch_matches else
                      'CAPTURE_UNAVAILABLE' if not mark_matches else 'FLOW_UNVERIFIED')


def _decode_evidence(document: str) -> CloseEvidence:
    payload = json.loads(document)
    if set(payload) != set(CloseEvidence.__dataclass_fields__):
        raise ValueError('invalid durable close evidence')
    for field in ('flows_verified', 'same_account_epoch', 'reconciled'):
        if type(payload[field]) is not bool:
            raise ValueError('invalid durable close flags')
    for field in ('session_date', 'expected_previous_session_date'):
        payload[field] = date.fromisoformat(payload[field])
    payload['previous_session_date'] = (
        date.fromisoformat(payload['previous_session_date'])
        if payload['previous_session_date'] else None)
    for field in ('close_at', 'captured_at'):
        payload[field] = datetime.fromisoformat(payload[field])
    for field in ('previous_close_equity', 'close_equity', 'net_external_flow'):
        payload[field] = Decimal(payload[field]) if payload[field] is not None else None
    return CloseEvidence(**payload)


def main() -> int:
    parser = argparse.ArgumentParser(description='Dedicated PAPER daily close evidence')
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--broker-close-reference', action='store_true',
                        help='Opt-in prospective exact broker-close lineage; never backfill')
    args = parser.parse_args()
    try:
        repository = Path(__file__).resolve().parents[3]
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository,
                                       text=True, timeout=30).strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=repository,
                                        text=True, timeout=30).strip()
        if head != args.commit or dirty:
            raise ValueError('clean pinned capture checkout required')
        from quantbot.operations.service import isolated_application_environment
        with isolated_application_environment(args.env_file):
            settings = Settings()
            if (settings.TRADING_MODE is not TradingMode.PAPER
                    or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
                    or not settings.EXPECTED_ACCOUNT_ID):
                raise ValueError('verified PAPER settings required')
            if args.initialize:
                initialize(args.directory, args.database)
            else:
                asyncio.run(_capture(args, settings))
    except Exception:
        print('PAPER_CLOSE_CAPTURE_FAILED')
        return 1
    return 0


async def _capture(args: argparse.Namespace, settings: Settings) -> None:
    if settings.ALPACA_PAPER_API_KEY is None or settings.ALPACA_PAPER_API_SECRET is None:
        raise ValueError('PAPER credentials required')
    transport = HttpxBrokerTransport()
    data = HttpxMarketDataTransport()
    try:
        broker = AlpacaPaperBrokerAdapter(api_key=settings.ALPACA_PAPER_API_KEY,
                                         api_secret=settings.ALPACA_PAPER_API_SECRET,
                                         transport=transport, max_retries=0)
        calendar = AlpacaMarketCalendar(api_key=settings.ALPACA_PAPER_API_KEY,
                                       api_secret=settings.ALPACA_PAPER_API_SECRET,
                                       transport=data, max_retries=0)

        async def prepare_reconciliation() -> None:
            from quantbot.runtime import build_runtime, runtime_paths

            if runtime_paths().database.resolve() != args.database.resolve():
                raise ValueError('close reconciliation database binding mismatch')
            runtime = build_runtime(settings)
            try:
                with SingleWriterLock(runtime.paths.lock):
                    result = await runtime.recovery.recover(
                        'daily-close-' + str(uuid4()), started_at=datetime.now(UTC))
                    if (result.reconciliation.status.value != 'RECONCILED'
                            or 'POSITION_LEDGER_MISMATCH' in result.reasons
                            or result.unresolved_intent_ids):
                        raise ValueError('close reconciliation unverified')
            finally:
                await runtime.aclose()
                runtime.database.close()

        result = await asyncio.wait_for(capture_close(
            args.directory, args.database, broker, calendar,
            expected_account_id=settings.EXPECTED_ACCOUNT_ID or '', now=datetime.now(UTC),
            clock=lambda: datetime.now(UTC), prepare_reconciliation=prepare_reconciliation,
            broker_close_reference=args.broker_close_reference),
            timeout=45)
        print(json.dumps(result))
    finally:
        await transport.aclose()
        await data.aclose()


if __name__ == '__main__':
    raise SystemExit(main())

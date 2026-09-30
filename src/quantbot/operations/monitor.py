"""Independent receipt monitor: read-only operational inputs, dedicated alert output.

Run separately from the learner. Neither scheduler installation nor external delivery is
implied by an invocation. An optional bounded PAPER GET probe establishes market hours;
source stores are never migrated or written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from quantbot.console.market_status import MarketStatus, paper_market_probe
from quantbot.operations.alerts import AlertStore, Condition


@dataclass(frozen=True)
class MonitorPolicy:
    learner_seconds: int = 900
    protection_seconds: int = 1200
    reconciliation_seconds: int = 3600
    ingestion_seconds: int = 2700
    digest_seconds: int = 86400

    def __post_init__(self) -> None:
        if any(type(v) is not int or v <= 0 for v in vars(self).values()):
            raise ValueError('positive integer monitor thresholds required')


QUERIES = {
    'PROTECTION': "SELECT completed_at, outcome FROM position_protection_cycles "
    "ORDER BY completed_at DESC LIMIT 1",
    'RECONCILIATION': "SELECT completed_at, status FROM reconciliation_runs "
    "ORDER BY completed_at DESC LIMIT 1",
    'INGESTION': "SELECT finished_at, status FROM ingestion_runs WHERE mode='LIVE' "
    "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1",
}


def _samples(learner: Path, database: Path) -> dict[str, object]:
    samples: dict[str, object] = dict.fromkeys(('LEARNER', *QUERIES))
    try:
        worker = learner / 'worker.json'
        if worker.stat().st_size > 1_000_000:
            raise ValueError('receipt too large')
        doc = json.loads(worker.read_text(encoding='utf-8-sig'))
        deployment = json.loads((learner / 'deployment.json').read_text(encoding='utf-8-sig'))
        if not isinstance(doc['run_id'], str):
            raise ValueError('receipt needs a run ID')
        UUID(doc['run_id'])
        pin = doc['code_commit']
        if (not isinstance(pin, str) or len(pin) != 40
                or any(c not in '0123456789abcdef' for c in pin)
                or pin != deployment['learner_commit']
                or doc['status'] not in ('STARTED', 'BLOCKED', 'ERROR')
                or doc['trading_enabled'] is not False or doc['promotion'] != 'NONE'):
            raise ValueError('invalid completed worker receipt')
        started = datetime.fromisoformat(doc['started_at'])
        if started.tzinfo is None:
            raise ValueError('invalid receipt start')
        if doc['status'] == 'STARTED':
            if doc['finished_at'] is not None:
                raise ValueError('started receipt cannot be completed')
            samples['LEARNER'] = dict(phase='STARTED', pin=pin)
            return _operational_samples(samples, database)
        finished = datetime.fromisoformat(doc['finished_at'])
        if started.tzinfo is None or finished.tzinfo is None or started > finished:
            raise ValueError('invalid receipt interval')
        blockers = doc.get('blockers')
        samples['LEARNER'] = dict(
            phase='COMPLETED', pin=pin, at=doc['finished_at'],
            protected_wait=(doc['status'] == 'BLOCKED' and isinstance(blockers, list)
                            and 'PROTECTED_FEATURE_WINDOW' in blockers),
        )
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _operational_samples(samples, database)


def _operational_samples(samples: dict[str, object], database: Path) -> dict[str, object]:
    try:
        with closing(sqlite3.connect(
            database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5
        )) as c:
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            for subsystem, query in QUERIES.items():
                try:
                    row = c.execute(query).fetchone()
                    success = {'PROTECTION': 'HEALTHY', 'RECONCILIATION': 'RECONCILED',
                               'INGESTION': 'SUCCEEDED'}[subsystem]
                    samples[subsystem] = row[0] if row and row[1] == success else None
                except sqlite3.Error:
                    pass
    except sqlite3.Error:
        pass
    return samples


def run_monitor(
    learner: Path, database: Path, output: Path, *, now: datetime,
    policy: MonitorPolicy | None = None,
    market: MarketStatus | None = None,
) -> dict[str, Any]:
    policy = policy or MonitorPolicy()
    if now.tzinfo is None:
        raise ValueError('aware clock required')
    if (output / 'alerts.db').resolve() == database.resolve():
        raise ValueError('alert output must not be the source database')
    limits = dict(LEARNER=policy.learner_seconds, PROTECTION=policy.protection_seconds,
                  RECONCILIATION=policy.reconciliation_seconds, INGESTION=policy.ingestion_seconds)
    conditions = []
    measurements = {}
    retained = []
    current_market = (market or MarketStatus()).current(now)
    verified = current_market.reported_open is not None and current_market.warning is None
    forecast_wait = 'UNVERIFIED_SESSION'
    if verified:
        forecast_wait = 'EXPECTED_CLOSED_SESSION'
        if current_market.reported_open:
            forecast_wait = ('EXPECTED_FORECAST_WARMUP' if current_market.session_open
                             and (now - current_market.session_open).total_seconds() < 1860
                             else 'ELIGIBLE_SESSION')
    samples = _samples(learner, database)
    with AlertStore(output / 'alerts.db') as store:
        with store.connection:
            store._time(now)
        sample = samples['LEARNER']
        if isinstance(sample, dict):
            if (sample.get('protected_wait') is True and isinstance(sample.get('at'), str)
                    and 0 <= (now - datetime.fromisoformat(sample['at'])).total_seconds()
                    <= policy.learner_seconds):
                forecast_wait = 'PROTECTED_FEATURE_WINDOW'
            if sample['phase'] == 'STARTED':
                previous = store.state('completed_learner')
                existing = any(a['code'] == 'LEARNER_HEARTBEAT_STALE' for a in store.active())
                samples['LEARNER'] = (previous.get('at') if
                    previous.get('pin') == sample['pin'] and not existing else None)
            else:
                samples['LEARNER'] = sample['at']
                if datetime.fromisoformat(sample['at']) <= now:
                    store.save_state('completed_learner',
                                     {'at': sample['at'], 'pin': sample['pin']})
    for subsystem, value in samples.items():
        age = None
        if isinstance(value, str):
            try:
                at = datetime.fromisoformat(value)
                if at.tzinfo is not None:
                    age = (now - at).total_seconds()
            except ValueError:
                pass
        valid = age is not None and age >= 0
        stale = not valid or (age is not None and age > limits[subsystem])
        status = 'STALE' if stale else 'FRESH'
        if stale and valid and verified and subsystem in ('PROTECTION', 'RECONCILIATION'):
            if current_market.reported_open is False:
                status = 'EXPECTED_WAIT'
            elif (current_market.session_open is not None
                  and isinstance(value, str)
                  and datetime.fromisoformat(value) < current_market.session_open
                  and 0 <= (now - current_market.session_open).total_seconds() < limits[subsystem]):
                status = 'SESSION_STARTUP_GRACE'
            if status != 'STALE':
                retained.append(subsystem + '_HEARTBEAT_STALE')
                stale = False
        measurements[subsystem] = dict(status=status,
                                       age_seconds=age if valid else None)
        if stale:
            conditions.append(Condition(
                subsystem + '_HEARTBEAT_STALE', subsystem.lower(),
                'WARNING' if subsystem == 'INGESTION' and valid else 'CRITICAL',
                'UNVERIFIED_COMPONENT_HEALTH', 'Engineering / Operator',
                'Inspect component receipts, scheduler and safe failure codes. '
                'A missing, invalid or overdue successful measurement cannot establish '
                'current health. Preserve PAPER limits, kill switch and protection controls.',
            ))
    with AlertStore(output / 'alerts.db') as store:
        events = store.update(conditions, now=now, retain_codes=tuple(retained))
        digest = None
        last_digest_at = store.state('digest').get('at')
        if (not last_digest_at or
                (now - datetime.fromisoformat(last_digest_at)).total_seconds()
                >= policy.digest_seconds):
            noncritical = [a for a in store.active() if a['severity'] != 'CRITICAL']
            if noncritical:
                digest = dict(at=now.isoformat(), alerts=noncritical,
                              route='LOCAL_RECEIPT', external_delivery='NOT_CONFIGURED')
                store.connection.execute('INSERT INTO events(document) VALUES (?)',
                                         (json.dumps(dict(digest, event='DIGEST')),))
            store.save_state('digest', {'at': now.isoformat()})
        receipt = dict(measured_at=now.isoformat(), measurements=measurements,
                       forecast_wait=forecast_wait,
                       active_alerts=store.active(), notifications=events, digest=digest,
                       external_delivery='NOT_CONFIGURED')
    temporary = output / 'receipt.json.tmp'
    temporary.write_text(json.dumps(receipt), encoding='utf-8')
    temporary.replace(output / 'receipt.json')
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Read-only component freshness monitor.')
    parser.add_argument('--learner-directory', required=True, type=Path)
    parser.add_argument('--operations-database', required=True, type=Path)
    parser.add_argument('--output-directory', required=True, type=Path)
    parser.add_argument('--probe-paper-market', action='store_true',
                        help='Use existing PAPER GET clock/calendar (five second limit).')
    args = parser.parse_args(argv)
    try:
        market = MarketStatus()
        if args.probe_paper_market:
            try:
                probe = paper_market_probe()

                async def bounded_probe() -> MarketStatus:
                    return await asyncio.wait_for(probe(), timeout=5)

                market = asyncio.run(bounded_probe())
            except Exception:
                # An unavailable probe must never turn an unknown session into a closure.
                market = MarketStatus()
        receipt = run_monitor(args.learner_directory, args.operations_database,
                              args.output_directory, now=datetime.now(UTC), market=market)
        print(json.dumps(receipt))
        return 1 if receipt['active_alerts'] else 0
    except (OSError, sqlite3.Error, ValueError):
        print('MONITOR_MEASUREMENT_FAILED; component health is unverified')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

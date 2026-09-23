"""Collect immutable intraday inputs, then measure one predeclared paper-only candidate.

Run collection and measurement separately. This program never submits broker orders or edits the
active strategy. Credentials are read only by collection and never copied to the research bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from quantbot.domain import Bar
from quantbot.strategy import XNYSSession

SYMBOLS = ("SPY", "QQQ", "IWM", "EFA", "EEM", "XLE", "XLF", "XLK", "XLI", "XLV")
START = "2026-03-02"
END = "2026-08-21"
RULE = "opening-range-breakout-30m-hold90m-long-v1"
NY = ZoneInfo("America/New_York")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)


def collect(env_file: Path, bundle: Path) -> None:
    from quantbot.operations.service import _parse_dotenv

    if bundle.exists():
        raise ValueError("input bundle already exists; immutable inputs cannot be overwritten")
    values = _parse_dotenv(env_file)
    if values.get("TRADING_MODE", "PAPER") != "PAPER":
        raise ValueError("paper configuration required")
    headers = {
        "APCA-API-KEY-ID": values["ALPACA_PAPER_API_KEY"],
        "APCA-API-SECRET-KEY": values["ALPACA_PAPER_API_SECRET"],
    }
    with httpx.Client(headers=headers, timeout=30) as client:
        calendar = client.get(
            "https://paper-api.alpaca.markets/v2/calendar", params={"start": START, "end": END}
        )
        calendar.raise_for_status()
        sessions = [
            XNYSSession(
                session_date=date.fromisoformat(row["date"]),
                open_at=datetime.fromisoformat(row["date"] + "T" + row["open"]).replace(tzinfo=NY),
                close_at=datetime.fromisoformat(row["date"] + "T" + row["close"]).replace(
                    tzinfo=NY
                ),
            )
            for row in calendar.json()
        ]
        if not sessions:
            raise ValueError("no exchange sessions returned")
        by_day = {session.session_date: session for session in sessions}
        params = {
            "symbols": ",".join(SYMBOLS),
            "timeframe": "15Min",
            "feed": "sip",
            "adjustment": "raw",
            "start": START + "T00:00:00Z",
            "end": END + "T23:59:59Z",
            "limit": "10000",
        }
        histories: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in SYMBOLS}
        tokens: set[str] = set()
        pages = 0
        while True:
            response = client.get("https://data.alpaca.markets/v2/stocks/bars", params=params)
            response.raise_for_status()
            payload = response.json()
            pages += 1
            for symbol, records in payload.get("bars", {}).items():
                if symbol not in histories:
                    raise ValueError("unexpected symbol in source response")
                for raw in records:
                    timestamp = datetime.fromisoformat(raw["t"].replace("Z", "+00:00"))
                    session = by_day.get(timestamp.astimezone(NY).date())
                    if session is None or not session.open_at <= timestamp < session.close_at:
                        continue
                    bar = Bar(
                        symbol=symbol,
                        timestamp=timestamp,
                        open=str(raw["o"]),
                        high=str(raw["h"]),
                        low=str(raw["l"]),
                        close=str(raw["c"]),
                        volume=str(raw["v"]),
                        adjustment="1",
                    )
                    histories[symbol].append(bar.model_dump(mode="json"))
            token = payload.get("next_page_token")
            if not token:
                break
            if token in tokens or pages >= 100:
                raise ValueError("pagination did not terminate within its bound")
            tokens.add(token)
            params["page_token"] = token
    for history in histories.values():
        history.sort(key=lambda row: row["timestamp"])
    content = {
        "rule": RULE,
        "feed": "sip",
        "timeframe": "15Min",
        "adjustment": "raw",
        "start": START,
        "end": END,
        "symbols": SYMBOLS,
        "sessions": [session.model_dump(mode="json") for session in sessions],
        "histories": histories,
    }
    result = {
        "collected_at": datetime.now(UTC).isoformat(),
        "content_hash": digest(content),
        "content": content,
    }
    write_new(bundle, result)
    print(
        json.dumps(
            {
                "bundle": str(bundle),
                "content_hash": result["content_hash"],
                "sessions": len(sessions),
                "pages": pages,
                "bar_counts": {symbol: len(rows) for symbol, rows in histories.items()},
            }
        )
    )


def measure(bundle: Path, database: Path, output: Path) -> None:
    from sqlalchemy import select

    from quantbot.research.intraday import simulate
    from quantbot.research.intraday_accounting import record_intraday_attempt
    from quantbot.storage import Database, SingleWriterLock
    from quantbot.storage.schema import search_runs

    document = json.loads(bundle.read_text(encoding="utf-8"))
    content = document["content"]
    if digest(content) != document["content_hash"]:
        raise ValueError("input content hash differs")
    if (
        content["rule"] != RULE
        or tuple(content["symbols"]) != SYMBOLS
        or content["start"] != START
        or content["end"] != END
        or content["feed"] != "sip"
        or content["timeframe"] != "15Min"
        or content["adjustment"] != "raw"
    ):
        raise ValueError("input is outside the frozen candidate specification")
    if output.exists():
        raise ValueError("measurement result already exists; no automatic replay")
    # Binding static simulator code makes changed implementations distinct attempted tests.
    import quantbot.research.intraday as implementation

    code_hash = hashlib.sha256(Path(implementation.__file__).read_bytes()).hexdigest()
    config_hash = digest(
        {
            "rule": RULE,
            "implementation": code_hash,
            "input_hash": document["content_hash"],
            "cost_cases": [5, 10],
        }
    )
    identity = "intraday:" + config_hash
    with (
        SingleWriterLock(database.with_suffix(".intraday-research.lock")),
        Database(database) as db,
    ):
        with db.transaction() as session:
            if (
                session.scalar(
                    select(search_runs.c.search_id).where(search_runs.c.search_id == identity)
                )
                is not None
            ):
                raise ValueError("attempt already charged; interrupted measurements need review")
            record_intraday_attempt(
                session,
                search_id=identity,
                configuration_hash=config_hash,
                dataset="us-equities-daily",
                start=date.fromisoformat(START),
                end=date.fromisoformat(END),
                now=datetime.now(UTC),
            )
        histories = {
            symbol: tuple(Bar.model_validate(row) for row in rows)
            for symbol, rows in content["histories"].items()
        }
        sessions = tuple(XNYSSession.model_validate(row) for row in content["sessions"])
        # Outside the DB write transaction, after the attempted test was committed.
        baseline = simulate(histories, sessions, slippage_bps=5)
        stressed = simulate(histories, sessions, slippage_bps=10)
        result = {
            "search_id": identity,
            "configuration_hash": config_hash,
            "input_hash": document["content_hash"],
            "input_bundle": str(bundle),
            "implementation_hash": code_hash,
            "measured_at": datetime.now(UTC).isoformat(),
            "baseline": baseline.model_dump(mode="json"),
            "cost_stress": stressed.model_dump(mode="json"),
            "status": "EXPLORATORY_ONLY",
            "promotion_authorized": False,
            "limitations": [
                "Historical discovery window; no confirmation or forward paper fills",
                "SIP historical input; current SIP quote entitlement unavailable",
                "Missing symbol-days are excluded, not imputed",
                "Costs are assumptions; real spread, impact and latency may differ",
            ],
        }
        write_new(output, result)
        from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict

        with db.transaction() as session:
            ResearchMemory(session).record(
                ResearchRecord(
                    record_id=identity + ":result",
                    kind=RecordKind.FINDING,
                    subject="Intraday opening-range breakout with maximum 90-minute holdings",
                    statement=(
                        f"Exploratory fixed-rule intraday test: {baseline.stats.trade_count} "
                        f"trades across {len(sessions)} sessions; net return "
                        f"{baseline.stats.net_return} at 5bps per side and "
                        f"{stressed.stats.net_return} at 10bps per side. "
                        "No forward validation or strategy promotion authorized."
                    ),
                    source="intraday-research/v1",
                    verdict=Verdict.EXPLORATORY_ONLY,
                    recorded_at=datetime.now(UTC),
                    evidence_hash=digest(result),
                    detail={
                        "search_id": identity,
                        "artifact": str(output),
                        "input_hash": document["content_hash"],
                        "baseline_stats": baseline.stats.model_dump_json(),
                        "stress_stats": stressed.stats.model_dump_json(),
                    },
                )
            )
        print(
            json.dumps(
                {
                    "output": str(output),
                    "search_id": identity,
                    "baseline": baseline.stats.model_dump(mode="json"),
                    "cost_stress": stressed.stats.model_dump(mode="json"),
                    "status": "EXPLORATORY_ONLY",
                    "promotion_authorized": False,
                }
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "measure"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "collect" and args.env_file:
        collect(args.env_file, args.bundle)
    elif args.action == "measure" and args.database and args.output:
        measure(args.bundle, args.database, args.output)
    else:
        parser.error("collection needs --env-file; measurement needs --database and --output")


if __name__ == "__main__":
    main()

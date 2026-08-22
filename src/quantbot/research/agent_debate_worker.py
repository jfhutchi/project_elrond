"""One-request JSON worker running the TradingAgents graph (#37).

Staged into the TradingAgents virtualenv and run as a child process, exactly as the Kronos
worker is. It must not import Elrond: this is third-party code that reaches the network, and
the boundary is the only thing that makes that acceptable.

**The trading decision is dropped here, at the far side of the boundary**, rather than in the
caller. `propagate()` returns it as its second value and `final_trade_decision` sits at the top
level of the graph state, so a caller that forgot to strip it would be the normal case rather
than the exceptional one. Dropping it in the worker means the decision never crosses the
process boundary at all, and a test asserts it is absent from what does.

What survives is the bull and bear debate -- the preserved material disagreement #37 asks for,
and the part this project's architecture review judged worth having.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

#: Keys of the graph state that may cross the boundary. An allowlist rather than a denylist:
#: a new field in a future TradingAgents revision should arrive blocked, not admitted, because
#: the field most likely to be added to a trading framework is another opinion about direction.
PERMITTED_STATE = ("investment_debate_state", "risk_debate_state")

#: Debate texts worth carrying. `judge_decision` is deliberately excluded from the investment
#: debate: it is the synthesis that resolves the disagreement, and resolving it is what destroys
#: the only part a critic can act on.
PERMITTED_DEBATE = ("bull_history", "bear_history")
PERMITTED_RISK = ("aggressive_history", "conservative_history", "neutral_history")

#: Anything matching these must never appear in the returned payload.
FORBIDDEN_KEYS = ("final_trade_decision", "judge_decision", "trader_investment_plan")


class WorkerRefused(RuntimeError):
    """Raised when the graph cannot be run or its output cannot be trusted."""


def extract(state: dict[str, Any]) -> dict[str, str]:
    """Pull the debate out of the graph state, leaving every conclusion behind."""
    out: dict[str, str] = {}
    debate = state.get("investment_debate_state") or {}
    for key in PERMITTED_DEBATE:
        value = str(debate.get(key) or "").strip()
        if value:
            out[key] = value
    risk = state.get("risk_debate_state") or {}
    for key in PERMITTED_RISK:
        value = str(risk.get(key) or "").strip()
        if value:
            out[key] = value
    if not out:
        # A graph that produced no debate produced nothing this project wants. Returning an
        # empty bundle would read downstream as "the agents considered it and had nothing to
        # say", which is a claim about the hypothesis rather than about the run.
        raise WorkerRefused("the graph returned no bull or bear debate; there is nothing to carry")
    return out


def installed_revision(repository: str, expected: str) -> str:
    """Read the revision from the checkout itself, and refuse a mismatch.

    The parent used to pass the expected revision in and the child echoed it back, which proved
    only that the child returned the string it was given. That is a worker self-reporting its
    own provenance -- the exact pattern #23 closed for search cardinality, rebuilt here by the
    person who closed it.

    `git rev-parse HEAD` in the installed checkout is measured from the dependency. A dirty tree
    is refused too: a modified checkout is not the revision it claims to be.
    """
    import subprocess  # noqa: PLC0415

    try:
        head = subprocess.run(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=60,
        )
        dirty = subprocess.run(
            ["git", "-C", repository, "status", "--porcelain=v1", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkerRefused("could not read the installed TradingAgents revision") from error
    if head.returncode != 0:
        raise WorkerRefused("the TradingAgents repository does not report a revision")
    actual = head.stdout.strip()
    if actual != expected:
        raise WorkerRefused(f"the installed TradingAgents is {actual}, not the reviewed {expected}")
    if dirty.returncode == 0 and dirty.stdout.strip():
        raise WorkerRefused("the TradingAgents checkout is modified; it is not the pinned code")
    return actual


def encode(payload: dict[str, Any]) -> str:
    """Serialise the payload, refusing if any conclusion survived into it.

    The last line of defence, and separate from `main` so a test can reach it. It was inline,
    and a mutation that reintroduced the decision passed the whole suite because nothing
    exercised `main` -- the guard existed and was never asked to guard anything.

    A substring scan rather than a key check, because the leak that matters is a conclusion
    embedded in a debate string, not a tidy extra field.
    """
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    for forbidden in FORBIDDEN_KEYS:
        if forbidden in encoded:
            raise WorkerRefused(f"{forbidden} reached the payload; the boundary leaked")
    return encoded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradingagents-worker")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--expect-revision", required=True)
    args = parser.parse_args(argv)

    # Imported dynamically, as the Kronos worker does. TradingAgents is not an Elrond
    # dependency and must never become one -- it exists only inside this worker's own
    # virtualenv, and a static import here would make Elrond's type check demand it.
    import importlib  # noqa: PLC0415

    config_module = importlib.import_module("tradingagents.default_config")
    graph_module = importlib.import_module("tradingagents.graph.trading_graph")

    graph = graph_module.TradingAgentsGraph(debug=False, config=dict(config_module.DEFAULT_CONFIG))
    # `propagate` returns (state, decision). The decision is bound to `_` and never referenced:
    # it is the one output of this framework that has no route into Elrond.
    state, _ = graph.propagate(args.ticker, args.trade_date)

    payload = {
        "ticker": args.ticker,
        "trade_date": args.trade_date,
        "revision": installed_revision(args.repository, args.expect_revision),
        "debate": extract(dict(state)),
    }
    sys.stdout.write(encode(payload) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

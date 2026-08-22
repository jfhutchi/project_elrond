"""TradingAgents behind the worker boundary, producing material rather than conclusions (#37).

**This module is deliberately not called `tradingagents`.** It was, and the worker then died
with `ModuleNotFoundError: No module named 'quantbot'` -- because running a script puts its own
directory on `sys.path[0]`, so the child imported *this* file instead of the third-party
`tradingagents` package it was reaching for. Naming a wrapper after the package it wraps is a
collision waiting for exactly that. The worker is now also staged outside the Elrond tree before
it runs, which is the boundary property the Kronos provider already relies on.

Running the framework (pinned `a33fd4c0`) established three things that shaped this module, and
each of them is a measurement rather than a preference:

1. **Its data layer fails open.** Reddit returned HTTP 429 twice and the run continued to a
   confident directional call, logging the failure and reporting anyway. An unreachable source
   read as "no evidence" rather than as an outage -- the failure #4 exists to prevent. So its
   output is marked `UNVERIFIABLE`: this project cannot establish which sources it actually saw.
2. **It is not reproducible.** Two identical invocations returned `Hold` and then `Buy`. No seed
   is exposed for the debate, so a rerun cannot be expected to reproduce an artifact.
3. **The decision is the most prominent thing it returns.** `propagate()` hands it back as the
   second value. It is dropped in the worker, on the far side of the process boundary.

**The debate becomes `Source` records, not a `SemanticAnalysis`.** That is the design decision
worth stating, because the obvious alternative fails: real bull/bear text is saturated with
"buy" and "sell", so mapping it straight into an analysis would trip the leaked-directional-call
refusal in `semantic.py` on every run -- and a refusal that fires on everything is one somebody
removes. Feeding it to `SemanticWorker` as material instead leaves every existing refusal doing
its job, and the confounders and falsification tests come out of the worker that was built to
produce them.

Every record is `derived=True`. `Source` already refuses to let a derived artifact be cited as
evidence, which is exactly right for an LLM debate: it may be read, and it may not be a reason
to believe anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from quantbot.research.sources import Source, SourceHealth, SourceKind

#: The reviewed upstream revision. Pinned for the same reason Kronos is: "TradingAgents said so"
#: is not provenance unless it says which TradingAgents.
TRADINGAGENTS_REVISION = "a33fd4c0f134485a43553a2c23a63cb14adbd88f"
PARSER_VERSION = "tradingagents-debate-v1"

#: Environment the child is allowed to see. Everything else is dropped, including every broker
#: and application credential -- this is third-party code that opens sockets.
_SAFE_PARENT_ENVIRONMENT = frozenset({"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "USER"})

#: Which speaker each debate key belongs to, for the record's `publisher`.
_SPEAKERS = {
    "bull_history": "TradingAgents bull researcher",
    "bear_history": "TradingAgents bear researcher",
    "aggressive_history": "TradingAgents aggressive risk analyst",
    "conservative_history": "TradingAgents conservative risk analyst",
    "neutral_history": "TradingAgents neutral risk analyst",
}


class TradingAgentsUnavailable(RuntimeError):
    """Raised when the framework could not be run or returned something unusable."""


def worker_environment(
    parent: Mapping[str, str], *, endpoint: str, model: str, cache_directory: Path
) -> dict[str, str]:
    """Build the child's environment: local model, no credentials, no Elrond.

    The model is pinned to a local endpoint rather than defaulted. A framework that silently
    reached a hosted provider would spend money nobody authorised and send this project's
    research questions to a third party.
    """
    environment = {key: value for key, value in parent.items() if key in _SAFE_PARENT_ENVIRONMENT}
    environment.update(
        {
            "TRADINGAGENTS_LLM_PROVIDER": "openai",
            "TRADINGAGENTS_LLM_BACKEND_URL": endpoint,
            "TRADINGAGENTS_DEEP_THINK_LLM": model,
            "TRADINGAGENTS_QUICK_THINK_LLM": model,
            "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
            "TRADINGAGENTS_MAX_RISK_ROUNDS": "1",
            "TRADINGAGENTS_CACHE_DIR": str(cache_directory),
            "TRADINGAGENTS_RESULTS_DIR": str(cache_directory / "results"),
            # The OpenAI-compatible client requires a key to be present; a local Ollama endpoint
            # ignores its value. Set to an explicit placeholder so no real key is ever read from
            # the parent and the intent is legible in a process listing.
            "OPENAI_API_KEY": "local-endpoint-no-key-required",
        }
    )
    return environment


class TradingAgentsSourceProvider:
    """Runs the pinned framework out of process and returns its debate as source material."""

    def __init__(
        self,
        *,
        python_executable: str | Path,
        cache_directory: str | Path,
        endpoint: str,
        model: str,
        timeout_seconds: int = 3600,
    ) -> None:
        interpreter = Path(python_executable).expanduser().absolute()
        if not interpreter.is_file():
            raise ValueError("python_executable must be an existing dedicated interpreter")
        source_root = Path(__file__).resolve().parents[3]
        # Symlink-aware, for the reason the Kronos provider records: `.resolve()` on a venv
        # interpreter follows the link to the base interpreter and launches the wrong one.
        if _inside(interpreter, source_root) or _inside(interpreter.resolve(), source_root):
            raise ValueError("the TradingAgents interpreter must be outside the Elrond tree")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._interpreter = interpreter
        self._cache = Path(cache_directory).expanduser().absolute()
        self._endpoint = endpoint
        self._model = model
        self._timeout = timeout_seconds

    def _stage_worker(self) -> Path:
        """Copy the worker out of the Elrond tree, named by its own digest.

        The same pattern as the Kronos provider, for the same two reasons: the child should not
        be executing a file that sits inside the tree it must not import, and a digest-named
        copy makes it obvious which version ran. The digest is verified rather than assumed, so
        a staged file that no longer matches is refused instead of silently reused.
        """
        source = Path(__file__).with_name("agent_debate_worker.py").resolve()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        staged = self._cache / f"agent-debate-worker-{digest}.py"
        if staged.exists():
            if hashlib.sha256(staged.read_bytes()).hexdigest() != digest:
                raise TradingAgentsUnavailable("the staged worker has conflicting content")
        else:
            shutil.copyfile(source, staged)
        return staged

    def fetch(self, ticker: str, trade_date: str, *, now: datetime) -> tuple[Source, ...]:
        """Run the graph and return its debate as derived source records, or raise."""
        self._cache.mkdir(parents=True, exist_ok=True)
        worker = self._stage_worker()
        try:
            completed = subprocess.run(
                [
                    str(self._interpreter),
                    "-B",
                    str(worker),
                    "--ticker",
                    ticker,
                    "--trade-date",
                    trade_date,
                    "--revision",
                    TRADINGAGENTS_REVISION,
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                shell=False,
                cwd=self._cache,
                env=worker_environment(
                    os.environ,
                    endpoint=self._endpoint,
                    model=self._model,
                    cache_directory=self._cache,
                ),
            )
        except subprocess.TimeoutExpired as error:
            raise TradingAgentsUnavailable("the TradingAgents graph did not finish") from error
        except OSError as error:
            raise TradingAgentsUnavailable("the TradingAgents worker could not start") from error
        if completed.returncode != 0:
            # stderr is not interpolated: it is arbitrary text from third-party code and can
            # echo back whatever it fetched.
            raise TradingAgentsUnavailable(
                f"the TradingAgents worker exited {completed.returncode}"
            )
        try:
            payload = json.loads(completed.stdout)
        except ValueError as error:
            raise TradingAgentsUnavailable("the worker did not return JSON") from error
        return self.to_sources(payload, now=now)

    @staticmethod
    def to_sources(payload: Mapping[str, object], *, now: datetime) -> tuple[Source, ...]:
        """Turn a worker payload into derived, unverifiable source records.

        Separate from `fetch` so the mapping -- which is where the safety properties live -- can
        be tested without running a language model.
        """
        debate = payload.get("debate")
        if not isinstance(debate, Mapping) or not debate:
            raise TradingAgentsUnavailable("the payload carries no debate")
        ticker = str(payload.get("ticker", "")).strip()
        trade_date = str(payload.get("trade_date", "")).strip()
        revision = str(payload.get("revision", "")).strip()
        if not ticker or not trade_date:
            raise TradingAgentsUnavailable("a debate record needs the ticker and date it is about")
        if revision != TRADINGAGENTS_REVISION:
            raise TradingAgentsUnavailable(
                "the worker reports a different framework revision than the pinned one"
            )

        sources: list[Source] = []
        for key in sorted(debate):
            text = str(debate[key])
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            sources.append(
                Source(
                    source_id=f"tradingagents:{ticker}:{trade_date}:{key}:{digest[:12]}",
                    kind=SourceKind.ALTERNATIVE,
                    uri=f"tradingagents://{revision}/{ticker}/{trade_date}/{key}",
                    title=f"{_SPEAKERS.get(key, key)} on {ticker}",
                    publisher=_SPEAKERS.get(key, "TradingAgents"),
                    # The debate is about this trading date, and is knowable no earlier. Using
                    # `now` would let a backtest read it before the date it discusses.
                    published_at=datetime.fromisoformat(f"{trade_date}T00:00:00+00:00"),
                    retrieved_at=now,
                    content_hash=digest,
                    parser_version=PARSER_VERSION,
                    excerpt=text[:2000],
                    # Its fetchers fail open, so this project cannot establish which sources the
                    # debate actually rests on. `UNVERIFIABLE` is the honest health, not a
                    # pessimistic one.
                    health=SourceHealth.UNVERIFIABLE,
                    # An LLM artifact produced from sources. `Source` refuses to let a derived
                    # record be cited as evidence, which is exactly the standing this deserves.
                    derived=True,
                    derived_from=f"tradingagents@{revision}",
                )
            )
        return tuple(sources)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


__all__ = [
    "PARSER_VERSION",
    "TRADINGAGENTS_REVISION",
    "TradingAgentsSourceProvider",
    "TradingAgentsUnavailable",
    "worker_environment",
]

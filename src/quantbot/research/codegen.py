"""Model-authored experiment code, and the boundary it runs behind (#8, #21, #18).

`ExperimentRunner` takes a `Measurement` callable supplied by trusted application code. That is
correct as far as it goes and it is not what #8 asked for: the issue wants feature and strategy
code *generated*, executed under isolation, and bound to the production evaluation path.

This module does all three, the third only since the operator authorised
`BenchmarkVariant.EXTERNAL_SIGNAL` and the `external_weights` parameter on `BacktestEngine.run()`.

**Why the binding waited for that.** The engine had no parameter through which an external signal
could arrive, so reaching the production evaluation path meant changing trusted trading code
shared with the live paper daemon. The alternative -- scoring the generated signal here against a
reimplementation of the fill and cost model -- would have created a second scoring path that can
disagree with the first, and a number two paths compute differently is worse than a number one
path cannot yet compute. So it was described and left for a deliberate decision rather than
approximated.

`measurement_from_signal` is the binding. The generated weights go through the same engine, the
same fill model, the same costs and the same look-ahead protection every comparator uses.

**What is enforced here.**

*Generated code never runs outside kernel isolation.* `SandboxRunner` reports `os_enforced=False`
and is refused, exactly as anomaly mining refuses it. Model-authored code is the strongest case
for that rule: nobody reviewed it, and under the Windows backend an absolute path still resolves.

*The code is content-addressed before it runs.* The hash covers what was executed, so a manifest
can name it and a later reader can tell whether the same code produced a different answer or
different code produced the same one.

*Everything the run emitted is captured.* stdout, stderr and artifacts travel with the result,
because #18's outstanding box is that generated-code output be automatically connected to the
result bundle rather than reconstructed from logs somebody may not have kept.

*A run that was stopped produces nothing.* A signal series from a script the sandbox killed is a
partial computation over an unknown fraction of the data, and reading it as a result would be the
same error as treating a worker timeout as a search that found nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from quantbot.backtest.benchmarks import BenchmarkVariant
from quantbot.backtest.engine import BacktestEngine
from quantbot.domain import Bar
from quantbot.research.builder import REQUIRED_PROBES, ExperimentPlan
from quantbot.research.causality import CausalityReport
from quantbot.research.mining import IsolatedSandbox
from quantbot.research.models import ModelResponse, ModelRole, ModelRuntime, PromptTemplate
from quantbot.research.registry import Registration
from quantbot.research.runner import Measured, Measurement, ProtectedAccess

SIGNAL_PROMPT = PromptTemplate(
    name="signal-implementation",
    version="1",
    text="""Write a Python script that computes a trading signal. It runs with no network, no
installed packages beyond the standard library, and no access to anything except the file
described below.

Read "inputs/bars.json": an object mapping symbol to a list of daily bars, each with keys
"date" (YYYY-MM-DD), "open", "high", "low", "close" and "volume", all as strings. Bars are
ordered oldest first.

Hypothesis: {question}
Prediction: {prediction}
Universe: {universe}
Features permitted: {features}

Rules:
- Use only past and current bars when computing the signal for a date. Using a later bar is
  look-ahead and invalidates the entire experiment.
- Emit one line of JSON on stdout as the last line: {{"signal": {{"YYYY-MM-DD": {{"SYM": "0.0"}}}}}}
  where each value is a target portfolio weight as a decimal string between "0" and "1".
- Print nothing else on the last line. Progress output on earlier lines is fine.
- Do not import anything outside the standard library.

Return only the Python source, with no markdown fence and no commentary.""",
)


class GenerationRefused(RuntimeError):
    """Raised when generated code cannot be accepted or its output cannot be trusted."""


@dataclass(frozen=True, slots=True)
class GeneratedCode:
    """Source a model wrote, with enough provenance to say which source ran."""

    source: str
    sha256: str
    response: ModelResponse

    @property
    def provenance(self) -> dict[str, str]:
        """What #18 needs about the code, in the shape a manifest stores."""
        return {
            "code_sha256": self.sha256,
            "model": self.response.provenance().model,
            "prompt_template_hash": self.response.provenance().prompt_template_hash,
        }


@dataclass(frozen=True, slots=True)
class SignalRun:
    """One execution of generated code, and everything it emitted.

    `stdout`, `stderr` and `artifacts` are carried rather than logged, because #18 asks that
    generated-code output be automatically connected to the result bundle. Reconstructing it
    later from logs assumes somebody kept the logs.
    """

    signal: dict[str, dict[str, str]]
    code: GeneratedCode
    stdout: str
    stderr: str
    artifacts: tuple[str, ...]
    duration_seconds: float


def generate_signal(
    runtime: ModelRuntime, registration: Registration, *, now: datetime
) -> GeneratedCode:
    """Ask a model for an implementation of the registered hypothesis.

    Takes the frozen registration rather than a free-text brief, so the code is written against
    what was pre-registered rather than against whatever a caller feels like describing. A
    generator handed a different question from the one under test would produce code that
    measures something else, and the manifest would still say it measured this.
    """
    draft = registration.draft
    answer = runtime.call(
        ModelRole.CODER,
        SIGNAL_PROMPT,
        {
            "question": draft.question,
            "prediction": draft.prediction,
            "universe": ", ".join(draft.universe),
            "features": ", ".join(draft.features),
        },
        now=now,
    )
    source = _strip_fence(answer.text)
    if not source.strip():
        raise GenerationRefused("the model returned no source; silence is not an implementation")
    return GeneratedCode(
        source=source,
        sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        response=answer,
    )


def run_generated_signal(
    sandbox: IsolatedSandbox,
    code: GeneratedCode,
    bars: dict[str, list[dict[str, str]]],
) -> SignalRun:
    """Execute generated code under kernel isolation and return the signal it produced.

    Refuses a sandbox that only conceals. Model-authored code is the strongest case for that
    rule: nobody reviewed it, and under the Windows backend an absolute path still resolves to
    the dotenv and the durable ledger.
    """
    if not sandbox.os_enforced:
        raise GenerationRefused(
            "generated experiment code requires an OS-enforced sandbox; nobody reviewed this "
            "source, and under the Windows backend an absolute path still resolves"
        )
    if not bars:
        raise GenerationRefused("generated code needs bars to compute a signal from")

    script = _with_inputs(code.source, bars)
    result = sandbox.run(script)

    if result.terminated_reason is not None:
        raise GenerationRefused(
            f"the generated code was stopped ({result.terminated_reason}); a signal from a "
            "killed script covers an unknown fraction of the data and is not a result"
        )
    if not result.ok:
        raise GenerationRefused(
            f"the generated code failed with exit code {result.exit_code}"
        )

    return SignalRun(
        signal=_signal_from(result.stdout, bars),
        code=code,
        stdout=result.stdout,
        stderr=result.stderr,
        artifacts=tuple(item.name for item in result.artifacts),
        duration_seconds=result.duration_seconds,
    )


#: The plan's name for look-ahead detection. Imported rather than re-spelled: a probe reported
#: under a name the plan does not use is a probe the grader counts as missing, and the run would
#: be INCONCLUSIVE for a reason nobody could see.
LOOK_AHEAD_PROBE = REQUIRED_PROBES[0]


def measurement_from_signal(
    engine: BacktestEngine,
    histories: Mapping[str, Sequence[Bar]],
    run: SignalRun,
    *,
    causality: CausalityReport | None = None,
) -> Measurement:
    """Bind a generated signal to the production engine, as an `ExperimentRunner` measurement.

    This is #8's last box. The weights computed by model-authored code inside the sandbox are
    executed by the engine that would actually trade -- not by a second implementation of the
    fill model that could disagree with it.

    **The measurement reports; it does not grade.** `Measured` carries the effect and the probes
    that ran, and `ExperimentRunner` decides what those are allowed to be called. That separation
    already existed and matters more here than anywhere else: this is the one measurement whose
    code nobody reviewed, and letting it return its own verdict would put SURVIVED in the gift of
    the thing being judged.

    A backtest that produces no Sharpe reports `None` rather than zero. A strategy that never
    traded and a strategy that broke even are different findings, and zero is the answer to the
    second question.

    **`probes_run` reports what actually ran, which is usually less than the plan requires.**

    It used to report `plan.probes` -- the whole list, unconditionally. `ExperimentRunner._grade`
    refuses to read a number as evidence unless every required probe ran, and echoing the plan's
    own list back at it defeated that check completely: the one measurement whose code nobody
    reviewed was asserting that its own falsification attempts had happened. A generated
    experiment will therefore now usually grade INCONCLUSIVE, and that is the correct verdict for
    a run whose probes did not run.

    Pass `causality` to have the look-ahead probe count. A clean report contributes
    `shifted-feature-probe`; a report with violations contributes it to `probes_failed` instead,
    which disqualifies the result. A report that probed nothing contributes neither -- a check
    that could not run must not look like one that passed.
    """

    def measure(plan: ExperimentPlan, access: ProtectedAccess) -> Measured:
        # `access` is required by the protocol and unused here on purpose: the histories were
        # already consumed by the runner before this was called, which is what minting the token
        # means. Reaching for more data through it would be reading a window nobody spent.
        _ = access
        result = engine.run(
            histories,
            BenchmarkVariant.EXTERNAL_SIGNAL,
            external_weights=_weights_by_date(run.signal),
        )
        ran: list[str] = []
        failed: list[str] = []
        if causality is not None and causality.probed:
            if causality.violations:
                # It ran and objected. Reported as failed rather than omitted: "the probe did not
                # run" and "the probe caught something" are different, and only one of them is a
                # reason to disqualify rather than to re-run.
                failed.append(LOOK_AHEAD_PROBE)
            else:
                ran.append(LOOK_AHEAD_PROBE)
        return Measured(
            effect=result.metrics.sharpe,
            probes_run=tuple(ran),
            probes_failed=tuple(failed),
            # Which source actually ran, which model wrote it, under which prompt. Without this
            # the manifest for a generated experiment records a git commit that describes the
            # harness and says nothing about the code that computed the signal -- the one input
            # that most determines the result and the only one nobody reviewed.
            diagnostics=run.code.provenance,
        )

    return measure


def _weights_by_date(
    signal: dict[str, dict[str, str]],
) -> dict[date, dict[str, Decimal]]:
    """Convert the run's string weights into the engine's types.

    Decimal rather than float, because the engine works in Decimal throughout and a float here
    would introduce a rounding difference between what the model asked for and what was traded
    -- small, invisible, and exactly the kind of discrepancy that makes a result irreproducible.
    """
    converted: dict[date, dict[str, Decimal]] = {}
    for day, weights in signal.items():
        converted[date.fromisoformat(day)] = {
            symbol: Decimal(weight) for symbol, weight in weights.items()
        }
    return converted


def _with_inputs(source: str, bars: dict[str, list[dict[str, str]]]) -> str:
    """Prepend a preamble that materialises the input file the prompt promised.

    Written by this module rather than by the model, so the generated code cannot decide where
    its inputs come from. A script that could choose its own input path could choose one outside
    the workspace, and the whole point of copying data in is that it is the only data there is.
    """
    payload = json.dumps(bars, sort_keys=True)
    return (
        "import json as _json, pathlib as _pathlib\n"
        "_pathlib.Path('inputs').mkdir(exist_ok=True)\n"
        f"_pathlib.Path('inputs/bars.json').write_text({payload!r}, encoding='utf-8')\n"
        "del _json, _pathlib\n"
        f"{source}\n"
    )


def _signal_from(
    stdout: str, bars: dict[str, list[dict[str, str]]]
) -> dict[str, dict[str, str]]:
    """Read the signal from the run's last JSON line, refusing anything unusable.

    Every date and symbol is checked against the bars that were supplied. A signal for a date
    that was not in the inputs is either a hallucinated row or a look-ahead the script computed
    from somewhere else, and neither can be evaluated -- so the run is refused rather than
    silently trimmed to the dates that happen to line up.
    """
    for line in reversed(stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "signal" in parsed:
            break
    else:
        raise GenerationRefused(
            "the generated code printed no signal; nothing about the run can be evaluated"
        )

    signal = parsed["signal"]
    if not isinstance(signal, dict) or not signal:
        raise GenerationRefused("the generated code emitted an empty signal")

    known_dates = {bar["date"] for series in bars.values() for bar in series}
    known_symbols = set(bars)
    for day, weights in signal.items():
        if day not in known_dates:
            raise GenerationRefused(
                f"the signal names {day}, which was not among the bars supplied; a weight for a "
                "date the script was not given did not come from the data"
            )
        if not isinstance(weights, dict):
            raise GenerationRefused(f"the signal for {day} is not a mapping of symbol to weight")
        unknown = set(weights) - known_symbols
        if unknown:
            raise GenerationRefused(
                f"the signal names {', '.join(sorted(unknown))} on {day}, which is outside the "
                "universe the script was given"
            )
    return signal


def _strip_fence(text: str) -> str:
    """Remove a markdown fence if the model added one despite being told not to.

    Tolerated because it is a formatting habit rather than a claim -- unlike a wrong verdict or
    an invented citation, a fence changes nothing about what the code does. Everything inside it
    is still hashed and still runs under isolation.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    body = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
    return "\n".join(body).strip()


__all__ = [
    "SIGNAL_PROMPT",
    "GeneratedCode",
    "GenerationRefused",
    "SignalRun",
    "generate_signal",
    "measurement_from_signal",
    "run_generated_signal",
]

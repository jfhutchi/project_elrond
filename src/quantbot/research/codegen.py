"""Model-authored experiment code, and the boundary it runs behind (#8, #21, #18).

`ExperimentRunner` takes a `Measurement` callable supplied by trusted application code. That is
correct as far as it goes and it is not what #8 asked for: the issue wants feature and strategy
code *generated*, executed under isolation, and bound to the production evaluation path.

This module does the first two. It deliberately does not do the third, and the reason is worth
stating rather than working around.

**Why the binding is not here.** `BacktestEngine.run()` takes histories and a `BenchmarkVariant`;
it has no parameter through which an external signal could arrive. Making generated code reach the
production evaluation path therefore requires changing the engine, which is trusted trading code
shared with the live paper daemon. The alternative -- evaluating the generated signal here against
a reimplementation of the fill and cost model -- would create a second scoring path that can
disagree with the first, which is the failure class this project exists to resist. A number that
two code paths compute differently is worse than a number one path cannot yet compute.

So the seam is described precisely on #8 and left for a deliberate change to the engine, rather
than approximated.

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
from dataclasses import dataclass
from datetime import datetime

from quantbot.research.mining import IsolatedSandbox
from quantbot.research.models import ModelResponse, ModelRole, ModelRuntime, PromptTemplate
from quantbot.research.registry import Registration

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
    "run_generated_signal",
]

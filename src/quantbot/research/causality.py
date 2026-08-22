"""Does a generated signal read the future? Answered by running it twice (#7, #8).

`shifted_feature_probe` in `critic.py` has been able to detect look-ahead since it was written,
and nothing ran generated code twice to feed it. So the detector existed and the defect it
detects went uncaught -- the same shape as `consume()` and `compile_experiment()` having no
callers, and it went uncaught in the module whose input nobody reviewed.

It is not hypothetical. The first real generated experiment, written by `llama3.1:8b` and
executed under the WSL sandbox, computed its moving average **from the final bar of the series
for every date**. Every signal in that run knew the whole future. The output validation caught a
different defect in the same run -- a literal `"SYM"` placeholder -- and could not have caught
this one, because the shape of the output was perfectly valid.

**The test used here is stronger than shifting features, and simpler.**

    A signal for date D must not change when data after D changes.

That is what "no look-ahead" *means*, so it is checked directly rather than through a proxy. Run
the code on history truncated at D, compare the weight it produces for D against the weight from
the full history. If they differ, the signal used something it could not have known.

Shifting features asks whether the *result* moved when inputs moved, which needs a second
carefully constructed input series and answers a related but weaker question. Truncation needs
no construction at all: the truncated history is a real history, exactly the one that existed on
day D.

**Why sampling rather than every date.** Each probe date costs one sandboxed execution, and a
sandboxed run of model-authored code is the most expensive thing in the pipeline. Look-ahead is
overwhelmingly structural -- code that reads the future for one date reads it for all of them,
because it is a loop with the wrong index or an aggregate over the wrong slice. Three dates
spread across the series catch that. They will not catch a signal that peeks only on one
specific date it was told to peek on, and nothing here claims to; that is adversarial rather
than mistaken, and it belongs to the sandbox and the reviewer, not to a statistical probe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: How many cut points a probe uses when the caller does not choose. Three is enough to catch a
#: structural violation and cheap enough to run on every generated experiment; a probe expensive
#: enough to skip is a probe that gets skipped.
DEFAULT_PROBE_POINTS = 3

#: Weights are strings on the wire, so they are compared as Decimals. Two representations of the
#: same number -- "0.5" and "0.50" -- are the same weight and must not read as a violation.
TOLERANCE = Decimal("1e-9")


@dataclass(frozen=True, slots=True)
class CausalityViolation:
    """One date where truncating the future changed the signal for that date."""

    trading_date: str
    symbol: str
    with_full_history: str
    with_history_to_date: str

    def __str__(self) -> str:
        return (
            f"{self.trading_date} {self.symbol}: {self.with_full_history} with the whole series, "
            f"{self.with_history_to_date} with data only up to that date"
        )


@dataclass(frozen=True, slots=True)
class CausalityReport:
    """What the probe checked and what it found.

    `probed` travels with `violations` for the reason every report in this project does: an empty
    violation tuple from zero probes and an empty one from three probes are different facts, and
    only one of them is evidence.
    """

    violations: tuple[CausalityViolation, ...]
    probed: tuple[str, ...]
    #: Dates the probe wanted to check and could not, because the signal produced nothing for
    #: them in one run or the other. Not a violation -- a signal is allowed to be flat -- but not
    #: a clean check either, and silently counting it as a pass is how a probe becomes decoration.
    unchecked: tuple[str, ...]

    @property
    def clean(self) -> bool:
        """True only when something was actually probed and nothing objected."""
        return bool(self.probed) and not self.violations


def probe_dates(dates: Sequence[str], *, points: int = DEFAULT_PROBE_POINTS) -> tuple[str, ...]:
    """Pick cut points spread across the series, never the first or last.

    The last date is excluded because truncating there is not a truncation -- the run would see
    the same history and pass trivially, which is a probe that cannot fail. The first is excluded
    because most signals need a warm-up window and produce nothing there, so it would land in
    `unchecked` and waste a sandboxed run.
    """
    usable = list(dates)[1:-1]
    if not usable:
        return ()
    if points >= len(usable):
        return tuple(usable)
    step = len(usable) / (points + 1)
    chosen = {usable[min(len(usable) - 1, int(step * (index + 1)))] for index in range(points)}
    return tuple(sorted(chosen))


def check_causality(
    run_to: Callable[[str], Mapping[str, Mapping[str, str]]],
    full: Mapping[str, Mapping[str, str]],
    *,
    points: int = DEFAULT_PROBE_POINTS,
) -> CausalityReport:
    """Re-run a signal against truncated history and report where the future leaked in.

    `run_to(date)` must execute the same generated code against bars up to and including `date`
    and return its signal. `full` is the signal from the untruncated series.

    Deliberately takes a callable rather than a sandbox: this module decides *what* to compare
    and stays testable without spawning anything, and the caller owns the expensive part. That
    also means the probe can be exercised against a deliberately cheating signal in a unit test,
    which is the only way to know it can fail.
    """
    targets = probe_dates(sorted(full), points=points)
    violations: list[CausalityViolation] = []
    probed: list[str] = []
    unchecked: list[str] = []

    for trading_date in targets:
        truncated = run_to(trading_date)
        expected = full.get(trading_date)
        actual = truncated.get(trading_date)
        if not expected or not actual:
            # A signal that is flat on this date in either run tells us nothing. Recorded rather
            # than counted as a pass.
            unchecked.append(trading_date)
            continue
        probed.append(trading_date)
        for symbol in sorted(set(expected) | set(actual)):
            before = _weight(expected.get(symbol))
            after = _weight(actual.get(symbol))
            if abs(before - after) > TOLERANCE:
                violations.append(
                    CausalityViolation(
                        trading_date=trading_date,
                        symbol=symbol,
                        with_full_history=str(expected.get(symbol, "0")),
                        with_history_to_date=str(actual.get(symbol, "0")),
                    )
                )

    return CausalityReport(
        violations=tuple(violations), probed=tuple(probed), unchecked=tuple(unchecked)
    )


def _weight(raw: str | None) -> Decimal:
    """A weight the probe cannot parse is treated as zero exposure, not as a match.

    Unparseable output is a defect for output validation to reject; here it must simply not be
    able to make two different signals compare equal.
    """
    if raw is None:
        return Decimal(0)
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return Decimal(0)


__all__ = [
    "DEFAULT_PROBE_POINTS",
    "CausalityReport",
    "CausalityViolation",
    "check_causality",
    "probe_dates",
]

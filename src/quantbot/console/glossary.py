"""Plain-language definitions for the jargon this console uses.

Written because a setting an operator cannot interpret is one they will either not touch or
touch wrongly, and "correlation threshold" is meaningless to anybody who has not already met the
idea. The field descriptions on `PortfolioVetoPolicy` explain each *setting*; this explains the
*words those explanations are made of*.

Two deliberate choices.

**Definitions live here rather than in the templates.** A term defined in HTML is a term nobody
can reuse, and the same word appears on the risk page, the discovery page and the capacity page.

**Each entry says what the term means for Elrond specifically**, not what a textbook says. "Paper
trading" has a general meaning and a particular one here -- orders that reach a real broker's
simulated account, with real prices and real rejections -- and the particular one is what an
operator needs when reading this system's output.
"""

from __future__ import annotations

from typing import NamedTuple


class Term(NamedTuple):
    """One piece of jargon, defined twice: briefly, then properly."""

    term: str
    #: One sentence, for a reader who needs the gist and will move on.
    short: str
    #: The explanation, for a reader deciding something. May be several sentences.
    long: str


#: Ordered by how likely a reader is to meet the term before they understand it, not
#: alphabetically -- the page reads top to bottom and the early entries are the ones the later
#: ones depend on.
GLOSSARY: tuple[Term, ...] = (
    Term(
        "basis points",
        "One hundredth of one percent. 100 bps is 1%, 10000 bps is 100%.",
        "A unit for talking about small percentages without ambiguity. 'Up 50 bps' cannot be "
        "confused the way 'up 0.5 percent' and 'up 0.5 percentage points' can be. Every limit "
        "in the risk policy is expressed this way, so 4000 means 40% of the account.",
    ),
    Term(
        "position",
        "A holding in one symbol: how much of it the account owns.",
        "A position is opened by an entry order and closed by an exit. The risk policy limits "
        "how large any one position may be, how many may be open, and how much they may add up "
        "to -- because the danger is rarely one position, it is what they collectively amount "
        "to.",
    ),
    Term(
        "exposure",
        "How much of the account is invested and therefore at risk of moving.",
        "Cash has no exposure; a position does. Total exposure is what every open position adds "
        "up to. It matters more than the number of positions, because ten small holdings and "
        "one large one can carry the same risk while looking very different in a list.",
    ),
    Term(
        "correlation",
        "Whether two investments tend to move at the same time.",
        "Near 1.0 two things rise and fall almost together; near 0 they move independently; "
        "below 0 they tend to move oppositely. It matters because holdings that move together "
        "are not really separate holdings. Five funds that all follow the US stock market are "
        "one bet with five names, and an account holding them is far less spread out than its "
        "position count suggests. Elrond measures this from actual past prices rather than "
        "assuming it from what the funds are called.",
    ),
    Term(
        "cluster",
        "A group of holdings that Elrond measured as moving together.",
        "Built from correlation rather than from labels. Two funds may have entirely different "
        "names and still land in one cluster because their prices behaved the same way. The "
        "risk policy caps how much may sit in any single cluster, which is the control that "
        "catches concentration a per-symbol limit would miss.",
    ),
    Term(
        "benchmark",
        "The reference the system measures itself against. Here, usually SPY.",
        "Beating a benchmark and making money are different questions. A strategy that gained "
        "8% in a year the market gained 12% lost against its benchmark while its account grew. "
        "Elrond judges results benchmark-relative so that market direction is not mistaken for "
        "skill.",
    ),
    Term(
        "drawdown",
        "How far the account has fallen from its highest point.",
        "Measured peak to trough, not from where you started. A 20% drawdown means the account "
        "is a fifth below its best-ever value, which is what actually determines whether a "
        "strategy is tolerable to hold.",
    ),
    Term(
        "slippage",
        "The gap between the price you saw and the price you got.",
        "Caused by the market moving between decision and fill, and by your own order pushing "
        "the price as it fills. It is the most common reason a strategy that looked profitable "
        "on paper is not, which is why every result Elrond reports is charged a cost assumption "
        "rather than assuming a perfect fill.",
    ),
    Term(
        "paper trading",
        "Real orders to a real broker, against a simulated account with no real money.",
        "Stronger evidence than a backtest, because the prices, the timing and the rejections "
        "are real -- only the money is not. It is where Elrond's strategies run now, and the "
        "boundary to real capital is not a setting this system can change.",
    ),
    Term(
        "backtest",
        "Running a strategy over past data to see what it would have done.",
        "Cheap, useful, and the easiest thing in this field to fool yourself with. Testing many "
        "ideas against the same history will eventually find one that fits it by luck, which is "
        "why Elrond tracks how much testing a dataset has already absorbed.",
    ),
    Term(
        "look-ahead",
        "Accidentally using information that was not available at the time.",
        "The classic way a backtest produces a result that cannot be repeated: a price, a "
        "filing or a news item that was actually published hours after the decision it appears "
        "to inform. Elrond checks for it mechanically rather than trusting that nobody made the "
        "mistake.",
    ),
    Term(
        "point-in-time",
        "Data as it stood on the day, including what was wrong with it then.",
        "Economic figures get revised; index membership changes; companies rename. A study run "
        "against today's corrected data is answering a question nobody could have asked at the "
        "time. Point-in-time data preserves the version a decision would actually have seen.",
    ),
    Term(
        "veto",
        "A rule that refuses an order outright rather than making it smaller.",
        "Elrond's risk controls are vetoes: they do not resize a proposed trade to fit, because "
        "a smaller order past a concentration limit still adds the exposure the limit exists to "
        "prevent. Every refusal is recorded with the exact rule and settings that produced it.",
    ),
    Term(
        "kill switch",
        "A single durable flag that stops any order being placed.",
        "Engaging needs only a reason. Clearing requires this system to actually measure that "
        "the conditions are safe -- broker reachable, records reconciled, data fresh -- because "
        "resuming into the fault that caused the stop is the failure a kill switch exists to "
        "prevent. It governs paper trading; it has nothing to do with real capital.",
    ),
    Term(
        "anomaly",
        "An unusual reading in the data, such as volume far above a symbol's own normal.",
        "A routing signal, not a trading signal. It tells the research side where to look; it "
        "carries no authority to buy or sell anything, and nothing in the trading path reads "
        "one.",
    ),
    Term(
        "catalyst",
        "A publicly known reason that might explain an unusual move.",
        "Elrond asks whether anything was published before an anomaly that could account for "
        "it. The interesting answer is 'no' -- unexplained activity is worth investigating -- "
        "which is exactly why the system refuses to say 'no' when it simply could not look.",
    ),
    Term(
        "hypothesis",
        "A specific, falsifiable claim, written down before it is tested.",
        "Writing it first is the point. A claim recorded after seeing the result can always be "
        "made to fit, so Elrond freezes the claim, the test and the success criteria before the "
        "experiment runs, and records the frozen version alongside whatever comes out.",
    ),
    Term(
        "multiple testing",
        "Test enough ideas and some will look good by chance alone.",
        "Try twenty coin-flipping strategies and one will beat the market by luck. The fix is "
        "not to stop testing but to account for how much testing has happened, so a result is "
        "judged against how many attempts it took to find.",
    ),
    Term(
        "holdout",
        "Data deliberately set aside and not looked at.",
        "Kept untouched so there is something left to test a finished idea against. Its value "
        "is spent the first time it is used, which is why Elrond tracks how much of it has "
        "been consumed rather than assuming it stays pristine.",
    ),
    Term(
        "effective sample size",
        "How many genuinely independent observations you have, rather than how many rows.",
        "A thousand daily readings from ten symbols that all move together carry far less "
        "information than a thousand independent ones. Treating rows as independent when they "
        "are not is how a result acquires confidence it has not earned.",
    ),
    Term(
        "counterfactual",
        "What would have happened if a refused trade had been allowed.",
        "Measured after the fact so the value of each risk rule can be judged rather than "
        "assumed. A veto that consistently refuses trades that would have made money is "
        "costing something, and a veto that refuses losers is earning its place.",
    ),
    Term(
        "promotion ladder",
        "The stages an idea climbs, each earned by evidence.",
        "Candidate, registered, research survivor, shadow, paper observation, paper qualified, "
        "and finally eligible for a human to review. Nothing skips a stage, and the last one "
        "grants no authority to trade real money -- it means a person should look.",
    ),
    Term(
        "shadow trading",
        "Producing signals and recording them without sending any order.",
        "The step between backtesting and paper trading. It runs against live data in real time "
        "so timing and data-availability problems surface, while nothing at all is submitted.",
    ),
)


def terms_in(text: str) -> tuple[str, ...]:
    """Glossary terms that appear in a piece of text, longest first.

    Longest first so 'effective sample size' is matched before 'sample'. The match is a plain
    case-insensitive substring: this decides which links to offer beneath a description, not what
    the description says, so a false positive costs a redundant link and never mangled prose.
    """
    lowered = text.lower()
    found = [entry.term for entry in GLOSSARY if entry.term.lower() in lowered]
    return tuple(sorted(found, key=len, reverse=True))


def anchor(term: str) -> str:
    """The fragment id for a term, stable enough to link to from anywhere."""
    return term.lower().replace(" ", "-")


__all__ = ["GLOSSARY", "Term", "anchor", "terms_in"]

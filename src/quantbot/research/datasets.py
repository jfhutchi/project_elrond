"""What a dataset *observes*, as distinct from what it is called (#23, #40).

`window_consumption` matched on the dataset name. That is a gate keyed on a string the registrant
chooses, which is the defect class this project keeps finding: a hypothesis registered against
`nexustrade-us-equities-daily` over 2016-2026 would report `UNTOUCHED` while
`sip-us-equities-daily` over the same span is `EXHAUSTED`. Same underlying observations, different
label, fresh multiple-testing budget.

Found by GPT-5.6 Sol, reviewing my own #40 review. I had written that a data lake is new
statistical budget; Sol's correction was that **a different vendor is not automatically new
budget** -- if a service serves the same historical outcomes and dates already searched, those
observations remain consumed however they are delivered. That is right, and it turned out to
describe a real hole rather than only a conceptual one, because nothing in the code tied a
dataset name to the observations behind it.

The multiple-testing burden attaches to **the observations**, not to the vendor, the API or the
file. Two providers serving SPY daily closes for 2016-2026 are one dataset for this purpose,
however differently they spell it.

**Unknown datasets are their own identity**, not pooled into a default. Pooling would make two
genuinely independent sources share a budget and refuse research that should proceed, which is
the opposite error and just as bad. A dataset nobody has classified is treated as new, and adding
an alias is a deliberate act by somebody who checked.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

#: Dataset names that observe the same underlying series. Keyed by dataset, valued by what it
#: observes -- so a new vendor for something already searched is declared here rather than
#: silently receiving a fresh budget.
#:
#: Deliberately short and hand-maintained. This is a claim that two sources contain the same
#: observations, which is a judgement somebody has to make and be accountable for; deriving it
#: automatically from column overlap would guess, and guessing wrong in the permissive direction
#: hands out significance that was already spent.
OBSERVATION_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        # US equity daily bars. The SIP feed is what cycles 2-10 searched.
        "sip-us-equities-daily": "us-equities-daily",
        "alpaca-us-equities-daily": "us-equities-daily",
        # Macro releases are keyed by series, and ALFRED serves the same series as FRED with
        # vintages attached. Asking the archival endpoint does not make the observations new.
        "fred-macro-monthly": "us-macro-monthly",
        "alfred-macro-monthly": "us-macro-monthly",
    }
)


def observation_identity(dataset: str) -> str:
    """What this dataset observes. Unrecognised names are their own identity.

    Falling back to the name itself is the conservative direction *here*, unlike most defaults in
    this project. Pooling an unknown dataset into an existing identity would charge it a budget
    it never spent and refuse research that should proceed.
    """
    return OBSERVATION_ALIASES.get(dataset, dataset)


def equivalent_datasets(dataset: str) -> tuple[str, ...]:
    """Every dataset name that observes the same thing, including the one asked about.

    Used to widen a consumption query from one label to every label for the same observations.
    A caller matching on the name alone gets a fresh budget by renaming.
    """
    identity = observation_identity(dataset)
    names = {name for name, value in OBSERVATION_ALIASES.items() if value == identity}
    names.add(dataset)
    return tuple(sorted(names))


__all__ = ["OBSERVATION_ALIASES", "equivalent_datasets", "observation_identity"]

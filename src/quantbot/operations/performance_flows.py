"""Shared strict external-flow classification for capture and immutable receipt audit."""
from collections.abc import Mapping, Sequence
from decimal import Decimal

_ECONOMIC = frozenset({'FILL', 'CFEE', 'CGD', 'DIV', 'DIVCGL', 'DIVCGS', 'DIVFEE', 'DIVFT',
                       'DIVNRA', 'DIVROC', 'DIVTW', 'DIVTXEX', 'FEE', 'INT', 'INTNRA', 'INTTW',
                       'PTC', 'PTR', 'MA', 'NC', 'REORG', 'SC', 'SSO', 'SSP'})
_EXTERNAL = frozenset({'CSD', 'CSW', 'ACATC'})


def external_flow(rows: Sequence[Mapping[str, object]]) -> Decimal:
    """Signed external cash; fees/income remain returns, ambiguous journals fail closed."""
    seen = set()
    total = Decimal(0)
    for row in rows:
        identity = row.get('id')
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError('invalid activity identity')
        seen.add(identity)
        kind = row.get('activity_type')
        if not isinstance(kind, str) or kind not in _ECONOMIC | _EXTERNAL:
            raise ValueError('unclassified external activity')
        if kind in _EXTERNAL:
            amount = Decimal(str(row['net_amount']))
            if (not amount.is_finite() or row.get('currency', 'USD') != 'USD'
                    or kind == 'CSD' and amount < 0 or kind == 'CSW' and amount > 0):
                raise ValueError('invalid external cash amount')
            total += amount
    return total

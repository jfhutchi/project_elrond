"""Optional short-lived diagnostic configuration; it grants no trading authority."""

from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

KEY = "QUANTBOT_PAPER_SQLITE_DIAGNOSTICS_UNTIL_UTC"


def configured_diagnostics_deadline(reference: str | None, *, now: datetime) -> datetime | None:
    if reference is None or now.utcoffset() != timedelta(0):
        return None
    path = Path(reference)
    if not path.is_absolute():
        return None
    try:
        selected = [
            line
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.split("=", 1)[0].strip().removeprefix("export ").strip() == KEY
        ]
        if len(selected) != 1:
            return None
        value = dotenv_values(stream=StringIO(selected[0]), interpolate=False).get(KEY)
        if not value:
            return None
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if deadline.utcoffset() != timedelta(0) or not 0 < (deadline - now).total_seconds() <= 1200:
            return None
        return deadline
    except (OSError, ValueError, TypeError):
        # Malformed optional diagnostics never changes execution or data authority.
        return None

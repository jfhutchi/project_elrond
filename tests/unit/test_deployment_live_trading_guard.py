"""The deployment-time live-trading guard, asserted against the settings that govern it.

`deploy/omen/setup.sh` carried this check inline and it checked nothing. It grepped for
`LIVE_TRADING=true`, and there is no `LIVE_TRADING` setting: `Settings` reads `TRADING_MODE` and
`LIVE_TRADING_ACKNOWLEDGED` with `extra="ignore"`, so the name it looked for is discarded by the
config layer and read by nothing else. The regex could not match `LIVE_TRADING_ACKNOWLEDGED`
either, since it required `=` immediately after `LIVE_TRADING`. An env file setting
`TRADING_MODE=LIVE` passed while the script reported that it had refused. `deploy/pi/setup.sh`
had no check at all and leaves an existing env file untouched.

These tests assert the **named** refusal, not merely a non-zero exit. That distinction is the
point: the defect being fixed was a guard that always passed, and its mirror image is a test that
always fails. Every refusal case here is paired with a control proving the same file shape passes
when the offending value is the only thing changed -- so deleting the check makes these fail
rather than leaving them green against a script that exits non-zero for some unrelated reason.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "deploy" / "refuse-live-trading.sh"

#: A paper env file with nothing objectionable in it. Every case below is this plus one line, so
#: a refusal can only be attributable to the line under test.
PAPER = """\
BROKER_PROVIDER=alpaca
TRADING_MODE=PAPER
BROKER_ENVIRONMENT=PAPER
LIVE_TRADING_ACKNOWLEDGED=false
ALPACA_PAPER_API_KEY=not-a-real-key
EXPECTED_ACCOUNT_ID=PA000000
"""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the guard is a bash script; there is no bash on this host to run it with",
)


def run_guard(env_file: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the guard exactly as a deployment script does."""
    return subprocess.run(
        ["bash", str(GUARD), str(env_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def write_env(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "quantbot.env"
    target.write_text(body, encoding="utf-8")
    return target


def test_a_paper_env_file_is_accepted(tmp_path: Path) -> None:
    """The control for every refusal below. Without it a guard that refused everything passes."""
    result = run_guard(write_env(tmp_path, PAPER))

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_an_env_file_with_no_trading_settings_at_all_is_accepted(tmp_path: Path) -> None:
    """Absence is safe: both settings default to PAPER in `Settings`, so silence is not consent."""
    result = run_guard(write_env(tmp_path, "ALPACA_PAPER_API_KEY=not-a-real-key\n"))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("line", "named"),
    [
        ("TRADING_MODE=LIVE", "TRADING_MODE"),
        ("BROKER_ENVIRONMENT=LIVE", "BROKER_ENVIRONMENT"),
    ],
)
def test_a_live_mode_is_refused_and_the_refusal_names_the_setting(
    tmp_path: Path, line: str, named: str
) -> None:
    """The case the old guard passed. `TRADING_MODE=LIVE` was invisible to a `LIVE_TRADING` grep."""
    body = PAPER.replace(f"{named}=PAPER", line)
    assert line in body, "the fixture must actually contain the value under test"

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 1
    # The *named* setting, not merely a non-zero exit. A refusal for some other reason would
    # satisfy a bare returncode assertion while the live mode sailed through.
    assert named in result.stderr
    assert "LIVE" in result.stderr


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "on", "t", "y"])
def test_every_spelling_pydantic_reads_as_true_is_refused(tmp_path: Path, value: str) -> None:
    """Pydantic accepts all of these for a bool. A guard knowing only `true` would be a hole."""
    body = PAPER.replace("LIVE_TRADING_ACKNOWLEDGED=false", f"LIVE_TRADING_ACKNOWLEDGED={value}")

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 1
    assert "LIVE_TRADING_ACKNOWLEDGED" in result.stderr


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_a_false_acknowledgement_is_accepted(tmp_path: Path, value: str) -> None:
    """The control for the case above: it must reject the value, not the setting's presence."""
    body = PAPER.replace("LIVE_TRADING_ACKNOWLEDGED=false", f"LIVE_TRADING_ACKNOWLEDGED={value}")

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "line",
    [
        "  TRADING_MODE=LIVE",
        "TRADING_MODE = LIVE",
        'TRADING_MODE="LIVE"',
        "TRADING_MODE='LIVE'",
        "TRADING_MODE=LIVE   ",
    ],
)
def test_whitespace_and_quoting_do_not_get_a_live_mode_past_the_guard(
    tmp_path: Path, line: str
) -> None:
    """Permissive *matching* in a refusal fails closed, so these spellings must all be caught."""
    body = PAPER.replace("TRADING_MODE=PAPER", line)

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 1
    assert "TRADING_MODE" in result.stderr


def test_the_last_assignment_wins(tmp_path: Path) -> None:
    """A later line overrides an earlier one for systemd and a shell alike, so it must here too."""
    body = PAPER + "TRADING_MODE=LIVE\n"

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 1
    assert "TRADING_MODE" in result.stderr


def test_a_paper_line_after_a_live_one_is_accepted(tmp_path: Path) -> None:
    """The other direction of the same rule. Refusing this would be reading the file wrongly."""
    body = "TRADING_MODE=LIVE\n" + PAPER

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 0, result.stderr


def test_a_lowercase_paper_is_refused_because_it_would_not_parse(tmp_path: Path) -> None:
    """`TradingMode` is a StrEnum whose only value is "PAPER". Anything else crashes at startup,
    and a value that would crash the daemon should not get as far as being installed."""
    body = PAPER.replace("TRADING_MODE=PAPER", "TRADING_MODE=paper")

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 1
    assert "TRADING_MODE" in result.stderr


def test_a_commented_out_live_mode_is_not_a_refusal(tmp_path: Path) -> None:
    """A comment is not an assignment. Refusing one would make the guard unusable against the
    env files this repository actually ships, which document the setting in prose above it."""
    body = PAPER + "# TRADING_MODE=LIVE would be a mistake\n"

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 0, result.stderr


def test_a_meaningless_live_trading_line_warns_without_refusing(tmp_path: Path) -> None:
    """`LIVE_TRADING` configures nothing, so it is not a refusal -- but somebody wrote it
    believing it did, and that belief is exactly what the old guard was built on."""
    body = PAPER + "LIVE_TRADING=true\n"

    result = run_guard(write_env(tmp_path, body))

    assert result.returncode == 0, result.stderr
    assert "configures nothing" in result.stderr
    assert "TRADING_MODE" in result.stderr


def test_a_missing_env_file_is_an_error_rather_than_a_pass(tmp_path: Path) -> None:
    """Distinguished from a refusal by its exit code: 2 is "could not look", 1 is "looked, and no".
    Collapsing them would let an unreadable file read as a clean one."""
    result = run_guard(tmp_path / "absent.env")

    assert result.returncode == 2


@pytest.mark.parametrize(
    ("installer", "invocation", "resolved"),
    [
        # Each installer names the guard by a path relative to a variable it sets. Both are
        # resolved here to a real file, because "the script mentions the guard" is satisfied by
        # a path that does not exist -- and a deployment script whose safety check is a broken
        # path fails at the check, or worse, is edited out by whoever hits it at 2am.
        (
            "deploy/omen/setup.sh",
            '"${here}/../refuse-live-trading.sh"',
            "deploy/refuse-live-trading.sh",
        ),
        (
            "deploy/pi/setup.sh",
            '"$QUANTBOT_HOME/deploy/refuse-live-trading.sh"',
            "deploy/refuse-live-trading.sh",
        ),
    ],
)
def test_both_installers_invoke_the_guard(installer: str, invocation: str, resolved: str) -> None:
    """The guard being correct is worth nothing if an installer stops calling it. Asserted by
    reading the scripts, because running them installs systemd units on the host."""
    body = (REPO / installer).read_text(encoding="utf-8")

    assert invocation in body, f"{installer} no longer calls the guard at the expected path"
    # `here` is the installer's own directory and `QUANTBOT_HOME` is the checkout root, so both
    # spellings land on the same file. Resolved rather than assumed.
    assert (REPO / resolved).is_file(), f"{installer} names a guard that does not exist"


def test_no_installer_still_greps_for_the_setting_that_does_not_exist() -> None:
    """The specific defect, pinned. `LIVE_TRADING=` as a *pattern to match on* is the bug; the
    scripts may still mention the name in prose explaining why it is not what to check."""
    for installer in ("deploy/omen/setup.sh", "deploy/pi/setup.sh"):
        body = (REPO / installer).read_text(encoding="utf-8")
        assert "LIVE_TRADING[[:space:]]*=" not in body, (
            f"{installer} matches on LIVE_TRADING, which Settings ignores"
        )

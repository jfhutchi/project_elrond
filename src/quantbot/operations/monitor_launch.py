"""Pinned PAPER observer launcher. --check performs no monitor or broker operation."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from quantbot.config import Settings
from quantbot.operations.monitor import main as run
from quantbot.operations.service import _git, isolated_application_environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--learner-directory', required=True, type=Path)
    parser.add_argument('--operations-database', required=True, type=Path)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    try:
        for path in (args.learner_directory, args.operations_database, args.env_file):
            if not path.is_absolute():
                raise ValueError('absolute paths required')
        if (not args.learner_directory.is_dir() or not args.operations_database.is_file()
                or not args.env_file.is_file() or not re.fullmatch('[0-9a-f]{40}', args.commit)):
            raise ValueError('missing input or invalid pin')
        repository = Path(__file__).resolve().parents[3]
        if (_git(repository, 'rev-parse', 'HEAD') != args.commit
                or _git(repository, 'status', '--porcelain', '--untracked-files=all')):
            raise ValueError('clean matching checkout required')
        learner = args.learner_directory.resolve()
        database = args.operations_database.resolve()
        if database == learner / 'monitor' / 'alerts.db':
            raise ValueError('source cannot be output')
        with isolated_application_environment(args.env_file):
            settings = Settings()
            if settings.TRADING_MODE.value != 'PAPER' or settings.LIVE_TRADING_ACKNOWLEDGED:
                raise ValueError('PAPER only')
            if args.check:
                print('MONITOR_LAUNCH_CHECK_OK')
                return 0
            return run(['--learner-directory', str(learner), '--operations-database', str(database),
                        '--output-directory', str(learner / 'monitor'), '--probe-paper-market'])
    except Exception:
        # Configuration/provider errors may contain secrets. Never print their text.
        print('MONITOR_LAUNCH_REFUSED')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

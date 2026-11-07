# Executed verification commands

Working directory: `D:/Elrond/App`. Logs and results are listed in the release report.
The final lifecycle cases were added after the broad gate; the separate ten-case run verifies that last patch.

```powershell
$ElrondTestPython='D:\Elrond\review\runtime-repair-20261104\test-python\Scripts\python.exe'
$env:PYTHONPATH='D:\Elrond\App\src'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:TEMP='D:\Elrond\scratch'
$env:TMP=$env:TEMP
$context=rg --files tests/unit/learner -g 'test_context*.py'
$newProtocol=rg --files tests/integration/paper_core -g '*context*protocol*.py'
& $ElrondTestPython -B -m pytest -q -p pytest_asyncio.plugin -p no:cacheprovider tests/unit/paper_core @context tests/unit/learner/test_experiment_store.py tests/integration/paper_core/test_crypto_paper_protocol.py tests/integration/paper_core/test_continuous_capital.py tests/integration/paper_core/test_crypto_resume.py tests/integration/paper_core/test_crypto_review_regressions.py tests/integration/paper_core/test_offline_asset_capabilities.py tests/integration/paper_core/test_final_repair.py tests/integration/paper_core/test_costed_loop.py @newProtocol --basetemp=D:\Elrond\scratch\autonomous-news-final-release-20261106
& $ElrondTestPython -B -m pytest -q -p pytest_asyncio.plugin -p no:cacheprovider tests/unit/console --basetemp=D:\Elrond\scratch\autonomous-news-console-final-20261106
& $ElrondTestPython -B -m pytest -q -p pytest_asyncio.plugin -p no:cacheprovider tests/unit/paper_core/test_context_review_producer.py --basetemp=D:\Elrond\scratch\autonomous-review-lifecycle-final-20261106
$files=rg --files tests/unit/learner -g 'test*.py' | Where-Object { $_ -notmatch 'test_context' }
& $ElrondTestPython -B -m pytest -q -p pytest_asyncio.plugin -p no:cacheprovider @files --basetemp=D:\Elrond\scratch\context-learner-noncontext-final-20261106
$env:PYTHONPATH='D:\Elrond\App\src;D:\Elrond\App\.venv\Lib\site-packages'
$env:PYTHONDONTWRITEBYTECODE='1'
& 'D:\Elrond\releases\paper-core-14352cc-20261015\.venv\Scripts\python.exe' -B -m pytest -q -p pytest_asyncio.plugin -p no:cacheprovider tests/unit/operations/test_service.py::test_service_wrapper_terminates_its_python_process_tree tests/unit/operations/test_service.py::test_windows_installer_registers_bounded_tasks_through_scheduler_doubles --basetemp=D:\Elrond\scratch\context-host-runtime-clean-final-20261106
```

Static checks used `ruff check` and `mypy --cache-dir D:/Elrond/scratch/mypy-autonomous-news-final-20261106`
against all twelve Python sources changed since component source d627897, including the newly added producer,
plus `git diff --check`. The earlier nineteen-source stage was checked separately.
No hosted CI workflow was dispatched; configured workflows are manual-only and pushing does not consume hosted minutes.

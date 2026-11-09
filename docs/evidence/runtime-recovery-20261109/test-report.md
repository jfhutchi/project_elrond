# Executed verification commands

Working directory D:/Elrond/App. PYTHONPATH=D:/Elrond/App/src;D:/Elrond/App/.venv/Lib/site-packages, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, PYTHONDONTWRITEBYTECODE=1, TEMP=TMP=D:/Elrond/scratch. Stable host interpreter: D:/Elrond/releases/paper-core-14352cc-20261015/.venv/Scripts/python.exe.

All runs used Python -B -m pytest -q -p pytest_asyncio.plugin -p no:cacheprovider with explicit owned --basetemp directories. Full sweeps additionally used --maxfail=10; the affected release gate used --maxfail=5 --durations=10.

Affected gate selections: tests/unit/paper_core; rg --files tests/unit/learner -g test_context*.py; tests/unit/learner/test_experiment_store.py; tests/integration/paper_core/{test_crypto_paper_protocol.py,test_continuous_capital.py,test_submission_readiness.py,test_guardian_inventory_budget.py,test_closed_session_polling.py,test_learning_acknowledgement_deferral.py,test_learning_changes_decision.py,test_learning_recovery.py,test_crypto_resume.py,test_final_repair.py,test_costed_loop.py}; test_submission_budget.py::test_final_send_does_not_replay_inventory_validation_before_writer_fence and ::test_prepared_entry_sends_with_remaining_final_acquisition_budget; tests/fault/paper_core/test_activation_races.py.

Corrected-fixture rerun: tests/integration/paper_core/test_learning_changes_decision.py tests/unit/paper_core/test_context_review_producer.py.

Final rejection gate: tests/fault/test_submission_rejection_provenance.py tests/fault/test_ambiguous_submit.py tests/contract/test_alpaca_paper_broker.py tests/unit/test_broker_transports.py tests/integration/test_execution_coordinator.py tests/integration/paper_core/test_crypto_paper_protocol.py tests/integration/paper_core/test_learning_acknowledgement_deferral.py.

Logs retained outside scratch:
- D:/Elrond/review/runtime-recovery-release-gate-20261109.log (589pass/3fixtures)
- D:/Elrond/review/learning-protocol-final-20261109.log (14pass)
- D:/Elrond/review/learning-ack-final-20261109.log (29pass)
- D:/Elrond/review/rejection-provenance-release-20261109.log (73pass)
- D:/Elrond/review/runtime-recovery-full-20261109.log (interrupted)
- D:/Elrond/review/runtime-recovery-final-full-20261109.log (bounded/interrupted)
- D:/Elrond/review/runtime-recovery-20261109/quote-results.json
- D:/Elrond/review/runtime-recovery-20261109/inventory-repair-verification.json

Ruff check and ruff format --check covered all16 changed Python files. Mypy --cache-dir D:/Elrond/scratch/mypy-runtime-recovery-final-20261109 covered all8 production files; allclean. git diff --check passed. No hosted CI was dispatched.

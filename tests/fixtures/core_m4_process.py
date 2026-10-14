"""Own disposable learner child processes; no service/process/OS configuration changes."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta


def wait_file(path):
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"fixture child did not reach {path.name}")
        time.sleep(0.01)


async def exercise_process_recovery(
    tmp_path, before, config, policy, cutoff, receipt, store, broker, joins
):
    from sqlalchemy import select

    from quantbot.paper_core.contracts import LearningReceipt
    from quantbot.paper_core.learning import consider_learning
    from quantbot.paper_core.store import CoreStore, table
    from quantbot.research.manifest import content_id
    from quantbot.research.registry import HypothesisRegistry
    from quantbot.storage import Database

    evidence = []
    with store.database.transaction() as session:
        encoded = session.execute(
            select(table("core_learning_inputs").c.payload).where(
                table("core_learning_inputs").c.id == receipt.input_export_id
            )
        ).scalar_one()
    assert len(broker.posts) == 80 and len(joins) == 40
    for variant in ("alive-orphan", "automatic-timeout", "pre-lock-orphan"):
        delayed = variant == "pre-lock-orphan"
        path = tmp_path / f"crashed-{variant}.db"
        with sqlite3.connect(before) as source, sqlite3.connect(path) as destination:
            source.backup(destination)
        scratch = path.parent / (path.name + ".learning-scratch")
        folder = scratch / receipt.run_id
        folder.mkdir(parents=True)
        input_path, result_path = folder / "input.json", folder / "result.json"
        input_path.write_text(encoded)
        (folder / "allowed").write_text(content_id(json.loads(encoded)))
        ready, start = folder / "ready", folder / "start"
        child_code = """
import sys,time
from pathlib import Path
from quantbot.paper_core import learning
source,result,ready,start,delayed=map(Path,sys.argv[1:])
if delayed.name == 'True':
    ready.write_text('pre-lock')
    while not start.exists(): time.sleep(.01)
original=learning.fit_candidate
def wait_fit(*args,**kwargs):
    ready.write_text('worker-lock-held')
    while not start.exists(): time.sleep(.01)
    return original(*args,**kwargs)
learning.fit_candidate=wait_fit
sys.argv=[sys.argv[0],str(source),str(result)]
learning._main()
"""
        child_args = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            child_code,
            str(input_path),
            str(result_path),
            str(ready),
            str(start),
            str(delayed),
        ]
        parent_code = """
import json,os,subprocess,sys,time
from datetime import datetime
from pathlib import Path
from quantbot.paper_core import learning
from quantbot.paper_core.contracts import CoreConfig,LearningPolicy
from quantbot.paper_core.store import CoreStore
from quantbot.storage import Database
path,config,policy,cutoff,args,ready,pid_path=sys.argv[1:]
def crash_after_child_start(export,policy,*,scratch_root):
    child=subprocess.Popen(json.loads(args),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    Path(pid_path).write_text(str(child.pid))
    while not Path(ready).exists(): time.sleep(.01)
    os._exit(23)
learning._fit_subprocess=crash_after_child_start
with Database(path) as db:
    learning.consider_learning(CoreStore(db,CoreConfig.model_validate_json(config)),
        LearningPolicy.model_validate_json(policy),cutoff=datetime.fromisoformat(cutoff))
"""
        pid_path = folder / "child-pid"
        env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "COMSPEC") if k in os.environ}
        with Database(path) as restarted:
            restarted_store = CoreStore(restarted, config)
            controller = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    parent_code,
                    str(path),
                    config.model_dump_json(),
                    policy.model_dump_json(),
                    cutoff.isoformat(),
                    json.dumps(child_args),
                    str(ready),
                    str(pid_path),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            controller.wait(timeout=15)
            assert controller.returncode == 23
            wait_file(ready)
            child_pid = int(pid_path.read_text())
            if not delayed:
                busy = consider_learning(restarted_store, policy, cutoff=cutoff)
                assert busy.reason == "LEARNING_WORKER_BUSY" and busy.trial_charge_id is None
                # Stop only this explicitly-created disposable fixture child.
                import ctypes

                if sys.platform == "win32":
                    api = ctypes.WinDLL("kernel32", use_last_error=True)
                    api.OpenProcess.restype = ctypes.c_void_p
                    handle = api.OpenProcess(0x0001 | 0x00100000 | 0x1000, False, child_pid)
                    assert handle
                    api.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                    api.CloseHandle.argtypes = [ctypes.c_void_p]
                    if variant == "alive-orphan":
                        assert api.TerminateProcess(handle, 24)
                    assert api.WaitForSingleObject(handle, 35000) == 0
                    code = ctypes.c_uint()
                    api.GetExitCodeProcess.argtypes = [
                        ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_uint),
                    ]
                    assert api.GetExitCodeProcess(handle, ctypes.byref(code))
                    if variant == "automatic-timeout":
                        assert code.value == 124
                    api.CloseHandle(handle)
                else:
                    import signal

                    if variant == "alive-orphan":
                        os.kill(child_pid, signal.SIGKILL)
                        time.sleep(0.05)
                    else:
                        time.sleep(31)
            recovered = consider_learning(restarted_store, policy, cutoff=cutoff)
            assert recovered.status == "FIT_FAILED" and recovered.reason == "PRIOR_PROCESS_LOST"
            assert recovered.trial_charge_id == receipt.trial_charge_id
            assert recovered.resulting_model_id == policy.parent_model.id
            assert not (folder / "allowed").exists()
            if delayed:
                start.write_text("resume canceled orphan")
                time.sleep(0.5)
                assert not result_path.exists()
            with restarted.transaction() as session:
                assert HypothesisRegistry(session).learner_trials_spent() == 1
            evidence.append(
                {
                    "variant": variant,
                    "parent_exit": controller.returncode,
                    "recovery": recovered.model_dump(mode="json"),
                    "orphan_result_adopted": False,
                }
            )
            if delayed:
                # An actual later pair of owned outcomes supplies new eligible revisions.
                from decimal import Decimal

                from quantbot.paper_core.contracts import (
                    BrokerEvent,
                    CostItem,
                    ModelArtifact,
                    Observation,
                )
                from quantbot.paper_core.guardian import guardian_cycle
                from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
                from quantbot.paper_core.trader import trader_cycle
                from quantbot.research.manifest import content_id

                day = broker.now + timedelta(days=1)
                for offset, joined in enumerate((joins[0], joins[-1])):
                    now = day.replace(hour=14, minute=5) + timedelta(minutes=65 * offset)
                    broker.now = now
                    broker.set_mark("XLE", Decimal(100), as_of=now)
                    await guardian_cycle(restarted_store, broker, now=now)
                    observation = Observation.model_validate(joined["observation"]).model_copy(
                        update={
                            "recorded_at_utc": now,
                            "available_at": now,
                            "source_start": now - timedelta(minutes=35),
                            "source_end": now,
                            "dependency_available_at": (now,),
                            "session_close": now.replace(hour=20, minute=0),
                            "sequence": 41 + offset,
                            "raw_data_digest": f"later-causal-{offset}",
                        }
                    )
                    decision = await trader_cycle(
                        restarted_store,
                        broker,
                        observation,
                        ModelArtifact(feature_version="five-minute-price-alpha-v1"),
                        restarted_store.latest_proof(),
                        now=now,
                    )
                    assert decision.action == "BUY"
                    broker.now = now + timedelta(minutes=30)
                    broker.set_mark(
                        "XLE",
                        Decimal("100.2") if offset == 0 else Decimal("99.8"),
                        as_of=broker.now,
                    )
                    await guardian_cycle(restarted_store, broker, now=broker.now)
                    events = []
                    for fill in broker.fills[-2:]:
                        cost = CostItem(
                            recorded_at_utc=broker.now,
                            fill_id=fill.fill_id,
                            amount=0,
                            source="BROKER_REPORTED",
                            document_id=f"later-fee-{fill.fill_id}",
                            effective_start=now,
                            effective_end=broker.now,
                        )
                        order = next(
                            o
                            for o in broker.orders.values()
                            if o.broker_order_id == fill.broker_order_id
                        )
                        events.append(
                            BrokerEvent(
                                recorded_at_utc=broker.now,
                                occurred_at=broker.now,
                                account_id=config.account_id,
                                endpoint="synthetic/later-fees",
                                event_id=cost.id,
                                raw_digest=content_id(cost),
                                client_order_id=order.client_order_id,
                                cost=cost,
                            )
                        )
                    import_broker_events(restarted_store, tuple(events))
                    assert process_outcomes(restarted_store, cutoff=broker.now).outcomes
                later = consider_learning(
                    restarted_store, policy, cutoff=broker.now + timedelta(hours=8)
                )
                assert later.status == "AWAITING_ACTIVATION", later.reason
                with restarted.transaction() as session:
                    assert HypothesisRegistry(session).learner_trials_spent() == 2
                evidence.append(
                    {
                        "variant": "later-session-recovery",
                        "receipt": later.model_dump(mode="json"),
                        "posts": len(broker.posts),
                        "canonical_trials": 2,
                        "joins": [
                            restarted_store.export_loop(d["decision_id"])
                            for d in restarted_store.links()
                            if d["role"] == "ENTRY"
                            and d["decision_id"] not in {j["decision_id"] for j in joins}
                        ],
                    }
                )
    # Parent loss recovery leaves the same live broker fixture unchanged; outcome worker
    # progress on the original store remains independent of any failed learner clone.
    assert isinstance(recovered, LearningReceipt)
    return evidence

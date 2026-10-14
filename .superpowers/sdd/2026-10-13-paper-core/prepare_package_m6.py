import json
from pathlib import Path
from datetime import datetime,UTC
from quantbot.paper_core.contracts import CoreConfig,ModelArtifact
from quantbot.paper_core.release import RuntimeConfig,prepare_release,verify_release
root=Path.cwd(); out=root/'docs/evidence/paper-core/m6'
cfg=RuntimeConfig(repository=str(root),store_path=r'D:\Elrond\state\quantbot-paper-fresh-20260908.db',store_identity='cd2a5911-853a-484e-bbc8-d0c27b5d27aa',expected_account_hash='ca2610f8f4c50efeb889856dd17445944557b3cd2f2a9ed67b83d59de2a49d19',environment_file_reference=r'C:\Users\leepa\.config\quantbot\quantbot.env',core=CoreConfig(account_id='RESOLVE_EXISTING_EXPECTED_ACCOUNT_ID',symbol='XLE',release_id='UNBOUND_PREPARED_RELEASE',owner='paper-core',generation=1,evidence_lane='BROKER_PAPER',clock_profile='PAPER_TIME_V2'),initial_model=ModelArtifact(feature_version='five-minute-price-alpha-v1'),holdout_source=r'D:\Elrond\learner\state\learner.db',learning_origin=datetime(2026,10,15,13,30,tzinfo=UTC),canonical_datasets=('us-equities-daily',),activation_policy='DAILY_BOUNDED_V2',submission_enabled=True)
release=prepare_release(cfg); verification=verify_release(release)
assert not verification['objections']
(out/'prepared-config.json').write_text(cfg.model_dump_json(indent=2),encoding='utf-8')
(out/'prepared-release.json').write_text(release.model_dump_json(indent=2),encoding='utf-8')
(out/'prepared-verification.json').write_text(json.dumps(verification,indent=2),encoding='utf-8')
request={'release_id':release.id,'approval_reference':'REQUIRES_EXPLICIT_ROLLOUT_APPROVAL_REFERENCE','fenced_writer_evidence_ids':['REQUIRES_FRESH_VERIFIED_LEGACY_ENTRY_WRITER_FENCE_AND_PROCESS_TREE_EVIDENCE'],'legacy_owners':[[s,'adaptive-momentum-v1-309894d8d8a5296e'] for s in ['DBC','EEM','EFA','IWM','QQQ','SPY','XLE','XLK','XLV']]}
(out/'acquisition-request.template.json').write_text(json.dumps(request,indent=2),encoding='utf-8')
print(json.dumps({'release_id':release.id,'config_id':cfg.id,'initial_model_id':cfg.initial_model.id,'executable_pins':len(release.executable_pins),'distributions':len(release.distributions),'verification':verification}))

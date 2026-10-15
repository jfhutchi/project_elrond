import gzip,hashlib,json,re
from pathlib import Path
root=Path.cwd(); out=root/'docs/evidence/paper-core/m6'; logs=out/'logs'; logs.mkdir(exist_ok=True)
known=[('v34-before-source','lx6se3_g'),('release-api-red','e5o1em7s'),('bootstrap-red','e7n9l6lw'),('helper-setup-failure','1g964n1k'),('currency-setup-failure','b7s1sgl0'),('initial-empty-role-green','oqlm6ztm'),('owned-witness-disabled-setup','1nzxbhpz'),('actual-feature-calendar-setup','s7jg_tvb'),('feature-assert-and-ack-race','60tn7zmq'),('presend-fixture-clock-setup','eq9hn5ue'),('owned-witness-green','yzosxz48'),('canonical-test-name-setup','9pc3euzf'),('canonical-green','0ics1xn3'),('canonical-daily-green','dhh0_qy_'),('joined-export-decimal-assert','pmhguc1m'),('focused-green','s2nlvmra'),('rollback-command-red','xzcjzvuj'),('rollback-command-green','pg67lbg3'),('frozen-affected-gate','__pjhcko'),('default-identity-red','e_jn3pla'),('default-fix-release-green','u1i97niy')]
records=[]; exports=[]; marker_counts={}
for stage,token in known:
 p=Path(r'C:\Users\leepa\AppData\Local\Temp')/('paper-core-check-'+token)/'result.txt'
 if not p.exists() or not p.stat().st_size:continue
 raw=p.read_bytes(); target=logs/(stage+'.txt.gz'); target.write_bytes(gzip.compress(raw,mtime=0))
 records.append({'stage':stage,'original_log':str(p),'archive':target.relative_to(root).as_posix(),'raw_bytes':len(raw),'raw_sha256':hashlib.sha256(raw).hexdigest(),'compressed_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'result_lines':[s for s in raw.decode('utf-8',errors='replace').splitlines() if re.search(r'\d+ (passed|failed|skipped)',s)][-2:]})
 if stage in {'frozen-affected-gate','default-fix-release-green'}:
  for line in raw.decode('utf-8',errors='replace').splitlines():
   match=re.search(r'(M[0-9]_[A-Z0-9_]+):',line)
   if not match:continue
   try:doc=json.loads(line[match.end():])
   except json.JSONDecodeError:continue
   name=match.group(1)
   if not name.startswith('M6_') and name not in {'M5_CAPACITY_PROGRESS','M5_CAPACITY'}: continue
   marker_counts[name]=marker_counts.get(name,0)+1; target=out/(('final-' if stage=='default-fix-release-green' else '')+name.lower()+'-'+str(marker_counts[name])+'.json'); target.write_text(json.dumps(doc,indent=2),encoding='utf-8'); exports.append({'stage':stage,'marker':name,'path':target.relative_to(root).as_posix(),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'source_log_raw_sha256':hashlib.sha256(raw).hexdigest()})
(out/'logs.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
(out/'exports.json').write_text(json.dumps(exports,indent=2),encoding='utf-8')
print('preserved',len(records),'logs and',len(exports),'exports')





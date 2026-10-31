import subprocess,shlex,json,pathlib,hashlib
base=pathlib.Path(__file__).parent;dest=base/'resumed-starship';dest.mkdir(exist_ok=True)
code="""import pathlib,json
b=pathlib.Path('/tmp/remote-chrome-next-pass-20261030');r={}
for result in b.glob('study-v5-*.result.json'):
 name=result.name.removesuffix('.result.json')
 for p in b.glob(name+'.*'):
  if p.is_file() and p.suffix in ['.json','.jsonl','.log'] and p.stat().st_size<20000000:r[p.name]=p.read_text(errors='replace')
print(json.dumps(r))"""
p=subprocess.run(['ssh','-o','BatchMode=yes','starship','rtk proxy python3 -c '+shlex.quote(code)],capture_output=True,text=True,timeout=45);p.check_returncode();files=json.loads(p.stdout)
for name,value in files.items():
 assert '/' not in name;(dest/name).write_text(value)
rows=[]
for p in sorted(dest.glob('study-v5-*.result.json')):
 r=json.loads(p.read_text());rows.append({'name':r['config']['name'],'ok':r['ok'],'error':r.get('error'),'validation':r.get('metricValidation'),'measurement':r.get('measurementValidation')})
(base/'starship-live-progress.json').write_text(json.dumps(rows,indent=2)+'\n')
print(json.dumps({'complete':len(rows),'passed':sum(x['ok'] for x in rows),'failed':[x for x in rows if not x['ok']],'files':len(files)}))

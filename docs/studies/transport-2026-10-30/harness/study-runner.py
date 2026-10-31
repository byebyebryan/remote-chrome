import asyncio,threading,socket,json,os,pathlib,subprocess,sys,time,signal
from netlab import Netlab
root=pathlib.Path(__file__).parent;c=json.loads(sys.argv[1]);lab=Netlab();loop=None;thread=None;proxy_stats={'client_to_server':0,'server_to_client':0,'connections':0};results=[];endpoint=None;proxy_path=root/'transport-proxy.sock';ready_path=root/'transport-proxy.ready'
async def bridge(reader,writer):
 proxy_stats['connections']+=1
 other=None
 try:
  rr,other=await asyncio.open_connection('snap',22,limit=65536)
  async def pump(r,w,key):
   while data:=await r.read(65536):
    proxy_stats[key]+=len(data);w.write(data);await w.drain()
   w.close()
  await asyncio.gather(pump(reader,other,'client_to_server'),pump(rr,writer,'server_to_client'))
 except (OSError,asyncio.CancelledError):pass
 finally:
  writer.close()
  if other:other.close()
def proxy_thread():
 global loop
 loop=asyncio.new_event_loop();asyncio.set_event_loop(loop);server=loop.run_until_complete(asyncio.start_unix_server(bridge,path=str(proxy_path),limit=65536));loop.run_forever();server.close();loop.run_until_complete(server.wait_closed());tasks=asyncio.all_tasks(loop)
 for t in tasks:t.cancel()
 loop.run_until_complete(asyncio.gather(*tasks,return_exceptions=True));loop.close()
try:
 remember=subprocess.run(['python3',str(root/'viewer-control.py'),json.dumps(dict(action='remember',host=c['host']))],capture_output=True,text=True)
 remember.check_returncode()
 if c.get('require_calibration',True):
  c['cases']=[dict(name='calibration-v5-'+c['host']+'-'+str(int(time.time())),condition='native',mode='h264',duration=10,calibration=True),*c['cases']]
 lab.setup();assert not proxy_path.exists();ready_path.unlink(missing_ok=True);thread=threading.Thread(target=proxy_thread,daemon=True);thread.start()
 for _ in range(100):
  if proxy_path.exists():break
  time.sleep(.02)
 proxy_path.chmod(0o600)
 endpoint=subprocess.Popen(['sudo','-n','ip','netns','exec',lab.e,'setpriv','--reuid='+str(os.getuid()),'--regid='+str(os.getgid()),'--init-groups','python3',str(root/'endpoint-proxy.py'),str(proxy_path),str(ready_path)],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 for _ in range(100):
  if ready_path.exists():break
  time.sleep(.02)
 assert ready_path.exists(),'endpoint proxy not ready'
 for case in c['cases']:
  name=case['name'];path=root/(name+'.result.json')
  if path.exists() and json.loads(path.read_text()).get('ok') and c.get('resume',True):print('SKIP '+name,flush=True);continue
  print('START '+name+' '+time.strftime('%Y-%m-%dT%H:%M:%S%z'),flush=True)
  lab.shape('proxy' if case['condition']=='native' else case['condition'],seed=121+case.get('repeat',0));before=lab.stats();traffic=dict(proxy_stats)
  args=['node',str(root/'study-case.mjs'),json.dumps(case)]
  if case['condition']!='native':args=lab.user_prefix()+['env','HOME=/home/bryan','USER=bryan','LOGNAME=bryan','PATH='+os.environ['PATH'],*args]
  p=subprocess.run(args,capture_output=True,text=True,timeout=180)
  record=dict(case=case,exit=p.returncode,stdout=p.stdout,stderr=p.stderr,qdisc_before=before,qdisc_after=lab.stats(),proxy_before=traffic,proxy_after=dict(proxy_stats));(root/(name+'.runner.json')).write_text(json.dumps(record,indent=2));results.append(record);print(p.stdout.strip(),flush=True)
  if p.returncode:
   print('STOP after failed case: '+p.stderr[-1000:],flush=True);break
finally:
 if endpoint:
  os.killpg(endpoint.pid,signal.SIGTERM)
  try:endpoint.wait(timeout=5)
  except subprocess.TimeoutExpired:os.killpg(endpoint.pid,signal.SIGKILL);endpoint.wait()
 if loop:loop.call_soon_threadsafe(loop.stop)
 if thread:thread.join(timeout=5)
 # Only processes inside this newly created test namespace are eligible.
 for ns in lab.created:
  p=subprocess.run(['sudo','-n','ip','netns','pids',ns],capture_output=True,text=True)
  for pid in p.stdout.split():
   try:
    if pathlib.Path('/proc',pid).stat().st_uid==os.getuid():os.kill(int(pid),signal.SIGTERM)
   except ProcessLookupError:pass
 lab.cleanup();proxy_path.unlink(missing_ok=True);ready_path.unlink(missing_ok=True);(root/('study-'+c['host']+'-network-cleanup.json')).write_text(json.dumps(dict(cleanup='verified',commands=lab.history,proxy=proxy_stats),indent=2));print('NETWORK CLEANUP VERIFIED',flush=True)
 # Restore only the window captured before this pass.
 subprocess.run(['python3',str(root/'viewer-control.py'),json.dumps(dict(action='restore',host=c['host']))],capture_output=True)
if any(r['exit'] for r in results):sys.exit(1)

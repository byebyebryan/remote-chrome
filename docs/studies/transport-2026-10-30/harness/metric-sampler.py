import sys,time,json,pathlib,subprocess
from probe import snapshot,parse_sockets
pid=int(sys.argv[1]);start=sys.argv[2];out=pathlib.Path(sys.argv[3]);shaped=len(sys.argv)>4 and sys.argv[4]=='shaped'
with out.open('w') as f:
 while True:
  try:s=snapshot(pid,start)
  except (AssertionError,OSError):break
  if shaped and len(s['transport_sockets'])==1:
   flow=s['transport_sockets'][0]
   p=subprocess.run(['sudo','-n','ip','netns','exec','rcstudy-7e91-e','ss','-Htinp','state','established'],text=True,capture_output=True,timeout=3)
   p.check_returncode();s['impairment_sender_tcp']=[r for r in parse_sockets(p.stdout) if r['local']==flow['peer'] and r['peer']==flow['local']]
   p=subprocess.run(['sudo','-n','ip','netns','exec','rcstudy-7e91-r','tc','-j','-s','qdisc','show'],text=True,capture_output=True,timeout=3)
   p.check_returncode();s['netem_queues']=json.loads(p.stdout)
  f.write(json.dumps(s)+'\n');f.flush();time.sleep(1)

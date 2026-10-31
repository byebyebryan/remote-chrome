import os,json,subprocess,time
class Netlab:
 def __init__(self):
  self.c='rcstudy-7e91-c';self.r='rcstudy-7e91-r';self.e='rcstudy-7e91-e';self.links=['rcs7c','rcs7r0','rcs7r1','rcs7h'];self.created=[];self.routed=False;self.history=[]
 def cmd(self,*args):
  p=subprocess.run(['sudo','-n',*args],capture_output=True,text=True);self.history.append(dict(args=args,code=p.returncode,stdout=p.stdout,stderr=p.stderr));p.check_returncode();return p.stdout
 def setup(self):
  self.forward=open('/proc/sys/net/ipv4/ip_forward').read()
  self.before_qdisc=subprocess.check_output(['tc','-j','qdisc','show'],text=True)
  names=[n.split()[0] for n in subprocess.check_output(['ip','netns','list'],text=True).splitlines()]
  assert not set([self.c,self.r,self.e]).intersection(names)
  existing={x['ifname'] for x in json.loads(subprocess.check_output(['ip','-j','link'],text=True))}
  assert not existing.intersection(self.links)
  assert not any(x.get('dst','').startswith('198.18.210.') for x in json.loads(subprocess.check_output(['ip','-j','route','show','table','all'],text=True)))
  for n in [self.c,self.r,self.e]:self.cmd('ip','netns','add',n);self.created.append(n)
  self.cmd('ip','link','add',self.links[0],'type','veth','peer','name',self.links[1])
  self.cmd('ip','link','set',self.links[0],'netns',self.c)
  self.cmd('ip','link','set',self.links[1],'netns',self.r)
  self.cmd('ip','link','add',self.links[2],'type','veth','peer','name',self.links[3])
  self.cmd('ip','link','set',self.links[2],'netns',self.r)
  self.cmd('ip','link','set',self.links[3],'netns',self.e)
  for ns,dev,address in [(self.c,self.links[0],'198.18.210.2/30'),(self.r,self.links[1],'198.18.210.1/30'),(self.r,self.links[2],'198.18.210.6/30'),(self.e,self.links[3],'198.18.210.5/30')]:
   prefix=['ip','-n',ns] if ns else ['ip']
   self.cmd(*prefix,'addr','add',address,'dev',dev);self.cmd(*prefix,'link','set',dev,'up')
  for ns in [self.c,self.r,self.e]:self.cmd('ip','-n',ns,'link','set','lo','up')
  self.cmd('ip','-n',self.c,'route','add','default','via','198.18.210.1')
  self.cmd('ip','-n',self.e,'route','add','198.18.210.0/30','via','198.18.210.6','dev',self.links[3])
  self.cmd('ip','netns','exec',self.r,'sysctl','-q','-w','net.ipv4.ip_forward=1')
 def shape(self,kind,seed=121):
  options={'proxy':[], 'delay':['delay','25ms'], 'jitter':['delay','25ms','10ms','distribution','normal'], 'loss':['delay','25ms','loss','random','0.5%','seed',str(seed)], 'cap':['rate','10mbit']}
  for dev in self.links[1:3]:
   self.cmd('ip','netns','exec',self.r,'tc','qdisc','replace','dev',dev,'root','netem','limit','2000',*options[kind])
 def user_prefix(self):return ['sudo','-n','ip','netns','exec',self.c,'setpriv','--reuid='+str(os.getuid()),'--regid='+str(os.getgid()),'--init-groups']
 def stats(self):return json.loads(self.cmd('ip','netns','exec',self.r,'tc','-j','-s','qdisc','show'))
 def cleanup(self):
  if self.routed:self.cmd('ip','route','del','198.18.210.0/30','via','198.18.210.6','dev',self.links[3]);self.routed=False
  for n in reversed(self.created):self.cmd('ip','netns','del',n)
  existing={x['ifname'] for x in json.loads(subprocess.check_output(['ip','-j','link'],text=True))}
  if self.links[3] in existing:self.cmd('ip','link','del',self.links[3])
  assert open('/proc/sys/net/ipv4/ip_forward').read()==self.forward
  assert json.loads(subprocess.check_output(['tc','-j','qdisc','show'],text=True))==json.loads(self.before_qdisc)
  assert not set(self.links).intersection(x['ifname'] for x in json.loads(subprocess.check_output(['ip','-j','link'],text=True)))
if __name__=='__main__':
 from pathlib import Path
 lab=Netlab();result={}
 try:
  lab.setup()
  for kind in ['proxy','delay']:
   lab.shape(kind)
   p=subprocess.run(['sudo','-n','ip','netns','exec',lab.c,'ping','-n','-c','4','-i','.2','198.18.210.5'],capture_output=True,text=True)
   result[kind]=dict(code=p.returncode,stdout=p.stdout,stderr=p.stderr);p.check_returncode()
 finally:
  lab.cleanup();result['cleanup']='verified';result['commands']=lab.history
  Path(__file__).with_name('netlab-prototype.json').write_text(json.dumps(result,indent=2))
 print(json.dumps({k:v for k,v in result.items() if k!='commands'}))

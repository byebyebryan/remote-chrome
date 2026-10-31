import pathlib,sys,json,subprocess
from probe import rows,snapshot,parse_sockets
import os,ipaddress
profile='--user-data-dir='+sys.argv[1];data=rows();found=[r for r in data.values() if r['exe'].removesuffix(' (deleted)') in ['/usr/bin/waypipe','/usr/lib/waypipe-remote-chrome/waypipe'] and 'server' in r['args'] and profile in r['args']]
assert len(found)==1,[r['args'] for r in found]
r=found[0];pid=next(p for p,v in data.items() if v is r);result=snapshot(pid,r['start']);args=r['args'];socket=args[args.index('--socket')+1] if '--socket' in args else None
if r['exe'].removesuffix(' (deleted)').endswith('waypipe-remote-chrome/waypipe') and socket:
 p=subprocess.run(['waypipe-remote-chrome','--remote-chrome-status','socket',socket],capture_output=True,text=True);result['codec_status']=json.loads(p.stdout)
connection=os.environ.get('SSH_CONNECTION','').split()
assert len(connection)==4,'SSH_CONNECTION missing from the probe channel'
client_ip,client_port,server_ip,server_port=connection
result['ssh_connection']=connection
def endpoint(value):
 address,port=value.rsplit(':',1);address=ipaddress.ip_address(address.strip('[]'));return (str(getattr(address,'ipv4_mapped',None) or address),int(port))
result['sender_tcp']=[s for s in parse_sockets(subprocess.check_output(['ss','-Htinp','state','established'],text=True)) if endpoint(s['local'])==(server_ip,int(server_port)) and endpoint(s['peer'])==(client_ip,int(client_port))]
assert len(result['sender_tcp'])==1,'no unique source SSH transport socket'
print(json.dumps(result))

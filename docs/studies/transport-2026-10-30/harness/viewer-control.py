import json,os,pathlib,subprocess,sys,time
base=pathlib.Path(__file__).parent
c=json.loads(sys.argv[1]);action=c['action'];env=dict(os.environ,WAYLAND_DISPLAY='wayland-1',XDG_RUNTIME_DIR='/run/user/1000',DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
env['NIRI_SOCKET']=str(next(pathlib.Path('/run/user/1000').glob('niri.*.sock')))
def niri(*args):return subprocess.check_output(['niri','msg',*args],env=env,text=True)
def window():
 for _ in range(100):
  w=[w for w in json.loads(niri('--json','windows')) if 'RC TRANSPORT TEST | '+c['name'] in (w.get('title') or '')]
  if len(w)==1:break
  time.sleep(.05)
 if len(w)!=1:raise ValueError('no unique test window: '+str(w))
 return w[0]
if action in ['launch','status','reset','stop']:
 launcher=str(pathlib.Path.home()/'.local/bin/remote-chrome')
 if c.get('legacy') and action=='launch':
  p=base/'launcher-250k';s=pathlib.Path(launcher).read_text();assert s.count('h264,$selector,bpf=500000')==1;p.write_text(s.replace('h264,$selector,bpf=500000','h264,$selector,bpf=250000'));p.chmod(0o700);launcher=str(p)
 if action=='launch':args=['launch','snap','--allow-existing','--no-yubikey','--no-notifications','--session',c['session'],'--transport',c.get('transport','video'),'--video-codecs',c.get('codecs','auto'),'--',*c['chrome_args']]
 elif action=='stop':args=['stop','--session',c['session']]
 else:args=[action,'snap','--session',c['session']]+(['--yes'] if action=='reset' else [])
 p=subprocess.run([launcher,*args],env=env,capture_output=True,text=True,timeout=60)
 print(json.dumps(dict(code=p.returncode,stdout=p.stdout,stderr=p.stderr)));sys.exit(0)
elif action=='focus':
 w=window();niri('action','power-on-monitors');niri('action','focus-window','--id',str(w['id']))
 if c.get('noresize'):
  print(json.dumps(window()));sys.exit(0)
 if not w['is_floating']:niri('action','toggle-window-floating','--id',str(w['id']))

 for _ in range(5):
  niri('action','set-window-width','--id',str(w['id']),'1200');time.sleep(.6);niri('action','set-window-height','--id',str(w['id']),'850');time.sleep(.6)
  if window()['layout']['window_size']==[1200,850]:break
 niri('action','focus-window','--id',str(w['id']))
 for _ in range(50):
  focused=json.loads(niri('--json','focused-window'));current=window()
  if focused and focused['id']==w['id'] and current['is_focused']:break
  time.sleep(.1)
 else:raise ValueError('test window did not gain focus after resize')
 print(json.dumps(current))
elif action=='capture':
 w=window();p=base/(c['name']+'.png');p.unlink(missing_ok=True);before=time.monotonic();niri('action','screenshot-window','--id',str(w['id']),'--path',str(p))
 for _ in range(100):
  if p.exists():break
  time.sleep(.01)
 assert p.exists();print(json.dumps(dict(path=str(p),elapsed=time.monotonic()-before,window=w)))
elif action=='remember':
 record={'focused':json.loads(niri('--json','focused-window'))}
 (base/(c['host']+'-viewer-before.json')).write_text(json.dumps(record))
 print('{}')
elif action=='restore':
 old=json.loads((base/(c['host']+'-viewer-before.json')).read_text())['focused']
 current=json.loads(niri('--json','focused-window'))
 if old and (not current or current['id']==old['id'] or (current.get('title') or '').startswith('RC TRANSPORT TEST | ')) and any(w['id']==old['id'] for w in json.loads(niri('--json','windows'))):niri('action','focus-window','--id',str(old['id']))
 print('{}')

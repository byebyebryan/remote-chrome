import json,os,pathlib,subprocess,sys,time,statistics,gzip
c=json.loads(sys.argv[1]);root=pathlib.Path(__file__).parent;env=dict(os.environ,NIRI_SOCKET=c['niri_socket']);out=dict(samples=[],keys=[],errors=[]);start=time.monotonic();next_key=start+2;pending=None;sent=0;last=None;last_frame=None;last_change=None;out['frame_gaps']=[];out['rejected_captures']=[];out['invalid_frame_captures']=[];last_valid=start;last_input=start+c.get('duration',60)-min(5,c.get('duration',60)/3)
def focus():
 w=json.loads(subprocess.check_output(['niri','msg','--json','focused-window'],env=env,text=True));return w and w['id']==c['window_id']
def ppm(raw):
 # grim emits binary P6, 8-bit RGB, with no comments.
 parts=raw.split(b'\n',3);assert parts[0]==b'P6' and parts[2]==b'255',parts[:3]
 width,height=map(int,parts[1].split());pixels=parts[3];assert len(pixels)==width*height*3
 def bright(x,y):
  return sum(sum(pixels[((y+dy)*width+x+dx)*3:((y+dy)*width+x+dx)*3+3]) for dy in [-1,0,1] for dx in [-1,0,1])/27
 assert bright(28,48)>200 and bright(48,48)<55,'marker alignment or visibility changed'
 def number(bits,y):
  value=0
  for b in range(bits):
   v=bright(73+b*35,y)
   if v>160:value|=1<<b
   elif v>95:raise ValueError('indeterminate barcode')
  return value
 key=number(32,27)
 seq=key&255;crc=seq^0xa5
 for _ in range(8):crc=((crc<<1)^(7 if crc&128 else 0))&255
 if (key>>8)&255 != seq^255 or (key>>16)&255 != crc or (key>>24)&255 != crc^255:raise ValueError('key complement/CRC mismatch')
 try:
  frame=number(32,69)
  if (frame>>16)&65535 != (frame&65535)^65535:raise ValueError('frame complement mismatch')
  return seq,frame&65535,None
 except ValueError as error:return seq,None,str(error)
try:
 assert focus(),'test window not focused'
 while time.monotonic()-start<c.get('duration',60):
  tick=time.monotonic()
  if pending and tick-pending['sent_end']>5:
   pending['status']='timeout';out['keys'].append(pending);pending=None;next_key=tick+1
  if tick>=next_key and pending is None and tick<last_input:
   if not focus():raise RuntimeError('focus changed; input sampling stopped')
   sent+=1;pending=dict(seq=sent,sent_begin=time.monotonic());p=subprocess.run(['wtype','-k','F8'],env=env,capture_output=True,text=True,timeout=2);pending['sent_end']=time.monotonic();pending['wtype_code']=p.returncode
   if p.returncode:raise RuntimeError('virtual keyboard unavailable: '+p.stderr)
   next_key=tick+3
  begin=time.monotonic();p=subprocess.run(['grim','-s','1','-t','ppm','-g',c['region'],'-'],env=env,capture_output=True,timeout=3);end=time.monotonic()
  if p.returncode:raise RuntimeError('capture failed: '+p.stderr.decode(errors='replace'))
  try:seq,frame,frame_error=ppm(p.stdout)
  except ValueError as error:
   if not focus():raise RuntimeError('focus changed during ambiguous capture')
   bad=dict(begin=begin,end=end,error=str(error));out['rejected_captures'].append(bad)
   if len(out['rejected_captures'])<=10:
    artifact=c['name']+'.rejected-'+str(len(out['rejected_captures']))+'.ppm.gz';(root/artifact).write_bytes(gzip.compress(p.stdout));bad['artifact']=artifact
   if end-last_valid>2:raise RuntimeError('no decodable barcode for two seconds')
   time.sleep(max(0,c.get('period',.05)-(time.monotonic()-tick)));continue
  last_valid=end;row=dict(begin=begin,end=end,seq=seq,frame=frame);out['samples'].append(row)
  if frame_error:out['invalid_frame_captures'].append(dict(begin=begin,end=end,error=frame_error))
  if pending and seq==pending['seq']:
   pending.update(status='observed',observed=end,latency_upper_ms=(end-pending['sent_begin'])*1000,latency_lower_ms=max(0,((last['begin'] if last else begin)-pending['sent_end'])*1000));out['keys'].append(pending);pending=None
  if frame is not None and (last_frame is None or frame!=last_frame):
   if last_change is not None:out['frame_gaps'].append(end-last_change)
   last_change=end;last_frame=frame
  last=row
  time.sleep(max(0,c.get('period',.05)-(time.monotonic()-tick)))
 out['input_marker_rejection_fraction']=len(out['rejected_captures'])/max(1,len(out['samples'])+len(out['rejected_captures']))
 out['quality_passed']=out['input_marker_rejection_fraction']<.05
 out['ok']=True
except Exception as e:out.update(ok=False,error=str(e))
finally:
 if pending:pending['status']='incomplete';out['keys'].append(pending)
 out.update(duration=time.monotonic()-start,period=c.get('period',.05));(root/(c['name']+'.image-metrics.json')).write_text(json.dumps(out,indent=2))
 print(json.dumps(dict(ok=out.get('ok'),error=out.get('error'),samples=len(out['samples']),rejected=len(out['rejected_captures']),invalid_frame_captures=len(out['invalid_frame_captures']),quality_passed=out.get('quality_passed'),keys=len(out['keys']),capture_p50_ms=statistics.median([(s['end']-s['begin'])*1000 for s in out['samples']]) if out['samples'] else None)))

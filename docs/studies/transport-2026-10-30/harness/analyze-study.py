"""Analyze completed study cases; partial matrices remain explicitly provisional."""
import collections,datetime,json,math,pathlib,statistics
BASE=pathlib.Path(__file__).parent
RAW=BASE/'resumed-starship'
CONDITIONS=['native','proxy','delay','jitter','loss','cap']
MODES=['h264','lossless']

def percentile(values,p):
 values=sorted(float(v) for v in values if v is not None and math.isfinite(v))
 if not values:return None
 i=(len(values)-1)*p/100;lo=int(i);hi=min(lo+1,len(values)-1)
 return values[lo]+(values[hi]-values[lo])*(i-lo)

def stats(values):
 values=[v for v in values if v is not None]
 return dict(mean=statistics.mean(values),min=min(values),max=max(values)) if values else None

def cpu(first,last,kind):
 duration=last['monotonic']-first['monotonic'];ticks=0;matched=0
 for pid,end in last['processes'].items():
  start=first['processes'].get(pid)
  if not start or start['start']!=end['start']:continue
  exe=end['exe'].removesuffix(' (deleted)')
  selected=(exe in ['/usr/bin/waypipe','/usr/lib/waypipe-remote-chrome/waypipe'] if kind=='waypipe' else exe.startswith('/opt/google/chrome/'))
  if selected:
   ticks+=max(0,end['ticks']-start['ticks']);matched+=1
 return 100*ticks/first['hz']/duration,matched

def tcp_delta(first,last):
 assert first['local']==last['local'] and first['peer']==last['peer'],'TCP flow identity changed'
 keys=['bytes_received','bytes_sent','bytes_retrans','retrans_total','rcv_ooopack','reord_seen','dsack_dups','segs_in','segs_out']
 result={k:last.get(k,0)-first.get(k,0) for k in keys}
 assert all(v>=0 for v in result.values()),'TCP counter reset'
 return result

def one_case(path):
 result=json.loads(path.read_text());config=result['config'];name=config['name']
 trial={'name':name,'viewer':'starship','condition':config['condition'],'mode':config['mode'],'repeat':config.get('repeat',0)+1,'ok':result['ok'],'error':result.get('error'),'started':result['started'],'finished':result['finished']}
 if not result['ok']:return trial
 images=json.loads((RAW/(name+'.image-metrics.json')).read_text())
 samples=[json.loads(line) for line in (RAW/(name+'.transport-metrics.jsonl')).read_text().splitlines() if line.strip()]
 assert images['ok'] and result['metricValidation']['uniqueTransport']
 first,last=samples[0],samples[-1];elapsed=last['monotonic']-first['monotonic']
 assert all(len(s['transport_sockets'])==1 for s in samples)
 before,after=first['transport_sockets'][0],last['transport_sockets'][0]
 flow=tcp_delta(before,after)
 observed=[x for x in images['keys'] if x['status']=='observed'];statuses=collections.Counter(x['status'] for x in images['keys'])
 capture=[(x['end']-x['begin'])*1000 for x in images['samples']]
 brackets=[x['latency_upper_ms']-x['latency_lower_ms'] for x in observed]
 metric={'rx_mbps':flow['bytes_received']*8/elapsed/1e6,'tx_mbps':flow['bytes_sent']*8/elapsed/1e6,'callback_fps':result['workload']['frames']/(result['workload']['duration']/1000),'received_code_changes_per_second':(len(images['frame_gaps'])+1)/images['duration'],'input_p50_lower_ms':percentile([x['latency_lower_ms'] for x in observed],50),'input_p50_upper_ms':percentile([x['latency_upper_ms'] for x in observed],50),'input_p95_lower_ms':percentile([x['latency_lower_ms'] for x in observed],95),'input_p95_upper_ms':percentile([x['latency_upper_ms'] for x in observed],95),'capture_p50_ms':percentile(capture,50),'capture_p95_ms':percentile(capture,95),'input_bracket_p50_ms':percentile(brackets,50),'received_frame_gap_p95_ms':percentile([x*1000 for x in images['frame_gaps']],95),'received_frame_gap_max_ms':max(images['frame_gaps'],default=0)*1000,'source_frame_gap_p95_ms':percentile(result['workload']['intervals'],95),'source_frame_gap_max_ms':max(result['workload']['intervals'],default=0)}
 source_before,source_after=result['serverBefore'],result['serverAfter']
 metric['invalid_frame_captures']=len(images.get('invalid_frame_captures',[]))
 metric['invalid_frame_fraction']=metric['invalid_frame_captures']/max(1,len(images['samples']))
 metric['undecodable_captures']=len(images.get('rejected_captures',[]))
 metric['undecodable_capture_fraction']=metric['undecodable_captures']/max(1,len(images['samples'])+metric['undecodable_captures'])
 metric['source_waypipe_cpu_one_core_pct'],source_wp=cpu(source_before,source_after,'waypipe')
 metric['source_chrome_cpu_one_core_pct'],source_chrome=cpu(source_before,source_after,'chrome')
 metric['viewer_waypipe_cpu_one_core_pct'],viewer_wp=cpu(first,last,'waypipe')
 source_tcp=tcp_delta(source_before['sender_tcp'][0],source_after['sender_tcp'][0])
 metric['real_path_sender_retransmissions']=source_tcp['retrans_total']
 metric['viewer_send_queue_peak_bytes']=max(s['transport_sockets'][0]['send_q'] for s in samples)
 metric['viewer_receive_queue_peak_bytes']=max(s['transport_sockets'][0]['recv_q'] for s in samples)
 metric['viewer_tcp_rtt_mean_ms']=statistics.mean(s['transport_sockets'][0]['rtt_ms'] for s in samples if 'rtt_ms' in s['transport_sockets'][0])
 if config['condition']!='native':
  assert all(len(s['impairment_sender_tcp'])==1 for s in samples)
  shaped=tcp_delta(first['impairment_sender_tcp'][0],last['impairment_sender_tcp'][0]);metric['impairment_sender_retransmissions']=shaped['retrans_total'];metric['impairment_sender_retransmitted_bytes']=shaped['bytes_retrans'];metric['impairment_sender_send_queue_peak_bytes']=max(s['impairment_sender_tcp'][0]['send_q'] for s in samples)
  metric['router_backlog_peak_bytes']=max(q.get('backlog',0) for s in samples for q in s['netem_queues'] if q['kind']=='netem')
  net_first={q['dev']:q for q in first['netem_queues'] if q['kind']=='netem'};net_last={q['dev']:q for q in last['netem_queues'] if q['kind']=='netem'}
  metric['router_drops']=sum(net_last[k].get('drops',0)-v.get('drops',0) for k,v in net_first.items())
 else:
  for key in ['impairment_sender_retransmissions','impairment_sender_retransmitted_bytes','impairment_sender_send_queue_peak_bytes','router_backlog_peak_bytes','router_drops']:metric[key]=None
 aux0=result['system']['gpu']['auxAttributes'];aux1=result['systemAfter']['gpu']['auxAttributes']
 metric['chrome_gpu_process_crashes']=aux1.get('processCrashCount',0)-aux0.get('processCrashCount',0)
 mode=result['config']['mode'];backend={'source':source_after.get('codec_status',{}).get('counts'),'viewer':result.get('viewerBackend',{}).get('counts')}
 if mode=='h264':assert backend['source']['encoder_hw']>0 and backend['viewer']['decoder_hw']>0
 output=result['output'];selected_mode=output['modes'][output['current_mode']]
 trial.update(input_marker_quality_passed=images['quality_passed'],metrics=metric,inputs={'attempted':len(images['keys']),'observed':statuses['observed'],'timeout':statuses['timeout'],'incomplete':statuses['incomplete'],'source_f8_events':sum(x['key'].lower()=='f8' for x in result['workload']['keyEvents']),'all_source_events_trusted':all(x['trusted'] for x in result['workload']['keyEvents'])},timing={'transport_sample_count':len(samples),'transport_seconds':elapsed,'image_sample_count':len(images['samples']),'image_seconds':images['duration'],'source_seconds':source_after['monotonic']-source_before['monotonic']},geometry=result['geometry'],output={'name':output['name'],'mode':selected_mode,'logical':output['logical']},backend=backend,chrome={'renderer':aux1.get('glRenderer'),'features':result['systemAfter']['gpu']['featureStatus']},matched_cpu_processes={'source_waypipe':source_wp,'source_chrome':source_chrome,'viewer_waypipe':viewer_wp},input_events=[{k:x.get(k) for k in ['seq','status','latency_lower_ms','latency_upper_ms']} for x in images['keys']])
 return trial

trials=[one_case(path) for path in sorted(RAW.glob('study-v5-*.result.json'))]
groups={}
for condition in CONDITIONS:
 groups[condition]={}
 for mode in MODES:
  cases=[t for t in trials if t['ok'] and t['condition']==condition and t['mode']==mode]
  if not cases:continue
  qualified=[t for t in cases if t['input_marker_quality_passed']]
  keys=sorted({k for t in cases for k in t['metrics']});group={'repeats':len(cases),'qualified_latency_repeats':len(qualified),'latency_names':[t['name'] for t in qualified],'metrics':{k:stats([t['metrics'][k] for t in (qualified if k.startswith('input_p') else cases)]) for k in keys},'inputs':{k:sum(t['inputs'][k] for t in cases) for k in ['attempted','observed','timeout','incomplete']},'names':[t['name'] for t in cases]}
  groups[condition][mode]=group
complete=len(trials)==36 and all(t['ok'] for t in trials) and all(len(groups[c].get(m,{}).get('names',[]))==3 for c in CONDITIONS for m in MODES)
report={'schema':1,'date':'2026-10-30','analyzed_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'complete Starship matrix; Carbon deferred at user request' if complete else 'provisional partial Starship matrix','matrix_complete':complete,'expected_trials':36,'latency_matrix_complete':complete and all(t.get('input_marker_quality_passed',False) for t in trials),'completed_trials':len(trials),'passed_trials':sum(t['ok'] for t in trials),'qualified_latency_trials':sum(t.get('input_marker_quality_passed',False) for t in trials),'marker_quality_failed_trials':[t['name'] for t in trials if t['ok'] and not t['input_marker_quality_passed']],'failed_trials':[{'name':t['name'],'error':t['error']} for t in trials if not t['ok']],'methods':{'summary':'Arithmetic means of per-run metrics, with three telemetry trials intended per condition/mode. Latency summaries use only qualified trials and report their count. Min/max are observed per-run ranges, not confidence intervals.','marker_quality':'The predeclared input-marker gate is fewer than five percent invalid captures. Baseline calibration must pass it. Full trials failing it retain network, CPU, marker failures and per-event evidence, but are excluded from group latency comparisons. Completed telemetry trials are distinct from qualified latency trials.', 'latency':'Virtual-key injection to first matching receiver image; lower/upper capture brackets. p50/p95 summarize observed events only; 5-second timeouts and incomplete end-of-trial events are counted separately. Not physical input-to-photon latency.','traffic':'Deltas on one exact viewer SSH TCP socket over the sampled interval, approximately 59 seconds of each 60-second workload; includes SSH, CDP control and protocol traffic.','cpu':'CPU ticks for matching process PID/start identities only, divided by each pair of snapshot times. Percentages are of one CPU core. Exited or newly appearing processes outside the matched snapshots are excluded.','retransmissions':'Real-path sender counters from the same SSH master connection on Snap; shaped sender counters from the exact private endpoint-to-viewer socket. Native and proxy RTTs cover different TCP legs.','queues':'Router backlog and endpoint queues are 1 Hz observations; brief peaks may be missed. Router drops are first/last sample deltas, not cumulative qdisc totals.','frame_timing':'Browser callbacks and 20 Hz receiver frame-code observations; neither is physical presentation FPS. Receiver gaps use only complemented frame counters; invalid frame-capture counts are retained, and their missing observations can enlarge measured gaps. Input markers have separate complements and CRC checks.','workload':'Fixed 1200x760 CSS viewport, DPR1 on Starship, 60 seconds of deterministic canvas/scroll phases; content includes a fixed redundant input/frame barcode with complemented fields and key CRC. Ambiguous captures are counted and skipped; their missing times widen input brackets. Only one key is outstanding at a time: attempts are nominally every three seconds, but wait for its echo or five-second timeout, so slow trials may attempt fewer keys. Input injection stops five seconds before the end to permit drain.','modes':'H264 hardware at500000 bits/frame with Chrome GPU enabled and Chrome Vulkan disabled; stock lossless recovery uses noGPU Waypipe and Chrome GPU disabled. This compares product modes rather than only codecs.','observer':'20 Hz grim screen captures and 1 Hz telemetry add observer load. Capture costs are retained; reported process CPU excludes Niri, capture helpers and whole-host activity. Absolute timing and callback pacing can differ during ordinary browsing.', 'background':'Production Carbon-to-Snap session preserved; isolated sockets/processes exclude its counters but cannot eliminate contention on shared network/GPU resources.','carbon':'Functional hardware/software/lossless and reset acceptance passed earlier; controlled Carbon performance comparison explicitly deferred while desktop is in use.'},'groups':groups,'trials':trials}
report.update({'date_timezone': 'America/Los_Angeles', 'passed_trials_definition': 'Completed trials with valid transport/process telemetry; distinct from the input-marker latency-quality gate.', 'interpretation': {'native': 'Hardware video reduced delivered payload and measured process CPU, but did not improve the measured native input-latency medians or callback pacing in these three repeats. This is descriptive, not a statistically established codec ranking.', 'proxy': 'The unshaped TCP-splitting proxy itself substantially changed pacing and latency. Impairment outcomes must be compared with that proxy control, and do not predict quantitatively what the same impairment would do on the native path.', 'quality': 'Three video trials failed input-marker quality. Pixel inconsistency is not localized to rendering, transport, encoder, decoder or compositor; Carbon performance and ordinary-page acceptance remain pending.'}})
(BASE/'starship-analysis.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
print(json.dumps({k:report[k] for k in ['status','completed_trials','passed_trials','failed_trials']}))
for condition in CONDITIONS:
 for mode,g in groups[condition].items():
  m=g['metrics'];get=lambda k:round(m[k]['mean'],2) if m.get(k) else None
  print(condition,mode,'n',g['repeats'],'latency_n',g['qualified_latency_repeats'],'Mbps',get('rx_mbps'),'input median bracket',get('input_p50_lower_ms'),get('input_p50_upper_ms'),'callbacks',get('callback_fps'),'timeouts',g['inputs']['timeout'])

import json, os, pathlib, re, subprocess, sys, time

def rows():
    data = {}
    for p in pathlib.Path('/proc').glob('[0-9]*'):
        try:
            st = (p/'stat').read_text().rsplit(')',1)[1].split()
            data[int(p.name)] = dict(ppid=int(st[1]), start=st[19], ticks=int(st[11])+int(st[12]), exe=os.readlink(p/'exe'), args=(p/'cmdline').read_bytes().decode(errors='replace').split('\0'))
        except (OSError,ValueError,IndexError):
            pass
    return data

def parse_sockets(raw):
    lines = raw.splitlines()
    sockets = []
    for i, line in enumerate(lines):
        if line[:1].isspace() or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        detail = lines[i + 1] if i + 1 < len(lines) and lines[i + 1][:1].isspace() else ''
        fields = {k: int(v) for k, v in re.findall(r'\b(bytes_received|bytes_sent|bytes_retrans|bytes_acked|rcv_ooopack|reord_seen|dsack_dups|segs_in|segs_out):([0-9]+)', detail)}
        rtt = re.search(r'\brtt:([\d.]+)/([\d.]+)', detail)
        if rtt:
            fields.update(rtt_ms=float(rtt[1]), rttvar_ms=float(rtt[2]))
        retrans = re.search(r'\bretrans:(\d+)/(\d+)', detail)
        fields.update(retrans_total=int(retrans[2]) if retrans else 0,
                      recv_q=int(parts[0]), send_q=int(parts[1]), local=parts[2], peer=parts[3],
                      endpoint=line.strip(), raw=detail.strip())
        fields['transport'] = int(parts[3].rsplit(':', 1)[1]) in {22, 22345}
        sockets.append(fields)
    return sockets

def snapshot(pid, start):
    t=time.monotonic(); data=rows()
    assert pid in data and data[pid]['start']==start, 'root identity changed'
    children={pid}
    for _ in range(12): children.update(p for p,r in data.items() if r['ppid'] in children)
    ssh=[p for p in children if data[p]['exe']=='/usr/bin/ssh']
    raw=subprocess.check_output(['ss','-Htinp','state','established'],text=True)
    sockets=[row for row in parse_sockets(raw) if any('pid='+str(p)+',' in row['endpoint'] for p in ssh)]
    return dict(monotonic=t, time=time.time(),hz=os.sysconf('SC_CLK_TCK'),processes={str(p):data[p] for p in children},sockets=sockets,transport_sockets=[s for s in sockets if s['transport']])

if __name__=='__main__':
    root=pathlib.Path(__file__).parent
    if sys.argv[1]=='sender':
        profile=sys.argv[2]; data=rows()
        found=[p for p,r in data.items() if r['exe'] in ['/usr/bin/waypipe','/usr/lib/waypipe-remote-chrome/waypipe'] and 'server' in r['args'] and '--user-data-dir='+profile in r['args']]
        assert len(found)==1, found
        print(json.dumps(snapshot(found[0],data[found[0]]['start'])))
    else:
        ident=json.loads((root/(sys.argv[1]+'.identity.json')).read_text())
        print(json.dumps(snapshot(ident['pid'],ident['start'])))

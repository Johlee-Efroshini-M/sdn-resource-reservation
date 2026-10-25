"""
Network Resource Reservation - Web Portal (no external dependencies)

Run:   sudo python3 app.py            then open  http://localhost:8080
(sudo is only needed for the iPerf3 button, which runs iperf3 inside the
Mininet hosts through 'mnexec'. Everything else works without root.)

Start order:  1) ryu-manager reservation.py   2) sudo python3 topology.py
              3) sudo python3 app.py
"""
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RESERVATION_SERVER = ("127.0.0.1", 9000)
RYU_OPENFLOW = ("127.0.0.1", 6653)
WEB_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
HOSTNAME = {"10.0.0.1": "h1", "10.0.0.2": "h2", "10.0.0.3": "h3", "10.0.0.4": "h4"}
iperf_lock = threading.Lock()


# ----------------------------------------------------------------- backend helpers
def tcp_request(line):
    """Send one command to the TCP reservation server, return parsed JSON."""
    with socket.create_connection(RESERVATION_SERVER, timeout=4) as s:
        s.sendall((line + "\n").encode())
        return json.loads(s.makefile().readline())


def port_open(addr):
    try:
        socket.create_connection(addr, timeout=1).close()
        return True
    except OSError:
        return False


def mininet_pid(host):
    out = subprocess.run(["pgrep", "-f", "mininet:%s$" % host],
                         capture_output=True, text=True).stdout.split()
    return out[0] if out else None


def get_status():
    conn = {"ryu": port_open(RYU_OPENFLOW), "server": False,
            "mininet": mininet_pid("h1") is not None,
            "iperf3": shutil.which("iperf3") is not None,
            "root": os.geteuid() == 0}
    status = None
    try:
        status = tcp_request("STATUS")
        conn["server"] = True
    except (OSError, ValueError):
        pass
    return {"conn": conn, "status": status}


def run_iperf(src, dst, dur):
    if os.geteuid() != 0:
        return {"error": "Run app.py with sudo to use the iPerf3 test."}
    if not shutil.which("iperf3"):
        return {"error": "iperf3 not installed (sudo apt install iperf3)."}
    if src not in HOSTNAME or dst not in HOSTNAME or src == dst:
        return {"error": "Invalid source/destination."}
    ps, pd = mininet_pid(HOSTNAME[src]), mininet_pid(HOSTNAME[dst])
    if not ps or not pd:
        return {"error": "Mininet hosts not found. Is topology.py running?"}
    dur = max(1, min(int(dur), 30))
    if not iperf_lock.acquire(blocking=False):
        return {"error": "Another iPerf test is already running."}
    port = str(random.randint(5300, 5999))
    server = None
    try:
        server = subprocess.Popen(["mnexec", "-a", pd, "iperf3", "-s", "-1", "-p", port],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        out = subprocess.run(["mnexec", "-a", ps, "iperf3", "-c", dst, "-p", port,
                              "-t", str(dur), "-J"],
                             capture_output=True, text=True, timeout=dur + 15)
        j = json.loads(out.stdout)
        if "error" in j:
            return {"error": j["error"]}
        end = j["end"]
        return {"src": src, "dst": dst, "duration": dur,
                "mbps": round(end["sum_received"]["bits_per_second"] / 1e6, 2),
                "retransmits": end["sum_sent"].get("retransmits", 0)}
    except (subprocess.TimeoutExpired, ValueError, KeyError) as e:
        return {"error": "iPerf test failed: %s" % e}
    finally:
        if server:
            server.terminate()
        iperf_lock.release()


# ----------------------------------------------------------------- HTTP handler
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/status":
            self.reply(get_status())
        elif self.path in ("/", "/index.html"):
            self.reply(PAGE, ctype="text/html; charset=utf-8")
        else:
            self.reply({"error": "not found"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/reserve":
                line = "RESERVE %s %s %s %s" % (d["src"], d["dst"], float(d["bw"]), int(d["duration"]))
                self.reply(tcp_request(line))
            elif self.path == "/api/release":
                self.reply(tcp_request("RELEASE %d" % int(d["id"])))
            elif self.path == "/api/iperf":
                self.reply(run_iperf(d["src"], d["dst"], d["duration"]))
            else:
                self.reply({"error": "not found"}, 404)
        except OSError:
            self.reply({"status": "ERROR", "reason": "Reservation server unreachable"})
        except (ValueError, KeyError) as e:
            self.reply({"status": "ERROR", "reason": "bad input: %s" % e})


# ----------------------------------------------------------------- front-end
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network Resource Reservation</title>
<style>
:root{--bg:#0b1020;--card:#141b2f;--line:#243050;--tx:#e8ecf8;--mu:#8b96b8;--ac:#4f8cff;--ok:#2ecc8f;--bad:#ff5d73;--warn:#ffb347}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:18px 28px;border-bottom:1px solid var(--line)}
h1{font-size:19px;margin:0}h1 small{display:block;color:var(--mu);font-weight:400;font-size:12px}
main{max-width:1200px;margin:0 auto;padding:22px;display:grid;gap:16px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{margin:0 0 12px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mu)}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c5{grid-column:span 5}.c7{grid-column:span 7}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media(max-width:900px){.c3,.c4,.c5,.c7,.c8{grid-column:span 12}}
.big{font-size:30px;font-weight:700}.big span{font-size:14px;color:var(--mu);font-weight:400}
.pill{display:inline-block;padding:4px 11px;border-radius:99px;font-size:12px;margin-left:6px;border:1px solid var(--line)}
.pill.ok{color:var(--ok);border-color:var(--ok)}.pill.bad{color:var(--bad);border-color:var(--bad)}
label{display:block;font-size:12px;color:var(--mu);margin:10px 0 4px}
input,select{width:100%;padding:9px 10px;background:#0d1326;color:var(--tx);border:1px solid var(--line);border-radius:8px;font:inherit}
.row{display:flex;gap:10px}.row>*{flex:1}
button{cursor:pointer;border:0;border-radius:8px;padding:10px 14px;font:inherit;font-weight:600;background:var(--ac);color:#fff}
button.sec{background:#26335a}button.del{background:transparent;color:var(--bad);border:1px solid var(--bad);padding:4px 10px}
button:disabled{opacity:.5;cursor:wait}.full{width:100%;margin-top:14px}
.bar{height:12px;background:#0d1326;border-radius:99px;overflow:hidden;margin-top:10px}
.bar>div{height:100%;width:0;background:var(--ok);transition:width .5s,background .5s}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 6px;border-bottom:1px solid var(--line)}
th{color:var(--mu);font-weight:500;font-size:12px}.empty{color:var(--mu);padding:14px 0}
.kv{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line)}.kv:last-child{border:0}
.acc{color:var(--ok)}.rej{color:var(--bad)}
#log{max-height:150px;overflow:auto;font:12px ui-monospace,Consolas,monospace}#log div{padding:2px 0}
canvas{width:100%;height:140px;display:block}
.cmp{display:flex;gap:18px;margin-top:12px;align-items:flex-end}.cmp div{flex:1}
.cmp .v{font-size:24px;font-weight:700}
</style></head><body>
<header>
 <h1>Network Resource Reservation System<small>TCP reservation service &middot; Ryu SDN controller &middot; OpenFlow meters &middot; Mininet</small></h1>
 <div>Ryu<span id="p-ryu" class="pill">...</span> Reservation server<span id="p-srv" class="pill">...</span> Mininet<span id="p-mn" class="pill">...</span></div>
</header>
<main>
 <div class="card c3"><h2>Link capacity</h2><div class="big"><span id="cap">-</span> <span>Mbps</span></div></div>
 <div class="card c3"><h2>Reserved</h2><div class="big"><span id="used" style="font-size:30px;color:var(--tx)">-</span> <span>Mbps</span></div></div>
 <div class="card c3"><h2>Available</h2><div class="big acc"><span id="free" style="font-size:30px;color:inherit">-</span> <span>Mbps</span></div></div>
 <div class="card c3"><h2>Utilization</h2><div class="big"><span id="util" style="font-size:30px;color:var(--tx)">-</span><span>%</span></div><div class="bar"><div id="ubar"></div></div></div>

 <div class="card c4"><h2>New reservation</h2>
  <label>Source IP</label><select id="src"></select>
  <label>Destination IP</label><select id="dst"></select>
  <div class="row"><div><label>Bandwidth (Mbps)</label><input id="bw" type="number" min="0.5" step="0.5" value="4"></div>
  <div><label>Duration (s)</label><input id="dur" type="number" min="1" max="3600" value="60"></div></div>
  <button class="full" id="btn-res" onclick="reserve()">Reserve</button>
 </div>

 <div class="card c8"><h2>Utilization over time</h2><canvas id="chart" width="720" height="140"></canvas>
  <div style="display:flex;gap:26px;margin-top:10px">
   <div>Accepted <b class="acc" id="s-acc">0</b></div><div>Rejected <b class="rej" id="s-rej">0</b></div>
   <div>Conflicts <b id="s-con">0</b></div><div>Expired <b id="s-exp">0</b></div><div>Invalid <b id="s-inv">0</b></div></div>
 </div>

 <div class="card c12"><h2>Active reservations</h2>
  <table><thead><tr><th>ID</th><th>Flow</th><th>Bandwidth</th><th>Expires in</th><th></th></tr></thead><tbody id="rows"></tbody></table>
  <div class="empty" id="none">No active reservations.</div>
 </div>

 <div class="card c5"><h2>iPerf3 bandwidth test</h2>
  <div class="row"><div><label>From</label><select id="isrc"></select></div><div><label>To</label><select id="idst"></select></div></div>
  <div class="row"><div><label>Duration (s)</label><input id="idur" type="number" min="1" max="30" value="8"></div></div>
  <button class="full sec" id="btn-ip" onclick="iperf()">Run test</button>
  <div id="ires" class="cmp" style="display:none"><div><div style="color:var(--mu)">Reserved</div><div class="v" id="i-res">-</div></div><div><div style="color:var(--mu)">Measured</div><div class="v" id="i-mes">-</div></div></div>
  <div id="inote" style="color:var(--mu);margin-top:8px"></div>
 </div>
 <div class="card c7"><h2>Event log</h2><div id="log"></div></div>
</main>
<script>
const $=s=>document.querySelector(s), HOSTS=['10.0.0.1','10.0.0.2','10.0.0.3','10.0.0.4'];
let snapAt=Date.now(), hist=[];
function fill(id,def){$(id).innerHTML=HOSTS.map((h,i)=>`<option ${i==def?'selected':''}>${h}</option>`).join('')}
['#src','#isrc'].forEach(s=>fill(s,0));['#dst','#idst'].forEach(s=>fill(s,2));
async function api(p,b){const r=await fetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:undefined);return r.json()}
function log(m,c){const d=document.createElement('div');d.className=c||'';d.textContent=new Date().toLocaleTimeString()+'  '+m;$('#log').prepend(d)}
function pill(id,ok){const e=$(id);e.className='pill '+(ok?'ok':'bad');e.textContent=ok?'connected':'offline'}
function fmt(s){s=Math.max(0,Math.round(s));return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
async function reserve(){
 const src=$('#src').value,dst=$('#dst').value;
 if(src===dst)return log('Source and destination must differ','rej');
 $('#btn-res').disabled=true;
 const r=await api('/api/reserve',{src,dst,bw:$('#bw').value,duration:$('#dur').value});
 $('#btn-res').disabled=false;
 if(r.status==='ACCEPTED')log(`ACCEPTED #${r.id}  ${src} -> ${dst}  ${r.bandwidth_mbps} Mbps for ${r.duration_s}s`,'acc');
 else log(`${r.status}: ${r.reason}`+(r.available_mbps!==undefined?` (${r.available_mbps} Mbps free)`:''),'rej');
 refresh();
}
async function rel(id){const r=await api('/api/release',{id});log(r.status==='RELEASED'?`RELEASED #${id}`:`ERROR: ${r.reason}`,r.status==='RELEASED'?'':'rej');refresh()}
async function iperf(){
 const src=$('#isrc').value,dst=$('#idst').value,dur=$('#idur').value;
 $('#btn-ip').disabled=true;$('#btn-ip').textContent='Running... ('+dur+'s)';$('#inote').textContent='';
 const r=await api('/api/iperf',{src,dst,duration:dur});
 $('#btn-ip').disabled=false;$('#btn-ip').textContent='Run test';
 if(r.error){$('#ires').style.display='none';$('#inote').textContent=r.error;return log('iPerf: '+r.error,'rej')}
 const m=(window.lastActive||[]).find(x=>x.src===src&&x.dst===dst);
 $('#ires').style.display='flex';$('#i-mes').textContent=r.mbps+' Mbps';
 $('#i-res').textContent=m?m.bw+' Mbps':'none (best effort)';
 $('#inote').textContent=m?'Traffic is rate-limited by the OpenFlow meter to the reserved bandwidth.':'No reservation for this flow, so it can use the full link.';
 log(`iPerf3 ${src} -> ${dst}: ${r.mbps} Mbps`);
}
function draw(){
 const c=$('#chart'),x=c.getContext('2d'),W=c.width,H=c.height;x.clearRect(0,0,W,H);
 x.strokeStyle='#243050';x.fillStyle='#8b96b8';x.font='11px sans-serif';
 [0,50,100].forEach(v=>{const y=H-6-(H-18)*v/100;x.beginPath();x.moveTo(30,y);x.lineTo(W,y);x.stroke();x.fillText(v+'%',2,y+4)});
 if(hist.length<2)return;
 const st=(W-34)/59,pts=hist.map((v,i)=>[34+i*st,H-6-(H-18)*v/100]);
 x.beginPath();pts.forEach((p,i)=>i?x.lineTo(...p):x.moveTo(...p));x.strokeStyle='#4f8cff';x.lineWidth=2;x.stroke();
 x.lineTo(pts[pts.length-1][0],H-6);x.lineTo(pts[0][0],H-6);x.fillStyle='rgba(79,140,255,.15)';x.fill();x.lineWidth=1;
}
async function refresh(){
 let d;try{d=await api('/api/status')}catch(e){return}
 snapAt=Date.now();pill('#p-ryu',d.conn.ryu);pill('#p-srv',d.conn.server);pill('#p-mn',d.conn.mininet);
 const s=d.status;if(!s){$('#rows').innerHTML='';window.lastActive=[];return}
 $('#cap').textContent=s.capacity_mbps;$('#used').textContent=s.used_mbps;
 $('#free').textContent=(s.capacity_mbps-s.used_mbps).toFixed(2);$('#util').textContent=s.utilization_pct;
 const b=$('#ubar');b.style.width=s.utilization_pct+'%';b.style.background=s.utilization_pct>85?'var(--bad)':s.utilization_pct>60?'var(--warn)':'var(--ok)';
 const t=s.stats;$('#s-acc').textContent=t.accepted;$('#s-rej').textContent=t.rejected;$('#s-con').textContent=t.conflicts;$('#s-exp').textContent=t.expired;$('#s-inv').textContent=t.invalid;
 hist.push(s.utilization_pct);if(hist.length>60)hist.shift();draw();
 window.lastActive=s.active;$('#none').style.display=s.active.length?'none':'block';
 $('#rows').innerHTML=s.active.map(r=>`<tr><td>#${r.id}</td><td>${r.src} &rarr; ${r.dst}</td><td>${r.bw} Mbps</td><td class="cd" data-r="${r.remaining_s}">${fmt(r.remaining_s)}</td><td><button class="del" onclick="rel(${r.id})">Release</button></td></tr>`).join('');
}
setInterval(()=>document.querySelectorAll('.cd').forEach(e=>e.textContent=fmt(e.dataset.r-(Date.now()-snapAt)/1000)),500);
setInterval(refresh,2000);refresh();log('Portal ready');
</script></body></html>
"""

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    print("Portal running at http://localhost:%d" % WEB_PORT)
    if os.geteuid() != 0:
        print("Note: not running as root - the iPerf3 test button will be disabled.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

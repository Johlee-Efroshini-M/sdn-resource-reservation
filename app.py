"""
Network Resource Reservation - Web Portal (no external dependencies)

Run:   sudo python3 app.py            then open  http://localhost:8080
(sudo is needed only for iPerf3 and for the measuring parts of the scenarios,
which run iperf3 inside the Mininet hosts through 'mnexec'.)

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
WEB_PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8080
HOSTNAME = {"10.0.0.1": "h1", "10.0.0.2": "h2", "10.0.0.3": "h3", "10.0.0.4": "h4"}
iperf_lock = threading.Lock()
JOB = {"name": "", "running": False, "progress": "", "result": None, "error": None}


# ----------------------------------------------------------------- helpers
def tcp_request(line, timeout=5):
    """Send one command to the TCP reservation server, return parsed JSON."""
    with socket.create_connection(RESERVATION_SERVER, timeout=timeout) as s:
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


def mininet_error():
    """Return a message if iperf-in-Mininet can't be used right now, else None."""
    if os.geteuid() != 0:
        return "Run app.py with sudo to use iPerf3 / this scenario."
    if not shutil.which("iperf3"):
        return "iperf3 not installed (sudo apt install iperf3)."
    if not mininet_pid("h1"):
        return "Mininet hosts not found. Is topology.py running?"
    return None


def get_status():
    conn = {"ryu": port_open(RYU_OPENFLOW), "server": False,
            "mininet": mininet_pid("h1") is not None}
    status = None
    try:
        status = tcp_request("STATUS")
        conn["server"] = True
    except (OSError, ValueError):
        pass
    return {"conn": conn, "status": status}


def iperf_mbps(src, dst, dur, udp_mbps=None):
    """Run iperf3 between two Mininet hosts; return receiver throughput (Mbps)."""
    ps, pd = mininet_pid(HOSTNAME[src]), mininet_pid(HOSTNAME[dst])
    port = str(random.randint(5300, 5999))
    server = subprocess.Popen(["mnexec", "-a", pd, "iperf3", "-s", "-1", "-p", port],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        cmd = ["mnexec", "-a", ps, "iperf3", "-c", dst, "-p", port, "-t", str(dur), "-J"]
        if udp_mbps:
            cmd += ["-u", "-b", "%dM" % udp_mbps]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=dur + 15)
        j = json.loads(out.stdout)
        if "error" in j:
            raise RuntimeError(j["error"])
        end = j["end"]
        bps = end["sum"]["bits_per_second"] if udp_mbps else end["sum_received"]["bits_per_second"]
        return round(bps / 1e6, 2)
    finally:
        server.terminate()


def measure_concurrent(flows, dur=8):
    """Run iperf3 for several (src, dst) flows at the same time -> list of Mbps."""
    out = [None] * len(flows)

    def work(i, s, d):
        try:
            out[i] = iperf_mbps(s, d, dur)
        except Exception:
            out[i] = None

    ts = [threading.Thread(target=work, args=(i, s, d)) for i, (s, d) in enumerate(flows)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def run_iperf(src, dst, dur):
    err = mininet_error()
    if err:
        return {"error": err}
    if src not in HOSTNAME or dst not in HOSTNAME or src == dst:
        return {"error": "Invalid source/destination."}
    if JOB["running"]:
        return {"error": "A scenario is running; wait for it to finish."}
    dur = max(1, min(int(dur), 30))
    if not iperf_lock.acquire(blocking=False):
        return {"error": "Another iPerf test is already running."}
    try:
        return {"src": src, "dst": dst, "duration": dur, "mbps": iperf_mbps(src, dst, dur)}
    except (subprocess.TimeoutExpired, ValueError, KeyError, RuntimeError) as e:
        return {"error": "iPerf test failed: %s" % e}
    finally:
        iperf_lock.release()


def why(r):
    if r["status"] == "ACCEPTED":
        return "reservation #%d" % r["id"]
    if r.get("bottleneck"):
        return "%s on %s (%s Mbps free)" % (r["reason"], r["bottleneck"], r["available_mbps"])
    return r.get("reason", "")


# ----------------------------------------------------------------- scenarios
def sc_race(rounds=30):
    """Two clients ask for 6 Mbps over the same 10 Mbps trunk at the same instant."""
    one = both = none = 0
    for i in range(rounds):
        JOB["progress"] = "round %d / %d" % (i + 1, rounds)
        tcp_request("RELEASEALL")
        res, barrier = ["?", "?"], threading.Barrier(2)

        def work(k, src, dst):
            barrier.wait()
            try:
                res[k] = tcp_request("RESERVE %s %s 6 30" % (src, dst))["status"]
            except Exception:
                res[k] = "FAILED"

        ts = [threading.Thread(target=work, args=(0, "10.0.0.1", "10.0.0.3")),
              threading.Thread(target=work, args=(1, "10.0.0.2", "10.0.0.4"))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        acc = res.count("ACCEPTED")
        one, both, none = one + (acc == 1), both + (acc == 2), none + (acc == 0)
    tcp_request("RELEASEALL")
    ok = both == 0 and one == rounds
    return {"header": ["Rounds", "Exactly one accepted", "Both accepted (race bug)",
                       "None accepted", "Result"],
            "rows": [[str(rounds), str(one), str(both), str(none), "PASS" if ok else "FAIL"]],
            "note": "h1->h3 and h2->h4 each ask for 6 Mbps on the shared 10 Mbps trunk, "
                    "released simultaneously. The atomic check-and-reserve admits exactly one."}


def sc_concurrency():
    cap = tcp_request("STATUS")["capacity_mbps"]
    rows = []
    for n in (5, 10, 20, 40):
        JOB["progress"] = "%d simultaneous clients..." % n
        tcp_request("RELEASEALL")
        tcp_request("POLICY ON")
        results, lock, barrier = [], threading.Lock(), threading.Barrier(n)

        def worker():
            src, dst = random.sample(list(HOSTNAME), 2)
            bw = random.choice([1, 2, 3, 4, 5, 6])
            barrier.wait()
            t0 = time.time()
            try:
                r = tcp_request("RESERVE %s %s %d 30" % (src, dst, bw), timeout=10)
            except Exception as e:
                r = {"status": "FAILED", "reason": str(e)}
            with lock:
                results.append((r, (time.time() - t0) * 1000))

        ts = [threading.Thread(target=worker) for _ in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        st = tcp_request("STATUS")
        acc = sum(1 for r, _ in results if r["status"] == "ACCEPTED")
        conf = sum(1 for r, _ in results if r.get("reason") == "conflict")
        nocap = sum(1 for r, _ in results if r.get("reason") == "insufficient bandwidth")
        worst = max(st["links"].values())
        rows.append([str(n), str(acc), str(conf), str(nocap), "%d%%" % (100 * acc / n),
                     str(worst), "YES" if worst <= cap + 1e-9 else "NO",
                     "%.1f" % (sum(l for _, l in results) / len(results))])
    tcp_request("RELEASEALL")
    return {"header": ["Clients", "Accepted", "Conflict", "No capacity", "Accept ratio",
                       "Busiest link Mbps", "No link over capacity", "Avg latency ms"],
            "rows": rows, "note": "All clients hit the single server at the same instant."}


def sc_fan(kind):
    """kind='out': h1 -> h2,h3,h4 share h1's uplink.  kind='in': h2,h3,h4 -> h1 share h1's downlink."""
    tcp_request("RELEASEALL")
    tcp_request("POLICY ON")
    hub = "10.0.0.1"
    others = [("10.0.0.2", 6), ("10.0.0.3", 3), ("10.0.0.4", 2)]
    reqs = [(hub, o, bw) if kind == "out" else (o, hub, bw) for o, bw in others]
    shared = "h1->s1" if kind == "out" else "s1->h1"
    label = lambda s, d: "%s -> %s" % (HOSTNAME[s], HOSTNAME[d])
    rows, accepted = [], []                      # accepted: (row index, src, dst)
    ids = {}
    for s, d, bw in reqs:
        JOB["progress"] = "Requesting %s at %d Mbps" % (label(s, d), bw)
        r = tcp_request("RESERVE %s %s %d 180" % (s, d, bw))
        rows.append([label(s, d), "%d Mbps" % bw, r["status"], why(r), "-"])
        if r["status"] == "ACCEPTED":
            accepted.append((len(rows) - 1, s, d))
            ids[(s, d)] = r["id"]
    used = tcp_request("STATUS")["links"][shared]
    JOB["progress"] = "Measuring accepted flows at the same time..."
    if mininet_error():
        for i, _, _ in accepted:
            rows[i][4] = "n/a (needs sudo + Mininet)"
    else:
        for (i, _, _), m in zip(accepted, measure_concurrent([(s, d) for _, s, d in accepted])):
            rows[i][4] = "%s Mbps" % m if m is not None else "failed"
    # free one reservation and retry the rejected request -> bandwidth is reusable
    rejected = [(s, d, bw) for (s, d, bw) in reqs if (s, d) not in ids]
    if rejected and accepted:
        s2, d2 = accepted[1][1:] if len(accepted) > 1 else accepted[0][1:]
        JOB["progress"] = "Releasing %s and retrying" % label(s2, d2)
        rr = tcp_request("RELEASE %d" % ids[(s2, d2)])
        rows.append(["release " + label(s2, d2), "-", rr["status"], rr.get("reason", ""), "-"])
        s3, d3, bw3 = rejected[0]
        r = tcp_request("RESERVE %s %s %d 60" % (s3, d3, bw3))
        rows.append(["retry " + label(s3, d3), "%d Mbps" % bw3, r["status"], why(r), "-"])
    tcp_request("RELEASEALL")
    side = "uplink" if kind == "out" else "downlink"
    return {"header": ["Flow", "Requested", "Result", "Detail", "Measured"], "rows": rows,
            "note": "Shared %s %s after the three requests: %s / 10 Mbps used. "
                    "6 + 3 fit, the extra 2 Mbps is rejected until bandwidth is released." %
                    (side, shared, used)}


def sc_abuse():
    tcp_request("RELEASEALL")
    cases = [
        ("Unknown destination host", "RESERVE 10.0.0.1 10.0.0.9 2 10", "ERROR"),
        ("Source equals destination", "RESERVE 10.0.0.1 10.0.0.1 2 10", "ERROR"),
        ("Negative bandwidth", "RESERVE 10.0.0.1 10.0.0.3 -5 10", "ERROR"),
        ("Bandwidth above link capacity", "RESERVE 10.0.0.1 10.0.0.3 50 10", "ERROR"),
        ("Zero duration", "RESERVE 10.0.0.1 10.0.0.3 2 0", "ERROR"),
        ("Non-IP addresses", "RESERVE abc def 2 10", "ERROR"),
        ("Release unknown id", "RELEASE 99999", "ERROR"),
        ("Unknown command", "HELLO WORLD", "ERROR"),
        ("Valid 8 Mbps request", "RESERVE 10.0.0.1 10.0.0.3 8 20", "ACCEPTED"),
        ("Overbooking (8 + 5 > 10)", "RESERVE 10.0.0.2 10.0.0.4 5 20", "REJECTED"),
        ("Duplicate flow (conflict)", "RESERVE 10.0.0.1 10.0.0.3 1 20", "REJECTED"),
        ("Fits in remaining 2 Mbps", "RESERVE 10.0.0.2 10.0.0.4 2 20", "ACCEPTED"),
    ]
    rows, ok_count = [], 0
    for name, line, expect in cases:
        JOB["progress"] = name
        r = tcp_request(line)
        ok = r["status"] == expect
        ok_count += ok
        rows.append([name, expect, r["status"], why(r) if r["status"] != "ACCEPTED" else "", "PASS" if ok else "FAIL"])
    try:
        with socket.create_connection(RESERVATION_SERVER, timeout=3) as s:
            s.sendall(b"\xff\xfe\x00garbage\n")
            time.sleep(0.3)
    except OSError:
        pass
    try:
        alive = tcp_request("STATUS")["capacity_mbps"] > 0
    except Exception:
        alive = False
    ok_count += alive
    rows.append(["Binary garbage, then server alive?", "alive", "alive" if alive else "DEAD",
                 "", "PASS" if alive else "FAIL"])
    tcp_request("RELEASEALL")
    return {"header": ["Case", "Expected", "Got", "Reason", "Result"], "rows": rows,
            "note": "%d / %d checks passed" % (ok_count, len(rows))}


def _safe_iperf(*a):
    try:
        iperf_mbps(*a)
    except Exception:
        pass


def sc_congestion(dur=10):
    cases = [("A. Baseline: no reservation, UDP flood", False, False),
             ("B. Static: reservation, no best-effort limit", True, False),
             ("C. Proposed: reservation + dynamic best-effort limit", True, True)]
    rows = []
    for label, reserve, protect in cases:
        JOB["progress"] = "Running " + label
        tcp_request("RELEASEALL")
        tcp_request("POLICY ON" if protect else "POLICY OFF")
        if reserve:
            tcp_request("RESERVE 10.0.0.1 10.0.0.3 4 300")
        time.sleep(1)
        flood = threading.Thread(target=_safe_iperf, args=("10.0.0.2", "10.0.0.4", dur + 4, 10))
        flood.start()
        time.sleep(3)
        mbps = iperf_mbps("10.0.0.1", "10.0.0.3", dur)
        flood.join()
        rows.append([label, "4" if reserve else "-", str(mbps)])
    tcp_request("RELEASEALL")
    tcp_request("POLICY ON")
    return {"header": ["Condition", "Reserved Mbps", "h1 to h3 measured Mbps"], "rows": rows,
            "bar": 2, "note": "h2 to h4 floods the trunk with 10 Mbps of unreserved UDP traffic."}


def sc_expiry():
    tcp_request("RELEASEALL")
    tcp_request("POLICY ON")
    t0 = time.time()
    tcp_request("RESERVE 10.0.0.1 10.0.0.3 4 18")
    JOB["progress"] = "Measuring while reserved (4 Mbps)..."
    used_before = tcp_request("STATUS")["used_mbps"]
    during = iperf_mbps("10.0.0.1", "10.0.0.3", 5)
    JOB["progress"] = "Waiting for the reservation to expire..."
    time.sleep(max(0, 21 - (time.time() - t0)))
    used_after = tcp_request("STATUS")["used_mbps"]
    JOB["progress"] = "Measuring after expiry..."
    after = iperf_mbps("10.0.0.1", "10.0.0.3", 5)
    return {"header": ["Phase", "Busiest link reserved Mbps", "h1 to h3 measured Mbps"],
            "rows": [["During reservation (4 Mbps)", str(used_before), str(during)],
                     ["After expiry", str(used_after), str(after)]],
            "bar": 2, "note": "Throughput returns to best effort once the policy is removed."}


def sc_custom(p):
    """User-defined scenario: any servers/clients, bandwidths and durations."""
    flows, n = p["flows"], len(p["flows"])
    if p.get("clear", True):
        tcp_request("RELEASEALL")
    results = [None] * n
    barrier = threading.Barrier(n) if p.get("mode") == "parallel" and n > 1 else None

    def req(i):
        f = flows[i]
        if barrier:
            barrier.wait()                     # every client fires at the same instant
        try:
            results[i] = tcp_request("RESERVE %s %s %s %d" % (
                f["src"], f["dst"], f["bw"], f["duration"]), timeout=10)
        except Exception as e:
            results[i] = {"status": "FAILED", "reason": str(e)}

    JOB["progress"] = "Sending %d request(s) %s..." % (
        n, "at the same instant" if barrier else "one after another")
    if barrier:
        ts = [threading.Thread(target=req, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    else:
        for i in range(n):
            req(i)

    rows, accepted = [], []
    for i, f in enumerate(flows):
        r = results[i]
        rows.append(["%s -> %s" % (HOSTNAME[f["src"]], HOSTNAME[f["dst"]]),
                     "%g Mbps for %ds" % (f["bw"], f["duration"]), r["status"], why(r), "-"])
        if r["status"] == "ACCEPTED":
            accepted.append(i)
    note = []
    if p.get("measure") and accepted:
        err = mininet_error()
        min_dur = min(flows[i]["duration"] for i in accepted)
        test_s = min(8, min_dur - 5)
        if err:
            note.append("Not measured: " + err)
        elif test_s < 3:
            note.append("Not measured: reservation duration too short (use 10 s or more).")
        else:
            JOB["progress"] = "Measuring %d accepted flow(s) with iPerf3 at the same time..." % len(accepted)
            vals = measure_concurrent([(flows[i]["src"], flows[i]["dst"]) for i in accepted], test_s)
            for i, m in zip(accepted, vals):
                rows[i][4] = "%s Mbps" % m if m is not None else "failed"
    st = tcp_request("STATUS")
    busy = ["%s %s" % (l, u) for l, u in st["links"].items() if u > 0]
    note.append("Accepted %d / %d. Reserved per link (Mbps): %s." % (
        len(accepted), n, ", ".join(busy) if busy else "none"))
    if p.get("release_after"):
        tcp_request("RELEASEALL")
        note.append("All reservations released.")
    return {"header": ["Flow", "Requested", "Result", "Detail", "Measured"],
            "rows": rows, "note": " ".join(note)}


def start_custom(d):
    try:
        flows = []
        for f in d["flows"]:
            src, dst = str(f["src"]), str(f["dst"])
            bw, dur = float(f["bw"]), int(f["duration"])
            if src not in HOSTNAME or dst not in HOSTNAME:
                return {"error": "Unknown host in flow list."}
            if not (0 < bw <= 1000) or not (1 <= dur <= 3600):
                return {"error": "Bandwidth must be > 0 and duration 1-3600 s."}
            flows.append({"src": src, "dst": dst, "bw": bw, "duration": dur})
        if not 1 <= len(flows) <= 40:
            return {"error": "Add between 1 and 40 flows."}
    except (KeyError, TypeError, ValueError):
        return {"error": "Malformed flow list."}
    params = {"flows": flows, "mode": "parallel" if d.get("mode") == "parallel" else "sequential",
              "clear": bool(d.get("clear", True)), "measure": bool(d.get("measure")),
              "release_after": bool(d.get("release_after"))}
    return start_job("custom", params)


# name -> (function, needs Mininet + root)
SCENARIOS = {"race": (sc_race, False), "concurrency": (sc_concurrency, False),
             "fanout": (lambda: sc_fan("out"), False), "fanin": (lambda: sc_fan("in"), False),
             "abuse": (sc_abuse, False), "congestion": (sc_congestion, True),
             "expiry": (sc_expiry, True)}


def run_job(name, params=None):
    try:
        if name == "custom":
            JOB["result"], JOB["error"] = sc_custom(params), None
        else:
            JOB["result"], JOB["error"] = SCENARIOS[name][0](), None
    except Exception as e:
        JOB["result"], JOB["error"] = None, "%s: %s" % (type(e).__name__, e)
    finally:
        if name != "custom":                    # presets restore the default policy
            try:
                tcp_request("POLICY ON")
            except Exception:
                pass
        JOB["running"], JOB["progress"] = False, "Finished"


def start_job(name, params=None):
    if name != "custom" and name not in SCENARIOS:
        return {"error": "Unknown scenario."}
    if JOB["running"]:
        return {"error": "A scenario is already running (use Reset if it is stuck)."}
    if name != "custom" and SCENARIOS[name][1]:
        err = mininet_error()
        if err:
            return {"error": err}
    JOB.update(name=name, running=True, progress="Starting...", result=None, error=None)
    threading.Thread(target=run_job, args=(name, params), daemon=True).start()
    return {"started": name}


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
        elif self.path == "/api/job":
            self.reply(JOB)
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
            elif self.path == "/api/policy":
                self.reply(tcp_request("POLICY ON" if d.get("on") else "POLICY OFF"))
            elif self.path == "/api/iperf":
                self.reply(run_iperf(d["src"], d["dst"], d["duration"]))
            elif self.path == "/api/scenario":
                self.reply(start_job(d.get("name", "")))
            elif self.path == "/api/custom":
                self.reply(start_custom(d))
            elif self.path == "/api/reset":
                JOB.update(running=False, progress="Reset")
                self.reply({"ok": True})
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
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.card h2{margin:0 0 12px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mu)}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c5{grid-column:span 5}.c7{grid-column:span 7}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media(max-width:900px){.c3,.c4,.c5,.c7,.c8{grid-column:span 12}}
.big{font-size:30px;font-weight:700}.big span{font-size:14px;color:var(--mu);font-weight:400}
.pill{display:inline-block;padding:4px 11px;border-radius:99px;font-size:12px;margin-left:6px;border:1px solid var(--line)}
.pill.ok{color:var(--ok);border-color:var(--ok)}.pill.bad{color:var(--bad);border-color:var(--bad)}
label{display:block;font-size:12px;color:var(--mu);margin:10px 0 4px}
input,select{width:100%;padding:9px 10px;background:#0d1326;color:var(--tx);border:1px solid var(--line);border-radius:8px;font:inherit}
input[type=checkbox]{width:auto;margin-right:8px}
.row{display:flex;gap:10px}.row>*{flex:1}
button{cursor:pointer;border:0;border-radius:8px;padding:10px 14px;font:inherit;font-weight:600;background:var(--ac);color:#fff}
button.sec{background:#26335a}button.del{background:transparent;color:var(--bad);border:1px solid var(--bad);padding:4px 10px}
button:disabled{opacity:.5;cursor:wait}.full{width:100%;margin-top:14px}
.bar{height:12px;background:#0d1326;border-radius:99px;overflow:hidden;margin-top:10px}
.bar>div{height:100%;width:0;background:var(--ok);transition:width .5s,background .5s}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 6px;border-bottom:1px solid var(--line)}
th{color:var(--mu);font-weight:500;font-size:12px}.empty{color:var(--mu);padding:14px 0}
.acc{color:var(--ok)}.rej{color:var(--bad)}.mu{color:var(--mu)}
#log{max-height:150px;overflow:auto;font:12px ui-monospace,Consolas,monospace}#log div{padding:2px 0}
canvas{width:100%;height:140px;display:block}
.cmp{display:flex;gap:18px;margin-top:12px;align-items:flex-end}.cmp div{flex:1}.cmp .v{font-size:24px;font-weight:700}
.sbtns{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px}.sbtns button{flex:1;min-width:170px}
#sc-out{overflow-x:auto}#sc-out .bar{margin-top:6px;max-width:220px}
.links{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 18px}
.lk .lh{display:flex;justify-content:space-between;font-size:12px}.lk .bar{margin-top:5px;height:9px}
</style></head><body>
<header>
 <h1>Network Resource Reservation System<small>TCP reservation service &middot; Ryu SDN controller &middot; OpenFlow meters &middot; Mininet</small></h1>
 <div>Ryu<span id="p-ryu" class="pill">...</span> Reservation server<span id="p-srv" class="pill">...</span> Mininet<span id="p-mn" class="pill">...</span></div>
</header>
<main>
 <div class="card c3"><h2>Capacity per link</h2><div class="big"><span id="cap">-</span> <span>Mbps</span></div></div>
 <div class="card c3"><h2>Busiest link reserved</h2><div class="big"><span id="used" style="font-size:30px;color:var(--tx)">-</span> <span>Mbps</span></div></div>
 <div class="card c3"><h2>Free on busiest link</h2><div class="big acc"><span id="free" style="font-size:30px;color:inherit">-</span> <span>Mbps</span></div></div>
 <div class="card c3"><h2>Peak link utilization</h2><div class="big"><span id="util" style="font-size:30px;color:var(--tx)">-</span><span>%</span></div><div class="bar"><div id="ubar"></div></div></div>

 <div class="card c4"><h2>New reservation</h2>
  <label>Source IP</label><select id="src"></select>
  <label>Destination IP</label><select id="dst"></select>
  <div class="row"><div><label>Bandwidth (Mbps)</label><input id="bw" type="number" min="0.5" step="0.5" value="4"></div>
  <div><label>Duration (s)</label><input id="dur" type="number" min="1" max="3600" value="60"></div></div>
  <button class="full" id="btn-res" onclick="reserve()">Reserve</button>
 </div>

 <div class="card c8"><h2>Peak utilization over time</h2><canvas id="chart" width="720" height="140"></canvas>
  <div style="display:flex;flex-wrap:wrap;gap:22px;margin-top:10px">
   <div>Accepted <b class="acc" id="s-acc">0</b></div><div>Rejected <b class="rej" id="s-rej">0</b></div>
   <div>Conflicts <b id="s-con">0</b></div><div>Expired <b id="s-exp">0</b></div><div>Invalid <b id="s-inv">0</b></div></div>
  <label style="margin-top:14px;color:var(--tx);font-size:13px"><input type="checkbox" id="pol" onchange="setPolicy(this.checked)">Dynamic best-effort protection <span class="mu">(controller limits unreserved traffic to the unreserved capacity)</span></label>
 </div>

 <div class="card c12"><h2>Per-link reserved bandwidth (shared links are where requests compete)</h2>
  <div class="links" id="links"></div></div>

 <div class="card c12"><h2>Active reservations</h2>
  <table><thead><tr><th>ID</th><th>Flow</th><th>Bandwidth</th><th>Path (links used)</th><th>Expires in</th><th></th></tr></thead><tbody id="rows"></tbody></table>
  <div class="empty" id="none">No active reservations.</div>
 </div>

 <div class="card c12"><h2>Test scenarios</h2>
  <div class="sbtns">
   <button class="sec sbtn" onclick="runScen('race')">1a. Race: 2 clients, same instant</button>
   <button class="sec sbtn" onclick="runScen('concurrency')">1b. Many concurrent clients</button>
   <button class="sec sbtn" onclick="runScen('fanout')">2. One server &rarr; many clients</button>
   <button class="sec sbtn" onclick="runScen('fanin')">3. Many servers &rarr; one client</button>
   <button class="sec sbtn" onclick="runScen('abuse')">Invalid / abusive requests</button>
   <button class="sec sbtn" onclick="runScen('congestion')">Congestion (flood)</button>
   <button class="sec sbtn" onclick="runScen('expiry')">Expiry &amp; release</button>
  </div>
  <div style="border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px">
   <div class="mu" style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px">Custom scenario - choose your own servers and clients</div>
   <div class="row" style="align-items:flex-end;flex-wrap:wrap">
    <div><label>Server / source</label><select id="c-src"></select></div>
    <div><label>Client / destination</label><select id="c-dst"></select></div>
    <div><label>Bandwidth (Mbps)</label><input id="c-bw" type="number" min="0.5" step="0.5" value="3"></div>
    <div><label>Duration (s)</label><input id="c-dur" type="number" min="1" value="60"></div>
    <div style="flex:0 0 auto"><button class="sec" onclick="addFlow()">+ Add flow</button></div>
   </div>
   <div class="sbtns" style="margin:12px 0 0">
    <button class="sec" onclick="fillFan('out')">Fill: selected server &rarr; all other hosts</button>
    <button class="sec" onclick="fillFan('in')">Fill: all other hosts &rarr; selected client</button>
    <button class="sec" onclick="flows=[];renderFlows()">Clear list</button>
   </div>
   <table style="margin-top:10px"><thead><tr><th>#</th><th>Flow</th><th>Bandwidth (Mbps)</th><th>Duration (s)</th><th></th></tr></thead><tbody id="c-rows"></tbody></table>
   <div class="empty" id="c-none">No flows yet. Add flows above, or use the fill buttons, then edit bandwidth per flow.</div>
   <div class="row" style="align-items:flex-end;flex-wrap:wrap;margin-top:6px">
    <div><label>Send requests</label><select id="c-mode"><option value="parallel">All at the same instant</option><option value="sequential">One after another</option></select></div>
    <div style="flex:2"><label style="color:var(--tx)"><input type="checkbox" id="c-clear" checked>Release existing reservations first</label>
     <label style="color:var(--tx)"><input type="checkbox" id="c-meas">Measure accepted flows with iPerf3 (needs sudo + Mininet, duration &ge; 10 s)</label>
     <label style="color:var(--tx)"><input type="checkbox" id="c-rel">Release everything when finished</label></div>
    <div style="flex:0 0 auto"><button class="sbtn" onclick="runCustom()">Run custom scenario</button></div>
   </div>
  </div>
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px"><div id="sc-status" class="mu">No scenario run yet.</div><button class="del" onclick="resetJob()">Reset</button></div>
  <div id="sc-out"></div>
 </div>

 <div class="card c5"><h2>iPerf3 bandwidth test</h2>
  <div class="row"><div><label>From</label><select id="isrc"></select></div><div><label>To</label><select id="idst"></select></div></div>
  <div class="row"><div><label>Duration (s)</label><input id="idur" type="number" min="1" max="30" value="8"></div></div>
  <button class="full sec" id="btn-ip" onclick="iperf()">Run test</button>
  <div id="ires" class="cmp" style="display:none"><div><div class="mu">Reserved</div><div class="v" id="i-res">-</div></div><div><div class="mu">Measured</div><div class="v" id="i-mes">-</div></div></div>
  <div id="inote" class="mu" style="margin-top:8px"></div>
 </div>
 <div class="card c7"><h2>Event log</h2><div id="log"></div></div>
</main>
<script>
const $=s=>document.querySelector(s), HOSTS=['10.0.0.1','10.0.0.2','10.0.0.3','10.0.0.4'];
let snapAt=Date.now(), hist=[];
function fill(id,def){$(id).innerHTML=HOSTS.map((h,i)=>`<option ${i==def?'selected':''}>${h}</option>`).join('')}
['#src','#isrc','#c-src'].forEach(s=>fill(s,0));['#dst','#idst','#c-dst'].forEach(s=>fill(s,2));
async function api(p,b){const r=await fetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:undefined);return r.json()}
function log(m,c){const d=document.createElement('div');d.className=c||'';d.textContent=new Date().toLocaleTimeString()+'  '+m;$('#log').prepend(d)}
function pill(id,ok){const e=$(id);e.className='pill '+(ok?'ok':'bad');e.textContent=ok?'connected':'offline'}
function fmt(s){s=Math.max(0,Math.round(s));return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function color(p){return p>85?'var(--bad)':p>60?'var(--warn)':'var(--ok)'}
async function reserve(){
 const src=$('#src').value,dst=$('#dst').value;
 if(src===dst)return log('Source and destination must differ','rej');
 $('#btn-res').disabled=true;
 const r=await api('/api/reserve',{src,dst,bw:$('#bw').value,duration:$('#dur').value});
 $('#btn-res').disabled=false;
 if(r.status==='ACCEPTED')log(`ACCEPTED #${r.id}  ${src} -> ${dst}  ${r.bandwidth_mbps} Mbps for ${r.duration_s}s`,'acc');
 else log(`${r.status}: ${r.reason}`+(r.bottleneck?` on ${r.bottleneck} (${r.available_mbps} Mbps free)`:''),'rej');
 refresh();
}
async function rel(id){const r=await api('/api/release',{id});log(r.status==='RELEASED'?`RELEASED #${id}`:`ERROR: ${r.reason}`,r.status==='RELEASED'?'':'rej');refresh()}
async function setPolicy(on){const r=await api('/api/policy',{on});log('Best-effort protection '+(r.best_effort_protection?'ON':'OFF'))}
async function iperf(){
 const src=$('#isrc').value,dst=$('#idst').value,dur=$('#idur').value;
 $('#btn-ip').disabled=true;$('#btn-ip').textContent='Running... ('+dur+'s)';$('#inote').textContent='';
 const r=await api('/api/iperf',{src,dst,duration:dur});
 $('#btn-ip').disabled=false;$('#btn-ip').textContent='Run test';
 if(r.error){$('#ires').style.display='none';$('#inote').textContent=r.error;return log('iPerf: '+r.error,'rej')}
 const m=(window.lastActive||[]).find(x=>x.src===src&&x.dst===dst);
 $('#ires').style.display='flex';$('#i-mes').textContent=r.mbps+' Mbps';
 $('#i-res').textContent=m?m.bw+' Mbps':'none (best effort)';
 $('#inote').textContent=m?'Traffic is rate-limited by the OpenFlow meter to the reserved bandwidth.':'No reservation for this flow, so it can use the free link capacity.';
 log(`iPerf3 ${src} -> ${dst}: ${r.mbps} Mbps`);
}
async function runScen(name){
 const r=await api('/api/scenario',{name});
 if(r.error){$('#sc-status').textContent=r.error;return log(r.error,'rej')}
 log('Scenario started: '+name);pollJob();
}
function showJob(j){
 $('#sc-status').textContent=j.running?`Running ${j.name}: ${j.progress}`:(j.name?`Last run: ${j.name}`+(j.error?' - FAILED: '+j.error:''):'No scenario run yet.');
 document.querySelectorAll('.sbtn').forEach(b=>b.disabled=j.running);
 const r=j.result;if(!r){if(!j.running)$('#sc-out').innerHTML='';return}
 let h='<table><thead><tr>'+r.header.map(x=>`<th>${x}</th>`).join('')+'</tr></thead><tbody>';
 h+=r.rows.map(row=>'<tr>'+row.map((c,i)=>{
  const cls=c==='PASS'||c==='YES'||c==='ACCEPTED'||c==='RELEASED'?'acc':c==='FAIL'||c==='NO'||c==='DEAD'||c==='REJECTED'?'rej':'';
  const v=parseFloat(c),bar=(r.bar===i&&!isNaN(v))?`<div class="bar"><div style="width:${Math.min(100,v*10)}%"></div></div>`:'';
  return `<td class="${cls}">${c}${bar}</td>`}).join('')+'</tr>').join('')+'</tbody></table>';
 if(r.note)h+=`<div class="mu" style="margin-top:8px">${r.note}</div>`;
 $('#sc-out').innerHTML=h;
}
async function pollJob(){try{const j=await api('/api/job');const was=window.jobRunning;window.jobRunning=j.running;showJob(j);
 if(j.running)setTimeout(pollJob,1500);else if(was)log('Scenario finished: '+j.name+(j.error?' (failed)':''),j.error?'rej':'acc')}catch(e){}}
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
 const b=$('#ubar');b.style.width=s.utilization_pct+'%';b.style.background=color(s.utilization_pct);
 const t=s.stats;$('#s-acc').textContent=t.accepted;$('#s-rej').textContent=t.rejected;$('#s-con').textContent=t.conflicts;$('#s-exp').textContent=t.expired;$('#s-inv').textContent=t.invalid;
 $('#pol').checked=!!s.best_effort_protection;
 $('#links').innerHTML=Object.entries(s.links).map(([n,u])=>{const p=100*u/s.capacity_mbps;
  return `<div class="lk"><div class="lh"><span>${n.replace('->',' &rarr; ')}</span><b>${u} / ${s.capacity_mbps}</b></div><div class="bar"><div style="width:${p}%;background:${color(p)}"></div></div></div>`}).join('');
 hist.push(s.utilization_pct);if(hist.length>60)hist.shift();draw();
 window.lastActive=s.active;$('#none').style.display=s.active.length?'none':'block';
 $('#rows').innerHTML=s.active.map(r=>`<tr><td>#${r.id}</td><td>${r.src} &rarr; ${r.dst}</td><td>${r.bw} Mbps</td><td class="mu">${r.path.join(', ')}</td><td class="cd" data-r="${r.remaining_s}">${fmt(r.remaining_s)}</td><td><button class="del" onclick="rel(${r.id})">Release</button></td></tr>`).join('');
}
setInterval(()=>document.querySelectorAll('.cd').forEach(e=>e.textContent=fmt(e.dataset.r-(Date.now()-snapAt)/1000)),500);
let flows=[];
function renderFlows(){
 $('#c-none').style.display=flows.length?'none':'block';
 $('#c-rows').innerHTML=flows.map((f,i)=>`<tr><td>${i+1}</td><td>${f.src} &rarr; ${f.dst}</td>
  <td><input type="number" min="0.5" step="0.5" style="width:110px" value="${f.bw}" oninput="flows[${i}].bw=+this.value"></td>
  <td><input type="number" min="1" style="width:110px" value="${f.duration}" oninput="flows[${i}].duration=+this.value"></td>
  <td><button class="del" onclick="flows.splice(${i},1);renderFlows()">Remove</button></td></tr>`).join('');
}
function addFlow(){
 const src=$('#c-src').value,dst=$('#c-dst').value;
 if(src===dst)return log('Server and client must differ','rej');
 flows.push({src,dst,bw:+$('#c-bw').value,duration:+$('#c-dur').value});renderFlows();
}
function fillFan(kind){
 const hub=kind==='out'?$('#c-src').value:$('#c-dst').value,bw=+$('#c-bw').value,duration=+$('#c-dur').value;
 flows=HOSTS.filter(h=>h!==hub).map(h=>kind==='out'?{src:hub,dst:h,bw,duration}:{src:h,dst:hub,bw,duration});renderFlows();
}
async function runCustom(){
 if(!flows.length)return log('Add at least one flow first','rej');
 const r=await api('/api/custom',{flows,mode:$('#c-mode').value,clear:$('#c-clear').checked,measure:$('#c-meas').checked,release_after:$('#c-rel').checked});
 if(r.error){$('#sc-status').textContent=r.error;return log(r.error,'rej')}
 log('Custom scenario started ('+flows.length+' flows)');pollJob();
}
async function resetJob(){await api('/api/reset',{});log('Scenario state reset');pollJob()}
renderFlows();
setInterval(refresh,2000);refresh();pollJob();log('Portal ready');
</script></body></html>
"""

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    print("Portal running at http://localhost:%d" % WEB_PORT)
    if os.geteuid() != 0:
        print("Note: not running as root - iPerf3 and the measuring scenarios are disabled.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

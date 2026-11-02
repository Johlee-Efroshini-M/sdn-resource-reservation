"""
Network Resource Reservation System
Ryu SDN controller + embedded TCP reservation server (ONE server, MANY clients).

Run:  ryu-manager reservation.py     (or: python -m ryu.cmd.manager reservation.py)

TCP protocol (one text line per request, JSON reply):
    RESERVE <src_ip> <dst_ip> <bandwidth_mbps> <duration_s>
    RELEASE <reservation_id>
    RELEASEALL                 release every reservation (used by test scripts)
    POLICY ON|OFF              dynamic best-effort protection on/off (baseline)
    STATUS

How admission control works
---------------------------
* The network is modelled as a map of DIRECTED links, each with a capacity:
      h1->s1  s1->h1  h2->s1  s1->h2  h3->s2  s2->h3  h4->s2  s2->h4  s1->s2  s2->s1
* A reservation src->dst uses every link on its path, e.g. h1->h3 uses
      h1->s1, s1->s2, s2->h3
* RESERVE is ATOMIC: under one lock the controller checks the free bandwidth on
  EVERY link of the path and then either commits or rejects.  Two clients can
  therefore never both obtain the same free bandwidth (no race).
* Shared links give the interesting cases:
      one server -> many clients  (h1->h2,h3,h4)  share the uplink  h1->s1
      many servers -> one client  (h2,h3,h4->h1)  share the downlink s1->h1
* Only after the commit are the OpenFlow meter + flow rules pushed to the switches.

Dynamic SDN behaviour
---------------------
* Each accepted reservation gets a metered high-priority flow (rate = reserved).
* A shared best-effort meter limits ALL other IP traffic leaving a switch towards
  the trunk to (capacity - reserved on that trunk direction).  It is MODIFIED
  whenever a reservation is accepted, released or expires, so reserved flows are
  protected from unreserved congestion.
"""
import ipaddress
import itertools
import json
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

LINK_CAPACITY_MBPS = 10.0      # capacity of EVERY directed link (match topology.py)
MAX_DURATION_S = 3600
TCP_HOST, TCP_PORT = "0.0.0.0", 9000
UTIL_LOG = "utilization.csv"

HOSTS = {"10.0.0.1": ("h1", 1), "10.0.0.2": ("h2", 1),     # ip -> (name, switch id)
         "10.0.0.3": ("h3", 2), "10.0.0.4": ("h4", 2)}
LINKS = ([f"{h}->s{s}" for h, s in HOSTS.values()] +
         [f"s{s}->{h}" for h, s in HOSTS.values()] + ["s1->s2", "s2->s1"])
LINKS = sorted(LINKS, key=lambda l: (0 if l.startswith("h") else 1 if l.endswith(("h1", "h2", "h3", "h4")) else 2, l))

BE_METER_ID = 60000            # shared best-effort meter id
MIN_BE_KBPS = 100              # never starve best-effort traffic completely


def burst_kb(rate_kbps):
    """Burst allowance (kilobits) = ~100 ms of traffic, minimum 32 kb."""
    return max(32, int(rate_kbps * 0.1))


def path_links(src, dst):
    (hs, sd), (hd, dd) = HOSTS[src], HOSTS[dst]
    links = ["%s->s%d" % (hs, sd)]
    if sd != dd:
        links.append("s%d->s%d" % (sd, dd))
    links.append("s%d->%s" % (dd, hd))
    return links


class ReservationController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.reservations = {}            # id -> dict (shared reservation table)
        self.next_id = itertools.count(1)
        self.lock = hub.Semaphore(1)      # protects table + link map
        self.be_protect = True            # dynamic best-effort limiting
        self.stats = {"accepted": 0, "rejected": 0, "conflicts": 0,
                      "invalid": 0, "expired": 0}
        self.start_time = time.time()

        # TCP server: one green thread per client connection => concurrent clients
        self.server = hub.StreamServer((TCP_HOST, TCP_PORT), self.handle_client)
        hub.spawn(self.server.serve_forever)
        hub.spawn(self.expiry_loop)
        hub.spawn(self.utilization_loop)
        with open(UTIL_LOG, "w") as f:
            f.write("time_s,used_mbps,utilization_pct,active\n")
        self.logger.info("Reservation server listening on %s:%d", TCP_HOST, TCP_PORT)

    # ------------------------------------------------------------------ OpenFlow
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        normal = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]
        # priority 0: plain L2 switching for everything (ARP etc.)
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, priority=0, match=parser.OFPMatch(),
            instructions=[parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, normal)]))
        # priority 1: all unreserved IP traffic goes through the best-effort meter
        self.set_be_meter(dp, ofp.OFPMC_ADD)
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, priority=1, match=parser.OFPMatch(eth_type=0x0800),
            instructions=[parser.OFPInstructionMeter(BE_METER_ID),
                          parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, normal)]))
        self.logger.info("Switch %s connected", dp.id)

    def be_rate_kbps(self, dpid):
        """Best-effort rate for traffic leaving switch dpid over the trunk."""
        cap = int(LINK_CAPACITY_MBPS * 1000)
        if not self.be_protect:
            return cap
        trunk = "s%d->s%d" % (dpid, 3 - dpid)
        return max(MIN_BE_KBPS, int(cap - self.link_used(trunk) * 1000))

    def set_be_meter(self, dp, command):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        rate = self.be_rate_kbps(dp.id)
        band = parser.OFPMeterBandDrop(rate=rate, burst_size=burst_kb(rate))
        dp.send_msg(parser.OFPMeterMod(datapath=dp, command=command,
                                       flags=ofp.OFPMF_KBPS,
                                       meter_id=BE_METER_ID, bands=[band]))

    def update_best_effort(self):
        """Called after every change of the reservation table (dynamic SDN)."""
        for dp in self.datapaths.values():
            self.set_be_meter(dp, dp.ofproto.OFPMC_MODIFY)
        self.logger.info("Best-effort rate now: %s kbps",
                         {d: self.be_rate_kbps(d) for d in self.datapaths})

    def install_policy(self, r):
        """Meter + high-priority flow on every switch for this reservation."""
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            rate = int(r["bw"] * 1000)
            band = parser.OFPMeterBandDrop(rate=rate, burst_size=burst_kb(rate))
            dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD,
                                           flags=ofp.OFPMF_KBPS,
                                           meter_id=r["id"], bands=[band]))
            match = parser.OFPMatch(eth_type=0x0800,
                                    ipv4_src=r["src"], ipv4_dst=r["dst"])
            inst = [parser.OFPInstructionMeter(r["id"]),
                    parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS,
                        [parser.OFPActionOutput(ofp.OFPP_NORMAL)])]
            dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=100, match=match,
                                          instructions=inst, cookie=r["id"]))

    def remove_policy(self, rid):
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY, cookie=rid, cookie_mask=0xFFFFFFFF,
                match=parser.OFPMatch()))
            dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE,
                                           flags=ofp.OFPMF_KBPS, meter_id=rid))

    # ------------------------------------------------------------ resource state
    def link_used(self, link):
        """Reserved bandwidth on one directed link (sum over all reservations)."""
        return sum(r["bw"] for r in self.reservations.values() if link in r["path"])

    def link_usage(self):
        return {l: round(self.link_used(l), 2) for l in LINKS}

    def used_bandwidth(self):
        """Usage of the busiest link (the bottleneck)."""
        return max(self.link_used(l) for l in LINKS)

    def reserve(self, src, dst, bw, duration):
        # 1) validation
        try:
            ipaddress.IPv4Address(src)
            ipaddress.IPv4Address(dst)
            bw, duration = float(bw), int(duration)
        except ValueError:
            self.stats["invalid"] += 1
            return {"status": "ERROR", "reason": "invalid parameters"}
        if src not in HOSTS or dst not in HOSTS:
            self.stats["invalid"] += 1
            return {"status": "ERROR", "reason": "unknown host"}
        if src == dst or not (0 < bw <= LINK_CAPACITY_MBPS) \
                or not (1 <= duration <= MAX_DURATION_S):
            self.stats["invalid"] += 1
            return {"status": "ERROR", "reason": "parameters out of range"}

        path = path_links(src, dst)
        with self.lock:                      # ---- atomic check-and-commit ----
            # 2) conflict detection: same flow already reserved
            for r in self.reservations.values():
                if r["src"] == src and r["dst"] == dst:
                    self.stats["conflicts"] += 1
                    self.stats["rejected"] += 1
                    return {"status": "REJECTED", "reason": "conflict",
                            "conflicts_with": r["id"]}
            # 3) admission control: EVERY link on the path needs enough free bandwidth
            free = {l: LINK_CAPACITY_MBPS - self.link_used(l) for l in path}
            worst = min(path, key=lambda l: free[l])
            if bw > free[worst] + 1e-9:
                self.stats["rejected"] += 1
                return {"status": "REJECTED", "reason": "insufficient bandwidth",
                        "bottleneck": worst, "available_mbps": round(free[worst], 2)}
            # 4) commit, then push the SDN policy and retune the best-effort meter
            rid = next(self.next_id)
            r = {"id": rid, "src": src, "dst": dst, "bw": bw, "path": path,
                 "expires": time.time() + duration, "duration": duration}
            self.reservations[rid] = r
            self.install_policy(r)
            self.update_best_effort()
            self.stats["accepted"] += 1
            self.logger.info("ACCEPT #%d %s->%s %.1fMbps %ds via %s",
                             rid, src, dst, bw, duration, path)
            return {"status": "ACCEPTED", "id": rid, "bandwidth_mbps": bw,
                    "duration_s": duration, "path": path}

    def release(self, rid, expired=False):
        with self.lock:
            r = self.reservations.pop(rid, None)
            if r is None:
                return {"status": "ERROR", "reason": "unknown reservation id"}
            self.remove_policy(rid)
            self.update_best_effort()
            if expired:
                self.stats["expired"] += 1
            self.logger.info("%s #%d", "EXPIRED" if expired else "RELEASED", rid)
            return {"status": "RELEASED", "id": rid}

    def release_all(self):
        with self.lock:
            ids = list(self.reservations)
            for rid in ids:
                self.reservations.pop(rid)
                self.remove_policy(rid)
            self.update_best_effort()
            return {"status": "RELEASED", "count": len(ids)}

    def set_policy(self, on):
        with self.lock:
            self.be_protect = on
            self.update_best_effort()
            return {"status": "OK", "best_effort_protection": on}

    def status(self):
        with self.lock:
            used = self.used_bandwidth()
            return {"capacity_mbps": LINK_CAPACITY_MBPS,
                    "used_mbps": round(used, 2),
                    "utilization_pct": round(100 * used / LINK_CAPACITY_MBPS, 1),
                    "links": self.link_usage(),
                    "best_effort_protection": self.be_protect,
                    "best_effort_kbps": {str(d): self.be_rate_kbps(d) for d in self.datapaths},
                    "stats": dict(self.stats),
                    "active": [{"id": r["id"], "src": r["src"], "dst": r["dst"],
                                "bw": r["bw"], "path": r["path"],
                                "remaining_s": max(0, int(r["expires"] - time.time()))}
                               for r in self.reservations.values()]}

    # ------------------------------------------------------------ background
    def expiry_loop(self):
        while True:
            hub.sleep(1)
            now = time.time()
            expired = [rid for rid, r in list(self.reservations.items())
                       if r["expires"] <= now]
            for rid in expired:
                self.release(rid, expired=True)

    def utilization_loop(self):
        while True:
            hub.sleep(2)
            with self.lock:
                used = self.used_bandwidth()
                active = len(self.reservations)
            with open(UTIL_LOG, "a") as f:
                f.write("%.1f,%.2f,%.1f,%d\n" % (time.time() - self.start_time, used,
                                                 100 * used / LINK_CAPACITY_MBPS, active))

    # ------------------------------------------------------------ TCP protocol
    def handle_client(self, sock, addr):
        self.logger.info("Client connected: %s", addr)
        f = sock.makefile("rw")
        try:
            for line in f:
                parts = line.strip().split()
                if not parts:
                    continue
                cmd = parts[0].upper()
                if cmd == "RESERVE" and len(parts) == 5:
                    resp = self.reserve(*parts[1:])
                elif cmd == "RELEASE" and len(parts) == 2 and parts[1].isdigit():
                    resp = self.release(int(parts[1]))
                elif cmd == "RELEASEALL":
                    resp = self.release_all()
                elif cmd == "POLICY" and len(parts) == 2 and parts[1].upper() in ("ON", "OFF"):
                    resp = self.set_policy(parts[1].upper() == "ON")
                elif cmd == "STATUS":
                    resp = self.status()
                elif cmd == "QUIT":
                    break
                else:
                    resp = {"status": "ERROR", "reason": "bad command"}
                f.write(json.dumps(resp) + "\n")
                f.flush()
        except Exception as e:  # keep server alive on client errors
            self.logger.warning("client %s error: %s", addr, e)
        finally:
            try:
                f.close()
                sock.close()
            except Exception:
                pass

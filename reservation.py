"""
Network Resource Reservation System
Ryu SDN controller + embedded TCP reservation server.
 
Run:  ryu-manager reservation_controller.py
TCP protocol (one text line per request, JSON reply):
    RESERVE <src_ip> <dst_ip> <bandwidth_mbps> <duration_s>
    RELEASE <reservation_id>
    STATUS
"""
import ipaddress
import itertools
import json
import time
 
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3
 
LINK_CAPACITY_MBPS = 10.0      # bottleneck link capacity (must match topology)
MAX_DURATION_S = 3600
TCP_HOST, TCP_PORT = "0.0.0.0", 9000
UTIL_LOG = "utilization.csv"
 
 
class ReservationController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
 
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.reservations = {}            # id -> dict
        self.next_id = itertools.count(1)
        self.lock = hub.Semaphore(1)      # protects shared state
        self.stats = {"accepted": 0, "rejected": 0, "conflicts": 0,
                      "invalid": 0, "expired": 0}
        self.start_time = time.time()
 
        # TCP server (green thread per client => concurrent requests)
        self.server = hub.StreamServer((TCP_HOST, TCP_PORT), self.handle_client)
        hub.spawn(self.server.serve_forever)   #Starts TCP server in the background
        hub.spawn(self.expiry_loop)   #TCP server that checks for exiration loop
        hub.spawn(self.utilization_loop)    #TCP server that checks bandwidth usage
        with open(UTIL_LOG, "w") as f:
            f.write("time_s,used_mbps,utilization_pct,active\n")
        self.logger.info("Reservation server listening on %s:%d", TCP_HOST, TCP_PORT)
 
    # ------------------------------------------------------------------ OpenFlow
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath    #Gets connected switch object from event
        ofp, parser = dp.ofproto, dp.ofproto_parser    # gets open flow constants and open flow messages
        self.datapaths[dp.id] = dp     #save switch in datapath dict
        # default (best-effort) behaviour: act like a normal L2 switch
        actions = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]   # Creates an action telling the switch to handle packets using its normal switching behavior.
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]  # Packages that action into an OpenFlow instruction.
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0,
                                      match=parser.OFPMatch(), instructions=inst))  #Sends a flow rule to the switch telling it to use that normal behavior by default.
        self.logger.info("Switch %s connected", dp.id)  #Logs that the switch connected.
 
    def install_policy(self, r):
        """Meter + high-priority flow on every switch for this reservation."""
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            band = parser.OFPMeterBandDrop(rate=int(r["bw"] * 1000), burst_size=0)   #We multily rate by 1000 because r->Mbps but to convert to kbps multiply by 1000
            dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD,
                                           flags=ofp.OFPMF_KBPS,
                                           meter_id=r["id"], bands=[band])) # Sends the meter configuration to the switch.
            match = parser.OFPMatch(eth_type=0x0800,
                                    ipv4_src=r["src"], ipv4_dst=r["dst"])  # Creates a rule matching IPv4 packets from the reservation's source to destination.
            inst = [parser.OFPInstructionMeter(r["id"]),  #Says: apply this reservation's bandwidth meter.
                    parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS,
                        [parser.OFPActionOutput(ofp.OFPP_NORMAL)])]  # After applying the meter, let the switch handle the packet normally.
            dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=100, match=match,
                                          instructions=inst, cookie=r["id"]))  #Installs this flow rule on the switch with high priority.
 
    def remove_policy(self, rid):
        for dp in self.datapaths.values():
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY, cookie=rid, cookie_mask=0xFFFFFFFF,
                match=parser.OFPMatch()))   # Deletes the flow rule whose cookie matches this reservation ID.
            dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE,
                                           flags=ofp.OFPMF_KBPS, meter_id=rid)) #Deletes the bandwidth meter belonging to that reservation.
 
    # ------------------------------------------------------------ resource state
    def used_bandwidth(self):
        return sum(r["bw"] for r in self.reservations.values())
 
    def reserve(self, src, dst, bw, duration):
        # 1) validation
        try:
            ipaddress.IPv4Address(src)  #valid source IP Address
            ipaddress.IPv4Address(dst)  #vallid destination IP Address
            bw, duration = float(bw), int(duration)
        except ValueError:
            self.stats["invalid"] += 1  
            return {"status": "ERROR", "reason": "invalid parameters"}
        if src == dst or bw <= 0 or bw > LINK_CAPACITY_MBPS \
                or not (1 <= duration <= MAX_DURATION_S):
            self.stats["invalid"] += 1
            return {"status": "ERROR", "reason": "parameters out of range"}
 
        with self.lock:
            # 2) conflict detection: same flow already reserved
            for r in self.reservations.values():
                if r["src"] == src and r["dst"] == dst:
                    self.stats["conflicts"] += 1
                    self.stats["rejected"] += 1
                    return {"status": "REJECTED", "reason": "conflict",
                            "conflicts_with": r["id"]}
            # 3) admission control: capacity check
            free = LINK_CAPACITY_MBPS - self.used_bandwidth()
            if bw > free + 1e-9:   #1e-9 means 0.000000001, added to avoid tiny floating-point calculation errors.
                self.stats["rejected"] += 1
                return {"status": "REJECTED", "reason": "insufficient bandwidth",
                        "available_mbps": round(free, 2)} #free round to 2 decimal places
            # 4) accept + install SDN policy
            rid = next(self.next_id)
            r = {"id": rid, "src": src, "dst": dst, "bw": bw,
                 "expires": time.time() + duration, "duration": duration}
            self.reservations[rid] = r
            self.install_policy(r)
            self.stats["accepted"] += 1
            self.logger.info("ACCEPT #%d %s->%s %.1fMbps %ds", rid, src, dst, bw, duration)
            return {"status": "ACCEPTED", "id": rid, "bandwidth_mbps": bw,
                    "duration_s": duration}
 
    def release(self, rid, expired=False):
        with self.lock:
            r = self.reservations.pop(rid, None)
            if r is None:
                return {"status": "ERROR", "reason": "unknown reservation id"}
            self.remove_policy(rid)
            if expired:
                self.stats["expired"] += 1
            self.logger.info("%s #%d", "EXPIRED" if expired else "RELEASED", rid)
            return {"status": "RELEASED", "id": rid}
 
    def status(self):
        with self.lock:
            used = self.used_bandwidth()
            return {"capacity_mbps": LINK_CAPACITY_MBPS,
                    "used_mbps": round(used, 2),
                    "utilization_pct": round(100 * used / LINK_CAPACITY_MBPS, 1),
                    "stats": dict(self.stats),
                    "active": [{"id": r["id"], "src": r["src"], "dst": r["dst"],
                                "bw": r["bw"],
                                "remaining_s": max(0, int(r["expires"] - time.time()))}
                               for r in self.reservations.values()]}
 
    # ------------------------------------------------------------ background
    def expiry_loop(self):
        while True:
            hub.sleep(1)
            now = time.time()
            """expired = [rid for rid, r in list(self.reservations.items())
                       if r["expires"] <= now]
            for rid in expired:
                self.release(rid, expired=True)"""
            for rid, r in list(self.reservations.items()):
                if r["expires"] <= now:
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
                elif cmd == "STATUS":
                    resp = self.status()
                elif cmd == "QUIT":
                    break
                else:
                    resp = {"status": "ERROR", "reason": "bad command"}
                f.write(json.dumps(resp) + "\n")  #stores response string in buffer
                f.flush()  #Displays the string
        except Exception as e:  # keep server alive on client errors
            self.logger.warning("client %s error: %s", addr, e)
        finally:
            try:
                f.close()
                sock.close()
            except Exception:
                pass
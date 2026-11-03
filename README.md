# Network Resource Reservation System

A TCP-based bandwidth reservation service integrated with an SDN controller. Clients ask a
reservation server for a share of network bandwidth for a fixed time. The server validates the
request, checks that **every link on the path** has enough free capacity, and, if it does,
installs the corresponding OpenFlow policy through the **Ryu** controller on **Open vSwitch**
switches emulated in **Mininet**. When the reservation expires, the policy is removed and the
bandwidth becomes available again.

Built for the Computer Networks "SDN + Socket Programming" mini project (Project 15).

---

## 1. Objectives

| Objective (from the project brief) | How it is met |
|---|---|
| TCP-based reservation service | Text-based request/response protocol over TCP port 9000 (`reservation.py`, `client.py`) |
| Handle concurrent requests | One green thread per client connection; shared state protected by a lock |
| Maintain reservation information | Reservation table (id, flow, bandwidth, path, expiry) and a per-link capacity map |
| Detect reservation conflicts | Duplicate flows and over-subscribed links are rejected, naming the bottleneck link |
| Install corresponding SDN policies | OpenFlow 1.3 meters and flow rules pushed to every switch after a successful commit |
| Implement reservation expiry | Background loop removes flows and meters when the duration ends |
| Track resource utilization | Per-link usage, utilization CSV log and a live web dashboard |
| Evaluate accepted and rejected requests | Seven scripted scenarios plus a custom scenario builder, with CSV output |

---

## 2. Features

- **TCP reservation server** embedded in the Ryu controller; many clients at once.
- **Atomic admission control**: the check-and-reserve step runs under one lock, so two clients can
  never both obtain the same free bandwidth.
- **Per-link capacity model**: every directed link has a capacity. A reservation consumes bandwidth
  on each link of its path, so shared links (a server's uplink, a client's downlink, the trunk)
  produce realistic contention.
- **Rate enforcement**: each accepted reservation gets an OpenFlow meter plus a high-priority flow.
- **Dynamic best-effort protection**: a shared meter limits all *unreserved* traffic to the
  capacity that is not reserved, and is re-tuned whenever a reservation is accepted, released or
  expires. Reserved flows therefore keep their bandwidth even under congestion.
- **Expiry and release**: reservations expire automatically or can be released manually.
- **Web portal** (`app.py`): reservation form, live per-link utilization, active reservations with
  countdown, accepted/rejected counters, iPerf3 throughput test, event log and test scenarios.
- **Custom scenario builder**: choose any servers, clients, bandwidth and duration per flow, send
  requests at the same instant or one after another, and optionally measure real throughput.

---

## 3. Architecture (summary)

A client sends a `RESERVE` request over TCP to the reservation server, which runs inside the Ryu
controller. The resource manager checks the request and every link on its path under a lock; if
the bandwidth is available, the policy manager installs an OpenFlow meter and flow rule on the
Open vSwitch switches. An expiry loop removes the policy when the reservation ends.

```
 Clients (portal / CLI)  --TCP-->  Reservation server --> Resource manager --> Policy manager
                                          (inside Ryu controller)                  |
                                                                            OpenFlow 1.3
                                                                                   v
                                                        h1,h2 -- s1 ====== s2 -- h3,h4   (Mininet)
```

**Full design (component diagram, topology, sequence diagram, protocol, admission control,
SDN policy design): see [ARCHITECTURE.md](ARCHITECTURE.md).**

---

## 4. Repository contents

```
README.md         Project overview, setup and usage
ARCHITECTURE.md   Architecture and design
CONTRIBUTIONS.md  Contribution of each team member
reservation.py    Ryu controller + TCP reservation server (resource and policy managers)
topology.py       Mininet topology (4 hosts, 2 switches, 10 Mbps links)
app.py            Web portal, scenario runner, iPerf3 integration (standard library only)
client.py         Command-line TCP client
scenarios.py      Runs the scenarios from the terminal and saves results/*.csv
results/          CSV output of the scenarios
screenshots/      Screenshots of the working system
```

---

## 5. Setup

Tested on Ubuntu 20.04 (a VM or WSL2). Ryu needs an older Python, so it runs in a Python 3.9
virtual environment; Mininet runs with the system Python.

```bash
# system packages
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf3 curl

# Python 3.9 environment for Ryu (using uv)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
uv venv --python 3.9 ~/ryu-env
source ~/ryu-env/bin/activate
uv pip install "setuptools==58.2.0" wheel eventlet==0.30.2 "dnspython<2" oslo.config
uv pip install ryu --no-build-isolation
sed -i 's/from oslo.config import cfg/from oslo_config import cfg/' ~/ryu-env/bin/ryu-manager
```

On WSL2, start Open vSwitch first with `sudo service openvswitch-switch start`.

---

## 6. Running the system

Use three terminals, in this order.

```bash
# Terminal 1: controller and reservation server (venv active)
source ~/ryu-env/bin/activate
python -m ryu.cmd.manager reservation.py

# Terminal 2: virtual network
sudo mn -c
sudo python3 topology.py

# Terminal 3: web portal (system Python, not the venv)
sudo python3 app.py
```

Open **http://localhost:8080**. The three status pills (Ryu, Reservation server, Mininet) should all
read "connected". `sudo` is needed only for the iPerf3 features, which run `iperf3` inside the
Mininet hosts.

The command-line client works from a normal shell (not from inside a Mininet host):

```bash
python3 client.py RESERVE 10.0.0.1 10.0.0.3 4 60   # src dst Mbps seconds
python3 client.py STATUS
python3 client.py RELEASE 1
python3 client.py LOADTEST 30                       # 30 concurrent random requests
```

Inspect the installed policies on a switch:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
sudo ovs-ofctl -O OpenFlow13 dump-meters s1
```

---

## 7. Test scenarios and evaluation

All scenarios are buttons in the portal. They can also be run from the terminal, which saves a CSV
into `results/`:

```bash
python3 scenarios.py race          # two 6 Mbps requests at the same instant
python3 scenarios.py concurrency   # 5, 10, 20, 40 simultaneous clients
python3 scenarios.py fanout        # one server to many clients
python3 scenarios.py fanin         # many servers to one client
python3 scenarios.py abuse         # invalid, malformed and overbooking requests
sudo python3 scenarios.py congestion
sudo python3 scenarios.py expiry
sudo python3 scenarios.py all
```

| Scenario | What it shows |
|---|---|
| Race | Exactly one of two simultaneous requests is accepted in every round |
| Concurrency | Accepted / rejected counts and latency with 5 to 40 clients; no link exceeds capacity |
| Fan-out and fan-in | Shared uplink or downlink limits admission; releasing a flow lets a rejected request succeed |
| Abuse | Invalid input is rejected, overbooking is refused, and the server survives binary garbage |
| Congestion | A 4 Mbps reserved flow competing with a 10 Mbps unreserved UDP flood |
| Expiry | Throughput while reserved versus after the reservation expires |

The **custom scenario builder** in the portal lets you pick any servers and clients, set the
bandwidth and duration per flow, choose simultaneous or sequential requests, and optionally measure
the real throughput of the accepted flows with iPerf3.

### Congestion result (reserved flow h1 to h3, 4 Mbps, versus a 10 Mbps UDP flood h2 to h4)

| Condition | Measured throughput of the h1 to h3 flow |
|---|---|
| A. Baseline: no reservation | 0.73 Mbps |
| B. Static: reservation, no best-effort limit | 0.73 Mbps |
| C. Proposed: reservation + dynamic best-effort limit | 4.05 Mbps |

A reservation by itself only caps a flow; it does not protect it. The dynamic best-effort meter is
what turns the reservation into a guarantee.

Measured throughput is slightly different from the reserved rate because meters count whole
packets (headers included) and allow a small burst.

---

## 8. Limitations

- The topology and the IP-to-switch mapping are fixed (four hosts, two switches).
- Reservations are one-way (source to destination); return traffic is not reserved.
- Best-effort protection covers the trunk only. Host links are admission-controlled, but are not
  shielded from unreserved traffic.
- The best-effort meter also applies to unreserved traffic between hosts on the same switch.
- Meter enforcement needs an Open vSwitch datapath with meter support.
- There is no authentication of clients.

---

## 9. Technologies

Python 3, Ryu (OpenFlow 1.3), Open vSwitch, Mininet, iPerf3, TCP sockets, and a dependency-free web
portal built on Python's `http.server`.

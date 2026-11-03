# Team Contributions

Project: Network Resource Reservation System (Computer Networks, SDN + Socket Programming)

| Member | Name | Roll number | GitHub username |
|---|---|---|---|
| Member 1 | Haripranesh S | PES1UG25CS197 | Haripranesh16 |
| Member 2 | Johlee Efroshini M | PES1UG25CS231 | Johlee-Efroshini-M |
| Member 3 | Jai Akash Mahendran | PES1UG25CS218 | akasshh1 |

---

## Member 1: TCP communication and system architecture

**Code**
- TCP reservation server in `reservation.py`: connection handling, one green thread per client,
  command parsing (`RESERVE`, `RELEASE`, `RELEASEALL`, `POLICY`, `STATUS`, `QUIT`) and JSON replies.
- Error handling for malformed input, so a bad client cannot crash the server.
- Command-line TCP client `client.py`, including the concurrent `LOADTEST` mode.

**Design and documentation**
- Overall system architecture and module separation (client, reservation server, resource manager,
  policy manager) and the application-level message format.
- `README.md` and `ARCHITECTURE.md`; installation and run instructions.
- Fresh-clone test of the whole setup to confirm the results can be reproduced.

**Presentation:** TCP, client-server communication and the system architecture.

---

## Member 2: Resource allocation, conflict detection and concurrency

**Code**
- Resource manager in `reservation.py`: reservation table, per-link capacity map and path
  computation.
- Atomic admission control: lock-protected check-and-reserve across every link of the path,
  conflict detection for duplicate flows, and bottleneck reporting for rejections.
- Web portal in `app.py`: reservation form, per-link utilization view, active reservations table
  with expiry countdown, accepted and rejected counters.
- Scenarios: race test, many concurrent clients, one server to many clients, many servers to one
  client, invalid and abusive requests, and the custom scenario builder.

**Evaluation**
- Ran the concurrency, fan-out, fan-in and abuse scenarios; saved the CSV results and produced the
  accepted versus rejected graphs.

**Presentation:** bandwidth allocation, conflict handling, concurrency and the live demonstration.

---

## Member 3: SDN policies, expiry and monitoring

**Code**
- Policy manager in `reservation.py`: OpenFlow 1.3 meters and flow rules installed through Ryu,
  and their removal when a reservation ends.
- Dynamic best-effort meter that is re-tuned whenever a reservation is accepted, released or
  expires, and the `POLICY ON|OFF` switch used as the baseline for comparison.
- Reservation expiry loop and utilization monitoring (`utilization.csv`, `STATUS`).
- Mininet topology in `topology.py` (switches, hosts, 10 Mbps links).
- iPerf3 integration in `app.py` and the congestion and expiry scenarios.

**Evaluation**
- Ran the congestion and expiry experiments several times, averaged the results and produced the
  baseline versus proposed comparison.
- Collected the switch evidence (`ovs-ofctl dump-flows` and `dump-meters`, Ryu logs).

**Presentation:** SDN concepts, network policies, expiry and monitoring.

---

## Shared work

- Testing the full system end to end in Mininet.
- Repository set-up and keeping the GitHub repository up to date.

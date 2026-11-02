"""
Command-line runner for the test scenarios (same code as the web portal's buttons).
Results are printed and saved as CSV files in ./results/ for your report.

Prerequisites: ryu-manager reservation.py  and  sudo python3 topology.py running.

    python3      scenarios.py race          # 2 clients, same instant, 6 Mbps each
    python3      scenarios.py concurrency   # 5/10/20/40 simultaneous clients
    python3      scenarios.py fanout        # one server -> many clients
    python3      scenarios.py fanin         # many servers -> one client
    python3      scenarios.py abuse         # invalid / abusive requests
    sudo python3 scenarios.py congestion    # reserved flow vs. UDP flood
    sudo python3 scenarios.py expiry        # resources freed after expiry
    sudo python3 scenarios.py all

fanout / fanin also measure real throughput with iPerf3 when run with sudo.
"""
import csv
import os
import sys

import app          # reuses SCENARIOS from the web portal (does not start the server)


def show(header, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    line = lambda r: "  ".join(str(x).ljust(w) for x, w in zip(r, widths))
    print(line(header))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))


def run(name):
    fn, needs_mininet = app.SCENARIOS[name]
    if needs_mininet and app.mininet_error():
        sys.exit(app.mininet_error())
    print("\n=== %s ===" % name)
    res = fn()
    show(res["header"], res["rows"])
    if res.get("note"):
        print("\n" + res["note"])
    os.makedirs("results", exist_ok=True)
    path = os.path.join("results", name + ".csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(res["header"])
        w.writerows(res["rows"])
    print("-> saved", path)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "all":
        for n in app.SCENARIOS:
            run(n)
    elif what in app.SCENARIOS:
        run(what)
    else:
        print(__doc__)

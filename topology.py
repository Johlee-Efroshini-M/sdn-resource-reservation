"""
Run (after starting ryu-manager):  sudo python3 topology.py

      h1 --10M--+                  +--10M-- h3
                 s1 ====10M==== s2
      h2 --10M--+                  +--10M-- h4

Every link is 10 Mbps, matching LINK_CAPACITY_MBPS in reservation.py.
Shared links make the interesting cases:
  * h1 -> h2,h3,h4  (one server, many clients)  all share h1's uplink
  * h2,h3,h4 -> h1  (many servers, one client)  all share h1's downlink
  * h1,h2 -> h3,h4                              share the s1-s2 trunk
"""
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

LINK_MBPS = 10


def run():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    s2 = net.addSwitch("s2", protocols="OpenFlow13")
    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")
    h4 = net.addHost("h4", ip="10.0.0.4/24")

    for h, s in ((h1, s1), (h2, s1), (h3, s2), (h4, s2)):
        net.addLink(h, s, bw=LINK_MBPS)
    net.addLink(s1, s2, bw=LINK_MBPS)          # trunk

    net.start()
    print("Reservation server (in the Ryu controller) is on 127.0.0.1:9000")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()

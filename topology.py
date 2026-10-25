"""
Run (after starting ryu-manager):  sudo python3 topology.py
 
h1,h2 -- s1 ==(10 Mbps bottleneck)== s2 -- h3,h4
"""
from mininet.net import Mininet   #Virtual network
from mininet.node import RemoteController, OVSSwitch   #Connect switch to external controller like ryu
from mininet.link import TCLink   #Link properties such as bandwidth
from mininet.cli import CLI   #Command Line Interface
from mininet.log import setLogLevel   #Mininet logging display
 
 
def run():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True) #Creates an empty Mininet network using OVS switches and TCLink, with automatic MAC addresses.
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653) #Adds Ryu as the controller, reachable at 127.0.0.1:6653
 
    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    s2 = net.addSwitch("s2", protocols="OpenFlow13")
    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")
    h4 = net.addHost("h4", ip="10.0.0.4/24")
 
    net.addLink(h1, s1)
    net.addLink(h2, s1)
    net.addLink(h3, s2)
    net.addLink(h4, s2)
    net.addLink(s1, s2, bw=10)          # bottleneck = LINK_CAPACITY_MBPS
 
    net.start()  #Starts the virtual network
    print("Controller TCP reservation server is on 127.0.0.1:9000")
    CLI(net)  #Opens Mininet's interactive command line so you can run network commands.
    net.stop()  #Stops and cleans up the virtual network when you exit the CLI.
 
 
if __name__ == "__main__": #Ensures run() is executed only when you directly run topology.py.
    setLogLevel("info")
    run()
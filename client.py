"""
TCP reservation client.
 
  python3 client.py RESERVE 10.0.0.1 10.0.0.3 4 30     # one request
  python3 client.py RELEASE 1
  python3 client.py STATUS
  python3 client.py LOADTEST 30                         # 30 concurrent random requests
"""
import json
import random #Used to generate random requests during the load test.
import socket #Lets the client communicate with the reservation server using TCP.
import sys #Lets us read arguments given when running the program.
import threading #Lets us create multiple threads for concurrent requests.
 
SERVER = ("127.0.0.1", 9000)
HOSTS = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]
 
 
def send(line):
    with socket.create_connection(SERVER, timeout=5) as s: #Opens a TCP connection to 127.0.0.1:9000, with a 5-second timeout.
        s.sendall((line + "\n").encode()) #Converts the command to bytes and sends it to the server.
        return json.loads(s.makefile().readline()) #Reads the server's JSON response, converts it back into a Python dictionary, and returns it.
 
 
def loadtest(n):
    results, lock = [], threading.Lock()
 
    def worker():
        src, dst = random.sample(HOSTS, 2)
        bw = random.choice([1, 2, 3, 4, 5])
        dur = random.choice([5, 10, 15])
        r = send(f"RESERVE {src} {dst} {bw} {dur}")
        with lock:
            results.append((src, dst, bw, r))
            print(f"{src}->{dst} {bw}Mbps => {r['status']} {r.get('reason', '')}")
 
    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()      #Starts all the threads, so they can send requests concurrently
    for t in threads:
        t.join()       #wait until every thread finishes
 
    acc = sum(1 for *_, r in results if r["status"] == "ACCEPTED")  #(*_) means multiple args and last element of each tuple is r
    print(f"\nTotal={n}  Accepted={acc}  Rejected={n - acc}  "
          f"Acceptance ratio={100 * acc / n:.1f}%")
    print(json.dumps(send("STATUS"), indent=2)) #Sends STATUS to the server and prints the complete current system status in a nicely formatted JSON.
 
 
if __name__ == "__main__":
    if len(sys.argv) < 2: 
        print(__doc__)  #Print the doc strint at the top
    elif sys.argv[1].upper() == "LOADTEST":
        loadtest(int(sys.argv[2]) if len(sys.argv) > 2 else 20)  
    else:
        print(json.dumps(send(" ".join(sys.argv[1:])), indent=2)) #For normal commands, joins the command-line arguments and sends them to the server.
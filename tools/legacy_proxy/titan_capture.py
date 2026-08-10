#!/usr/bin/env python3
"""TitanStack login-frame capture listener.

Listens on 0.0.0.0:22110 and hex-dumps whatever the game client sends as its
first frames (the type-121 Login message: [Header][RC4(username,password,json)]).
We do NOT know the RC4 keys / Header layout yet — this captures the raw bytes so
we can reverse them empirically. Keeps the socket open and logs any retries.

Run in a terminal (survives across game interactions):
    python3 titan_capture.py
"""
import socket, threading, time, os, sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 22110
LOG = os.path.join(os.path.dirname(__file__), "titan_capture.log")

def logline(s):
    line = time.strftime("%H:%M:%S ") + s
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def hexdump(b: bytes) -> str:
    out = []
    for i in range(0, len(b), 16):
        chunk = b[i:i+16]
        hexs = " ".join(f"{x:02x}" for x in chunk)
        asci = "".join(chr(x) if 32 <= x < 127 else "." for x in chunk)
        out.append(f"    {i:04x}  {hexs:<47}  {asci}")
    return "\n".join(out)

def handle(conn, addr):
    logline(f"[+] CONNECT from {addr}")
    conn.settimeout(30)
    total = 0
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                logline(f"[-] {addr} closed by peer (total {total} bytes)")
                break
            total += len(data)
            logline(f"[<] {len(data)} bytes from {addr}:\n{hexdump(data)}")
    except socket.timeout:
        logline(f"[t] {addr} idle timeout (total {total} bytes)")
    except Exception as e:
        logline(f"[!] {addr} error: {e}")
    finally:
        conn.close()

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(8)
    logline(f"[*] TitanStack capture listening on 0.0.0.0:{PORT}")
    while True:
        conn, addr = s.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()

if __name__ == "__main__":
    main()

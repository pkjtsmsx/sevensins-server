"""A fake game client for driving titan_server.handle() over a socketpair, in tests.

Underscore-named on purpose: tools/run_tests.py runs every `test_*.py`, and this is a
helper, not a suite. Import it AFTER setting SEVENSINS_ACCOUNTS -- it imports
titan_server, which imports player_state, which fixes STATE_DIR at import time.

    c = Client(tag)
    c.login("7003")                        # a real LOGIN frame, RC4 on both directions
    n = c.rpc(INDEX, CMD, [ints], [strs])  # a real Rpc frame; -> frames received so far
    c.wait_frames(n + 2)                   # block until the server has answered
    c.close()                              # EOF, so handle() sees "closed by peer"

Frames are parsed only far enough to count them and record their message types
(`c.got`); a test that needs a reply's contents can extend `_drain`.
"""
import socket
import sys
import threading
import time

import titan_server as ts
from wire import (RC4, KEY_C2S, KEY_S2C, MSG_LOGIN, MSG_RPC, HDR_LEN, make_header,
                  make_rpc, rpc_pack, pb_field_bytes)


def login_frame(pid, cipher):
    body = pb_field_bytes(11, pb_field_bytes(1, f"titan_token_{pid}".encode())
                          + pb_field_bytes(3, b"{}"))
    return cipher.crypt(make_header(MSG_LOGIN, len(body)) + body)


class Client:
    def __init__(self, tag):
        self.ours, theirs = socket.socketpair()
        self.c2s = RC4(KEY_C2S)
        self.got = []
        self.thread = threading.Thread(target=ts.handle, args=(theirs, ("test", tag)),
                                       daemon=True)
        self.thread.start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        s2c = RC4(KEY_S2C)
        try:
            while True:
                hdr = self.ours.recv(HDR_LEN)
                if not hdr:
                    return
                hdr = s2c.crypt(hdr)
                size = int.from_bytes(hdr[2:6], "big")
                body = b""
                while len(body) < size:
                    chunk = self.ours.recv(size - len(body))
                    if not chunk:
                        return
                    body += chunk
                s2c.crypt(body)
                self.got.append(hdr[1])
        except OSError:
            return

    def wait_frames(self, n, timeout=5.0):
        """-> True once at least n frames have arrived."""
        deadline = time.time() + timeout
        while len(self.got) < n and time.time() < deadline:
            time.sleep(0.02)
        return len(self.got) >= n

    def login(self, pid):
        self.ours.sendall(login_frame(pid, self.c2s))
        deadline = time.time() + 5
        while MSG_LOGIN not in self.got and time.time() < deadline:
            time.sleep(0.02)
        return MSG_LOGIN in self.got

    def rpc(self, index, cmd, intargs=(), strargs=(), rid=1):
        """Send one client->server Rpc. -> the frame count BEFORE sending, to wait on."""
        before = len(self.got)
        body = rpc_pack(make_rpc(index, uint32s=[[cmd], list(intargs)],
                                 uint64s=[[rid]], strings=[list(strargs)]))
        self.ours.sendall(self.c2s.crypt(make_header(MSG_RPC, len(body)) + body))
        return before

    def close(self):
        # A half-close, so the server sees EOF ("closed by peer") the way a client
        # exiting does, rather than a reset it logs as a crash.
        try:
            self.ours.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.ours.close()
        self.thread.join(5)
        return not self.thread.is_alive()

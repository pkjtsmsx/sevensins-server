#!/usr/bin/env python3
"""TitanStack wire protocol: framing, RC4, protobuf, and RPC packing.

The pure transport layer, with NO game state and no I/O -- extracted from
titan_server.py so the socket loop and the ~1200-line command dispatch read as
application code rather than being buried under byte-twiddling. Everything here is a
plain function of its inputs and is unit-testable in isolation.

Wire format, recovered from libil2cpp.so (see docs/GAME_SERVER.md):

  frame = RC4_stream( [chksum][type][size:u32 big-endian] + protobuf_body )

  * ONE RC4 keystream per direction, per connection, covering header+body of every
    frame -- not re-keyed per message.
  * header is 6 bytes: TitanStack.Header cctor sets ChkSumLen=1, MsgTypeLen=1,
    MsgSizeLen=4, so HeaderLen=6.
  * chksum = ChkSumKey ^ ((MsgTypeMul * type + size) % ChkSumMod)
           = 0x5C ^ ((3 * type + size) % 30)

Body is a protobuf-net `titan.Client`:
  1 = cmd (uint)   11 = login   12 = login_reply   13 = queue_reply   31 = rpc_pack
  Login       : 1 = username (string), 2 = password (bytes), 3 = json_data (string)
  LoginReply  : 1 = success (bool),    2 = errno (uint)
"""

__all__ = [
    "KEY_C2S", "KEY_S2C", "MSG_LOGIN", "MSG_QUEUE", "MSG_RPC",
    "CHKSUM_KEY", "CHKSUM_MOD", "MSGTYPE_MUL", "HDR_LEN",
    "RC4", "chksum", "make_header",
    "pb_parse", "pb_varint", "pb_enc_varint", "pb_field_varint", "pb_field_bytes",
    "pb_zigzag_decode", "pb_field_zigzag",
    "parse_rpc", "make_rpc", "rpc_pack", "login_reply",
]

KEY_C2S = bytes.fromhex("de31b5197af8042c")   # NetCore.KeyC2S
KEY_S2C = bytes.fromhex("5ab862fc15097d3e")   # NetCore.KeyS2C

MSG_LOGIN, MSG_QUEUE, MSG_RPC = 121, 122, 131
CHKSUM_KEY, CHKSUM_MOD, MSGTYPE_MUL, HDR_LEN = 0x5C, 30, 3, 6


class RC4:
    """Stateful keystream -- one instance per direction per connection."""

    def __init__(self, key):
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) & 0xFF
            S[i], S[j] = S[j], S[i]
        self.S, self.i, self.j = S, 0, 0

    def crypt(self, data):
        S, i, j = self.S, self.i, self.j
        out = bytearray()
        for c in data:
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
            out.append(c ^ S[(S[i] + S[j]) & 0xFF])
        self.i, self.j = i, j
        return bytes(out)


def chksum(mtype, size):
    return CHKSUM_KEY ^ ((MSGTYPE_MUL * mtype + size) % CHKSUM_MOD)


def make_header(mtype, size):
    return bytes([chksum(mtype, size), mtype]) + size.to_bytes(4, "big")


# ---- minimal protobuf ----------------------------------------------------

def pb_parse(buf):
    """-> {field_no: [value,...]}, values are int (varint) or bytes (len-delim)."""
    out, p = {}, 0
    while p < len(buf):
        key, p = pb_varint(buf, p)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            v, p = pb_varint(buf, p)
        elif wt == 2:
            n, p = pb_varint(buf, p)
            v, p = buf[p:p + n], p + n
        elif wt == 5:
            v, p = buf[p:p + 4], p + 4
        elif wt == 1:
            v, p = buf[p:p + 8], p + 8
        else:
            raise ValueError(f"bad wire type {wt}")
        out.setdefault(fno, []).append(v)
    return out


def pb_varint(buf, p):
    r = s = 0
    while True:
        b = buf[p]
        p += 1
        r |= (b & 0x7F) << s
        if not b & 0x80:
            return r, p
        s += 7


def pb_enc_varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def pb_field_varint(fno, v):
    return pb_enc_varint(fno << 3) + pb_enc_varint(v)


def pb_field_bytes(fno, b):
    return pb_enc_varint((fno << 3) | 2) + pb_enc_varint(len(b)) + b


def login_reply(success=True, errno=0):
    """Client{ login_reply = LoginReply{ success, errno } }"""
    inner = pb_field_varint(1, 1 if success else 0) + pb_field_varint(2, errno)
    return pb_field_bytes(12, inner)


# ---- RPC ----------------------------------------------------------------
# ClientRpc.lookup dispatches on Rpc.index, a per-subsystem constant (full list in
# re/rpc_client_cmds.txt). RpcUtil.push_uint32 wraps EACH value in its own
# Rpc.UInt32 message holding a 1-element `args` list, so pop_uint32(r, i) reads
# r.uint32[i].args[0].

def pb_zigzag_decode(v):
    return (v >> 1) ^ -(v & 1)


def parse_rpc(buf):
    """-> (index, cmd, id, intargs, strargs, strargs2) for a client->server Rpc.

    Most subsystems use only one string group, but PlayerJSAgent pushes two --
    group 0 is the JS module name, group 1 the actual string arguments -- so the
    second group has to be surfaced or the JS-RPC payload is invisible.
    """
    f = pb_parse(buf)
    index = pb_zigzag_decode(f[1][0]) & 0xFFFFFFFF
    groups = [pb_parse(w).get(1, []) for w in f.get(11, [])]
    cmd = groups[0][0] if groups and groups[0] else None
    intargs = groups[1] if len(groups) > 1 else []
    if not intargs:
        # Subsystems built on shape B (PlayerBattle, PlayerCurrency, ...) put their
        # args in the SIGNED slot instead, so a battle request's skill id arrives as
        # zigzag sint32 and looks like "no args" if only uint32 is read.
        sints = [pb_parse(w).get(1, []) for w in f.get(15, [])]
        intargs = [pb_zigzag_decode(v) for v in (sints[0] if sints else [])]
    u64 = [pb_parse(w).get(1, []) for w in f.get(12, [])]
    rid = u64[0][0] if u64 and u64[0] else 0
    strs = [pb_parse(w).get(1, []) for w in f.get(14, [])]
    strargs = [s.decode("utf-8", "replace") for s in (strs[0] if strs else [])]
    strargs2 = [s.decode("utf-8", "replace") for s in (strs[1] if len(strs) > 1 else [])]
    return index, cmd, rid, intargs, strargs, strargs2


def pb_field_zigzag(fno, v):
    """protobuf-net sint32 (DataFormat.ZigZag).

    Rpc.index is ZigZag, not two's complement -- sending the plain varint made the
    client report `unknown client rpc (-1213645727)`, which is exactly
    zigzag_decode(0x90AD873D read as unsigned).
    """
    v = v if v < 0x80000000 else v - 0x100000000     # to signed int32
    zz = ((v << 1) ^ (v >> 31)) & 0xFFFFFFFF
    return pb_enc_varint(fno << 3) + pb_enc_varint(zz)


def make_rpc(index, uint32s=(), uint64s=(), sint32s=(), strings=()):
    """Each argument is its own wrapper message holding an `args` list, because
    RpcUtil indexes the WRAPPER, not the value: pop_uint32(r,i) is
    r.uint32[i].args[0] and pop_uint32_array(r,i) is r.uint32[i].args. A handler
    that reads wrapper i throws IndexOutOfRange unless that wrapper exists, so
    pass one (possibly empty) list per slot the handler touches.
    """
    b = pb_field_zigzag(1, index)
    for group in uint32s:
        b += pb_field_bytes(11, b"".join(pb_field_varint(1, v) for v in group))
    for group in uint64s:
        b += pb_field_bytes(12, b"".join(pb_field_varint(1, v) for v in group))
    # Rpc.sint32 args are [ProtoMember(1, DataFormat=ZigZag)] -- verified in the
    # attribute thunk -- so they are zigzag-encoded, unlike the uint32 args.
    for group in sint32s:
        b += pb_field_bytes(15, b"".join(pb_field_zigzag(1, v) for v in group))
    for group in strings:
        b += pb_field_bytes(14, b"".join(pb_field_bytes(1, s.encode()) for s in group))
    return b


def rpc_pack(*rpcs):
    """Client{ rpc_pack = [...] } -- sent as message type 131."""
    return b"".join(pb_field_bytes(31, r) for r in rpcs)

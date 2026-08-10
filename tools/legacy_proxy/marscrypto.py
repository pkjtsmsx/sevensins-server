"""Reimplementation of UserJoy 'Mars' SDK request/response codec.
Faithful port of com.userjoy.mars.* (XXTEA + Base64Kit + the 5-field wrapper).
"""
import base64, struct, random

DELTA = 0x9E3779B9
MASK = 0xFFFFFFFF

# ---------- Base64Kit ----------
def b64_encode(b: bytes) -> str:               # Base64Kit.Encode (flag 2 = NO_WRAP)
    return base64.b64encode(b).decode()

def b64_decode(s: str) -> bytes:               # Base64Kit.Decode
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))

def urlsafe_encode(b: bytes) -> str:           # Urlsafe_Encode
    return base64.b64encode(b).decode().replace('+', '-').replace('/', '_')

def trimmed_encode(b: bytes) -> str:           # Trimmed_Encode: urlsafe then strip trailing '='
    return urlsafe_encode(b).rstrip('=')

def trimmed_decode(s: str) -> bytes:           # Trimmed_Decode
    t = s.replace('-', '+').replace('_', '/').strip()
    t = t + '=' * ((4 - len(t) % 4) % 4)
    return base64.b64decode(t)

# ---------- XXTEA (Ccase) ----------
def _to_ints(data: bytes, add_len: bool):
    n = (len(data) + 3) // 4
    arr = [0] * (n + (1 if add_len else 0))
    for i, byte in enumerate(data):
        arr[i >> 2] |= byte << ((i & 3) << 3)
    if add_len:
        arr[n] = len(data)
    return arr

def _to_bytes(ints, has_len: bool):
    length = len(ints) << 2
    if has_len:
        n = ints[-1]
        if n > length:
            return None
        length = n
    out = bytearray(length)
    for i in range(length):
        out[i] = (ints[i >> 2] >> ((i & 3) << 3)) & 0xFF
    return bytes(out)

def _mx(sum_, y, z, p, e, key):
    return (((z >> 5 & 0x07FFFFFF) ^ (y << 2)) + ((y >> 3 & 0x1FFFFFFF) ^ (z << 4)) & MASK) ^ \
           (((sum_ ^ y) + (key[(p & 3) ^ e] ^ z)) & MASK)

def _encrypt_ints(v, key):
    n = len(v) - 1
    if n < 1:
        return v
    if len(key) < 4:
        key = key + [0] * (4 - len(key))
    y = v[0]; sum_ = 0
    q = 52 // (n + 1) + 6
    z = v[n]
    for _ in range(q):
        sum_ = (sum_ + DELTA) & MASK
        e = (sum_ >> 2) & 3
        for p in range(n):
            y = v[p + 1]
            z = v[p] = (v[p] + _mx(sum_, y, z, p, e, key)) & MASK
        p = n
        y = v[0]
        z = v[n] = (v[n] + _mx(sum_, y, z, p, e, key)) & MASK
    return v

def _decrypt_ints(v, key):
    n = len(v) - 1
    if n < 1:
        return v
    if len(key) < 4:
        key = key + [0] * (4 - len(key))
    y = v[0]; z = v[n]
    q = 52 // (n + 1) + 6
    sum_ = (q * DELTA) & MASK
    while sum_ != 0:
        e = (sum_ >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            y = v[p] = (v[p] - _mx(sum_, y, z, p, e, key)) & MASK
        p = 0
        z = v[n]
        y = v[0] = (v[0] - _mx(sum_, y, z, p, e, key)) & MASK
        sum_ = (sum_ - DELTA) & MASK
    return v

def xxtea_encrypt(data: bytes, key: bytes) -> bytes:   # Ccase.m166null
    if len(data) == 0:
        return data
    return _to_bytes(_encrypt_ints(_to_ints(data, True), _to_ints(key, False)), False)

def xxtea_decrypt(data: bytes, key: bytes) -> bytes:   # Ccase.cast(byte[],byte[])
    if len(data) == 0:
        return data
    return _to_bytes(_decrypt_ints(_to_ints(data, False), _to_ints(key, False)), True)

def ccase_encode(s: str, key: str) -> str:     # Ccase.m165null
    return trimmed_encode(xxtea_encrypt(s.encode(), key.encode()))

def ccase_decode(s: str, key: str) -> str:     # Ccase.cast(String,String)
    try:
        return xxtea_decrypt(trimmed_decode(s), key.encode()).decode('utf-8', 'replace')
    except Exception:
        return ""

# ---------- derived keys (NetworkDefine static init) ----------
PROTO_KEY = ccase_encode("9aapruc&", "S4u?Hu")   # 'cast' -> used by En_ProtoString
REQ_KEY   = ccase_encode("=8sw", "&3jax")        # f290null -> request encrypt key
RESP_KEY  = ccase_encode("atu", "8*+=")          # f289false -> response key

# ---------- 5-field wrapper (utils.cast) ----------
def encrypt_string(plain: str, key: str) -> str:        # utils.cast.m168null
    n1 = random.randint(0, 1000)
    n2 = random.randint(0, 1000)
    payload = "%d,%s,%d,%s,%d" % (n1, b64_encode(plain.encode()), n2,
                                  ccase_encode(key, str(n2)), 5000 - (n1 + n2))
    return trimmed_encode(payload.encode())

def decrypt_string(enc: str, key: str) -> str:          # utils.cast.cast
    dec = trimmed_decode(enc)
    parts = dec.decode('utf-8', 'replace').split(",")
    if len(parts) != 5:
        return ""
    n1 = int(parts[0]); body = parts[1]; n2 = int(parts[2]); sig = parts[3]; n5 = int(parts[4])
    if ccase_decode(sig, str(n2)) == key and (n1 + n2 + n5) == 5000:
        return b64_decode(body).decode('utf-8', 'replace')
    return ""

# high level
def decrypt_request(p: str) -> str:      # inverse of En_EncryptString
    return decrypt_string(p, REQ_KEY)

def encrypt_reply(json_str: str) -> str: # what the server must return (Decrypt_Reply inverse)
    return encrypt_string(json_str, RESP_KEY)


if __name__ == "__main__":
    print("PROTO_KEY =", repr(PROTO_KEY))
    print("REQ_KEY   =", repr(REQ_KEY))
    print("RESP_KEY  =", repr(RESP_KEY))
    print()
    # ---- validate against captured request ----
    p = "NDgyLGV5SmpiV1FpT2pFeExDSnpaWE56YVc5dUlqb2laRFUyTURkbE9HRXRPVEkzWkMwME56UXhMV0ZtTjJFdFpUVTNOV1ZsWW1JMU9HSTJJaXdpYkdGdVozVmhaMlZmWTI5a1pTSTZNVEFzSW10bGVYTWlPbHNpSWwwc0ltSjFibVJzWlY5MlpYSnphVzl1SWpvaU5EUWlMQ0ppZFc1a2JHVmZibUZ0WlNJNkltTnZiUzUxYzJWeWFtOTVMbk5wYm1WdVp5SXNJbTl6SWpveExDSnpaWFIwYVc1blgzWmxjbk5wYjI0aU9qQjksMTQ2LHZPRWtTMGZHUGJwbFlJc05mU0x6V1EsNDM3Mg"
    e = "v9Xw4I3AfSPzWVKo"
    print("decrypt_request(p) =>")
    print("  ", decrypt_request(p))
    print("decode e with PROTO_KEY =>", repr(ccase_decode(e, PROTO_KEY)))
    # ---- round trip a reply ----
    reply = '{"SVRCB":{"0":{"reply":12,"status":"0"}}}'
    enc = encrypt_reply(reply)
    print("round-trip reply ok =>", decrypt_string(enc, RESP_KEY) == reply)

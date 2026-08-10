#!/usr/bin/env python3
"""Resolve [JsonProperty] wire key strings straight out of libil2cpp.so.

Il2CppDumper reports each attribute as a tiny thunk:

    adrp x0, <page>
    add  x0, x0, #<off>
    b    <cache/ctor helper>

The computed address is a plain NUL-terminated C string in the binary, so
decoding the two instructions gives the wire key without needing an IDB.

**Use the 2.2.4 binary, not 2.2.7.** On the 2.2.7 `.so` these RVAs land inside
attribute *generator* functions that resolve their string from global-metadata.dat
at runtime, so the key is not in the binary at all. On 2.2.4 the thunk is still the
literal adrp+add above -- and since the whole 2.2.4->2.2.7 wire delta is the
super-limit system, the keys read off 2.2.4 are valid for 2.2.7 for every class
except the super-limit-era ones (SuperLimitDefineData & co), which are 2.2.7-only
and cannot be read this way.

Layout note: the adrp+add for a thunk's OWN key sits at **RVA+0x10**; there is a
second pair at +0x48 that belongs to the NEXT field's thunk. `--class` handles this
for you; bare RVA mode decodes exactly where you point it.

Validation: `--class OFABannerData` must print idx/bgt/edt/id/type/strarg/status,
which were established independently. If it does not, distrust the run.

Usage:
  json_keys.py --class OFABannerData RouletteInfo ...   # look RVAs up in dump.cs
  json_keys.py LIBIL2CPP.SO RVA [RVA ...]
  json_keys.py LIBIL2CPP.SO --scan 0x1128000 0x1129000     # sweep a thunk range
"""
import os, re, sys, struct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 2.2.4 -- see the module docstring for why it is not apk227/il2cpp227.
SO_224 = os.path.join(ROOT, 're/apk/lib/arm64-v8a/libil2cpp.so')
DUMP_224 = os.path.join(ROOT, 're/il2cpp/dump.cs')
# The thunk's own key; +0x48 is the next field's.
OWN_KEY_OFFSET = 0x10


def u32(buf, off):
    return struct.unpack_from('<I', buf, off)[0]


def sx(val, bits):
    return val - (1 << bits) if val & (1 << (bits - 1)) else val


def decode_adrp_add(buf, rva):
    """-> target address, or None if the two words aren't adrp+add x?, x?, #imm."""
    if rva + 8 > len(buf):
        return None
    w0, w1 = u32(buf, rva), u32(buf, rva + 4)

    # ADRP: op=1, bits 28..24 == 0b10000
    if not ((w0 >> 31) & 1) or ((w0 >> 24) & 0x1F) != 0x10:
        return None
    immlo = (w0 >> 29) & 3
    immhi = (w0 >> 5) & 0x7FFFF
    imm = sx((immhi << 2) | immlo, 21) << 12
    page = (rva & ~0xFFF) + imm

    # ADD (immediate), 64-bit: 1 00 100010 shift(2) imm12 Rn Rd  -> 0x91 in 31..24
    if (w1 >> 24) != 0x91:
        return None
    imm12 = (w1 >> 10) & 0xFFF
    shift = (w1 >> 22) & 3
    if shift == 1:
        imm12 <<= 12
    elif shift not in (0, 1):
        return None
    return page + imm12


def cstr(buf, off, limit=128):
    if off is None or off < 0 or off >= len(buf):
        return None
    end = buf.find(b'\x00', off, off + limit)
    if end < 0:
        return None
    s = buf[off:end]
    try:
        t = s.decode('utf-8')
    except UnicodeDecodeError:
        return None
    # wire keys are short printable identifiers
    if not t or not all(32 <= ord(c) < 127 for c in t):
        return None
    return t


def resolve(buf, rva):
    return cstr(buf, decode_adrp_add(buf, rva))


FIELD_RE = re.compile(
    r"\[JsonPropertyAttribute\][^\n]*RVA: (0x[0-9A-Fa-f]+)[^\n]*\n"
    r"(?:\s*\[\w+Attribute\][^\n]*\n)*"
    r"\s*public\s+[\w<>, \.\[\]]+\s+(\w+);")


def first_string_in_thunk(buf, rva, words=40):
    """First C string the thunk at `rva` materialises -- its own JsonProperty name.

    Scans rather than assuming a fixed offset: OWN_KEY_OFFSET (+0x10) only holds when
    the field carries a single attribute. A field with TWO (e.g.
    BackpackItemData.attr, which is [JsonProperty]+[JsonConverter]) shifts its string
    to +0x3c, and the fixed-offset read silently returns None.
    """
    for i in range(words):
        s = resolve(buf, rva + i * 4)
        if s:
            return s
    return None


def class_keys(buf, dump_text, name):
    """-> [(field, rva, key)] for one class, or None if the class is not in the dump."""
    # Nested classes are dumped under their qualified name (PlayerGeneral.Foo) and are
    # often `private sealed`, so accept any visibility and a dotted name. Matching a
    # bare leaf name is allowed too -- handy since the dump is the only place the
    # qualification is visible.
    pat = re.escape(name)
    if '.' not in name:
        pat = r"(?:\w+\.)*" + pat
    m = re.search(r"^(?:public|private|internal|protected)\s+(?:sealed\s+)?class %s\b"
                  r".*?^\}" % pat, dump_text, re.S | re.M)
    if not m:
        return None
    return [(field, rva, first_string_in_thunk(buf, int(rva, 16)))
            for rva, field in FIELD_RE.findall(m.group(0))]


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == '--class':
        with open(SO_224, 'rb') as f:
            buf = f.read()
        with open(DUMP_224) as f:
            dump_text = f.read()
        for name in sys.argv[2:]:
            rows = class_keys(buf, dump_text, name)
            if rows is None:
                print(f'{name}: not found in {DUMP_224}')
                continue
            print(f'=== {name}')
            for field, rva, key in rows:
                print(f'  {field:<22} {rva:<12} -> {key!r}')
        return 0

    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    path = sys.argv[1]
    with open(path, 'rb') as f:
        buf = f.read()

    if sys.argv[2] == '--scan':
        lo, hi = int(sys.argv[3], 0), int(sys.argv[4], 0)
        for rva in range(lo, hi, 4):
            s = resolve(buf, rva)
            if s:
                print(f'{rva:#x}  {s}')
        return 0

    for a in sys.argv[2:]:
        rva = int(a, 0)
        s = resolve(buf, rva)
        print(f'{rva:#x}  {s!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

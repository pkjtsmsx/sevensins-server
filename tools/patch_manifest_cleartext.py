#!/usr/bin/env python3
"""Permit cleartext HTTP in the game APK, by lowering targetSdkVersion.

The local bundle server (see patch_cdn_config.py) has no certificate a stock client
would trust, so it serves plain HTTP -- and this APK is targetSdk 33 with neither
`usesCleartextTraffic` nor a `networkSecurityConfig`, which means Android blocks
cleartext outright. Terra Battle needed none of this: it ships
`usesCleartextTraffic="true"` already (targetSdk 28), which is why reTB could simply
serve HTTP on 127.0.0.1.

TWO WAYS TO CREATE THAT CONDITION, and this file takes the cheap one:

1. Add `android:usesCleartextTraffic="true"` to <application>. Correct and targeted,
   but in binary AXML an attribute NAME must live in the string pool at the same index
   as its resource id in the ResourceMap. Both are positional, so inserting one shifts
   every string index at or after it and the whole file has to be re-emitted -- every
   tag, namespace and STRING-typed value re-pointed.

2. Lower targetSdkVersion below 28, where `usesCleartextTraffic` DEFAULTS to true.
   That is a single int32 already sitting in the attribute's value slot: a byte patch
   with zero structural change.

This does (2). The trade is real and worth stating: dropping the declared target level
also opts the app back into pre-28 behaviour generally (install-time permissions,
legacy storage). For a sideloaded, offline build of a 2022 game that is benign, and
the win is that the manifest's structure -- and therefore its installability -- is
untouched. If the target level ever needs to stay at 33, implement (1) instead; this
module is then the thing to delete, not to extend.

Android refuses to install anything targeting below 23, so the floor is enforced.
"""
import argparse, os, shutil, struct, sys, zipfile

MANIFEST = "AndroidManifest.xml"
ATTR_TARGET_SDK = 0x01010270      # android:targetSdkVersion
TYPE_INT_DEC = 0x10               # Res_value.dataType for a decimal int
CLEARTEXT_DEFAULT_MAX = 27        # last level where cleartext is allowed by default
MIN_INSTALLABLE = 23


def _string_pool(d):
    """-> (count, getter) for the AXML string pool at offset 8."""
    off = 8
    _t, hs, _cs, count, _sty, flags, strstart, _systart = struct.unpack_from(
        "<HHIIIIII", d, off)
    utf8 = bool(flags & (1 << 8))
    offs = [struct.unpack_from("<I", d, off + hs + 4 * i)[0] for i in range(count)]

    def get(i):
        if i == 0xFFFFFFFF or i >= count:
            return None
        p = off + strstart + offs[i]
        if utf8:
            n = d[p]
            p += 2 if n & 0x80 else 1
            n2 = d[p]
            if n2 & 0x80:
                n2 = ((n2 & 0x7F) << 8) | d[p + 1]
                p += 2
            else:
                p += 1
            return d[p:p + n2].decode("utf-8", "replace")
        n = struct.unpack_from("<H", d, p)[0]
        return d[p + 2:p + 2 + n * 2].decode("utf-16-le", "replace")

    return count, get


def _resource_map(d):
    """-> {string index: resource id} from the ResourceMap chunk."""
    off = 8
    _t, _hs, cs = struct.unpack_from("<HHI", d, off)
    rm = off + cs
    t, hs, cs2 = struct.unpack_from("<HHI", d, rm)
    if t != 0x0180:
        sys.exit("no ResourceMap chunk where one was expected")
    n = (cs2 - hs) // 4
    return {i: struct.unpack_from("<I", d, rm + hs + 4 * i)[0] for i in range(n)}


def find_target_sdk(d):
    """-> (file offset of the value int32, current value).

    Walks START_TAG chunks and their attribute arrays looking for the attribute whose
    name string index maps to android:targetSdkVersion in the ResourceMap. Matching on
    the RESOURCE ID rather than the string means a stripped/renamed pool still works.
    """
    resmap = _resource_map(d)
    wanted = {i for i, r in resmap.items() if r == ATTR_TARGET_SDK}
    if not wanted:
        sys.exit("targetSdkVersion is not in the ResourceMap")

    off = 8
    while off < len(d):
        t, hs, cs = struct.unpack_from("<HHI", d, off)
        if cs == 0:
            break
        if t == 0x0102:                                   # RES_XML_START_ELEMENT
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", d, off + hs + 8)
            base = off + hs + attr_start
            for k in range(attr_count):
                a = base + k * attr_size
                _ns, name = struct.unpack_from("<II", d, a)
                if name in wanted:
                    # Res_value: uint16 size, uint8 pad, uint8 dataType, uint32 data
                    vt = d[a + 15]
                    if vt != TYPE_INT_DEC:
                        sys.exit(f"targetSdkVersion is not an int (type {vt:#x})")
                    return a + 16, struct.unpack_from("<I", d, a + 16)[0]
        off += cs
    sys.exit("no targetSdkVersion attribute found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("apk", help="APK to patch in place")
    ap.add_argument("--level", type=int, default=CLEARTEXT_DEFAULT_MAX,
                    help=f"new targetSdkVersion (default {CLEARTEXT_DEFAULT_MAX})")
    args = ap.parse_args()

    if args.level > CLEARTEXT_DEFAULT_MAX:
        sys.exit(f"level {args.level} still blocks cleartext "
                 f"(needs <= {CLEARTEXT_DEFAULT_MAX})")
    if args.level < MIN_INSTALLABLE:
        sys.exit(f"Android refuses to install apps targeting below {MIN_INSTALLABLE}")

    with zipfile.ZipFile(args.apk) as z:
        d = z.read(MANIFEST)
    off, cur = find_target_sdk(d)
    print(f"targetSdkVersion {cur} -> {args.level} (at manifest offset {off})")
    if cur <= CLEARTEXT_DEFAULT_MAX:
        print("  already permits cleartext by default; nothing to do")
        return
    out = bytearray(d)
    struct.pack_into("<I", out, off, args.level)

    stage = os.path.join(os.path.dirname(os.path.abspath(args.apk)), "_mfstage")
    os.makedirs(stage, exist_ok=True)
    with open(os.path.join(stage, MANIFEST), "wb") as f:
        f.write(bytes(out))
    rc = os.system(f'cd {stage!r} && zip -q {os.path.abspath(args.apk)!r} {MANIFEST}')
    shutil.rmtree(stage, ignore_errors=True)
    if rc:
        sys.exit(f"zip update failed ({rc})")
    print(f"updated {args.apk} -- re-sign it before installing")


if __name__ == "__main__":
    main()

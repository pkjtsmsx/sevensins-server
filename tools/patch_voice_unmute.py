#!/usr/bin/env python3
"""Unmute cast voice + ambient for good, in libil2cpp.so.

Background. Game.Scene.SceneInit$$OnInitProcedureFinished does, when IsGENTELMAN:

    set_AmbientMuted(true); set_VoiceMuted(true);

The earlier build skipped the whole block (TBZ -> B at 0x34244a4). That is not enough:
AudioSetting.SetMuted writes through SettingBase.SetBoolValue -> PlayerPrefs.SetInt, so a
device that ever ran a muting build has VoiceMuted=1 STORED, and skipping the writer just
leaves the stale 1 in place forever -- which is exactly what "still no voice" looked like.

So instead: restore the branch (let the block run) and flip both arguments to false. Every
launch now writes muted=0, which clears the stale pref on existing installs. Cost: a mute
chosen in the settings menu does not survive a restart.

File offset == vaddr for this region of the .so (verified against the previous patch).
"""
import shutil
import struct
import sys

# (vaddr, expected-now, expected-original, replacement)
SITES = [
    # TBZ W0,#0,+0x5c -- the "skip the mute block" patch, undone here.
    (0x34244A4, 0x14000017, 0x360002E0, 0x360002E0),
    # ORR W1,WZR,#1 (IDA prints "MOV W1,#1") -> MOV W1,WZR, twice: set_AmbientMuted / set_VoiceMuted.
    (0x34244D8, 0x320003E1, 0x320003E1, 0x2A1F03E1),
    (0x34244F4, 0x320003E1, 0x320003E1, 0x2A1F03E1),
]


def main(path):
    shutil.copy2(path, path + ".prevoice")
    with open(path, "r+b") as f:
        for va, now, orig, new in SITES:
            f.seek(va)
            cur = struct.unpack("<I", f.read(4))[0]
            if cur == new:
                print(f"{va:#x}: already patched")
                continue
            if cur not in (now, orig):
                sys.exit(f"{va:#x}: unexpected {cur:#010x} (want {now:#010x})")
            f.seek(va)
            f.write(struct.pack("<I", new))
            print(f"{va:#x}: {cur:#010x} -> {new:#010x}")


if __name__ == "__main__":
    main(sys.argv[1])

#!/usr/bin/env python3
"""Read static array initializers (`InitializeArray` blobs) out of global-metadata.dat.

C# arrays like

    public static int[] InheritDiamondCnts = { 2000, 1500, ... };

compile to a `RuntimeHelpers.InitializeArray` call against a field on
`<PrivateImplementationDetails>` whose NAME is the SHA-1 of the blob, e.g.

    v13.fields.value = Field__PrivateImplementationDetails__4E0BB167B7EAF...;
    RuntimeHelpers.InitializeArray(v12, v13);

The bytes are NOT in libil2cpp.so -- they live in global-metadata.dat's
fieldAndParameterDefaultValueData. This walks: string table -> fields array ->
fieldDefaultValues -> blob.

Usage:
    python3 tools/metadata_blob.py <SHA1NAME> <count> [<type>]
    python3 tools/metadata_blob.py 4E0BB167B7EAF6836E494F6B369CB7F71E400EF3 5

`type` is a struct format char (default 'i' = int32). Get the SHA-1 name and the element
count by decompiling the class's `.cctor` -- the count is the argument to the array
allocation right before the InitializeArray call.

Validated 2026-08-05 by recovering CharDefine.SellMoneyStar as
[100, 250, 500, 1500, 3000, 15000], whose 3-star entry of 500 matches the unsummon payout
measured in game.
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METADATA = os.path.join(ROOT, "re", "apk227", "assets", "bin", "Data",
                        "Managed", "Metadata", "global-metadata.dat")

# Il2CppFieldDefinition is 12 bytes in THIS build (nameIndex, typeIndex, token) even
# though the header reports version 24 -- the docs' 16-byte layout (with
# customAttributeIndex) silently resolves garbage names. Sanity-check by dumping the
# first few field default values: they should look like enum members.
FIELD_DEF_SIZE = 12
FIELD_DEFAULT_VALUE_SIZE = 12


def _header(data):
    """Only the fields BEFORE the first [Version]-gated member, so this is
    version-independent: they are always at these fixed offsets."""
    h = struct.unpack_from("<26i", data, 0)
    return {
        "stringOffset": h[6], "stringSize": h[7],
        "fieldDefaultValuesOffset": h[16], "fieldDefaultValuesSize": h[17],
        "fieldAndParameterDefaultValueDataOffset": h[18],
        "fieldsOffset": h[24], "fieldsSize": h[25],
    }


def read_blob(name, count, fmt="i", path=METADATA):
    """-> list of `count` values for the <PrivateImplementationDetails> field `name`."""
    data = open(path, "rb").read()
    h = _header(data)

    strings = data[h["stringOffset"]:h["stringOffset"] + h["stringSize"]]
    pos = strings.find(name.encode() + b"\0")
    if pos < 0:
        raise SystemExit(f"{name}: not in the metadata string table")

    field_index = None
    for i in range(h["fieldsSize"] // FIELD_DEF_SIZE):
        (name_index,) = struct.unpack_from("<I", data,
                                           h["fieldsOffset"] + i * FIELD_DEF_SIZE)
        if name_index == pos:
            field_index = i
            break
    if field_index is None:
        raise SystemExit(f"{name}: no field references that name")

    for j in range(h["fieldDefaultValuesSize"] // FIELD_DEFAULT_VALUE_SIZE):
        f, _t, data_index = struct.unpack_from(
            "<iii", data, h["fieldDefaultValuesOffset"] + j * FIELD_DEFAULT_VALUE_SIZE)
        if f == field_index:
            start = h["fieldAndParameterDefaultValueDataOffset"] + data_index
            size = struct.calcsize("<" + fmt)
            return list(struct.unpack_from(f"<{count}{fmt}", data, start))
    raise SystemExit(f"{name}: field {field_index} has no default value")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    print(read_blob(sys.argv[1], int(sys.argv[2]),
                    sys.argv[3] if len(sys.argv) > 3 else "i"))

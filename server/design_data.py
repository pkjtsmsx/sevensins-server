#!/usr/bin/env python3
"""Read the game's design tables (DesignXForm ScriptableObjects) server-side.

The client drives almost everything off these tables -- a battle needs the stage's
mob groups, each mob's DesignCharRow, its skills and its model -- so the server has
to see the same data the client does or it cannot build a legal BattleDatas.

The forms live as MonoBehaviours inside `design_pack_<hash>.ab`. They are il2cpp
builds, so the bundle carries no type trees; we regenerate them from the
Il2CppDumper DummyDll assemblies with TypeTreeGeneratorAPI (needs dotnet) and hand
them to UnityPy. That is slow (~30s for the whole pack), so parsed forms are cached
as JSON under `design_cache/` and only regenerated when the bundle is newer.

Usage:
    import design_data as dd
    dd.row("stage", 1101)["_mobGroup_datas"]   -> "1101,1102,1103"
    dd.rows("mob_group")                       -> {id: row, ...}
"""
import json, os, glob, hashlib, threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Every path below is relocatable through the environment. On desktop the defaults
# keep the historical layout; inside an Android host app the code dir is READ-ONLY and
# the bundles are user-imported, so the app points these at its own storage.
PATCH_ROOT = os.environ.get("SEVENSINS_PATCH_ROOT") or os.path.join(HERE, "patch_root")


def _pack_path():
    """Read the SAME bundle the client downloads (patch_root/bundles), so server and
    client never disagree about the tables. patch_design.py renames it on every
    patch -- the hash in the name is what invalidates the device's cache -- so find
    it by glob rather than hardcoding."""
    packs = glob.glob(os.path.join(PATCH_ROOT, "bundles", "design_pack_*.ab"))
    if not packs:
        return os.path.join(HERE, "design_pack_ad8f540a149f3e9be4f939e87f0c15cb.ab")
    return max(packs, key=os.path.getmtime)


PACK = _pack_path()


def _dummy_dll_path():
    """Type trees must come from the SAME client build as the pack. Applying 2.2.4
    trees to the 2.2.7 pack is the silent-garbage case _extract warns about, so
    prefer the 2.2.7 dump and only fall back if it has not been staged."""
    for d in ("il2cpp227", "il2cpp"):
        p = os.path.join(ROOT, "re", d, "DummyDll")
        if os.path.isdir(p) and glob.glob(os.path.join(p, "*.dll")):
            return p
    # Absent is NOT an error at import time: the DLLs are needed only to reparse the
    # pack, and a deployment shipping a warm cache (the Android host app) never does.
    # _extract raises with a clear message if it is actually reached.
    return None


DUMMY_DLL = _dummy_dll_path()
CACHE_DIR = os.environ.get("SEVENSINS_DESIGN_CACHE") or os.path.join(HERE, "design_cache")
UNITY_VERSION = "2019.4.40f1"          # BundleFile.version_engine of the pack

_lock = threading.Lock()
_forms = {}                            # form name -> {row id: row dict}

# Everything a battle needs. Regenerating a form needs TypeTreeGeneratorAPI (venv +
# dotnet), which the plain `python3` the server may run under does not have, so keep
# this list in sync and prime the cache with `python design_data.py` after any change
# to the pack.
BATTLE_FORMS = ("stage", "char", "char_grow", "mob_group", "skill", "role_model_info")


def _cache_path(form):
    return os.path.join(CACHE_DIR, f"{form}.json")


def _stamp_path():
    return os.path.join(CACHE_DIR, ".source")


def _source_id():
    """Identify what the cache was built FROM, by content rather than timestamp.

    The 2.2.4 -> 2.2.7 migration broke the old `cache mtime >= pack mtime` test: the
    correct EN 2.2.7 pack carries a Dec-2024 mtime, older than the 2.2.4-derived
    cache, so every form looked fresh forever and the server kept answering from
    2.2.4 tables while the client ran 2.2.7 assets. A migration generally makes the
    RIGHT pack older, so mtime is exactly the wrong signal -- hash the pack instead,
    and include the DummyDll source since changing it reparses to different rows.
    """
    h = hashlib.sha1()
    with open(PACK, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    # NOTE: this last field has always been the literal "DummyDll" -- basename() of
    # both candidate dirs is the same string, so despite the docstring above it never
    # distinguished il2cpp227 from il2cpp. Kept verbatim rather than "fixed" because
    # changing it would invalidate every existing cache; it is also what lets a
    # DummyDll-less deployment compute an identical stamp.
    return f"{os.path.basename(PACK)}:{h.hexdigest()}:DummyDll"


_source_id_cached = None


def _current_source():
    global _source_id_cached
    if _source_id_cached is None:
        _source_id_cached = _source_id()
    return _source_id_cached


_stamp_checked = False


def _ensure_stamp():
    """Drop the whole cache when it was built from a different pack/DummyDll.

    The stamp is per-cache, not per-form, so a surviving file from the old source
    would still satisfy `os.path.isfile` and be read back as fresh under the new
    stamp. Purging is safe: everything under design_cache/ is derived from the pack.
    """
    global _stamp_checked
    if _stamp_checked:
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(_stamp_path()) as f:
            same = f.read().strip() == _current_source()
    except OSError:
        same = False
    if not same and DUMMY_DLL is None:
        # NEVER delete what we cannot rebuild. Without a DummyDll the purge below is
        # unrecoverable: every form goes, the reparse cannot run, and the server dies on
        # the first table it needs. That is precisely the situation in a deployment that
        # ships a warm cache and no parser (the Android host app), where the stamp can
        # differ for a completely benign reason -- e.g. the shipped pack was re-patched
        # to change a server address after the cache was built.
        #
        # A stale cache is enormously better than an empty one, so keep it and say so.
        print(f"[design_data] WARNING: cache stamp does not match "
              f"{os.path.basename(PACK)}, and no DummyDll is available to reparse. "
              f"KEEPING the existing cache -- tables may be stale.")
        _stamp_checked = True
        return
    if not same:
        stale = glob.glob(os.path.join(CACHE_DIR, "*.json"))
        for p in stale:
            os.remove(p)
        if stale:
            print(f"[design_data] pack/DummyDll changed -- dropped {len(stale)} "
                  f"stale cached form(s); they will be reparsed from "
                  f"{os.path.basename(PACK)}")
        with open(_stamp_path(), "w") as f:
            f.write(_current_source())
        _forms.clear()
    _stamp_checked = True


def _cache_is_fresh(form):
    _ensure_stamp()
    return os.path.isfile(_cache_path(form))


def _extract(forms_wanted):
    """Parse the named forms out of the bundle and write them to the cache.

    Each MonoBehaviour names its own class through m_Script, so we look the script
    up first and generate the type tree for exactly that class -- applying the wrong
    form's tree silently reads garbage or dies with "read_str out of bounds".
    """
    if DUMMY_DLL is None:
        raise RuntimeError(
            "design cache miss and no DummyDll staged under re/*/DummyDll -- cannot "
            f"reparse {os.path.basename(PACK)}. Wanted: {sorted(forms_wanted)}")
    import UnityPy
    from TypeTreeGeneratorAPI import TypeTreeGenerator

    gen = TypeTreeGenerator(UNITY_VERSION)
    for dll in glob.glob(os.path.join(DUMMY_DLL, "*.dll")):
        with open(dll, "rb") as f:
            gen.load_dll(f.read())

    def nodes_for(cls):
        return [{"m_Type": n.m_Type, "m_Name": n.m_Name,
                 "m_Level": n.m_Level, "m_MetaFlag": n.m_MetaFlag}
                for n in gen.get_nodes("Assembly-CSharp", cls)]

    os.makedirs(CACHE_DIR, exist_ok=True)
    env = UnityPy.load(PACK)
    found = {}
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        mb = obj.read(check_read=False)
        if mb.m_Name not in forms_wanted:
            continue
        script = mb.m_Script.read()
        tree = obj.read_typetree(nodes_for(f"{script.m_Namespace}.{script.m_ClassName}"))
        rows = {r["_id"]: r for r in tree.get("_rows", [])}
        found[mb.m_Name] = rows
        with open(_cache_path(mb.m_Name), "w") as f:
            json.dump(rows, f, separators=(",", ":"))
    return found


def rows(form):
    """-> {row id (int): row dict} for one design form, cached across runs."""
    with _lock:
        if form in _forms:
            return _forms[form]
        if _cache_is_fresh(form):
            with open(_cache_path(form)) as f:
                _forms[form] = {int(k): v for k, v in json.load(f).items()}
            return _forms[form]
        got = _extract({form})
        if form not in got:
            raise KeyError(f"design form {form!r} not present in {PACK}")
        _forms[form] = got[form]
        return _forms[form]


def row(form, row_id):
    return rows(form).get(int(row_id))


def csv_ints(value):
    """Design tables pack lists into CSV strings with empty slots ("50084,50081,,,").
    Returns only the populated entries, as ints."""
    if not value:
        return []
    return [int(p) for p in str(value).split(",") if p.strip() not in ("", "0")]


def warm(*forms):
    """Parse several forms in ONE bundle pass -- each pass re-reads 31 MB and
    regenerates type trees, so batching matters on a cold cache."""
    missing = [f for f in forms if f not in _forms and not _cache_is_fresh(f)]
    if missing:
        with _lock:
            for name, data in _extract(set(missing)).items():
                _forms[name] = data
    for f in forms:
        rows(f)


if __name__ == "__main__":
    import sys
    warm(*BATTLE_FORMS)
    if len(sys.argv) > 2:
        print(json.dumps(row(sys.argv[1], sys.argv[2]), indent=1, ensure_ascii=False))
    else:
        for f in BATTLE_FORMS:
            print(f, len(rows(f)), "rows")

#!/usr/bin/env python3
"""Build the AssetBundleManifest 'Android' the client downloads: lists every
LOCAL PAD pack (served from device) + every REMOTE bundle we host (served from
the proxy), with dependencies copied from the JP manifest. Names/hashes:
 - local  : from the 2.2.x apk assetpack filenames (<clearname>_<hash>)
 - remote : from the downloaded bundle filenames in bundles/ (<clearname>_<hash>.ab)
"""
import UnityPy, re, os, sys, glob

# Serve dir defaults to <project>/server, computed from the project root, so this
# build tool works whether it lives in server/ or tools/. Override with argv[1].
SP = (sys.argv[1] if len(sys.argv) > 1
      else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "server"))
LOCAL_LIST = SP + "/build/localpacks.txt"     # apk assetpack names, from the 2.2.4 apk
BUNDLES = SP + "/patch_root/bundles"          # remote bundles we host (<name>_<hash>.ab)
JP_MANIFEST = SP + "/Android_manifest.ab"     # JP reference manifest, for dependencies
OUT = SP + "/patch_root/Android_AssetBundles/231101en/Android"

def clr(n): return re.sub(r"_[0-9a-f]{32}(\.ab)?$", "", n)
def hbytes(h): return {f"bytes[{i}]": int(h[i*2:i*2+2], 16) for i in range(16)}


def build():
  """Rebuild and OVERWRITE the served manifest at OUT. Runs only as a script.

  This work used to sit at module top level, so merely importing the file (e.g. to
  read its path constants) rebuilt the manifest -- which is STALE for 2.2.7 and
  bypassed, so it clobbered the good EN manifest with a wrong one. Guard it behind
  __main__ + this function; the path constants above stay import-safe.
  """
  # JP manifest: clearname -> [dep clearnames]
  env = UnityPy.load(JP_MANIFEST)
  jt = [o for o in env.objects if o.type.name == "AssetBundleManifest"][0].read_typetree()
  jn = dict(jt["AssetBundleNames"]); jinf = dict(jt["AssetBundleInfos"])
  jp_deps = {clr(n): [clr(jn[d]) for d in jinf[i]["AssetBundleDependencies"]] for i, n in jn.items()}

  # collect our bundles: (clearname, hash, manifest_name)
  entries = {}  # clearname -> (hash, manifest_name)
  for line in open(LOCAL_LIST):
    f = line.strip()
    m = re.search(r"_([0-9a-f]{32})$", f)
    if m: entries[clr(f)] = (m.group(1), f + ".ab")
  EXCLUDE_REMOTE = os.environ.get("EXCLUDE_REMOTE_ART", "1") == "1"
  for fp in glob.glob(BUNDLES + "/*.ab"):
    f = os.path.basename(fp)[:-3]
    m = re.search(r"_([0-9a-f]{32})$", f)
    if not m: continue
    c = clr(f)
    # The manifest must never list ITSELF. The device caches point to the top-level
    # "Android" manifest bundle under its own v-timestamp hash, so extracting a device
    # cache sweeps in an "Android_<ts>" entry. Leaving it in makes the client look up
    # a manifest name it can't reconcile ("AssetBundle with name Android_<ts>.ab
    # doesn't exist in the AssetBundleManifest") and offer a patch update every launch.
    if c == "Android": continue
    # LOCAL WINS on duplicates: a clearname already present came from the apk's PAD
    # assetpacks, so it is on-device with the EN 2.2.4 hash. Advertising the JP/DMM
    # hash instead would make the client re-download ~1500 bundles it already has.
    if c in entries: continue
    # bundles/ is a CURATED set -- we only download what the client actually asks for,
    # so presence of the file is the decision to host it. The old art_/audio_ prefix
    # exclusion existed to stop AssetInitiator preloading the whole CDN; that no longer
    # applies now that everything listed here is served from localhost. A bundle that is
    # missing from the manifest hard-blocks the client with
    # "無法取得 <name> 的 hashname" and an AssetOp that never completes.
    if EXCLUDE_REMOTE and not os.path.isfile(fp): continue
    entries[c] = (m.group(1), f + ".ab")   # remote only when not already local

  clears = sorted(entries)
  idx = {c: i for i, c in enumerate(clears)}
  names, infos = [], []
  for c in clears:
    h, mname = entries[c]
    deps = [idx[d] for d in jp_deps.get(c, []) if d in idx]
    names.append([idx[c], mname])
    infos.append([idx[c], {"AssetBundleHash": hbytes(h), "AssetBundleDependencies": deps}])

  mo = [o for o in env.objects if o.type.name == "AssetBundleManifest"][0]
  jt["AssetBundleNames"] = names
  jt["AssetBundleInfos"] = infos
  jt["AssetBundlesWithVariant"] = []
  mo.save_typetree(jt)
  open(OUT, "wb").write(env.file.save())
  nremote = len(glob.glob(BUNDLES + "/*.ab"))
  print(f"manifest: {len(names)} bundles ({nremote} remote-hosted) -> {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
  build()

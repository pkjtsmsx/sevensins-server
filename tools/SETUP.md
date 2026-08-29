# Local tooling setup

Things that are NOT in the repo and must be installed on a fresh machine.

This page covers the **Python packages** only. The game data a fresh clone also needs --
the design pack and the il2cpp DummyDlls -- is in `docs/BRINGUP.md`. Run
`python3 tools/check_setup.py` to see which of either is still missing.

## TypeTreeGeneratorAPI (required for anything that reads Unity assets)

```
pip install --user --break-system-packages TypeTreeGeneratorAPI
```

A prebuilt `cp36-abi3` manylinux wheel exists (8.6 MB), so no dotnet SDK is needed at
install time. The `--break-system-packages` override is required on Arch-likes (PEP 668)
and only writes to `~/.local`, which is already where UnityPy lives.

**What breaks without it:**

* `server/design_data.py` cannot reparse the design pack — it works only off a warm
  `server/design_cache/`, and dies with `ModuleNotFoundError` on any cache miss.
* `tools/patch_server_row.py` cannot repack the design pack (so the client cannot be
  repointed at a new server address).
* `tools/skill_cinematics.py` cannot read the skill cinematics at all — without type
  trees, raw-byte parsing returns `None` for every object name.

Verify:

```
python3 -c "from TypeTreeGeneratorAPI import TypeTreeGenerator; TypeTreeGenerator('2019.4.40f1')"
```

### Reading MonoBehaviours with it

Do **not** hand-build node lists and pass them to `read_typetree(nodes)` for the Bsc\*
types — that path raises `read_str out of bounds`, because the MonoBehaviour base nodes
(`m_GameObject`, `m_Enabled`, `m_Script`, `m_Name`) end up doubled. Instead attach the
generator to the environment and call `read_typetree()` with **no arguments**:

```python
from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
gen = TypeTreeGenerator("2019.4.40f1")
for dll in glob.glob("re/il2cpp227/DummyDll/*.dll"):
    gen.load_dll(open(dll, "rb").read())

env = UnityPy.load(bundle)
env.typetree_generator = gen          # UnityPy now asks it per object
d = obj.read_typetree()               # no nodes argument
```

`design_data.py`'s own extractor predates this and passes nodes explicitly; that works
for the design forms, just not for these.

The generator also exposes `load_il2cpp(il2cpp, metadata)`, which would build trees
straight from `libil2cpp.so` + `global-metadata.dat` without DummyDlls. Not needed so far.

## Versions this was working against

* UnityPy 1.25.0, Python 3.14
* TypeTreeGeneratorAPI 0.0.10
* Unity 2019.4.40f1 (the pack's `BundleFile.version_engine`)

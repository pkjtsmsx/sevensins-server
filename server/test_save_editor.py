#!/usr/bin/env python3
"""The save editor writes rows the CLIENT can actually file.

Reported 2026-08-20: adding a Starshard or Soulmirror through the editor made the whole
list go blank -- "No Available Soulmirror", Owned `-/-` -- until the added piece was
removed again. One malformed row takes the entire list with it.

`_set_item` hand-wrote a stackable record for everything:

    {"amount": n, "attr": {}, "iid": iid, "sid": slot, "uid": ""}   in storage 1

For a Starshard or Soulmirror that is wrong three ways at once -- wrong storage, no uid,
and no rolled `attr` block. They are INSTANCES, not stacks: one slot each, in storage 2
and 3 respectively, with a uid and the mandatory `lv`/`ts` attributes. Exactly the trap
`grant_reward` was fixed for earlier; the editor bypassed it.

    python3 test_save_editor.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-editor-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt                                     # noqa: E402
import player_state as ps                               # noqa: E402
import save_editor as se                                # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def _sample_ids():
    """A real Soulmirror and a real Starshard item id from the design data."""
    soulmirror = starshard = None
    for iid in bt.dd.rows("item"):
        if ps.item_bucket(iid) != "equipment":
            continue
        action = (bt.dd.row("item", iid) or {}).get("_action")
        if action in ps.SOULFRAG_SLOT_INDEX:
            soulmirror = soulmirror or iid
        else:
            starshard = starshard or iid
        if soulmirror and starshard:
            break
    return soulmirror, starshard


def check_instances_cannot_be_edited():
    """Starshards and Soulmirrors were REMOVED from the editor. Guard the removal.

    They are instances, not stacks, and the editor could not build one the client
    accepts. A malformed storage-2/3 entry makes PlayerBackpack's login sync throw
    partway through, so the client hangs on `Subsystem 'PlayerBackpack' still in
    syncing...` -- on every login thereafter, because the entry persists, and again
    whenever a cast holding one is opened. The save then needs repairing by hand.

    Three independent surfaces have to stay closed, because closing only one leaves a
    route back in: the search that offers an id, the view that lists an owned one, and
    the write that accepts a hand-posted edit.
    """
    soulmirror, starshard = _sample_ids()
    check("the design data has a Soulmirror item", bool(soulmirror))
    check("  ...and a Starshard item", bool(starshard))

    for iid, label in ((soulmirror, "Soulmirror"), (starshard, "Starshard")):
        st = ps.load(1000070 + iid % 100)
        before = json.dumps(st.get("backpack") or {}, sort_keys=True)
        try:
            se._set_item(st, {"iid": iid, "amount": 2})
            check(f"{label}: the write is refused", False, "no exception raised")
        except ValueError as exc:
            check(f"{label}: the write is refused", True)
            check("  ...with a message naming the item",
                  se.item_name(iid) in str(exc), str(exc)[:80])
        check("  ...and the backpack is untouched",
              json.dumps(st.get("backpack") or {}, sort_keys=True) == before)

        # The search must not OFFER one -- rejecting after the fact would still show it.
        name = se.item_name(iid)
        offered = [r for r in se.search_items(name) if r["iid"] == iid]
        check(f"  ...and search never offers the {label}", not offered, str(offered[:2]))


def check_owned_instances_are_hidden():
    """An already-owned piece is omitted from the view entirely, not shown-and-locked.

    Nothing can be done with it here, and the item list already runs to 250+ rows on a
    real save -- a row that cannot be touched is noise.
    """
    _sm, starshard = _sample_ids()
    pid = "1000079"
    st = ps.load(int(pid))
    ps.grant_rune(st, starshard, 1)          # the REAL path, so the piece is well-formed
    ps.save(st)
    view = se.account_view(pid)
    shown = [i for i in view["items"] if int(i["iid"]) == int(starshard)]
    check("an owned Starshard is hidden from the item list", not shown, str(shown[:1]))
    check("  ...while ordinary items still list",
          all("iid" in i for i in view["items"]))


def check_diamonds_cannot_overflow_int32():
    """The client reads balances as SIGNED 32-BIT, and shows diamonds as 1 + 32.

    Reported from a real save: both halves set to 2,000,000,000 -- each individually
    legal -- summed to 4,000,000,000 and wrapped to -294,967,296, at which point the
    game refuses to spend them ("diamonds insufficient, go to the shop?" against a
    negative bar). The cap has to be on the PAIR, not the field.
    """
    int32_max = 2 ** 31 - 1
    for order in (("1", "32"), ("32", "1")):
        pid = "1000096" if order[0] == "1" else "1000097"
        ps.save(ps.load(int(pid)))
        for cid in order:
            se.apply_edits(pid, {"currencies": {cid: 2_000_000_000}})
        cur = se._read(pid).get("currency") or {}
        total = int(cur.get("1") or 0) + int(cur.get("32") or 0)
        check(f"raising {order[0]} then {order[1]}: the diamond total fits in int32",
              total <= int32_max, f"{total:,}")
        check("  ...with headroom for what the player earns next",
              total <= se.CURRENCY_MAX, f"{total:,}")

    # The GRANT paths share the same ceiling -- an editor-only cap would just move the
    # overflow to "play for a while after editing". See player_state.core.add_currency.
    pid = "1000099"
    ps.save(ps.load(int(pid)))
    st = ps.load(int(pid))
    for _ in range(400):
        ps.grant_reward(st, 1, 5_000_000)          # free diamonds
        ps.grant_currency(st, 32, 5_000_000)       # paid diamonds
        ps.grant_reward(st, 2, 14_000_000)         # Mira, one farm clear at a high rate
        ps.grant_item(st, 101, 9_000_000)          # an ordinary stack
    cur = st.get("currency") or {}
    cash = int(cur.get("1") or 0) + int(cur.get("32") or 0)
    check("granting cannot overflow the diamond pair either", cash <= int32_max,
          f"{cash:,}")
    check("  ...nor a single currency", int(cur.get("16") or 0) <= int32_max,
          f"{int(cur.get('16') or 0):,}")
    stacks = [int(e.get("amount") or 0) for b in (st.get("backpack") or {}).values()
              for e in b.values()]
    check("  ...nor a backpack stack", max(stacks) <= int32_max, f"{max(stacks):,}")

    # A normal edit must not be collateral damage.
    pid = "1000098"
    ps.save(ps.load(int(pid)))
    se.apply_edits(pid, {"currencies": {"1": 50_000, "32": 1_200}})
    cur = se._read(pid).get("currency") or {}
    check("ordinary diamond amounts pass through untouched",
          int(cur.get("1")) == 50_000 and int(cur.get("32")) == 1_200,
          f"{cur.get('1')}/{cur.get('32')}")


def check_ordinary_items_are_still_stacks():
    """The instance path must not swallow normal consumables."""
    st = ps.load(1000062)
    iid = 487                                    # Popular Poster, an ordinary gift item
    check("an ordinary item is not treated as an instance",
          se._instance_storage(iid) is None, str(se._instance_storage(iid)))
    se._set_item(st, {"iid": iid, "amount": 25})
    rows = [r for r in st["backpack"]["1"].values() if int(r.get("iid", -1)) == iid]
    check("  ...and stays ONE row with an amount", len(rows) == 1, str(len(rows)))
    check("  ...carrying the amount asked for", rows[0]["amount"] == 25,
          str(rows[0]["amount"]))
    se._set_item(st, {"iid": iid, "amount": 0})
    rows = [r for r in st["backpack"]["1"].values() if int(r.get("iid", -1)) == iid]
    check("  ...and 0 removes the slot", not rows, str(rows))



def check_every_account_opens():
    """A stored NULL must not stop the editor opening an account.

    `dict.get(key, default)` returns the stored None for a key that EXISTS and is null,
    which is not what the view wants -- and null is meaningful here: a roster entry
    stores `star: None` to mean "use the cast's rarity default". `int(c.get("star", 1))`
    therefore raised

        TypeError: int() argument must be a string, a bytes-like object or a real
                   number, not 'NoneType'

    and the editor could not open ANY account whose roster had one. Only the account
    that happened to have every star set explicitly worked, so it looked like the
    default selection was broken rather than most saves being unreadable.
    """
    check("_num treats a stored null as the default",
          se._num(None, 7) == 7, str(se._num(None, 7)))
    check("  ...and junk too", se._num("nonsense", 3) == 3)
    check("  ...while a real value passes through", se._num("12", 0) == 12)

    st = ps.load(1000063)
    uid = next(iter(st["roster"]))
    st["roster"][uid]["star"] = None            # the legitimate "use the default" form
    ps.save(st)
    try:
        view = se.account_view("1000063")
    except TypeError as exc:                    # noqa: BLE001
        check("an account with a null star still opens", False, str(exc))
        return
    check("an account with a null star still opens", True)

    row = next(r for r in view["roster"] if r["uid"] == uid)
    # **Not 1.** The UI posts this value back, so showing a bare 1 would write star=1
    # over the cast on the next save.
    cid = st["roster"][uid].get("id")
    want = int(ps.char_star((bt.dd.row("char", cid) or {}).get("_rarity")) or 1)
    check("  ...showing the cast's real default star, not 1",
          row["star"] == want, f'{row["star"]} vs {want}')
    check("    ...which for a high-rarity cast is above 1", want > 1, str(want))


def main():
    for fn in (check_diamonds_cannot_overflow_int32,
               check_instances_cannot_be_edited,
               check_owned_instances_are_hidden,
               check_ordinary_items_are_still_stacks,
               check_every_account_opens):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

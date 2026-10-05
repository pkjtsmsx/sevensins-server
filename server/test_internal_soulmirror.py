"""The pack's internal Soulmirror rows must never reach a player.

They are rollable and equippable -- real bonus rows, no crash -- so the only tell is the
name: a player saw "UR Soulmirror Piece of Summer (ATK)" and a blank-named one where every
real mirror shows a character. Two paths handed them out, the banner and the fuse, both
filtering on (_action, char, rarity) and neither on the name.
"""
import os, tempfile, unittest
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-intmirror-")

import battle as bt
from player_state.core import (
    SOULFRAG_SLOT_INDEX, is_internal_soulmirror, make_soulmirror)
from player_state import gacha as ga, gear as gd
import player_state as ps
from player_state.core import BP_STORAGE_SOULFRAG, soulfrag_slot


def _soulmirrors():
    return {int(i): r for i, r in (bt.dd.rows("item") or {}).items()
            if isinstance(r, dict) and r.get("_action") in SOULFRAG_SLOT_INDEX}


class TestInternalSoulmirror(unittest.TestCase):
    def test_discriminator_splits_the_rows_cleanly(self):
        """Underscore in the Chinese name, and it partitions by id band."""
        rows = _soulmirrors()
        internal = {i for i in rows if is_internal_soulmirror(i)}
        self.assertTrue(internal, "pack changed: no internal rows found")
        self.assertTrue(rows.keys() - internal, "the filter ate every mirror")
        # The real ones and the internal ones never share an id band.
        band = lambda i: i // 1000
        self.assertFalse({band(i) for i in internal}
                         & {band(i) for i in rows.keys() - internal})
        # Every internal name ends in one of the three stat characters.
        for i in internal:
            self.assertRegex(str(rows[i].get("_itemName")), r"_[體攻防]$")

    def test_both_reported_shapes_are_caught(self):
        """The two a player actually saw: the table-key name, and the blank one."""
        self.assertTrue(is_internal_soulmirror(711161))   # "...Piece of Summer (ATK)"
        self.assertTrue(is_internal_soulmirror(711064))   # _itemName_en is ""
        self.assertFalse(is_internal_soulmirror(420136))  # "Summer I", the real one

    def test_the_funnel_refuses_them(self):
        internal = next(i for i in _soulmirrors() if is_internal_soulmirror(i))
        with self.assertRaises(ValueError):
            make_soulmirror(None, internal)

    def test_no_banner_can_draw_one(self):
        """Pre-fix this was 2,619 of 12,279 drawable ids -- about one pull in five."""
        for alignment in range(100, 105):
            pool = ga._soulmirror_gacha_pool(alignment)
            drawable = {i for ids in pool.values() for i in ids}
            self.assertTrue(drawable, f"alignment {alignment} draws nothing at all")
            self.assertFalse({i for i in drawable if is_internal_soulmirror(i)},
                             f"alignment {alignment} can still roll an internal row")

    def test_no_fuse_cell_can_yield_one(self):
        """Pre-fix 261 of 789 transmute cells held one, half the candidates in some."""
        cells, reached = set(), 0
        for iid, row in _soulmirrors().items():
            cell = (int(row.get("_param2") or 0), int(row.get("_param3") or 0),
                    int(row.get("_action") or 0))
            if cell[0] != gd.SOULFRAG_TRANSMUTE_RARITY or cell in cells:
                continue
            cells.add(cell)
            candidates = gd.soulmirror_items_for(*cell)
            reached += len(candidates)
            self.assertFalse([c for c in candidates if is_internal_soulmirror(c)],
                             f"fuse cell {cell} still offers an internal row")
        self.assertTrue(reached, "no fuse cell produced any candidate -- test is blind")

class TestPurgeFromExistingSaves(unittest.TestCase):
    """Closing the sources only helps new grants -- players already hold these."""

    def _wounded(self, pid, equip_it):
        bad = next(i for i in _soulmirrors() if is_internal_soulmirror(i))
        good = next(i for i in _soulmirrors() if not is_internal_soulmirror(i)
                    and soulfrag_slot(i) == soulfrag_slot(bad))
        st, uid = ps.load(pid), f"{pid}m0001"
        bag = st["backpack"].setdefault(str(BP_STORAGE_SOULFRAG), {})
        bag["1"] = {"iid": bad, "sid": 1, "uid": uid, "amount": 1,
                    "attr": {"lv": 0, "ts": 0}}
        bag["2"] = {"iid": good, "sid": 2, "uid": f"{pid}m0002", "amount": 1,
                    "attr": {"lv": 0, "ts": 0}}
        cast = next(iter(st["roster"]))
        if equip_it:
            worn = [""] * 18
            worn[soulfrag_slot(bad)] = uid
            st["roster"][cast]["equips_list"] = worn
        ps.save(st)
        return bad, good, uid, cast

    def test_bagged_one_is_removed_and_the_real_one_kept(self):
        bad, good, _uid, _cast = self._wounded(1000601, equip_it=False)
        bag = (ps.load(1000601)["backpack"] or {}).get(str(BP_STORAGE_SOULFRAG)) or {}
        held = {r.get("iid") for r in bag.values()}
        self.assertNotIn(bad, held, "the internal mirror survived the load purge")
        self.assertIn(good, held, "a legitimate mirror was removed too -- too aggressive")

    def test_an_equipped_one_is_also_unequipped(self):
        """A bag row deleted out from under a worn uid is its own crash."""
        bad, _good, uid, cast = self._wounded(1000602, equip_it=True)
        st = ps.load(1000602)
        worn = st["roster"][cast].get("equips_list") or []
        self.assertNotIn(uid, worn, "cast still wears a uid whose bag row is gone")
        self.assertEqual(len(worn), 18, "equips_list must stay 18 slots")
        bag = (st["backpack"] or {}).get(str(BP_STORAGE_SOULFRAG)) or {}
        self.assertNotIn(bad, {r.get("iid") for r in bag.values()})


if __name__ == "__main__":
    unittest.main()

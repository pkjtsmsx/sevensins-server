# RPC coverage: what the client can ask vs what we answer
Generated 2026-08-05 from `re/il2cpp227/dump.cs` (every `*RpcServerCmd` / `*RpcRequestCmd` enum) diffed against `server/titan_server.py`.
**69 of 190 server commands handled.** Battle is now COMPLETE (16/16).
Updated 2026-08-06. Added: Gacha 51 `DrawRoulette`; OFA 3 `SyncOFAContent`; the Char
panel-hang set (276 `lock_char`, 277 `char_max`, 292 `char_decompose`, 312 `set_sort`,
321 `remove_from_allformation`); ChatRoom 339 `get_pmsg_list`; Guild 272 `sync`; and
General 339 `DeviceInfo` (acked — it has no reply cmd in the client enum).
**A full login now produces ZERO unanswered commands and zero client warnings.**

Updated 2026-08-18. **118 of 186** now handled. **Guild is COMPLETE (16/16)** and
**Challenge is COMPLETE (3/3)** — the Guild Weekly boss and a one-member guild, see
the note under Tier 3 below. The generator gained two fixes in the same pass: it can
now see `cmd in (A, B, …)` branches (which is why Backpack went 13→14 and Mail 3→4
with no code change — those branches were always answered, just invisible), and
`--missing` no longer dies on import because `titan_server` reads `sys.argv[1]` as its
port.

Updated 2026-08-25. **138 of 219** -- DOWN from 142 with nothing lost. Mail and Guild
moved out of `handle()`'s `elif` chain into `titan_server.HANDLERS`, a `(index, cmd)
-> function` table the tool now reads directly, and four phantom credits vanished: the
"table-driven" regex had also matched the chain's own `cmd in (GUILD_REQ_QUIT,
GUILD_REQ_DISBAND):`, read `GUILD_REQ_QUIT` as an index it could not label, and
credited cmd 279 to every subsystem with a 279 (Arena `fight`, ChatRoom `disband`);
likewise 305 via RECOMMEND/SEARCH. The regex now refuses `in (`, and re-running it
against the pre-migration file also says 138. **The registry is the way forward:** a
subsystem in the table is counted exactly, and once the chain is empty every regex in
`handled_pairs()` can be deleted. `test_rpc_registry.py` drives real frames through
`handle()` so a registered pair that never fires cannot read as handled.

> Prioritise by what actually fires, not by tier alone. `grep -oE "no handler for
> index=0x[0-9a-f]+ cmd=[0-9]+" server/titan_server.log | sort | uniq -c | sort -rn`
> ranks the real gaps; cross-reference `re/rpc_subsystems.txt` for the index names.
> That is how ChatRoom 339 (14575 occurrences, nominally Tier 3) was found to be the
> single noisiest thing on the wire.
> **Battle was re-verified by hand 2026-08-06 and the generated number was wrong**
> (it said 1/16; the truth is 8/16). Battle answers arrive through TWO paths the
> `index == X and cmd == Y` scan cannot see: `battle_replies()` and the
> `build_sync_replies` table. Treat any future regenerated Battle row as unverified.
>
> Note also that `battle_replies` logs its misses as
> `(battle: no handler for cmd N)` — different wording from the dispatcher's
> `(no handler for index=0x… cmd=N)`. Grep for both when ranking gaps.

Closed 2026-08-13 (Battle): **508 `ServerRPCRuneSelect`**, with its server-side pair
**1507 RuneList** and **1508 SelectRune**. The Starshard Temple's "Starshards Select"
flow — 1507 carries the candidates AND launches the panel, 508 is the player's pick,
1508 answers `[itemID, itemCount]` (not the index) and is what finally ends the results
sequence. Unanswered, a Temple clear hangs the client. See docs/STARSHARD_TEMPLE.md.

## Tier 1 — Reachable from the main UI — an unanswered cmd here can HANG or lock the client

### Char  (18/37)
| cmd | name |
|---:|---|
| 275 | `formationTeam` |
| 289 | `char_super_rank_up` |
| 295 | `char_wear_rune` |
| 296 | `char_wear_soulfrag` |
| 297 | `char_wear_bloodpact` |
| 304 | `set_helper` |
| 305 | `set_showgirl` |
| 306 | `set_char_id` |
| 308 | `unlock_skin` |
| 309 | `set_skin` |
| 322 | `char_info` | (JS-only: the sole caller is the Puerts wrapper, no C# path)
| 353 | `char_reset_part` |
| 354 | `char_reset_all` |
| 355 | `char_reset_preview` |
| 369 | `sync_custom_rune_set` |
| 370 | `set_custom_rune_set_info` |
| 371 | `set_custom_rune_set_top` |
| 372 | `delete_custom_rune_set` |
| 373 | `active_collection` |

### Stage  (6/9)
| cmd | name |
|---:|---|
| 3 | `Autoruns` |
| 6 | `SyncAuto` |
| 7 | `AutoStop` |

### Shop  (5/6)
| cmd | name |
|---:|---|
| 274 | `PurchaseAsk` |

Closed 2026-08-13:
* **261 `GoodsIDToShopID`** -> reply **517**. The GO! button on a "go and exchange X"
  goal. Reply intargs are `[shopID, tabID]` and the handler needs BOTH -- it bails
  unless `_size >= 2`, then calls `PanelStore.EnterSpecificStore(filterID=intargs[1],
  shopID=intargs[0])`. Unanswered the button is silently inert.
* **273 `query_coupon_list`** -> reply **529**. Drop Info for an `_action 7` SELECTOR.
  `PanelItemInfo.OnPanelDirty` (0x15AA038) routes Drop Info by `_action`: `_action 2`
  goes to the BACKPACK (129), `_action 7` to the SHOP. Same `List<List<uint>>` payload.
  Unanswered this LOCKS the client -- the popup is already behind a modal that only the
  reply populates.

### Gacha  (7/13)
| cmd | name |
|---:|---|
| 3 | `GetDropInfo` |
| 4 | `CheckBox` |
| 33 | `SyncMonopoly` |
| 34 | `RollRandDiceMonopoly` |
| 35 | `RollSpecDiceMonopoly` |
| 36 | `SetWishList` |

### General  (4/13)
| cmd | name |
|---:|---|
| 258 | `PlayTutorial` |
| 276 | `SetPersonalInfo` |
| 337 | `BPassCompleted` |
| 338 | `BuyBPassPoint` |
| 340 | `ShopBattlepassGift` |
| 353 | `JackpotInfo` |
| 369 | `ScoreboardInfo` |
| 370 | `ScoreboardRewardInfo` |
| 409 | `SendErrorLog` |

### Quest  (2/3)
| cmd | name |
|---:|---|
| 258 | `TouchCount` |

### Friend  (3/10)
| cmd | name |
|---:|---|
| 272 | `helpers` |
| 337 | `follow` |
| 338 | `remove` |
| 353 | `set_block` |
| 354 | `remove_block` |
| 355 | `query_player` |
| 357 | `personal_info` |

### Ranking  (1/4)
| cmd | name |
|---:|---|
| 272 | `get_ranking_event_top` |
| 273 | `sync_ranking_event` |
| 275 | `get_ranking_event_bs` |

### OFA  (3/4)
| cmd | name |
|---:|---|
| 4 | `CaseReset` |

### Battle  (16/16 — complete, verified in-game 2026-08-07)

All of `BattleRpcServerCmd` is answered. Contracts came out of PlayerBattle's `Handle*`
methods; the reply cmds are `battle.py`'s `CMD_*`.

| cmd | name | reply | notes |
|---:|---|---|---|
| 100 | `Ready` | 1100+1101 | sent only on the auto path |
| 101 | `StartTurn` | 1101 | repeat while a turn is open is ignored on purpose |
| 200 | `Judge` | 1200 / 1506 | wave result instead once the wave is decided |
| 201 | `Attack` | 1201 | |
| 500 | `Retreat` | 1500 | |
| 501 | `Auto` | 1501 | `strargs[0]` MUST be PlayerGeneral.Uid or the handler no-ops |
| 502 | `NextWave` | 1503 | |
| 504 | `ChangeChar` | 1504 | `strargs=[in, out, actionOrderJson]`; unreachable while we field the whole party |
| 505 | `BattleEnd` | — | rewards go out on the PlayerStage channel |
| 508 | `SelectRune` | 1508 | echo the pick; choices would come from 1507 RuneList, which we never push |
| 601 | `Info` | 1601 | one **BattleCharData** (NOT the LightBattleChar from BattleDatas) |
| 701 | `Reconnect` | 1505 | resend BattleDatas — we still hold the Battle object |
| 801 | `Sync` | via `build_sync_replies` | most frequent battle cmd in the log |
| 802 | `ReplayBattle` | 1903 | `intargs=[1,1] strargs=["NIL"]` = the "no record" path |
| 803 | `ReplayLastBattle` | 1903 | same |
| 804 | `ReportError` | — | no reply cmd exists; acked |

**Auto-battle is SERVER-driven.** `SituationJudgeState`'s OnEnter/OnUpdate/OnLeave are all
stubs and it only has `OnAttack(BattleEvent)` — the client parks there and acts only when
the server pushes Attack (1201). Answering 501 alone hides the skill bar and deadlocks the
fight; `Battle.auto_move()` drives the player's turn the same way the enemy turn always
worked.

## Tier 2 — Implementable, but the DATA was live-ops and is not in any client file

### Campaign  (0/6)
| cmd | name |
|---:|---|
| 273 | `Sync` |
| 289 | `SaveReservation` |
| 305 | `Attack` |
| 321 | `ResetProgress` |
| 322 | `Reborn` |
| 337 | `Cure` |

### Challenge  — COMPLETE (3/3)
Done 2026-08-18. The Guild Weekly boss. Its data was NOT live-ops after all: book 8 of
the stage form holds all 28 stages (7 virtue bosses × 4 difficulties, 1000001–1000037)
and `challenge_reward` holds the damage brackets, so the only thing missing was a
server. `272 sync` → 784 (exactly 5 ints + 1 strarg), `528 fight` → the ordinary
EXECUTE_SUCCESS + battle handoff, `544 rank_guild` → 801. See
`server/player_state/challenge.py`.

### Raid  (0/5)
| cmd | name |
|---:|---|
| 1 | `sync_roominfo` |
| 256 | `create_room` |
| 257 | `enter_room` |
| 258 | `leave_room` |
| 336 | `pickup_role` |

### Village  (0/5)
| cmd | name |
|---:|---|
| 272 | `sync_infos` |
| 273 | `sync_enemys` |
| 274 | `sync_history` |
| 288 | `enemy_detailed_info` |
| 289 | `renew_enemys` |

## Tier 3 — Multiplayer / live-service — little value on a single-player private server

### Arena  (0/20)
| cmd | name |
|---:|---|
| 272 | `sync` |
| 275 | `reward_list` |
| 276 | `refresh_enemy` |
| 277 | `ranking_board` |
| 279 | `fight` |
| 280 | `freePK` |
| 528 | `sync_sp` |
| 531 | `reward_list_sp` |
| 532 | `refresh_enemy_sp` |
| 533 | `ranking_board_sp` |
| 535 | `fight_sp` |
| 784 | `sync_team` |
| 787 | `reward_list_team` |
| 788 | `refresh_enemy_team` |
| 789 | `ranking_board_team` |
| 790 | `fight_record_team` |
| 791 | `fight_team` |
| 792 | `hot_fightrecord_team` |
| 793 | `get_arena_team_bonus` |
| 800 | `enemy_team_info` |

### Guild  — COMPLETE (16/16)
Done 2026-08-18, and the reason this tier heading is now wrong for Guild: a guild of
ONE is not a degraded guild. `PlayerGuild` keeps no local state, so a synthesized
single-member guild renders correctly and is what opens the Guild Weekly, the Guild Pt
faucet, and missions 10042/10052.

Five of the sixteen need a second player to exist (`274 apply`, `275 apply_cancel`,
`276 check`, `277 kick`, `280 set_rank`) and are answered as **cmd 65535 + a
GuildRpcErrno**. That is not a cop-out: 65535 is the only branch of
`OnClientCmdReceived` that calls `PanelLoadingWaiting.Close`, so an error reply is the
only thing that lifts the overlay `apply` puts up before sending.

See `server/player_state/guild.py`.

### ChatRoom  (1/17)
| cmd | name |
|---:|---|
| 273 | `create` |
| 274 | `apply_private` |
| 275 | `join_public` |
| 276 | `check` |
| 277 | `kick` |
| 278 | `quit` |
| 279 | `disband` |
| 304 | `search` |
| 305 | `tag_search` |
| 306 | `members` |
| 307 | `applys` |
| 308 | `edit` |
| 324 | `chat` |
| 336 | `private_msg_by_name` |
| 337 | `private_msg_by_uid` |
| 338 | `public_msg` |

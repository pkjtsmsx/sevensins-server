# TitanStack game server — protocol scope

What the client does after "TAP TO START", and what a server must answer.
All names from the il2cpp dump (`Game.Player.Session.*`, `TitanStack.*`).

## 1. Entry bridge (web — already interceptable at our proxy)

**`id=6` = `AskTokenVerifyRequest`** (FrontendWebRequest) to
`7sin-web.ujj.co.jp/web_ob01_jp/login/service.php` (WebApi id/time/data/cs format).
- Request data: `access_token` (our Mars token), `bili_uid`, `mobile_os_type`,
  `sdk_version`, `device_id`, `client_version`, `relogin`, `channelToken`, `sessionid`, `uid`, `sdk_type`.
- **Response** `{errno:0, data:{...}}` fields the client reads (`OnApiOk`):
  `open_id`, `user_name`, **`token`** (the TitanStack login token), `first_login`, `session_status`.
- Errors: 37=SDK_TOKEN_VERIFY, 39=ASK_TITAN_TOKEN, 33=ACC_NOT_FOUND, 3=MAINTAINED.
- Our generic web addon currently 200s this but WITHOUT a `token` → client proceeds and
  then the TCP connect times out (Error 19). Needs a real `token` field.

**`ServerAddress` = {`_host` string, `_port` int}** — from the selected server
(DesignServerForm `_address` = "host,port"; JP data has dead `192.168.199.44,222x0`).
To redirect, point this at our own game server (patch the server row or the GetAllLoginServers reply).

## 2. Login chain (C#: `Game.Common.LoginUtil`)

`UJLogin(mars_token, playerID, address, relogin, cb)`
 → `AskTokenVerify` (id=6 above, gets `token`)
 → `LoginWithTitanToken(token, address, cb)`
 → `EstablishSession(address, username, password, cb)`
 → `StackCore.Connect(host, port, timeout)` + `StackCore.SendLogin(username, password, json_data)`
 → on success `OnSessionLoggedInEvent` → `SyncProcedure` (server pushes full player state) → home scene.

## 3. Wire protocol (`TitanStack.StackCore` + `NetCore`)

TCP socket (`AsyncSock`), **RC4-encrypted** stream with separate keys
`NetCore.KeyC2S` / `KeyS2C` (client→server / server→client; `RC4` class TypeDefIndex 8076).
Framed messages: `[Header][RC4(payload)]`, dispatched by a **type byte**:
- `MessageTypeLogin = 121` — `SendLogin(username, password, json_data)`
- `MessageTypeQueue = 122` — login-queue notifications
- `MessageTypeRPC   = 131` — gameplay RPCs

`StackCore` callbacks (`NetCoreEvent`): `on_connected`, `on_connect_timeout`,
`on_connect_error(errno,errmsg)`, `on_closed`, `on_message(byte type, Client msg)`, `on_message_error`.

### 3a. SOLVED — wire format (implemented in `server/titan_server.py`)

Recovered from `libil2cpp.so` (dump rebuilt into `re/il2cpp/`, see `re/` for the
tooling). Verified against a real captured login frame and accepted by the client.

```
frame = RC4_stream( [chksum:1][type:1][size:4 BIG-endian] + protobuf_body )
```

* **One RC4 keystream per direction per connection**, covering header *and* body of
  every frame — it is not re-keyed per message. Decrypt the 6-byte header, then
  continue the same stream through the body.
* `TitanStack.Header..cctor` (RVA 0x184C9A0) sets `ChkSumKey=0x5C`, `ChkSumMod=30`,
  `MsgTypeMul=3`, `ChkSumLen=1`, `MsgTypeLen=1`, `MsgSizeLen=4` ⇒ **HeaderLen = 6**.
* `calc_chksum` (RVA 0x184C434): `chksum = 0x5C ^ ((3*type + size) % 30)`.
* Keys (`NetCore..cctor` RVA 0x184E598, both 8 bytes, stored as
  `<PrivateImplementationDetails>` array-init blobs in `global-metadata.dat`):
  * `KeyC2S = de31b5197af8042c`
  * `KeyS2C = 5ab862fc15097d3e`

Body is a protobuf-net `titan.Client` (field numbers read out of the `ProtoMember`
attribute thunks — **not** sequential, so don't guess):

| field | name | type |
|---|---|---|
| 1 | `cmd` | uint |
| 11 | `login` | `Login` |
| 12 | `login_reply` | `LoginReply` |
| 13 | `queue_reply` | `QueueReply` |
| 31 | `rpc_pack` | repeated `Rpc` |

* `Login`      : 1 `username` (string), 2 `password` (bytes), 3 `json_data` (string)
* `LoginReply` : 1 `success` (bool), 2 `errno` (uint)
* `QueueReply` : `status`, `index`, `wait_time`
* `Rpc`        : `index`, `uid` (bytes), repeated `uint32/sint32/uint64/sint64/double/string`

Observed login frame (31 bytes on the wire): type=121, size=25, body
`5a170a13 "titan_token_1000001" 1200` = `Client{login:{username:<titan token>,
password:""}}`. The username is the `token` our web API returns from id=6.

Replying `Client{login_reply:{success:true, errno:0}}` as type 121 is accepted — the
client stops erroring and holds the connection open waiting for state.

### 3b. Getting the client to talk to us

The EN server address is baked into the client, and it **changed between builds**:

| client | serverId | server name | address |
|--------|----------|-------------|---------|
| EN 2.2.4 | 501 | 堕落天国 | `35.221.77.200`, ports ~22110-22113 |
| EN 2.2.7 | 601 | Fallen Eden | `35.199.50.150`, port 22111 |

The client picks it itself (`SceneLogin:EstablishSession: Game session establishing
(host:…, port:…, username:…)` in logcat) — it is *not* handed out by any web API, and
`GetAllLoginServersRequest` (id=105) carries no server list at all, only the
version/patchServer/maintain fields.

**It is NOT baked into the binary, though — it comes from the design pack, which we
serve.** `SceneLogin.OnStartButtonClicked` (0x3427968) fetches DesignServerForm, looks
the row up by PlayerSession's serverId, reads `_address`, splits it on ',' and builds the
ServerAddress. `FrontendWebRequest.Send` (0x17fcd84) does the same lookup for
`_web_api_address` and uses it as the web root `GetApiUri` concatenates module paths onto.
So both are data, and redirecting the client is a one-command data patch:

```
server/patch_server_row.py                     # -> 10.0.2.2,22110
server/patch_server_row.py --host 127.0.0.1    # e.g. for an on-device server
```

`_address` is `'<host>,<port>'` entries joined by `_`, and OnStartButtonClicked picks one
at **random**, so the patcher publishes exactly one entry to keep it deterministic. The
output keeps the same FILENAME — that name is the bundle's identity in the EN 2.2.7
manifest, not a content digest — so clear the device's Unity cache (or reinstall) to stop
it using a cached copy.

**The iptables DNAT rules this section used to require are obsolete** (they were:
`iptables -t nat -A OUTPUT -p tcp -d <old ip> -j DNAT --to-destination 10.0.2.2:22110`).
Verified 2026-08-09: with an empty nat OUTPUT chain the client reaches
`LOGIN username='titan_token_1000001'`.

If the address is wrong the symptom is a ~14 s stall then
`SessionException: (0x0001) Session operation timeout` and "failed to connect with the
server" — i.e. login itself succeeded, only the TCP connect failed.

### 3c. Mars SDK guest sign-in differs in 2.2.7

2.2.4's SDK (`sineng.153.3.9.0`) asked for the hashed account with cmd **43**
(`RequestHashedAccountIdByPlayerId`). 2.2.7's (`153.4.2.0.12`) uses cmd **69**
(`RequestHashedAccountIdV2`); both reply **42**. The V2 handler
(`re/mars-java/sources/com/userjoy/mars/net/marsagent/cast/p009byte/outer.java`) returns
early unless the reply carries `status == "0"` **and** `platformId == "9"` (9 =
OneClick/guest) — otherwise it only logs `NG... platform is not onclick` and never calls
`LoginByHashAccountId` (cmd 44). It then reads `"0"`=account id, `"1"`=player id,
`"2"`=is-new-account, `"3"`=OneClick password, `"4"`=password version (optional).

Symptom of a bare reply: the sign-in sheet shows "getting user data", then returns to
"Select a way to sign in" with nothing in the log. Handled in `tools/legacy_proxy/mars_addon.py`
`handle_cmd()` for cmds 69/40/41/72.

## 4. RPC surface (`TitanStack.ServerRpc` client→server, `ClientRpc` server→client)

`ServerRpc.<Subsystem>ServerCmd(uint cmd, List<int|uint|string|...> args)` — one method per subsystem:
Achievement, Arena, Backpack, Battle, Campaign, Challenge, Char, ChatRoom, Currency, Energy,
Friend, Gacha, General, Gm, Guild, JSAgent, Level, LoginBonus, Mail, Market, Notification,
OFA, Quest, ... (mirror `Player<Subsystem>ClientCmd` for pushes).
Serialized via **`RpcUtil`** — positional/indexed typed fields:
`push_uint32/sint32/uint64/sint64(+_array)`, strings, `pop_*` by index. NOT protobuf (protobuf-net
exists but for other data). An `Rpc` = command id + ordered typed values.

## 5. Build order (to get a new guest into the home screen)

1. ~~Serve `id=6` with a real `token`; point `ServerAddress` at our host:port.~~ DONE
   (`tools/legacy_proxy/mars_addon.py` id=6 branch + the iptables DNAT in §3b).
2. ~~Extract `Header` layout + RC4 keys; implement the framed RC4 TCP codec.~~ DONE — §3a,
   implemented in `server/titan_server.py`.
3. ~~Accept type-121 Login, reply login-ok.~~ DONE — client accepts it and holds the
   connection open instead of erroring.
4. ~~**SyncProcedure**: push a valid new-guest player state via `Player*ClientCmd`.~~ DONE —
   all 14 subsystems sync, the client reaches the home scene (`SYNC_REPLIES` in
   `titan_server.py`, rendered from `player_state.py`).
5. ~~Campaign battle.~~ DONE for stage 1-1 — see §6.
6. From there, per-subsystem `Player*ServerCmd` handlers = the actual game logic (large, incremental).
7. ~~Clear rewards + karma.~~ DONE 2026-08-03 — §6.
8. **NEXT: the login bonus.** Unusually, this one needs almost no reconstruction — the
   whole reward ladder is in the pack. `login_bonus` (42 rows) is
   `{_id, _group, _day, _book, _mail}`, and `_mail` points at a `mail` row carrying the
   actual payout as `_itemID` + `_param` (count) + `_expired_day`. Two groups, matching
   the two screens seen in game: **group 1 = 28 days**, **group 3 = 14 days** (the OB
   event), with the banner/text ids on `login_bonus_group`. Spot-checked against footage:
   28-day days 1-3 are mail 1001/1002/1003 = coin 50000, item 202 ×5, ダイヤ ×100, all
   exact; day 4 differs (item 102 ×20 here vs ★3 Trainer ×5 on screen), the same
   live-ops tuning drift as quest 31002's `_case_cnt`.
   Server-side and therefore ours to define: which group is active, the per-account day
   counter and what counts as a rollover, and claim bookkeeping. **Note the payout is
   delivered as MAIL**, so this depends on implementing `PlayerMail` properly — we
   currently stub it with an empty list and a zero unread count — and `_expired_day` (7)
   implies mail expiry. The client is already asking: `Request LoginBonus` appears in
   logcat with `DoSyncBonusData` sync timeouts, and cmd **339** on index `0x3a6083e0`
   repeats every ~10s against no handler.
9. **Helpers — not implemented, and it shows.** The battle-preparation screen should
   offer a helper and run a tutorial popup about them ("Available Helpers: 10", "these
   invincible Helpers can only be summoned in Story Mode 10 times per day"); ours shows
   neither. Three separate gaps, all ours:
   - `PlayerFriendHelpers` (server cmd 273 → reply **528**) currently sends two EMPTY
     string groups. `PlayerFriend.ReviceServerHelpersData` clears the static
     `PlayerFriend.Helpers` and then deserializes **one `PlayerInfo` per strarg** — a
     list of JSON strings, NOT one JSON array. So the helper list is simply empty.
   - `general_json`'s **`helperUseLimit` is 0**, which is almost certainly the "Available
     Helpers" count the panel shows as 10.
   - `char_json`'s **`helper` is `""`**, which is the source of the `helper uid error`
     warning on every single char sync: `PlayerChar.receivedHelperData` warns and returns
     when the uid is not in the char dict, and an empty string never is. That warning has
     been in the log since the beginning and is *this*, not a bug in the char sync.
   Whether the tutorial popup itself is gated on helpers being available is unproven.
   Doing this properly needs `PlayerInfo`'s [JsonProperty] keys extracted, plus a fake
   helper roster to hand out.

## 6. Campaign battle (implemented — `server/battle.py`)

The client is a **pure renderer**: the server decides turn order, damage, wave changes and
when the fight ends. Battle RPC index `0xFBC2FA08` server→client / `0xFA6D759E`
client→server, build shape B (`sint_msg`). **Battle requests carry their args in the
sint32 slot**, so `parse_rpc` must read protobuf field 15 (zigzag) or they look empty.

Flow for one stage:

| step | direction | message |
|---|---|---|
| stage picked | C→S | PlayerStage `Execute`(2) / `NewbieStage`(257) |
| | S→C | `Execute`(18) reply, then **`ServerStart`(1505)** with a `BattleDatas` json |
| scene up | C→S | `Ready`(100) / `StartTurn`(101) |
| | S→C | `WaveBegin`(1100) **once per wave**, then `StartTurn`(1101) with `intargs[0]=0` |
| | C→S | `Judge`(200) |
| | S→C | `Judge`(1200) for a player turn (shows the skill bar), or `Attack`(1201) for an enemy |
| player acts | C→S | `Attack`(201) `int[skillSlot, class]` `str[attacker, defender]` |
| wave cleared | S→C | `WaveEnd`(1506) `[result, wave, nextAVG, endAVG, startAVG]` + `{"dmgTbl":[]}` |
| | C→S | `NextWave`(502) → S→C `NextWave`(1503) with the new enemies |
| stage cleared | C→S | `BattleEnd`(505) → S→C **PlayerStage `EndReward`(23)** with a `BattleReward` |

Hard-won details (each cost a debugging cycle — do not "simplify" them away):

- **Unit order keys are sequential decimal strings from `"101"`** (players first, then the
  wave). The tutorial compares the tapped target against the literal `"103"`.
- `StartTurn`'s `intargs[0]` must be **0** so the client parks in SituationJudge; `1` sends
  it to TurnEnd, which instantly re-asks for a turn and spins the skill bar.
- `Judge` silently does nothing unless `strargs[0]` equals `PlayerGeneral.Uid`.
- `sk_overwrite` / `mod_overwrite` must be **absent** from `BattleDatas` — `""` is treated
  as an override and every character model resolves to `art/character/_01`.
- `all_mob_ids` / `all_skill_ids` are **preload lists covering every wave**; miss one and
  that wave's boss spawns with no model and the fight stalls.
- Every wave needs its own `WaveBegin` — `SetAllCollider()` only runs there, so skipping it
  leaves the new enemies unclickable.
- Damage uses the client's own curve, `CharFunction.GetDefendRatio` (`def/5000` under 500
  DEF — very shallow). Skill coefficients exist only in the localized note text.
- Skills are **ranked rows** sharing a `_group`; `DesignCharForm._skills` holds rank I.
  Cooldowns are `_cdTurn`; the blue bar is `scv` (0..100) and gates the ultimate. Enemies
  must be gated by both or bosses spam their ultimate.

Design tables are readable server-side via `server/design_data.py`, and rows the recovered
pack is missing (notably the battle formation table, DesignText row 499) are injected by
`server/patch_design.py`.

### Retreat (implemented 2026-08-03)

Menu → Retreat had no effect, so a battle could not be left. **The client was sending the
request all along** (`cmd 500` was sitting in our log, unhandled); we simply never replied.

```
onRetreatClick  -> confirm dialog, text 10083 (boss) / 10084 / 10085 (expedition) / 10000
CB_Retreat      -> PlayerBattle.Retreat()
Retreat()       -> PlayerBattleServerCmd **500**, NO args
server          -> **cmd 1500**, strargs[0] = a BtCollector json
HandleRetreat   -> BattleResultType = 2, dispatch BattleEvent 4  -> battle tears down
```

`Retreat()` is gated on `BattleData.BattleResultType >= 2`, which passes normally because
`BattleDatas..ctor` seeds that field with **0xFF** ("no result yet") and `HandleWaveEnd`
only overwrites it on the FINAL wave. `HandleRetreat` needs strargs with at least one
entry; `BtCollector`'s only `[JsonProperty]` is `type`, everything else arriving through
its ctor param `dmgTbl`, so we send `{"type":0,"dmgTbl":[]}` rather than risk a null ctor
arg. Retreating counts as a **defeat** (result type 2) — that is the game's own value, not
a side effect of our reply.

**Search lesson:** `CMD_RETREAT = 1500` was already defined in `battle.py` and unused, and
the method is plain `PlayerBattle.Retreat()` — filtering method names for
`ServerRPC*`/`Request*`/`Send*` walked straight past it. Start from the BUTTON handler
(`on*Click` on the panel) and follow the callback instead.

### Clear rewards (implemented 2026-08-03)

`BattleReward` carries **two** reward streams, and the popup is display-only — nothing is
granted unless the server grants it and pushes the matching sync.

**Ratings.** `_rating_datas1..4` are CSV rows that `DesignStageRow.PaserRatingData`
splits into ints: **`[type, item_id, count, threshold]`**. `[0]` picks the label out of
`DesignTextForm` and the label is formatted with `[3]`, which is what identifies `[1]`
and `[2]` as the reward pair. Types seen on campaign stages:

| type | text id | meaning |
|---|---|---|
| 1 | 11121 | "Stage cleared!" |
| 2 | 11122 | "Ally cast(s) defeated within `[3]` times" |
| 3 | 11123 | "Clear in at most `[3]` turns" |
| 4 | 11124 | "Clear with Kizuna quest's leading cast" (5..22 = other modes) |

Cross-checked against footage of a real 1-1 clear: stage 1101's `"1,1,10"` / `"2,1,1,2"`
/ `"3,1,3,18"` / `"3,1,5,12"` are exactly the 10 / 1 / 3 / 5 gems the panel shows, with
item 1 = ダイヤ. `ratingList` is index-aligned 1/0 over these rows.

**Drops** (`item_list`, rows `[_, item_id, count]` — the ctor reads indices 1 and 2 and
never 0). The stage table has **no** drop column: `box_rank` is a chest-rank pair.

*Count* is confirmed: **one drop per wave**, across two clears — 1-1 has three waves and
shows three icons, 2-1 has two and shows two.

*Contents* are NOT solved. 1-1 pays three 250 coin stacks, but 2-1 pays an emblem ×10
plus a character card, so "250 Mira per wave" was overfitted to the tutorial and is only
a placeholder. Real per-stage drop tables were live-ops data and need per-stage footage
to reconstruct, exactly like the karma payouts. Verified 2026-08-03 against a real 2-1
results screen: our Ratings block matches it exactly (trainer ×15, then 1/3/5, all four
ticked) and only the Drops row differs.

`bar_list` (per-character XP bars) stays empty — no XP curve in the pack, no footage.

**Where a granted item goes** is decided by `DesignItemRow._action`, with `_param1`
naming the target: `5` → currency `_param1` (item 1 → Cash 1, 2 → Mira 16, 4 → Guild
64), `6` → energy (item 5 → Action 1), anything else → a backpack item. This is a
server-side rule — the client has no grant path at all — inferred from the data lining up
with the CurrencyType/EnergyType enums. `player_state.grant_reward` implements it;
`grant_item` is now only for things that really are bag items. Quest 21001 pays
`10000 × item 2`, which is Mira, and used to become a 10000-deep backpack stack.

**The tutorial also grants Jacqueline silently** — char **11001** at star 4, level 1,
with no entry on the results panel. Guarded on already owning her so replays of 1-1 do
not mint copies.

### Karma (story decisions) — implemented 2026-08-03

Picking an option in an AVG scene pays currency plus **karma** (favour) with one
character. Karma is the `flv`/`fxp` pair on `CharIDData` — rank and rank progress.

Round trip, both on `PlayerStage`:

| dir | cmd | payload |
|---|---|---|
| C→S | **5** | `[avgID, optionIndex]` (`RequestServerAvgSelectOption`) |
| S→C | **21** | `[currencyType, currencyValue, charID, fexp]` — **exactly 4 ints** |
| S→C | **20** | AVG sync: `[lockedOption, unlockedDigits, 0, 0]` — also exactly 4 |

`HandleAVGChoice` bails on any other arg count, and `SetRewardInfo` is what lets the
scene continue, so a wrong length hangs the story on the choice. From
`OptionButton.SetReward`:

- `currencyType` → `GetCurrencyTypeSpriteID`, which maps **only type 1 (ダイヤ)** to a
  sprite (3001); every other type returns 0 and the icon stays blank.
- `charID` → a **`DesignRoleModelInfoForm` row**, i.e. an ordinary char id (an older
  note guessed avg_role ids like 101/201 — wrong). 0 leaves the portrait alone.
- `fexp` → the `+N` label, and picks the banner text **by magnitude**: `>=100` → 10086,
  `>=20` → 10087 ("Big Up!"), else 10088 ("Up!"). The "Karma Rank" part is static prefab
  text, so the banner is **not** evidence that a rank threshold was crossed.

Confirmed against three clips: +10 → "Up!", +20 → "Big Up!", +100 → the `>=100` branch.
(That third one reads "Super Up!" in the captured build while our pack's 10086 says
"Ultimate Up!" — an EN text difference between builds, not a payload difference.)

**What we have vs. what we must rebuild.** In the pack: the entire rank ladder —
`char_flv`, **4200 rows = 140 characters × 30 ranks** (`MaxFLv = 30`), each with
`_unlock_type`/`_unlock_v1` and `_bonus_item_id/_bonus_item_cnt`. 2240 rows pay an item
(1680 × 50 ダイヤ, 560 × 聖女の血); type 2 unlocks a bond quest by id, type 6 is a stat
bonus (tying to `DesignCharRow`'s `v_flv`). Missing, because it was server-side: **which
option pays what** — the `avg` design form is not even in our pack, so neither rewards
nor option text/branching are available. It is a **hardcoded per-(scene, option) table
that has to be rebuilt from video**, one clip per row: the three observations pay 5/10/15
gems for decisions 1/2/3 while the options picked were 1/2/2, so it is a function of
neither on its own. Also still unknown: the fxp-per-rank curve (`char_flv` says what each
rank *gives*, never what it *costs* — `player_state.KARMA_XP_PER_RANK` is a placeholder).

**A choice is permanent per difficulty.** Replaying a stage on the same difficulty
re-shows the scene with the previous option already locked in; only a different
difficulty lets you choose again. So the AVG sync reply's `intargs[0]` must be the stored
answer rather than 0, and a scene **pays only the first time it is decided**
(`player_state.set_avg_choice`).

**TODO for higher difficulties (not implemented).** Meeting the same scene on a *higher*
difficulty also **locks out the options already taken** — you choose from the remaining
ones, so a three-option scene offers two the second time and one the third. The mechanism
is already in hand: `intargs[1]` of the AVG sync is the unlock mask, and
`AvgUIOptions.UpdateAVGOptionLockState` reads digit *i* as `tag[1] / 10^i % 10` and
unlocks `_btnOptions[digit - 1]`. We currently send the constant `4321` (all four). Doing
this properly means storing the picks **per (scene, difficulty)** rather than per scene,
and building the mask from the options not yet used. Deferred until there is a second
difficulty to test against.

## 7. Roster and formations (implemented — `server/player_state.py`)

`PlayerCharData` drives every lobby character screen (cast list, formation, character
detail, battle preparation). It is rendered from `player_state`'s `roster`
(uid -> {id, lv, star, xp, plus}) and `formations`.

`CharData`, `DBCharData`, `CharIDData` and `FormationData` are all
`[JsonObject(MemberSerialization.OptIn)]`, so **only `[JsonProperty]` members cross the
wire** — the extra public fields (`EquipValues`, `curHp`, `FinalSkillList`, …) cannot be
populated from the server and stay null whatever is sent.

Wire keys (from the attribute thunks, none of which match the field names):

| type | keys |
|---|---|
| `CharData` | `dbdata`, `hp`, `atk`, `def`, `spd`, `cri`, `tgn`, `cdi`, `cdr`, `prc`, `ehit`, `eanti` |
| `DBCharData` | `uid`, `time`, `id`, `mod`, `lv`, `xp`, `plus`, `pxp`, `limit_book`, `limit_char`, `star`, `super_star`, `lock`, `equips_list`, `skin` |
| `CharIDData` | `flv`, `fxp`, `skin1..3`, `lv`, `star`, `super_star`, `plus`, `skill`, `score`, `kset`, `klv` |
| `FormationData` | `array` (slot uids), `sup` (1-based support slot, 0 = none) |

Counts that are structural, not stylistic — each one is a crash if wrong:

- **Six formations.** `PanelCharacterList.OnPreviousClick` wraps `_teamIndex - 1 < 0`
  round to the literal `5`, so teams are indexed 0..5.
- **Five slots per formation.** `InitFormationSelfTeam` allocates `new int[5]` for the
  per-slot support skills and walks the icon list against the CharList in lockstep. An
  empty slot is the **empty string**, not null and not a short list.
- **`sort_list` needs 14 entries**, each a `"<type>_<order>"` string that
  `PlayerCharData..ctor` splits on `'_'` and `uint.Parse`s.
  `InitSortOrderAndSortType` picks one entry per `_panelActionType`
  (0->0, 7->1, 1->2, 2->3, 3->4, 4->5, 10->8, 9/16/17->**13**) and reads `[0]` as the
  sort type and `[1]` as the direction. Too few and the formation panel throws before
  drawing anything.
- **Backpack infos (cmd 83) are positional.** `SetBackpackInfo` parses
  `List<List<int>>` and files entry *i* under dictionary key ***i+1***, each inner list
  `[capacity, quantity, buyCapacityLimit, buyCapacity]`. An empty array made the
  battle-preparation **Go button silently do nothing**:
  `CommonUtil.CheckAndShowReadyGoMsg` looks up BackpackType 1/4/8 with no containment
  check, and the `KeyNotFoundException` aborts the click handler. Eight entries cover
  the whole enum, and `capacity` must exceed `GetBpFixCount` or the bag reads as full.

### Star and super-star — one 12-rung ladder, two fields

`DesignCharRow._growStar` always has **12** entries and is two ladders stacked: rungs
0..5 are the normal star tiers (`10001..10006` for Lucifer), rungs 6..11 the super-star
ones (`110001..110006`). The pair that addresses them is:

- **`star` is 1..6**, never a rung index. `UpgradeDefine.RankUpMaterialCount` is an
  `int[6]` and `PlayerChar.UpdateCheckRankUpList` indexes it as `[star - 1]` on every
  char sync, so anything outside 1..6 throws (see §8).
- **`super_star` is an OFFSET added on top, 0..6** (`UpgradeDefine.MaxSuperStar`), not a
  flag. `DesignCharRow.GetCharGrowRow(star, super_star)` and `GetStatusPercentList` both
  compute the rung as **`star - 1 + super_star`**, falling back to rung 0 (with a log
  line) when that is past the end or lands on a 0. `isSuperStarOpen` is merely
  `_growStar[6] != 0`, i.e. "this character has a super ladder at all".

So a fully awakened character is `star: 6, super_star: 6` → rung 11. The formation panel
draws that as six stars plus a **`+6`** badge. `LightBattleChar` carries only `Star`, so
super_star never crosses the wire into a battle — it only moves which grow row the
server computes stats from (`battle.grow_rung`).

**Formation edits.** `PlayerChar.RequestServerFormation(index, list, support)` sends
cmd **274** with intargs `[index + 1, support, 1]` and the five slot uids as strargs.
Reply with cmd **530** (`receivedFormation`), intargs `[index + 1, 0]` and the stored
`FormationData` json: the handler does `formations[intargs[0] - 1] = <json>` and, when
`intargs[1] == 1`, additionally pops confirm message 19. The client does not apply its
own edit until that reply lands, so this is a real round trip.

## 8. Quests and the newbie tutorial (`PlayerQuest`)

`PlayerQuest` is **not part of the login sync set**. The client asks for it from
`PlayerStage.HandleSyncReplyCmd`, which calls `PlayerQuest.SendSyncCmd()` right after
installing the stage data. Leaving it unanswered leaves cmd 272 repeating in the log
forever and the newbie tutorial permanently stuck.

| direction | index | cmds |
|---|---|---|
| client → server | `0x132D9204` | **272** sync request, **257** claim (`RequestServerQuestCompleted`) |
| server → client | `0x12821D92` | **529** sync reply, **513** reward popup |

Build shape **C** (`pop_uint32(0)`=cmd, `pop_uint32_array(1)`=intargs,
`pop_string_array(0)`=strargs), from `PlayerQuestClientCmdRT$$build`.

- **529** is chunked (`intargs [chunk, total]`) and **incremental**: the handler merges
  `update_*` into the live collections and applies the `remove_*` lists, so re-sending
  the full set is harmless. A payload of exactly `{}` is legal — the deserialize is
  skipped but `AnalysisQuest()` still runs.
- **513** takes intargs in **`[questID, itemID, count]` triples** and needs at least 3.
- Send **every** `PlayerQuestData` field even when empty; each is applied only
  `if (field != null)`.

**`PlayerQuestData` WIRE keys are NOT the field names** — read them off the
`[JsonProperty]` thunks, not `dump.cs`:

| field | wire key |
|---|---|
| `update_db` | `db_u` |
| `remove_db` | `db_r` |
| `update_quests` | **`q_u`** |
| `remove_quests` | `q_r` |
| `sp_quests` | `sp_u` |
| `remove_sp_quests` | `sp_r` |
| `gq_db` | `gq_db` |
| `gq_ow` | `gq_ow` |

Sending `update_quests` is **silently ignored** — the deserialize succeeds, the handler
sees a null `update_quests`, skips the merge, and `QuestHasCompleted` stays false
forever. Symptom: the reward popup (513) plays correctly and the Goal panel then reverts
from "Receive" back to a claimable state, and a full relogin with the completion in the
payload changes nothing.

Beware the attribute-thunk heuristic: for fields carrying BOTH `[JsonProperty]` and
`[JsonConverter]`, the first string operand in the thunk is not the property name —
walk the thunk to the `JsonPropertyAttribute::.ctor` call and take the string loaded
just before it.

**Completion is stored in two places, by quest `_type`** (`QuestDefine..cctor`:
Main=1, Forver=2, Daily=3, EventForever=4, Auto=5, EventDaily=6, OFA=7, BP=8):

- `_type 1` → `QuestSyncQuestData.quests` (`Dictionary<int,int>`); `QuestHasCompleted`
  is a bare `ContainsKey(id)`.
- `_type 2` → `SPQuestSyncQuestData.sp_quests[id]`, and the check is
  **`status == 1`** (`LDR W8,[X0,#0x1C]`). `SPQuestDB_Data` = `{id, a_time, cnt, status}`.

Marking a type-2 quest in `update_quests` is silently ignored — that is what happened
to quest 21001, which stage 1-1 also satisfies.

### Clearing a stage does NOT complete its quest

This is the thing that cost the most time. A stage clear only makes the quest
**claimable** (`CheckQuestWillbeCompleted`, i.e. `GetQuestValue(row) >= CaseCnt`). The
player then taps "Collect Reward" on the Next Goal banner, the client sends **cmd 257**
with the ids, and only that turns into a real completion. Completing it server-side at
stage-clear time skips the claim the tutorial is waiting on.

### The forced newbie flow

`NewbieForceDefine..cctor`: `STAGE_STEP1 = 1101`, `QUEST_STEP1 = 31001`,
`QUEST_STEP2 = 31002`, `BATTLE_ANNOUNCE_STAGE = 2107`.

`PlayerStage.CheckNewbieForceStep()` returns a 2-element `List<int>`:

```
QuestHasCompleted(31002)              -> [-1,-1]   flow finished
GetStageRating(1101) == 0             -> [-1,-1]   no forced step
CheckQuestWillbeCompleted(31001)      -> [1, 1]    claim the 1-1 goal reward
QuestHasCompleted(31001)
    and NOT CheckQuestWillbeCompleted(31002)
                                      -> [1,2] / [2,0]   the GACHA step
otherwise                             -> [-1,-1]
```

`[2,0]` is returned when the gacha panel is already active, `[1,2]` when it is not.
Consumers: `PanelMain`/`PanelGacha`/`PanelGoalQuest.CheckForceTutorial` (PanelMain
returns true for `[1,1]` and `[1,2]`) and `PlayerQuest.ProcessForceTutorial`, which on
`[1,2]` hooks `PanelItemMsg`'s finish event and drives `PanelTutorial.ResetUI`.

Note the gacha step needs 31002 **not** completable, so no gacha implementation is
required for the step itself to appear.

### Quest design semantics

- `_case_id 1002` = "clear main story stage `_case_v1`" (516 rows, all named
  「攻略主線劇情 (N)」). `GetQuestCntFromDataType1` maps `_case_id` 0x3E9..0x3F9 to
  player getters; 0x3EA (=1002) is `PlayerStage.GetStageRating(_case_v1)`.
- `GetQuestValue` routes `_case_id` 1001-1999 to that switch, 3001-3999 to
  `GQ_DB_Datas[CaseKey]`, and otherwise falls back on `_case_type`
  (1 → `DB_Datas.db[CaseKey]`, 2 → `sp_quests[id]`).
- **Group 310 is the goal-quest chain**: 210 quests, `_order` 1..210, 85 of them
  `_milestone`. 31001 = order 1 (clear 1-1, milestone), 31002 = order 2 (pull gacha),
  31210 = order 210 ("28-10 Nightmare") — which is what the Goal panel falls back to
  when it finds no incomplete milestone.

### `LuaTableConverter` — how it actually reads dictionaries

`PopulateDictionary` branches on the JSON token:

- **Array** → treated as a Lua 1-based table: element *i* is stored under key **`i + 1`**
  (converted to the key type; enums go through `Enum.Parse`). Null elements are skipped.
- **Object** → `JsonSerializer.Populate(reader, target)` against the **existing**
  dictionary instance.

So an int-keyed dictionary with sparse keys (like `update_quests`, keyed by quest id)
can only be expressed as an object, and that path depends on the target dictionary
already existing. Suspected cause of `update_quests` not landing — unconfirmed.

### `PlayerStage` sync (cmd 1 → 17)

`HandleSyncReplyCmd` takes intargs `[chunk, total, _, SP_Points]`, assigns
`tempJsonStr` outright on chunk 1 and **replaces `PlayerStage.SyncData` wholesale**, so
re-pushing it is idempotent. It then calls `PaserAllCompletedData()` and
`PlayerQuest.SendSyncCmd()`, and dispatches `StageEventType` 1. Pushing it after a
battle is what makes a clear visible without a relaunch.

### SOLVED: the NullReference that pinned the Goal panel to step 210

**`PlayerShop` sync `intargs[1]` is `ShopVersion` and it MUST NOT be 0.**
`HandleLoginSync` (cmd 515) compares it against `PlayerShop.ShopVersion`, which starts
at 0, and **on a match skips the entire re-init** — so `_shopDic` (offset 0x20) is never
created and stays null.

That null detonates a long way from the shop:

1. `AnalysisQuest` calls `CheckPreCase` for **every incomplete quest**.
2. 288 quests carry `_pre_case 6`, which calls
   `PlayerShop.GetLimitShopGoodsHasBuy` — and that dereferences `_shopDic` on its
   first line.
3. The NullReference aborts `AnalysisQuest` **before `QuestSort`**.
4. `QuestSort` is what sorts `QuestsMap[QuestMain]` with **`QuestReverseOrderComparer`**
   (reverse `_order`, so the last element is order 1).
5. `PanelGoalQuest.RefreshBrowsableQuestList` walks that list **from the end backwards**,
   skipping completed quests, and selects the first incomplete one as
   `_newestQuestData`/`_selectQuestData`. Unsorted (ascending) that yields order **210**;
   sorted it yields order **1**.

Symptom: the Goal panel opened on step 210 ("28-10 Nightmare") with an "Unavailable"
button instead of step 1 with **Claim**, the forced tutorial locked the UI on that dead
button, and the post-`AnalysisQuest` `QuestEvent` dispatch never fired (so the
"We found something!" line never played).

Fix: send `[0, 1]` instead of `[0, 0]`. Confirm in logcat with `HandleLoninSync
version=1` followed by `AnalysisQuest` then `QuestSort` with no error between.

**General lesson:** a placeholder `0` in a version/sequence field is not inert — the
client may use it to short-circuit initialisation, and the resulting null surfaces in a
completely unrelated subsystem.

### Claiming a goal quest

`PanelGoalQuest.OnConfirmClick` (the **Claim** button) is what calls
`RequestServerQuestCompleted`. Other callers: `UIQuestItem.onQuestItemGetClick`,
`PanelQuest.onQuestListDragChange`, and the auto path in
`PlayerQuest.OnClientCmdReceived` (which fires for `QuestAuto`, type 5, from
`autoeCompletedQuestList`).

### RESOLVED: why the claim did nothing (2026-08-02)

The claim request was always fine (`cmd=257 int=[31001]`), and cmd 513's reward popup
rendered correctly. The completion simply never persisted because our 529 used the key
`update_quests` instead of **`q_u`** (see the table above).

`PlayerQuest.GetQuestState(row)` returns **3** if `QuestHasCompleted`, **2** if the row
is in `willbeCompletedQuestList`, else 1/0 from `_pre_quest`.
`PanelGoalQuest.OnConfirmClick` only acts on **2** (send the claim) and **1** (navigate)
— at state 3 the button is inert. So once the completion lands, the panel should move on
rather than re-offering Receive.

Also note `QuestHasCompleted` branches on **`_case_type` (DesignQuestRow +0xA8)**, NOT
`_type` (+0x94): `_case_type 1` -> `quests.ContainsKey(id)`, `2` -> `sp_quests[id].status
== 1`, anything else -> always false. (Verified in the disassembly: the branch reads
`LDR W8, [X0,#0xA8]`.)

**FIXED 2026-08-03.** `complete_quests` and `complete_stage_quests` both routed on
`_type` and now route on `_case_type` (`player_state._quest_is_sp`). Note
`complete_stage_quests` is currently **dead code** — nothing calls it, only comments
mention it — because clearing a stage does not complete anything; the claim does, and
that goes through `complete_quests`. Verified live: claiming the 1-1 goal files 31001 and
grants its 10× item 205 to the bag. That claim does not *discriminate* between the two
columns though (31001 is `_type 1`/`_case_type 1`); 21001 is the case that does. The two columns
disagree constantly — 459 rows are `_type 2 / _case_type 1` — and quest **21001**, the
second quest that clearing stage 1-1 satisfies, is one of them: it was being filed under
`sp_quests` where the client never looks, so it could never read as complete. Clearing
1101 now files both 21001 and 31001 in `quests`.

### OLD NEXT STEP (superseded)

The Goal panel now opens on **step 1** with a live **Claim** button, and the claim round
trip fires correctly — the server logs
`RPC index=0x132d9204 cmd=257 int=[31001]` and replies `513` + `529`
(`quests claimed: [(31001, 205, 10)]`). **But the UI does not advance**: tapping Claim
appears to do nothing.

So the request works and the reply is the problem. In likely order:

1. **`update_quests` is probably not landing.** If it were, `QuestHasCompleted(31001)`
   would flip and `CheckNewbieForceStep` would move from `[1,1]` to the gacha step. The
   prime suspect is `LuaTableConverter.PopulateDictionary`: a JSON *object* is handed to
   `JsonSerializer.Populate` against the **existing** dictionary instance, so it does
   nothing if that dictionary is null. `QuestSyncQuestData.quests` is created by the
   lazy `get_Quest_Datas`, but the merge loop in `OnClientCmdReceived` only calls that
   getter *inside* the `foreach` over `update_quests.Keys` — so verify the deserialize
   itself populated `Data.update_quests` at all.
   Quick test: after a claim, reopen the Goal panel — if step 1 still shows Claim, the
   completion did not persist.
2. **cmd 513 may need more than we send.** It requires `intargs.Count >= 3` and reads
   `[questID, itemID, count]` triples; it looks up the item in `DesignItemForm` and only
   pops the reward (and plays `_avg_id`) when that lookup succeeds. Item 205 x10 should
   be valid — confirm the row exists.
3. Check logcat for a fresh exception at the moment of the tap.

### Still open

**Nothing — the login path is clean as of 2026-08-03.** All 14 subsystems sync with no
`event execution error` of any kind, and `AnalysisQuest` → `QuestSort` run every time.
The only remaining line is the `helper uid error` *warning*, which is correct behaviour:
`PlayerChar.receivedHelperData` warns and returns when the helper uid is not in the char
dict, and we do not send a helper.

**RESOLVED 2026-08-03 — the NullReference after `PlayerGeneral info synced`.** It was
not the info sync at all. PlayerGeneral answers THREE requests at login (256 → 512 info,
257 → 513 tutorial, 259 → 514 game rule); the info handler logged and returned fine, and
the exception came from the *next* queued reply. `HandleSyncGameRuleCmd` deserializes
`strargs[0]` into `GeneralSyncGameRuleData` and then iterates **five `List<string>`
fields with no null check**, `Decimal.TryParse`-ing each entry into a `List<Decimal>`:

| field | offset | wire key |
|---|---|---|
| `SoulfragEnhanceCoinMagnification` | 0x70 | `soulfrag_enhance_coin_magnification` |
| `SoulfragEnhanceDustMagnification` | 0x78 | `soulfrag_enhance_dust_magnification` |
| `SoulfragEnhanceCoinCharRarityMagnification` | 0x80 | `soulfrag_enhance_coin_char_rarity_magnification` |
| `SoulfragEnhanceDustCharRarityMagnification` | 0x88 | `soulfrag_enhance_dust_char_rarity_magnification` |
| `SoulfragEnhanceItemCharRarityMagnification` | 0x90 | `soulfrag_enhance_item_char_rarity_magnification` |

`"{}"` deserializes to a non-null object with all five null, so the first dereference
threw. **Empty arrays are enough** — each loop exits immediately on size 0. The keys are
snake_case and do not match the C# field names (read off the `JsonProperty` thunks, e.g.
`0x1043400`). Built by `player_state.game_rule_json`.

**NEW IN 2.2.7 — `super_limit_define`.** `GeneralSyncGameRuleData` gained a sixth field
that matters, `SuperLimitDefineData SuperLimitDefine` at 0xB8 (thunk `0x11520D0` in the
2.2.7 binary → key `super_limit_define`), with members:

| field | thunk (2.2.7) | wire key |
|-------|---------------|----------|
| `maxSuperLimit`  | 0x11293EC | `max_super_limit` |
| `personalEffect` | 0x1129424 | `personal_effect` |
| `teamEffect`     | 0x112945C | `team_effect` |

Omit it and `PlayerGeneral.SuperLimitDefine` is null, which does **not** blow up the sync
— it blows up later, in `PanelCharacterInformation.InitCharInfo`, whose four new
super-limit widgets (`_lbPopSuperLimitTitle`, `_lbPopSuperLimitEffect`, `_tsSuperLimit`,
`_goSwitchToSuperLimit`) dereference it. Symptom: the **cast list hangs** the moment you
tap a character, with a `NullReferenceException` under
`PanelCharacterList.ListIconOnClickAction`. Another instance of the rule that the panel
that crashes is rarely the payload that is wrong.

`DBCharData` gained `super_limit` (0x38, thunk `0x1152850`, key `super_limit`) in the same
release — the only wire change to per-character data between 2.2.4 and 2.2.7.

`player_state.SUPER_LIMIT_DEFINE` currently sends `5 / 0 / 0`, which is **a placeholder,
not the real config**. The cap is non-zero on purpose: `CharData.superLimitEffect` is a
float derived from these and a 0 cap risks a divide-by-zero. Calibrate from
`CharData.get_isSuperLimitAvailable` / `get_superLimitEffect` — both exist only in the
2.2.7 binary (`re/apk227/lib/arm64-v8a/libil2cpp.so`, dump in `re/il2cpp227/`).

*Reading the keys without IDA:* these thunks are `adrp x0, <page>` / `add x0, x0, #<off>`
followed by a tail-call, and the computed address is a plain C string in the binary — a
few lines of capstone over the `.so` will read any `JsonProperty` key, including from a
build IDA does not have loaded.

**RESOLVED 2026-08-03 — the `Index was outside the bounds of the array`.** It was NOT
the shop payload; that hypothesis was wrong. The timestamps put it *before*
`HandleLoninSync`, inside the char sync: `PlayerChar.receivedSync` →
`UpdateCheckRankUpList`, which for every roster entry does

```
if (star != 6) { ... UpgradeDefine.RankUpMaterialCount[star - 1] ... }
```

against a **static `int[6]`** (`UpgradeDefine..cctor`, `0x17c23a0`), with the array
bounds check hoisted above the branch — so any `star` outside 1..6 throws. We were
sending `star: 12`. `StackCore.poll` catches it and logs it as `event execution error,
message=…`, which is why there was never a stack trace. See §7 for the star/super_star
split; fixed by `player_state.MAX_STAR` plus the roster migration.

Unverified: whether `update_quests` actually lands. `LuaTableConverter.PopulateDictionary`
sends a JSON *object* through `JsonSerializer.Populate` against the **existing**
dictionary, so it depends on that dictionary already existing. If a claim completes but
the step does not advance, this is the next thing to check.

Note: only 51 of the 53 design forms in the pack are ever loaded — `battle_pass`,
`editor_msg` and `shop_goods` are not. `battle_passV2` is the only one of those names
referenced anywhere in the binary, so `DesignBattlePassForm` binds to it and is never
null; `battle_pass` is a dead asset.

## 8b. Shop (`PlayerShop`) — login sync solved, store panel NOT usable

**`HandleLoginSync` is cmd 515** (514 falls through to "Unknown RPC cmd"; stacked case
labels made them look like one handler). Args:

- `intargs[0]` — non-zero dispatches `ShopEvent` type 12 instead of type 1.
- `intargs[1]` — **`ShopVersion`, and it must differ from the client's current value**
  or the guard at the top of the handler skips the entire re-init, leaving `_shopDic`
  null. That null is what used to blow up far away in `AnalysisQuest` (§8).
- `strargs[0]` — **`List<List<int>>`, one row per shop**, each `[id, beginTime,
  endTime]`. `PlayerShop.ShopData` reads exactly those three back through `get_Id` /
  `get_BeginTime` / `get_EndTime`; title, subtitle and banner come from the
  `DesignShopRow` the ctor looks up by id.
- `strargs[1]` — `Dictionary<int, Dictionary<int, Dictionary<int, int>>>`
  (`FreeGoodsDic`, 0x50). **Required** — the handler bounds-checks `_size > 1` before
  touching it — but its deserialize is skipped when the string is exactly `"[]"`.

(An older note here had strargs[0] and [1] transposed; the deserializer generics at
`0x3E4E4D8` / `0x3E7E890` settle it.)

`GetItemAvailable(begin, end)` is `(begin < 1 || now >= begin) && (end < 1 || now <
end)`, so `[id, 0, 0]` means "always open". `DesignShopForm.Type` splits the 2146 rows:
**1** = the four store tabs (ids 1/2/3/5, the ones that land in `NormalShopList`), **7**
= the cash gacha shop (99999), **2**/**3** = the per-goods limited shops that quest
`_pre_case 6` looks up via `GetLimitShopGoodsHasBuy` (a dictionary miss returns false,
so those need not be synced). Only send ids that exist: a missing `DesignShopRow` sets
`PlayerShop.AnyRowNull`, after which `get_NormalShopList` pops a confirm dialog and
returns **null** forever.

**Why the shop list is currently sent EMPTY** (`player_state.SHOP_GOODS_IMPLEMENTED`):
with a non-empty list the store panel gets far enough to call
`PanelWaitingBlock.Open(-1.0, 0)` — an indefinite input-blocking overlay, no timeout —
and then waits on a goods sync we do not serve, so the client has to be restarted.
With an empty list `PanelStore.OnEnterItemList` instead throws ArgumentOutOfRange
indexing `NormalShopList[_curShopIndex]` *before* opening anything modal. Neither is a
working store; the empty list merely does not trap the player.

To finish the store: `PlayerShop.SendSyncShopGoodsCmd(shopID, syncBaughtData)` is
**server cmd 260** (`0x104`) on the shop index, sent from `PanelStore.EnterSpecificStore`
and `OnClickStoreBanner`. The reply is cmd **516** (`HandleShopSync`), which routes into
`DeserializeGoodsAndBoughtData`: `intargs[0]` = shop id, `strargs[0]` =
`Dictionary<int, GoodsBuyData>` (skipped when it equals `"[]"`), and then
`DeserializeShopGoodsData(shopData, strargs[1], strargs[2])` — `strargs[1]` is the goods
`List<List<int>>`, `strargs[2]` an optional `Dictionary<int, string>` read only on the
international build. Two strargs is legal (`strargs[2]` becomes null).

## 9. Gacha (`PlayerGacha`) — working (tutorial flow complete)

The tutorial's final step opens the gacha and does a 10-summon costing 10 Awaker
Scrolls (item 205 x10, exactly the quest-31001 reward).

**There is NO gacha design form in the pack** — box definitions were live-ops server
data. Everything below is structure recovered from the client; the *values* in
`player_state.gacha_json` are synthesised placeholders.

`GachaRpcServerCmd`: Sync=1, GetDropInfo=3, CheckBox=4, DrawV2=5, RedrawBoxDoGetDraw=20,
RedrawSave=21, SyncMonopoly=33, RollRandDice=34, RollSpecDice=35, SetWishList=36,
SyncRouletteDrop=49, SyncRoulette=50, DrawRoulette=51.

`GachaRpcClientCmd`: Sync=257, Draw=258, GetDropInfo=259, DrawTrueBox=260,
DrawFailed=261, RedrawBoxDoGetDraw=277, RedrawSave=278, SyncBoardMonopoly=289,
SyncPlayerMonopoly=290, RollDiceSuccess=291, RollDiceFail=292, SetWishList=293,
SyncRouletteDrop=305, SyncRoulette=306, DrawRouletteSuccess=307, DrawRouletteFail=308,
TestLog=2305, errno=2457.

`ReceiveSyncGacha` (cmd 257): `strargs[0]` = `List<UnlimitGachaBox>`, intargs =
`[TimeStamp, PassSpLock]`. It ends with **`PanelLoadingWaiting.Close()`**, so any
exception on this path (or in the panel it drives) leaves the gacha screen stuck on
"Data Loading..." forever. That is the signature to look for.

### `UnlimitGachaBox` wire names

`BoxID`->`id`, `SortOrder`->`sort`, `ImgID`->`img`, `BannerID`->`banner`, `Type`->`type`,
`SpLock`->`sp_lock`, `Newbie`->`newbie`, `Redraw`->`redraw`, `DrawSum`->`drawsum`,
`LeftCount`->`left_cnt`, `MaxCount`->`max`, `DeltaTurn`->`dturn`,
`IsAlreadyCheck`->`nflag`, `LeftCnts`->`data`, `CostV2Tbl`->`cost_tbl`,
`DiscountTbl`->`dc_tbl`, `Daily`->`daily`, `DrawStepTbl`->`step_tbl`,
`StepCostType`->`step_cost`, `EyeballTbl`->`eyeball_tbl`, `StepHint`->`step_hint`,
`IsCharOnly`->`is_char_only`, `EndLeftTime`->`dendtime`, `PickUpCharDic`->`pick_up_tbl`,
`CycleBonusTbl`->`cycle_bonus_tbl`, `missTACnt`->`missTA_cnt`, `goalTACnt`->`goalTA_cnt`,
`WishCount`->`wish_cnt`.

**Reading these thunks**: the property name is the string on the **ADRL**. The `BL`
that follows resolves to a bogus `"f"`, and for fields carrying both `[JsonProperty]`
and `[JsonConverter]` a naive first-string scan yields `"h"`. Both burned time today.

### Panel layout: honeycomb vs summon banner

`UpdateGachaInformation` calls `UpdateBoxGacha` **unconditionally**; the layout is
chosen at the top of `UpdateBoxGacha` (0x148f024):

```
SetActive(panel.field_0x240, false)      ; honeycomb root, hidden by default
box = panel._boxList[panel._curIndex]
if (box.LeftCnts /* +0x50 */ == null) return;   ; stay hidden
SetActive(panel.field_0x240, true)       ; box-gacha honeycomb SHOWN
```

So **`data` (LeftCnts) must be null for a normal summon banner** — sending an array is
what selects the honeycomb. The `length is not match! obj length = N, but UI length = M`
check, and the "each entry needs >= 4 ints" rule, only apply once it is non-null; they
are box-gacha requirements, not banner ones.

### Art

`UIBackground.SetBG(spriteID)` is passed `box + 0x18` = **`img` (ImgID)**, a
**`DesignSpriteForm` row id** (not an asset path). `banner` (BannerID) is the sidebar
tab sprite. Banners identified on screen:

| sprite | banner |
|---|---|
| 761 | 大罪召喚 Grand Sins |
| 762 | 魔星召喚 Awaker Summon |
| 763 | 初デビューガチャ (the TUTORIAL banner) |
| 764 | 美德天使限定 Virtue Angels |
| 765 | 天使召喚 Angel Summon |
| 766 | 魂鏡召喚 Soul Mirror (Sins) |
| 767 | 魂鏡召喚 Soul Mirror (Angels) |
| 769 | 御三家 Big Three, 100-pull limited |

Tabs are `atlas_banner_gacha_tab01` sprites 771-774 and `_tab01a` 775-779.

**`SEVENSINS_BANNER_PREVIEW=1`** publishes one box per unidentified sprite so a whole
batch can be flipped through in a single run, instead of one restart per guess (same
idea as `SEVENSINS_SLOTS` for battle formation positions).

### `cost_tbl` (CostV2Tbl) row layout — CONFIRMED

`[drawType, costCat, itemId, price]`

| index | meaning |
|---|---|
| `row[0]` | drawType. `CMP row[0], #1` then `CSINC W22, #10, WZR, NE`: **== 1 gives a 1-draw, anything else gives a 10-draw**. (CSINC returns Wn when the cond holds, else Wm+1 — easy to invert.) |
| `row[1]` | costCat. With row[0] it picks the button widget: **index = `row[0] + 2*row[1] - 3`**, bounds-checked against a widget list. These are small selectors, NOT prices — `[10,10,...]` yields 27 and throws `Index was out of range`. |
| `row[2]` | cost item id, read straight into `DesignItemForm.GetRow` (0 => `row ID 0 not found`). `UpdateGachaToken` treats `(1 << row[2]) & 0x803` (ids 0/1/11) as currency rather than an item. |
| `row[len-1]` | the price returned by `UnlimitGachaBox.GetCost`. |

`GetCost(i)` returns `row[DiscountTbl[i] - 1]` when `DiscountTbl` covers `i`, else
`row[len-1]`. **Keep `dc_tbl` empty** or the price slot is mis-selected.

Working tutorial value: `cost_tbl = [[2, 1, 205, 10]]`, `dc_tbl = []` -> widget index 1,
a 10-summon costing 10x item 205 (Awaker Scroll). Renders as "10 連抽 | scroll 10".

### A banner has up to FOUR cost buttons — `row[1]` picks the pair

`widget index = row[0] + 2*row[1] - 3`, so `row[0]` (1 = single / anything else = 10-pull)
and `row[1]` (the cost *category*) address a 4-slot button strip:

| row[0] | row[1] | slot | Awaker Summon shows |
|---|---|---|---|
| 1 | 1 | 0 | `1 Summon` — **Free, Daily Limited** |
| 2 | 1 | 1 | `10 Summons` — 600 gems |
| 1 | 2 | 2 | `1 Summon` — 1 Awaker Scroll |
| 2 | 2 | 3 | `10 Summons` — 10 Awaker Scrolls |

So category 1 is the premium/currency pair and category 2 the ticket pair. Banners differ
in how many they populate: the tutorial box has only the single 10-scroll button, our
standing banners currently emit two. **We emit two everywhere; only Awaker's real layout
is known.** The free daily needs a per-day counter the server does not yet track.

### Milestone rewards and the free daily pull — fields recovered, not implemented

The Awaker banner also shows a **cumulative pull reward track**: "NEXT 200000 / TO NEXT
REWARD: 10 TIME(S)", with rungs at 10 / 40 / 60 / 90 / 130 pulls paying 200000 gold,
30x a card item, 500000 gold, 375x a blue item, and a card. The backing fields are on
`UnlimitGachaBox` (dump.cs ~447372):

| C# field | off | meaning |
|---|---|---|
| `DrawSum` | 0x30 | lifetime pulls on this box — drives the track |
| `DrawStepTbl` | 0x70 | `int[][]`, the rungs |
| `StepCostType` | 0x78 | which counter the rungs are measured in |
| `StepHint` | 0x88 | the "TO NEXT REWARD" hint |
| `EyeballTbl` | 0x80 | (separate; the pity/spark table) |
| `Daily` | 0x68 | daily-limited flag |

The **free daily pull** is a sibling class, `UnlimitGachaBoxPTBL` (~447483):
`DayDrawSum`, `DrawSum`, `DeltaTime`, `DrawMax`, `CostItemId`, `CostItemNum`,
`NextDrawTime`, with properties `IsLimitDraw` / `IsCooldowning` / `RemainDrawCount` /
`CanDraw` / `IsCostItemEnough`. So the free daily is a cooldown+quota record, not a
cost row — the cost strip's slot-0 button just reads it.

We send `step_tbl` / `eyeball_tbl` empty and never populate PTBL, so neither the track
nor the free daily works. Note the wire names above are the **C# field names**; the
`[JsonProperty]` names still need the ADRL read (see `decompile-dont-guess`) before any
of this can be sent.

### Rewards are NOT granted by the reward popup

Quest cmd 513 (and presumably the gacha result) only *display* items. Nothing reaches
the bag unless the server puts it there. `player_state.grant_item` writes into
`state["backpack"][cbpType]` and `complete_quests` now deposits a quest's
`_item_id` x `_item_cnt` on claim. `BackpackItemData` wire keys are the field names:
`sid` (slot id), `iid` (item id), `amount`, `uid`, `attr`; the payload wraps them in
`{"sid": {...}}`. `ClientBackpackType`: StorageNormal=1, StorageEquipment=2,
StorageSoulfrag=3, StorageBloodpact=4.

### Constraints found so far

- **`data` (LeftCnts) length must equal the panel's slot count (10).**
  `PanelGacha.UpdateBoxGacha` compares them and pops
  `length is not match! obj length = N, but UI length = M`.
- **Each `data` entry needs at least 4 ints** — the bounds checks escalate
  `_size > 1`, `> 2`, `> 3`, reading `inner[1]`/`inner[2]`/`inner[3]`
  (+0x24/+0x28/+0x2C).
- **Do not use BoxID 101 or 102.** `RouletteBoxId` reserves them
  (`BaseBox=101`, `ItemCostBox=102`) and the client renders those as the roulette
  honeycomb board rather than a summon banner. This also explains the long-standing
  `boxId=101, roulette info is empty` warnings.
- **`cost_tbl` rows need `_size > 2`**; `UpdateGachaToken` reads `row[2]` as a cost
  TYPE — `(1 << row[2]) & 0x803` (types 0/1/11) means currency, anything else is an
  item looked up in `DesignItemForm`. Layout beyond index 2 unverified.
- `nflag` (IsAlreadyCheck) = 0 makes the client fire CheckBox (cmd 4). It is
  fire-and-forget — `RequestAlreadyCheckGachaBox` sets the flag locally straight after
  — so it is NOT a source of hangs; set 1 to avoid the round trip.

### The cast, via `_alignment`

The pack never labels the character groups; `_alignment` does it (verified against the
wiki's own 5*/4* Awaker lists, which match these counts exactly):

| `_alignment` | n (with `_order`) | who |
|---|---|---|
| 100 | 10 | Sins (5*) |
| 101 | 7 | Virtues (5*) |
| 102 | 4 | Riders (5*) |
| 103 | 28 | **5-star Awakers** |
| 104 | 15 | **4-star Awakers** |
| 9001 | 168 | **3-star fodder** (葛利姆 / 小丑 / 影魔操偶師, ids 30011-42381) |

**`_rarity` is one BELOW the in-game display rarity for Awakers/fodder** (103 reads 4,
104 reads 3, 9001 reads 2). Trusting `_rarity` directly rolls the wrong tiers.

**`_order == 0` rows in the 1xxxx band are 分靈 "spirit" duplicates** of the main cast
(10000 = 路西法分靈). They are not cast members: rolling them yields cards that render
greyed out on the result screen and never appear in the formation list.

Only Awakers + fodder come from the newcomer banner -- no Sins, Virtues or Riders.
Observed tutorial pull: **1x 5*, 1x 4*, 8x 3* fodder**, and duplicate fodder is normal.

### The draw

`RequestServerDrawV2(index, sindex)` -> server cmd **5**, observed args `[boxId, sindex]`.
Reply is client **258** (`ReceiveDraw`):
- `intargs [ok, removeBox, a, b, c]` -- >=5 entries; `[0]` must be 1 or it is a failure,
  `[1] == 1` removes the box.
- `strargs [updatedBoxJson, resultsJson]`, optional `[2]` = a newly-appearing box.

**Result row = `[objectType, id, count, star]`** (`GachaObjectType.Char = 1`).
`PanelGacha.OnGachaDraw` bounds-checks `_size > 1` then `> 3` and builds
`CharData(id = row[1], star = row[3], level = 1, plus = 0, ...)`. Two-element rows throw
`Index was outside the bounds of the array`.

`star` must point at a NON-ZERO rung of the RAW `_growStar` array -- the client indexes
`_growStar[star - 1]` directly. Some rows (e.g. 10032) have an all-zero ladder, which
yields `DesignCharGrowForm row ID 0 not found`. Note `battle._default_star` filters the
zeros out and then indexes the *filtered* list, so it can disagree with the client;
`player_state._gacha_star` reads the raw array instead.

### Redraw ("first gacha")

Set `redraw: 1` for the re-rollable tutorial box. Then:

| button | request | reply |
|---|---|---|
| Try Again | a fresh `DrawV2` (cmd 5) | 258 as above |
| **Collect!** | **`RedrawBoxDoGetDraw` (cmd 20)** | **277**, `intargs [boxId]` |
| (history) | `RedrawSave` (cmd 21) | 278, `intargs [boxId]`, `strargs[0]` = `List<List<int>>` -> `box.History` |

**cmd 20 is the COMMIT** -- "do get draw", i.e. actually take the roll. It is what
Collect! sends; cmd 21 never fires in this flow. So the draw itself must NOT grant or
charge: hold the roll pending and commit on 20, or the player is charged for rolls they
discard (and free rolls once they run out, if the spend result is ignored).

**Push the updated collections BEFORE the 277 reply.** The 277 is what returns the
client to the Goal panel; although `PanelGoalQuest.OnQuestSynced` does `MarkUIDirty`, a
sync landing after the panel has rebuilt is missed and the step only appears after
leaving and re-entering.

### The summon animation

`ShowGachaAnim(data, isItemBox)` **early-returns** when `isItemBox` is set: it closes the
loading panel and calls `ShowUIGachaResult(1, 1)` without ever reaching
`LoadGachaAnimRes`. `ReceiveDraw` dispatches `!curGachaBox.IsCharOnly` in the GachaEvent
args, so **`is_char_only` must be true for a character banner** -- false makes every pull
take the item path and skip the animation entirely. (The game does have an item gacha --
"soul mirrors" -- which is what that flag is for.)

Which animation plays is a separate gate: `get_resourceName` returns **`GachaAnimation`**
when `nowDrawNum > 0` and **`GachaAnimation_Simple`** otherwise, and
`isSimpleAnimation = nowDrawNum < 1`. `GachaResultData.nowDrawNum` defaults to -1 and
`ReceiveDraw` supplies `Redraw == 1 ? -1 : LeftCount`, so a redraw box always gets the
simple variant. The animation bundles themselves are present
(`ngui_animation_gacha_gacha_all`, `ngui_prefabs_gachaclips_common`, `art_gacha_*`).

### Quest counters ("do X N times")

`_case_type 1` quests resolve through `DB_Datas.db[CaseKey].total_cnt`, i.e. a counter,
not the completed-set. Two pieces:
- `DesignQuestRow.get_CaseKey` formats **`"{case_id}_{case_v1}"`**, or
  `"{case_id}_{case_v1}_{case_v2}"` when `_case_v2 >= 1`. Quest 31002 (pull gacha) is
  therefore key **`"13_0"`**.
- `QuestDB_Data`'s wire key is **`total`**, not `total_cnt`.

`player_state.quest_db` holds these and renders into `db_u`.

### Testing: `server/reset_tutorial.py`

The newbie flow is ONE-WAY -- completing quest 31002 ends it permanently with no in-game
way back, so testing the gacha step needs the account rewound first:

```
python reset_tutorial.py gacha    # drop 31002 + counter -> back at the gacha step
python reset_tutorial.py claim    # also drop 31001      -> back at the goal-reward claim
python reset_tutorial.py battle   # also clear stage 1101 -> tutorial battle runs again
python reset_tutorial.py all      # brand new account
```

### Box list is server-state dependent

Before the tutorial pull the sync carries ONE re-rollable newcomer box
(`redraw: 1, newbie: 1`); once it has been committed (`gacha_count > 0`) it carries the
standing banners instead -- Awaker Summon / Grand Sins Pick Up / Soulmirrors Limited --
which is what the live game shows. `player_state._gacha_box()` builds them.

**Ordering hazard:** a reply that resolves an object by id must be sent BEFORE any sync
that removes that object. `ReceiveRedrawBoxDoGetDraw` looks up
`GachaBoxIDToIndex[box_id]`, so pushing the post-tutorial box list (which no longer
contains the newcomer box) before the 277 made the lookup throw and Collect! silently do
nothing. Note this is the OPPOSITE of the goal-panel case, where the quest sync has to
go first so the panel rebuilds with fresh data. Sending the gacha sync last also still
refreshes PanelGacha's scroll label, which only redraws off the gacha sync path
(`UpdateGachaToken`) -- a backpack sync alone updates the data but leaves the label stale
until the panel is re-entered.

### Sync payloads must be rebuilt per request

`build_sync_replies(state)` renders every subsystem's payload from the current state,
and it was being built **once at login** and cached for the connection. So *any*
mid-session change -- roster, backpack, quests, stage progress, gacha state -- was
invisible to a later sync request. Symptom: re-entering the gacha screen fell back to
the tutorial box even though `gacha_count` had already advanced. It is now rebuilt per
request. This very likely also underlies several other "only updates after I leave and
come back" symptoms seen while building the tutorial.

### Status (2026-08-02)

**The newbie tutorial runs end to end**: clear 1-1 -> claim the goal reward -> gacha step
-> re-rollable first pull with the summon animation -> Collect! commits the cast, charges
the scrolls, advances the goal counter, and drops the tutorial box in favour of the
standing banners.

Box list: the standing banners are always present; the tutorial box is prepended at
sort 1 until used. Soul Mirror is the ITEM gacha and is the one box with
`is_char_only: false`.

Still synthesised rather than recovered (the drop tables were live-ops data we do not
have):
- **All four standing banners share one pool and cost.** Grand Sins should pull Sins,
  Angel Summon should pull Virtues, Soul Mirror should pull items. That needs per-box
  drop tables.
- 767 (Soul Mirror / Angels) and 769 (御三家 100-pull) are identified but unused --
  the latter needs `step_tbl`, which is still empty, as are `eyeball_tbl` and
  `pick_up_tbl`.
- `row[2]` of a result row (sent as 1; unread on the character path).
- The gacha deducts on commit but there is no transaction/refund path if the client
  rejects the reply.

## 10. Mail (`PlayerMail`) — working 2026-08-03

Requests are ODD, replies EVEN:

| dir | cmd | meaning |
|---|---|---|
| C→S | **1** | list, sent when the **panel opens** — carries a real request id |
| C→S | **3** | list, part of the login sync — id 0 |
| C→S | **5** / **7** | receive one (uid in strargs[0]) / receive all |
| C→S | 9 | delete (not implemented) |
| S→C | **2** | `SetMailList` |
| S→C | **4** | `set_UnreadMailCount` |
| S→C | **8** | `ReceiveAllAttachments` |
| S→C | 10 | `DeleteMail` |

**Two traps, both of which cost a debugging cycle:**

1. **The panel's request is cmd 1, not 3, and the reply MUST echo its request id.** The
   panel waits on an `AsyncOp` keyed by that id and an unmatched reply is dropped in
   silence — same rule as the heartbeat. Handling only cmd 3 left the panel on
   "Data Loading…" forever.
2. **`SetMailList(id, dataEnd, strArg)` is CHUNKED** — it appends to a per-request-id
   buffer and only parses when `dataEnd` (intargs[1]) == 1. The old stub sent `[0, 0]`,
   so the list was never parsed at all; harmless only because it also sent `"[]"`.

Because `SetMailList` ends with `PanelLoadingWaiting.Close()`, anything wrong on that
path shows up as a permanent "Data Loading…" with nothing in logcat — the same signature
as the gacha hang (§9).

**A mail element's keys are the numeric strings `"1"`..`"10"`**, not names:

| key | field |
|---|---|
| `"1"` | formatId → the `mail` design row supplying subject/content |
| `"2"` | uid (string), echoed back when claiming |
| `"3"` | read (bool) — false puts it in `UnreadMailList` |
| `"4"` | content override |
| `"5"` | timestamp |
| `"6"` | nested object whose `"7"` is the attachments `{item id: count}` |
| `"8"` | custom → replaces the literal `{custom}` in the row text |
| `"9"` | expired (bool) |
| `"10"` | updatetime |

`{attachment}` in the row text is replaced by `Mail.GetAttachmentString()`. Nested values
are read with `ToString()` and re-parsed, so plain nested JSON objects work;
`ConvertJsonTable` exists only to rewrite an empty `"[]"` into `"{}"`.

**Claiming.** The reply is cmd 8 with `strargs[0]` = the JSON list of uids actually
claimed. `ReceiveAllAttachments` grants NOTHING — it only marks those mails read, moves
them to the read list, decrements the unread count and pops the item display (skipping
items whose `DesignItemRow._class` is 2 or 7). **The server does the granting**, and the
currency/backpack/energy syncs must be pushed afterwards like every other reward.
Verified live: 3 mails paying coin ×50000, scroll ×5, diamond ×100 all landed correctly.

The **News** tab needs nothing from us — it renders the pack's own `announce` form
(4 rows, the real service notices including the 2023 shutdown announcement).

## 11. Login bonus — sync works, popup does NOT (parked 2026-08-03)

**Working:** the sync round trip. Request `0xFE16052C` cmd **1** → reply `0xFFB98ABA`
cmd **17** (this subsystem ignores the servercmd+0x100 rule; its switch is a 17..20 jump
table plus 33 and 49). Build layout A, and `HandleSyncCmd` bails unless strargs has
**exactly one** entry. The client logs `Handle LoginBonus Sync` and the old
`DoSyncBonusData` timeout is gone.

`LoginBonusSyncData` keys: `group_data`, `active`, `nextreset`, `total_day`.
`GroupInfo` keys are renamed almost throughout: **`dcnt`** (day), **`bcnt`** (book),
**`tcnt`** (total), **`duration_s`/`duration_e`** (start/end), **`cin_book`/`cin_day`**
(check-in pair), **`banner`**, **`text_id`**; only `group` and `close` match.
**`active` is a BOOLEAN** (`Active = active == 1`) and gates the whole dictionary
population — not a group id.

The handler builds `GroupData` (field 0x30) keyed by `group`, calls
`PanelLoginBonus.ResetGroupData`, and calls **`AddActivatedGroup` unconditionally for
every entry** — there is no filtering on `close`, durations or `cin_day`, so a single
group is enough to populate `_activatedList`.

**The blocker: `panel_login_bonus` is never opened.** logcat shows only `panel_bulletin`,
so `DoOpenPanelLoginBonus` is not reached — this is NOT a panel that opens and closes.
The prefab is present in our bundles (`panel_login_bonus`, `panel_login_return_bonus`
and a `Button_LoginBonus` all live in `ngui_prefabs_panels_utility_*.ab`), so it is not a
missing asset either.

What the flow does:

```
RequestServerSync (cmd 1)   -> sets WaitReturnLoginDataSync (field 0x40)
DoSyncLoginReturnBonusData  -> if that flag is set, runs WaitingForReturnLoginDataSync
                               (which POLLS the flag every frame)
                            -> else SceneMain.SetCurUIDirty(1) and the flow continues
```

The client **never requests** the return bonus — only cmd 1 ever appears on that index —
so it must be a server push. The **only** code that clears the flag is inside
`HandleDailyBonusCmd`, which is **cmd 18** (confirmed: pushing it logs
`Handle LoginBonus DailyBonus`). With non-null intargs it takes the branch that writes
three `dailyBonusData` fields from `intargs[0..2]` (the third stored as
`intargs[2] + now`) and then zeroes the flag.

**Unresolved:** pushing cmd 18 with `[0,0,0]` runs the handler but the
`DoSyncLoginReturnBonusData` timeout still fires, exactly 3.0s after the client's
request, and the popup never appears. Either that watchdog logs regardless of the step
completing, or the clear is not happening despite the handler running. Next moves:
work out the real meaning of `intargs[0..2]` (a bool, a value, and a duration added to
now), try cmd **20** (the only other unidentified case in the 17..20 table), or attach a
watch to `WaitReturnLoginDataSync` to see whether it actually flips.

Also unknown, and only reachable once the panel opens: the true semantics of `close`,
`duration_s`/`duration_e`, `cin_day` and the day counter, all currently guessed.

### Known gaps, not yet investigated (2026-08-03)

- ~~**Retreat does nothing.**~~ **FIXED 2026-08-03** — see §6 "Retreat".
- **Party positions are wrong with a full party of 5.** Not a regression — the formation
  table is *reconstructed*: DesignText row 499 is absent from our pack, so
  `patch_design.py` injects a 10x10 LATTICE of candidate positions instead of 5 real
  slots and the server picks a lattice index per unit (`LightBattleChar.Index`). The
  calibration env var `SEVENSINS_SLOTS` is empty, so `_add_player_team` falls back to
  `index = slot`, i.e. lattice cells 0-4 — a row of the grid, not a formation. Fine at 2
  characters, obvious at 5.
  Fix properly by inverting the measured camera projection (screen normalised 2000x900,
  `screen_x ≈ 122.5*z - 220 - 8.75*x`, `screen_y ≈ 575 + 28.75*x`) against reference
  footage of a 5-member party, and writing those five real `[x,y,z]` entries into
  `FORMATION_499` instead of the lattice. Calibrate ONLY on frames with the "Turn 1" HUD
  — the opening cinematic uses a different camera. Needs a pack re-patch (device
  re-download), unlike the env-var sweep.
- **Drop slots are graded treasure boxes, and we always send the lowest grade.**
  Observed 2026-08-03 on 2-1. The results screen ANIMATES: drop slots start as closed
  chests and open to reveal their contents, and the rating rows tick in sequence. So a
  mid-animation frame shows chests and greyed-out rows even on a full 3-star clear --
  do not read those as failed conditions or as an alternative drop presentation.
  The load-bearing observation is that within a SINGLE 3-star run the two chests are
  DIFFERENT COLOURS (silver, bronze). Same run, same performance, different art per
  slot -- which is what a per-drop grade looks like. That matches the decompiled grading
  no one had used yet: `PaserInterludeData` turns each `i_plays` value into
  `0` (`> ItemsRank[0]`), `1` (`> ItemsRank[1]`), `2` otherwise, or `3` for a zero, and
  `UpdateRegularRewardData` reads that per drop (3 = hide the slot). So `i_plays` is a
  PER-WAVE PERFORMANCE VALUE, higher = better tier, compared against the stage's
  `box_rank` ("21,11" for both 1-1 and 2-1). `battle.INTERLUDE_BOX_VALUE = 1` grades
  every wave to 2 — the lowest tier — so our chests are uniform and can never vary.
  Leading reading: the grade picks the chest ART for each slot. Still unknown is what
  the per-wave value actually MEASURES -- it cannot be overall clear speed, since both
  chests came from one 3-star run, so it is something that varies wave to wave.
- ~~**Lobby ad-banner strip is blank.**~~ **FIXED 2026-08-03** by publishing OFA static
  banner entries (cmd 257 strargs[0]); the strip is driven by that banner LIST, not by
  `QuestToOFAMapping` as first assumed. Still open on OFA:
  - **Only 2 of 55 entries are published.** Publishing every row of groups 1/201/202
    throws `Index was out of range` from an event handler, so one of them needs data we
    do not serve — **bisect the 55 to find it**, then set `player_state.OFA_ENTRIES` to
    `None` to publish the lot. That should restore the lobby entry buttons the real game
    has (NOVICE ULTRA PACK / SIGNUP REWARD / WEEKLY BEST SELLER / DEMON DESCENDED).
  - **The bulletin CONTENT pane stays blank.** An entry creates the tab and its art, but
    the body comes from the prefab variant's own data source (`strarg` plus the shop/quest
    state each variant reads), none of which we serve.
- **Helpers are not implemented** — see §5.9. Also the origin of the long-standing
  `helper uid error` warning.
- **The daily login-bonus popup never opens** — see §11. The sync itself works.
- **A stub "極樂輪盤" (Paradise Roulette) entry appears in our lobby but not the real
  game.** We answer the roulette syncs with empty objects -- cmd 305 `["{}","{}"]` and
  cmd 306 `["{}"]` -- so the client registers the reserved roulette boxes 101/102 with no
  data and draws placeholder entries (two labels overlapping). logcat repeats
  `boxId=101/102, roulette info is empty.` every ~13s.
  **Do NOT simply stop sending 305/306**: PlayerGacha waits on events 17/18
  (ROULETTE_DROP_SYNCED / ROULETTE_SYNCED) which only those two raise, so removing them
  hangs the gacha sync. The fix is whatever payload means "no roulette active" rather
  than "an empty roulette" -- read `ReceiveSyncRoulette` / `ReceiveSyncRouletteDrop`.

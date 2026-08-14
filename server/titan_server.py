#!/usr/bin/env python3
"""TitanStack game server (login handshake).

Wire format, recovered from libil2cpp.so (see docs/GAME_SERVER.md):

  frame = RC4_stream( [chksum][type][size:u32 big-endian] + protobuf_body )

  * ONE RC4 keystream per direction, per connection, covering header+body of
    every frame -- not re-keyed per message.
  * header is 6 bytes: TitanStack.Header cctor sets ChkSumLen=1, MsgTypeLen=1,
    MsgSizeLen=4, so HeaderLen=6.
  * chksum = ChkSumKey ^ ((MsgTypeMul * type + size) % ChkSumMod)
           = 0x5C ^ ((3 * type + size) % 30)

Body is a protobuf-net `titan.Client`:
  1 = cmd (uint)   11 = login   12 = login_reply   13 = queue_reply   31 = rpc_pack
  Login       : 1 = username (string), 2 = password (bytes), 3 = json_data (string)
  LoginReply  : 1 = success (bool),    2 = errno (uint)
"""
import socket, threading, time, os, sys, json
import player_state as ps
import battle as bt
# The pure transport layer -- RC4, framing, protobuf and RPC packing -- lives in
# wire.py. `import *` is scoped by wire's __all__, so this pulls exactly the named
# primitives (make_rpc, rpc_pack, pb_field_bytes, RC4, ...) and nothing else, which
# is why every call site below stays unchanged.
from wire import *                                                    # noqa: F401,F403

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 22110
LOG = os.path.join(os.path.dirname(__file__), "titan_server.log")


def log(s):
    line = time.strftime("%H:%M:%S ") + s
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


# ---- session RPC (application layer; the primitives it uses are from wire) -----
# ClientRpc.lookup dispatches on Rpc.index, a per-subsystem constant (full list in
# re/rpc_client_cmds.txt).
PLAYER_SESSION = 0x90AD873D          # server -> client
PLAYER_SESSION_SERVER = 0x910208AB   # client -> server

# Stable for the process lifetime: HandleHeartbeatReply latches boot_time and
# reset_day_time on the first reply, then pops a "server rebooted" (msg 31) or
# "daily reset" (msg 30) confirm dialog on any later heartbeat whose values differ.
BOOT_TIME = int(time.time())
RESET_DAY_TIME = BOOT_TIME - (BOOT_TIME % 86400)

# Game.Player.Session.PlayerSession.OnClientCmdReceived switch:
SESSION_NOTIFY_LOGGED_IN = 1
SESSION_NOTIFY_LOGGED_OUT = 2
SESSION_HEARTBEAT_REQUEST = 16   # client -> server
SESSION_HEARTBEAT_REPLY = 17     # server -> client


def notify_logged_in():
    """PlayerSessionClientCmd cmd=1. HandleNotifyLoggedIn takes no args; it just
    needs IsConnected+IsLoggedIn+IsDone on the establish request, then dispatches
    SessionLoggedInEvent and starts the heartbeat coroutine."""
    # PlayerSessionClientCmdRT.build reads uint32[0][0]=cmd, uint64[0][0]=id,
    # uint32[1]=intargs, string[0]=strargs -- all four slots must be present.
    return rpc_pack(make_rpc(PLAYER_SESSION,
                             uint32s=[[SESSION_NOTIFY_LOGGED_IN], []],
                             uint64s=[[0]],
                             strings=[[]]))


# --- subsystem sync -------------------------------------------------------
# SyncUtil.GetSyncCmds calls each subsystem's SendSyncCmd, which fires a ServerCmd
# and then WaitRpcEvents({event}) -- the async op completes only when a ClientCmd
# with that event id arrives. SyncWithServer blocks on all of them, logging
# "Subsystem '<name>' still in syncing..." every 5s until each op is done.
#
# IMPORTANT: WaitRpcEvents does NOT wait on a command id. Each Player* manager has
# TWO dispatchers -- `CmdEvent` (EventDispatcher<JsCmdEventArgs>, keyed by rpc cmd,
# what OnClientCmdReceived raises) and `Event` (EventDispatcher<XEvent>, keyed by an
# XEventType constant). SendSyncCmd's WaitRpcEvents binds to `Event`, so its int[]
# holds XEventType values, e.g. BackpackEventType.SYNCED = 4 (and the interrupt id
# 255 = RPC_ERROR). Do not confuse those 4s: sending backpack *cmd* 4 lands in
# HandleSellReply and throws.
#
# Backpack sync: HandleSyncSubBackpackRply appends strargs[0] to a per-cbpType
# StringBuilder and, when intargs[0]==1 (final chunk), JSON-deserializes it into
# deBackpackDatas[cbpType]. IsSyncReady requires keys 1,2,3,4 to be present before
# SyncdBackpackDatas will raise SYNCED. cmds 84..87 map to cbpType 1..4.
# BackpackItemsData.backpackItemData is [JsonProperty("sid")] with a custom
# [JsonConverter(LuaTableConverter)] -- so the wire key is "sid", NOT the field name.
# LuaTableConverter.ReadJson always returns a dictionary built by the contract
# resolver, so the field is only ever null when "sid" is absent altogether; that
# null is what SyncdBackpackDatas dereferences (`value.backpackItemData` at +0x10).
EMPTY_BACKPACK_JSON = '{"sid":{}}'

# Energy keys from the [JsonProperty] thunks: type/energy/energy_cap/
# refresh_time_bias. Energy.GetEnergy() just returns the stored _energy, and the dict
# is keyed by the EnergyType enum value (Action=1 is stamina; 16/17/18 are the arena
# pools). An empty dict leaves the player at 0 stamina and campaign refuses to start.
ENERGY_JSON = ('{"energies":{'
               '"1":{"type":1,"energy":999,"energy_cap":999,"refresh_time_bias":0},'
               '"16":{"type":16,"energy":99,"energy_cap":99,"refresh_time_bias":0},'
               '"17":{"type":17,"energy":99,"energy_cap":99,"refresh_time_bias":0},'
               '"18":{"type":18,"energy":99,"energy_cap":99,"refresh_time_bias":0}}}')

# Dictionary<CurrencyType,uint>: Cash=1, Mira=16, RealCash=32, DMMCash=48,
# GuildPoints=64.
CURRENCY_JSON = '{"1":999999,"16":99999,"32":9999,"48":0,"64":0}'

# Level JSON keys come from the [JsonProperty] thunks: type/uid/lv/xp/xp_cap/
# lv_min/lv_max, with LevelType.Player = 1.
PLAYER_LEVEL_JSON = ('{"levels":{"player_level":{"type":1,"uid":"player_level",'
                     '"lv":1,"xp":0,"xp_cap":100,"lv_min":1,"lv_max":200}}}')

# PlayerCharData deserializes from a single JSON OBJECT (not an array). Its wire keys
# come from [JsonProperty] and again differ from the field names: charDic->"pro_chars",
# charGroupRecord->"group_tbl", charIDDic->"id_tbl". The three dictionaries use
# LuaTableConverter, so omitting a key leaves that dictionary null.
# Newtonsoft also builds it through the parameterized ctor
# PlayerCharData(List orgArenaTeam, List sort_list, List act_collection), which
# iterates all three without null checks -- omit any and the ctor itself NPEs.
EMPTY_CHAR_JSON = ('{"pro_chars":{},"group_tbl":{},"id_tbl":{},"formations":[],'
                   # showgirl is fed straight into DesignCharForm.GetRow with no
                   # zero-check (receivedShowgirlData), so it must be a real row id;
                   # DesignCharForm has 3633 rows starting at 1.
                   '"add_char":0,"showgirl":1,"state":0,"offset":"","helper":"",'
                   '"book_rank":0,"book_xp":0,'
                   # acPeriod = actCollectionPeriod (List<uint>); the ctor does NOT
                   # initialise it, so omitting the key leaves it null and
                   # UIMainMenuBtn.onRefresh_ActCollection NPEs during
                   # PanelMain.OnEnterMainPage, aborting the whole main-page setup.
                   '"acPeriod":[],'
                   '"orgArenaTeam":[],"sort_list":[],"act_collection":[]}')

# (server_index, server_cmd) -> (name, [(client_index, cmd, intargs, strargs), ...])


def uint64_msg(index, cmd, intargs=(), strargs=(), req_id=0):
    """Build shape A: uint32[0][0]=cmd, uint64[0][0]=id, uint32[1]=intargs,
    string[0]=strargs. Used by Backpack, Stage, Mail.

    Some stage requests carry a meaningful id (RequestServerAvgSync puts the AVG id
    there), so echo it back rather than always sending 0."""
    return rpc_pack(make_rpc(index,
                             uint32s=[[cmd], list(intargs)],
                             uint64s=[[req_id]],
                             strings=[list(strargs)]))


def backpack_msg(cmd, intargs=(), strargs=()):
    return uint64_msg(0xC4A53FC0, cmd, intargs, strargs)


def twostr_msg(index, cmd, g0=(), g1=()):
    """Build shape D (PlayerFriend): uint32[0][0]=cmd, string[0], string[1] --
    two separate string wrapper groups, no int args at all."""
    return rpc_pack(make_rpc(index, uint32s=[[cmd]], strings=[list(g0), list(g1)]))


def jsagent_msg(cmd, module, intargs=(), strargs=()):
    """PlayerJSAgent's own shape -- it is the only subsystem that carries a JS module
    name alongside the args. PlayerJSAgentClientCmdRT.build pops, in order,
    string[0]=moduleName, uint32[0][0]=cmd, uint32[1]=intargs, string[1]=strargs,
    and it pops all four UNCONDITIONALLY, so every group must be present even when
    empty (twostr_msg omits the int-args group and would break the pop sequence)."""
    return rpc_pack(make_rpc(PLAYER_JSAGENT,
                             uint32s=[[cmd], list(intargs)],
                             strings=[[module], list(strargs)]))


def uint_msg(index, cmd, intargs=(), strargs=()):
    """Build shape: uint32[0][0]=cmd, uint32[1]=intargs, string[0]=strargs (no uint64).
    Used by Char, Energy, General, Level, OFA, Gacha, Shop."""
    return rpc_pack(make_rpc(index,
                             uint32s=[[cmd], list(intargs)],
                             strings=[list(strargs)]))


def sint_msg(index, cmd, intargs=(), strargs=()):
    """The other common build shape (PlayerBattle, PlayerCurrency, ...):
    uint32[0][0]=cmd, sint32[0]=intargs, string[0]=strargs -- no uint64 slot, and
    the args are SIGNED (zigzag). Always confirm a subsystem's
    Player<X>ClientCmdRT$$build before reusing this; the layouts are not uniform."""
    return rpc_pack(make_rpc(index,
                             uint32s=[[cmd]],
                             sint32s=[list(intargs)],
                             strings=[list(strargs)]))


# (server_index, server_cmd) -> (name, [encoded rpc bodies to send in order])
# --- gameplay RPC handlers (client request -> our reply) ---------------------
# Distinct from the sync table: these answer in-game actions rather than the one-shot
# login sync. Reply cmd ids come from the Player<X>Rpc{Request,Reply}Cmd enums.
PLAYER_STAGE = 0xFB3F665A          # server -> client
PLAYER_STAGE_SERVER = 0xFA90E9CC   # client -> server
STAGE_REQ_EXECUTE, STAGE_REQ_NEWBIE = 2, 257
# AUTO PLAY (the offline sweep). PanelAutoPlay.OnButtonStartClick (0x175CBD4) branches on
# its mode: Offline COMMON/EXPRESS both call RequestServerStageAutoStart -> **cmd 3**
# with [stageID, count, useQuickBattleCoupon]; Online instead loops real battles through
# cmd 2. The client polls its state with cmd 6 and can stop with cmd 7.
STAGE_REQ_AUTO_START, STAGE_REQ_AUTO_SYNC, STAGE_REQ_AUTO_STOP = 3, 6, 7
# HandleAutoSync (0x180A2DC) takes a whole StageSyncData and reads its `auto` dict.
STAGE_RPLY_AUTO_SYNC = 24
# HandleAutoSuccess (0x180ABA8) needs intargs of EXACTLY length 2 -- [stageID, count] --
# and returns silently on any other length. Its confirm 401 reads "Autoplay COMPLETED.
# Stage {0} / Time(s) {1}", so it belongs at the END of a sweep, not the start. Sending
# it on Start told the player their 99 runs had already finished the instant they
# pressed the button. HandleAutoStop (0x180ADAC) is EXACTLY 3 intargs and its confirm
# 402 is "Autoplay STOPPED. Stage {0} / Times {1} / Elapsed Time {2} sec" -- so the
# third argument is elapsed SECONDS, and it belongs on an early cancel.
STAGE_RPLY_AUTO_SUCCESS, STAGE_RPLY_AUTO_STOP = 33, 34
STAGE_RPLY_EXECUTE = 18            # -> StageEventType.EXECUTE_SUCCESS (2)
# AVG (story cutscene) sync. The client calls RequestServerAvgSync(avgID, replayMode)
# when a scene with choices starts -- avgID rides in the uint64 id slot -- and leaves
# the option buttons LOCKED until the reply lands.
STAGE_REQ_AVG_SYNC, STAGE_RPLY_AVG_SYNC = 4, 20
STAGE_REQ_AVG_CHOICE, STAGE_RPLY_AVG_CHOICE = 5, 21
STAGE_RPLY_END_REWARD = 23         # -> PlayerStage.HandleEndReward
# HandleSyncReplyCmd: intargs [chunk, total, _, SP_Points], strargs[0] = StageSyncData.
STAGE_REQ_SYNC, STAGE_RPLY_SYNC = 1, 17
# "Drop Info" on a stage. HandleGetDropsReplyCmd (RVA 0x180A9DC) reads intargs[0] as the
# stage id and deserializes strargs[0] as List<uint> item ids, then raises StageEvent 4.
# Unanswered, the button does nothing at all.
STAGE_REQ_GET_DROPS, STAGE_RPLY_GET_DROPS = 8, 25
# PlayerShop. 259 -> 515 is the login sync; 260 -> 516 is SendSyncShopGoodsCmd, sent when
# a store TAB is opened, carrying [shopID, syncBaughtData].
SHOP_SERVER, SHOP_CLIENT = 0x94357119, 0x959AFE8F
SHOP_REQ_BUY, SHOP_RPLY_BUY = 258, 513
SHOP_REQ_SYNC, SHOP_RPLY_SYNC = 259, 515
SHOP_REQ_SYNC_GOODS, SHOP_RPLY_SYNC_GOODS = 260, 516
# Drop Info for a SELECTOR (`_action 7`). PanelItemInfo.OnPanelDirty (0x15AA038) sends
# the box-list request to the BACKPACK for `_action 2` but to the SHOP for `_action 7`:
# PlayerShop.RequesQueryCouponList (0x18058E8) is ShopRpcServerCmd 0x111, intargs
# [itemID]. The reply is HandleQueryCouponRply (0x1804D98, jumptable case 0x211), which
# reads strargs[0] as List<List<uint>> exactly like the box list and calls
# ShowCouponItemInfoPopup -- the variant that turns ON the footer label (text 108806,
# "Select and acquire 1 Item or Cast.").
SHOP_REQ_QUERY_COUPON, SHOP_RPLY_QUERY_COUPON = 273, 529
# The "GO!" button on a quest step that sends you shopping. PlayerShop.
# SendGoodsIDToShopIDCmd (0x180597C) is ShopRpcServerCmd 0x105 = 261 with intargs
# [goodsID] -- the goods id comes straight off the quest row's `_case_v1`. The reply
# is HandleSendGoodsIDToShopID (jumptable case 0x205 = 517), which logs
# "shopID={0}, tabID={1}", requires intargs._size >= 2, and calls
# PanelStore.EnterSpecificStore(filterID=intargs[1], shopID=intargs[0]). Unanswered,
# the button simply does nothing -- no error, no overlay, no navigation.
SHOP_REQ_GOODS_TO_SHOP, SHOP_RPLY_GOODS_TO_SHOP = 261, 517
# PlayerOFA. 1 -> 257 static banners, 2 -> 258 event banners, 3 -> 259 banner CONTENT.
OFA_SERVER, OFA_CLIENT = 0xAE866295, 0xAF29ED03
OFA_REQ_CONTENT, OFA_RPLY_CONTENT = 3, 259
# Digit-encoded unlock mask for AVG choices -- digit i names the option to unlock at
# button i, so 4321 opens options 1..4 (the panels never have more than a handful).
#
# TODO (higher difficulties): the real rule is that an option you have already taken is
# LOCKED OUT when you meet the scene again on a higher difficulty -- a three-option scene
# offers two the second time, one the third. That is exactly what this mask is for, so it
# becomes "the digits of the options not yet used" instead of a constant, and the picks
# have to be stored per (scene, difficulty) rather than per scene. Deferred until there
# is a second difficulty to test against; see docs/GAME_SERVER.md §6.
AVG_OPTIONS_UNLOCKED = 4321
# PlayerLoginBonus. Note the reply is 17, not servercmd+0x100 -- see SYNC_REPLIES.
# PlayerMail. Requests are ODD, replies EVEN: 3 -> 2 (the login-time list), 5/7 -> 8
# (receive), 9 -> 10 (delete). Opening the Mail PANEL sends cmd **1**, not 3, and unlike
# the login sync it carries a real request id which the reply MUST echo -- the panel
# waits on an AsyncOp keyed by that id, so an unmatched reply is silently dropped and it
# sits on "Data Loading..." forever (SetMailList ends in PanelLoadingWaiting.Close()).
PLAYER_MAIL = 0x93F5C551               # server -> client
PLAYER_MAIL_SERVER = 0x925A4AC7        # client -> server
MAIL_REQ_LIST_PANEL, MAIL_REQ_LIST_SYNC = 1, 3
MAIL_REQ_RECEIVE, MAIL_REQ_RECEIVE_ALL = 5, 7
MAIL_RPLY_LIST, MAIL_RPLY_UNREAD, MAIL_RPLY_RECEIVE = 2, 4, 8

PLAYER_LOGINBONUS = 0xFFB98ABA         # server -> client
PLAYER_LOGINBONUS_SERVER = 0xFE16052C  # client -> server
LOGINBONUS_REQ_SYNC, LOGINBONUS_RPLY_SYNC = 1, 17
# The client NEVER asks for the return-login bonus -- `RequestServerSync` sets
# `WaitReturnLoginDataSync` when it sends cmd 1, and `DoSyncLoginReturnBonusData` then
# waits on that flag (running WaitingForReturnLoginDataSync) instead of proceeding to
# SetCurUIDirty. Only the server can clear it, and the single place that does is inside
# **HandleDailyBonusCmd** -- which needs intargs with **at least 3 entries**, computes
# `intargs[2] + now`, stores it, and then zeroes the flag. Its dispatcher case is in the
# 17..20 jump table; 17 is the sync and 19 is the periodic card, so this is 18 or 20.
# cmd 33 was tried first (its body is just `SyncTimer = 0`) and did NOT stop the timeout.
LOGINBONUS_RPLY_RETURN_SYNC = 33
LOGINBONUS_RPLY_DAILY = 18

NEWBIE_STAGE_ID = 1101             # campaign 1-1, the tutorial fight
# Clearing the tutorial also hands the player Jacqueline, silently -- she is not on the
# results panel, she is simply in the roster afterwards. 11001 is her playable row
# (_alignment 104 = the 4* Awaker band, and _growStar[3] = 11004 is a real rung, so
# star 4 is a tier she actually has).
TUTORIAL_GIFT_CHAR = 11001
TUTORIAL_GIFT_STAR = 4
TUTORIAL_GIFT_LV = 1

PLAYER_QUEST = 0x12821D92          # server -> client
PLAYER_QUEST_SERVER = 0x132D9204   # client -> server
# PlayerQuest.OnClientCmdReceived handles only 529 (the chunked sync, intargs
# [chunk, total]) and 513 (completion reward popups, intargs in [questID, itemID,
# count] triples). Build shape C -- pop_uint32(0)=cmd, pop_uint32_array(1)=intargs,
# pop_string_array(0)=strargs, checked in PlayerQuestClientCmdRT$$build.
QUEST_REQ_SYNC, QUEST_RPLY_SYNC = 272, 529
# RequestServerQuestCompleted -> PlayerQuestServerCmd(0x101) with the claimed quest
# ids as the uint list. This is the "Collect Reward" tap: clearing a stage only makes
# a quest CLAIMABLE, and the newbie tutorial does not advance past the 1-1 goal until
# this round trip completes it. Reply 513 pops the reward (intargs in
# [questID, itemID, count] triples) and 529 records the completion.
QUEST_REQ_COMPLETED, QUEST_RPLY_REWARD = 257, 513


def quest_sync_msg(st):
    return uint_msg(PLAYER_QUEST, QUEST_RPLY_SYNC, [1, 1], [ps.quest_json(st)])


def autorun_payout(state, stage_id, count):
    """Pay `count` clears of `stage_id` -> the sync messages to send.

    A sweep pays exactly what beating the stage by hand pays, N times, from
    bt.stage_drops_for -- the same call Battle.drops() wraps.
    """
    stage_id, count = int(stage_id), int(count)
    if count < 1:
        return []
    buckets, totals = set(), {}
    # A Temple clear offers TWO candidates and the player keeps ONE. A sweep cannot ask,
    # so it keeps the first -- taking both would pay double what playing by hand does.
    keep_one = bool(bt.starshard_temple_tier(stage_id))
    for _ in range(count):
        paid = bt.stage_drops_for(stage_id)
        for d in (paid[:1] if keep_one else paid):
            if isinstance(d, bt.RuneDrop):
                ps.grant_rune(state, d.item_id, d.slot, d.level, d.enhance)
                buckets.add("equipment")
                continue
            iid, n = d
            buckets.add(ps.grant_reward(state, iid, n))
            totals[iid] = totals.get(iid, 0) + n
    state["stages"].setdefault(str(stage_id), 15)
    log(f"    -> auto play paid {count}x stage {stage_id} -> "
        f"{totals or 'starshards'}")
    msgs = []
    if "backpack" in buckets:
        msgs.append(backpack_msg(84, [1],
                                 [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
    if "equipment" in buckets:
        # BACKPACK_CHANGE carries the item list AND the info rows, so the Starshards
        # panel's "Inventory n/999" follows the grant instead of going stale.
        msgs.append(backpack_msg(BACKPACK_CHANGE, [0],
                                 [ps.backpacks_all_json(state,
                                                        {ps.BP_STORAGE_EQUIPMENT}),
                                  ps.backpack_info_json(state)]))
    if "currency" in buckets:
        msgs.append(sint_msg(0xBC8FDA7C, 512, [], [ps.currency_json(state)]))
    if "energy" in buckets:
        msgs.append(uint_msg(0xAE487D79, 512, [], [ps.energy_json(state)]))
    return msgs


def autorun_settle(state, now):
    """Finish a sweep whose timer has run out -> the messages to send.

    The completion toast is cmd 33 (confirm 401, "Autoplay Completed"), which is why
    it must NOT be sent when the sweep starts.
    """
    job = ps.autorun_due(state, now)
    if not job:
        return []
    stage_id, count = int(job["stage_id"]), int(job["count"])
    ps.autorun_cancel(state)
    msgs = autorun_payout(state, stage_id, count)
    ps.save(state)
    # EXACTLY two intargs, or HandleAutoSuccess returns without a word.
    return [uint64_msg(PLAYER_STAGE, STAGE_RPLY_AUTO_SUCCESS,
                       [stage_id, count], [])] + msgs


def auto_sync_msg(st):
    """Push StageSyncData through **HandleAutoSync (cmd 24)**, not the general sync.

    Both carry the same payload, but only 24 dispatches StageEvent 1 -- the event the
    auto-play UI redraws on. Sending the state via cmd 17 alone left the client holding
    a perfectly good running sweep with nothing telling it to draw, so the panel closed
    and the sweep looked like it had finished instantly.
    """
    return uint64_msg(PLAYER_STAGE, STAGE_RPLY_AUTO_SYNC, [], [ps.stage_json(st)])


def stage_sync_msg(st):
    """Re-push StageSyncData. HandleSyncReplyCmd assigns tempJsonStr outright on
    chunk 1 and replaces PlayerStage.SyncData wholesale, so this is idempotent --
    and it also makes the client re-request the quest sync on its own."""
    return uint64_msg(PLAYER_STAGE, STAGE_RPLY_SYNC, [1, 1, 0, 0], [ps.stage_json(st)])


PLAYER_GACHA = 0x5B210AC7          # server -> client
PLAYER_GACHA_SERVER = 0x5A8E8551   # client -> server
# RequestServerDrawV2(index, sindex) -> server cmd 5. ReceiveDraw (client 258) wants
# intargs [ok, removeBox, a, b, c] (>=5; [0] must be 1 or it is treated as a failure,
# [1]==1 removes the box) and strargs [updatedBoxJson, resultsJson] where results is
# List<List<int>>; an optional strargs[2] is a newly-appearing box.
GACHA_REQ_DRAW, GACHA_RPLY_DRAW = 5, 258
# RequestDrawRoulette(boxId) -> server cmd 51 with intargs=[boxId] (0x18FD0FC).
# ReceiveDrawRouletteSuccess (307) wants intargs[0]=boxId, strargs[0]=results
# List<List<int>>, strargs[1]=the UPDATED RouletteInfo for that box; 308 is the failure.
GACHA_REQ_DRAW_ROULETTE = 51
GACHA_RPLY_ROULETTE_OK, GACHA_RPLY_ROULETTE_FAIL = 307, 308
# The redraw ("first gacha") pair. **cmd 20 = RedrawBoxDoGetDraw is the COMMIT** --
# "do get draw", i.e. actually take the roll; that is what the Collect! button sends
# (observed: Collect! -> cmd 20, Try Again -> a fresh DrawV2 cmd 5). Its reply 277
# takes intargs [boxId] and fires the pending callback when the box has no
# CashGachaGoodsID. RedrawSave (21 -> 278) is a separate save-to-history call:
# intargs [boxId], strargs[0] = List<List<int>> stored as box.History.
GACHA_REQ_REDRAW_GET, GACHA_RPLY_REDRAW_GET = 20, 277
GACHA_REQ_REDRAW_SAVE, GACHA_RPLY_REDRAW_SAVE = 21, 278
GACHA_DRAW_COUNT = 10


PLAYER_CHAR = 0x771EA36E           # server -> client
PLAYER_CHAR_SERVER = 0x76B12CF8    # client -> server

# The 15th subsystem, new in 2.2.7. It has no sync and no Data class -- it is a pure
# Puerts JS-RPC bridge, so anything routed through it is implemented in JavaScript
# rather than C#. Ultra Transcend (the super-limit upgrade) is the first user of it.
PLAYER_JSAGENT = 0x8FA20D6B        # server -> client (ClientRpc.lookup: -1885205141)
PLAYER_JSAGENT_SERVER = 0x8E0D82FD  # client -> server (ServerRpc _index: -1911717123)
JSAGENT_ULTRA_TRANSCEND = 290
# PlayerChar.RequestServerFormation(index, formationList, support) sends cmd 0x112
# with intargs [index + 1, support, 1] and the five slot uids as strargs; the reply
# is receivedFormation at cmd 530, which does formations[intargs[0] - 1] = <the
# FormationData in strargs[0]> and, when intargs[1] == 1, pops confirm message 19.
# CharRpcClientCmd.create -- "you have received these casts". Server-initiated: there
# is no matching request. `receivedCreateChar` (0x16992F0) deserialises strargs[0] as a
# **Dictionary<uid, CharData>**, AddChar's every entry into charDic, rebuilds the group
# record and dispatches CharEvent 4. It is both the real grant and the trigger for the
# single-pull reveal, and it is the only way a cast can arrive outside gacha -- the
# quest reward reply (513) builds an ItemPopupInfo whose CharDatas it never sets, so
# that path can only ever show an item card.
CHAR_RPLY_CREATE = 529
CHAR_REQ_FORMATION, CHAR_RPLY_FORMATION = 274, 530
# CharRpcServerCmd.sync_id_data / CharRpcClientCmd.sync_id_data -- the Soulpedia.
CHAR_REQ_SYNC_ID_DATA, CHAR_RPLY_SYNC_ID_DATA = 310, 567
CHAR_REQ_BOOK_RANK_UP, CHAR_RPLY_BOOK_RANK_UP = 311, 568
# CharRpcServerCmd.char_sell / CharRpcClientCmd.char_sell -- Unsummon.
CHAR_REQ_SELL, CHAR_RPLY_SELL = 291, 547
# CharRpcServerCmd.char_level_up / CharRpcClientCmd.char_level_up -- Level Training.
CHAR_REQ_LEVEL_UP, CHAR_RPLY_LEVEL_UP = 278, 534
# CharRpcServerCmd.char_rank_up / CharRpcClientCmd.char_rank_up -- Rank Up.
CHAR_REQ_RANK_UP, CHAR_RPLY_RANK_UP = 279, 535
# CharRpcServerCmd.char_plus_up / CharRpcClientCmd.char_plus_up -- Transcend.
CHAR_REQ_PLUS_UP, CHAR_RPLY_PLUS_UP = 280, 536
# Names and numbers below are from the CharRpcServerCmd / CharRpcClientCmd enums in
# the 2.2.7 dump, so they are authoritative rather than inferred.
CHAR_REQ_LIMIT_UP, CHAR_RPLY_LIMIT_UP = 281, 537          # Skill Up
CHAR_REQ_LIMIT_IMPART, CHAR_RPLY_LIMIT_IMPART = 288, 544  # Inherit
CHAR_REQ_KIZUNA_SET, CHAR_RPLY_KIZUNA_SET = 313, 569      # Soul Book Kizuna: equip
CHAR_REQ_KIZUNA_LVUP, CHAR_RPLY_KIZUNA_LVUP = 320, 576    # Soul Book Kizuna: level up
# CharRpcServerCmd.gift / CharRpcClientCmd.gift_reply -- Consonance karma gifts.
CHAR_REQ_GIFT, CHAR_RPLY_GIFT = 307, 564
# CharRpcClientCmd.update_friendly -- pushes {charId: [flv, fxp]} into charIDDic.
CHAR_RPLY_UPDATE_FRIENDLY = 552
# PlayerChatRoom. The lobby polls `get_pmsg_list` (339) every ~10s from the UpdateGChat
# coroutine, which is by far the noisiest thing on the wire -- 14575 unanswered in the
# log before this. RequestServerGetPublicList (0x18F4764) sends intargs=[GChatTimestamp];
# ReviceServerPublicMsgList (0x18F22A0) takes strargs = a list of ChatMsgInfo JSON.
CHATROOM_SERVER, CHATROOM_CLIENT = 0x3A6083E0, 0x3BCF0C76
CHATROOM_REQ_PUBLIC_LIST, CHATROOM_RPLY_PUBLIC_LIST = 339, 769
# PlayerGuild.
GUILD_SERVER, GUILD_CLIENT = 0x92F719BA, 0x9358962C
GUILD_REQ_SYNC, GUILD_RPLY_SYNC = 272, 528

# (index, cmd) -> name, for requests that have NO reply command in the client enums.
# These are genuinely fire-and-forget; logging them as "no handler" implied a gap that
# does not exist. GeneralRpcServerCmd.DeviceInfo (339) sends
# intargs=[2,0,3,4,1] / strargs=[model, device hash, "2.2.7"] at login and there is no
# DeviceInfo in GeneralRpcClientCmd at all.
FIRE_AND_FORGET = {
    (0x4DB7FD4B, 339): "General.DeviceInfo",
}

# The cast-list panel-hang set, all from the same enums.
CHAR_REQ_LOCK, CHAR_RPLY_LOCK = 276, 532                  # padlock toggle
CHAR_REQ_CHAR_MAX, CHAR_RPLY_CHAR_MAX = 277, 533          # roster CAPACITY, not "max lv"
CHAR_REQ_DECOMPOSE, CHAR_RPLY_DECOMPOSE = 292, 548        # cast -> soulfrag materials
CHAR_RPLY_DECOMPOSE_FAIL = 597
CHAR_REQ_SET_SORT = 312                                   # no reply cmd exists
CHAR_REQ_REMOVE_ALL_FORM, CHAR_RPLY_REMOVE_ALL_FORM = 321, 577
# Equipping starshards. There is no `char_wear_rune` in CharRpcClientCmd -- the reply is
# `update_equip`, which carries the cast's whole rebuilt equips array. See the notes on
# ps.wear_runes for why the length must be exactly ps.CHAR_EQUIP_SLOTS + 1.
CHAR_REQ_WEAR_RUNE, CHAR_RPLY_UPDATE_EQUIP = 295, 549
# 296/297 share the 549 reply -- all three wear cmds rebuild the same 18-slot array.
# **Their strarg orders differ**, which is easy to get backwards:
#   295 char_wear_rune      strargs = [...6 rune slots..., char_uid]   (uid LAST)
#   296 char_wear_soulfrag  strargs = [equip_uid, char_uid]  intargs = [slot index]
#   297 char_wear_bloodpact strargs = [char_uid, equip_uid]  (uid FIRST) no intargs
CHAR_REQ_WEAR_SOULFRAG = 296
CHAR_REQ_WEAR_BLOODPACT = 297
# Break artwork. 308's reply is NOT an echo -- it wants [charId, skinType] and no
# strargs; 309's IS. See the notes above ps.unlock_skin.
# The two lobby icons. 304's reply echoes its single strarg; 305's takes only TWO
# intargs (id, state) even though the request sends three (group, id, state).
CHAR_REQ_SET_HELPER, CHAR_RPLY_SET_HELPER = 304, 560
CHAR_REQ_SET_SHOWGIRL, CHAR_RPLY_SET_SHOWGIRL = 305, 561
CHAR_REQ_UNLOCK_SKIN, CHAR_RPLY_UNLOCK_SKIN = 308, 565
CHAR_REQ_SET_SKIN, CHAR_RPLY_SET_SKIN = 309, 566
# PlayerBackpack.HandleBackpackChagne -- the only cmd that dispatches BackpackEvent 1,
# which is what already-open panels (e.g. the gift list) refresh on.
BACKPACK_RPLY_CHANGE = 145
# BackpackRpcCmd.query_box_list / query_box_rply -- tapping a BOX item (item._class 2)
# opens PanelItemInfo and asks what is inside it.
BACKPACK_SERVER = 0xC50AB056        # client -> server
BACKPACK_REQ_QUERY_BOX, BACKPACK_RPLY_QUERY_BOX = 129, 130
# Starshard upgrade. The rply is a bare ack (HandleEnchantGemRply is a single RET);
# BackpackChange is what actually updates the client.
BACKPACK_REQ_ENCHANT_GEM, BACKPACK_RPLY_ENCHANT_GEM = 99, 100
# Soulmirror upgrade. Same shape as the gem one: SendEnhanceSoulFragReq(itemUID, cnt).
BACKPACK_REQ_ENHANCE_SOULFRAG, BACKPACK_RPLY_ENHANCE_SOULFRAG = 113, 114
# Bloodpact enhance. HandleEnhanceBloodpactRply (0x18EC594) is a single RET, so 259 is a
# bare ack like the gem reply -- NOT payload-carrying like the soulmirror one (114).
BACKPACK_REQ_ENHANCE_BLOODPACT, BACKPACK_RPLY_ENHANCE_BLOODPACT = 258, 259
# Forge / "transcribe". HandleMixBloodpactRply (0x18EC598) is NOT a stub: it requires
# intargs[0] == 1 (otherwise it pops an error dialog), and the request opens a
# PanelWaitingBlock, so leaving 260 unanswered hangs the UI until restart.
BACKPACK_REQ_MIX_BLOODPACT, BACKPACK_RPLY_MIX_BLOODPACT = 260, 261
# Dismantle. Reply is DATA-carrying: strargs[0] is a List<List<uint>> of [itemId, amount]
# pairs that HandleDecomposeBloodpactRply turns into the reward popup.
BACKPACK_REQ_DECOMPOSE_BLOODPACT, BACKPACK_RPLY_DECOMPOSE_BLOODPACT = 262, 263
# Soulmirror dismantle -- the SOULMIRRORS panel's Recycle button. Same idea as the
# bloodpact one but a DIFFERENT reply shape: 120 carries its reward list in **intargs**,
# not a JSON strarg. SendDecomposeSoulFragReq (0x18E9100) opens a PanelWaitingBlock and
# only HandleDecomposeSoulFragRply closes it, so an unanswered 119 freezes the game
# behind a modal blocker -- restart is the only way out.
BACKPACK_REQ_DECOMPOSE_SOULFRAG, BACKPACK_RPLY_DECOMPOSE_SOULFRAG = 119, 120
# Soulmirror fuse ("transmute"). SendTransmuteSoulFragReq (0x18E913C) is the same
# uids-in-strargs, PanelWaitingBlock-first shape as 119 -- so this one freezes the client
# too when unanswered. 118 is NOT 120 though: it reads intargs[1]/[2] for ONE item and
# throws ArgumentOutOfRange if fewer than 3 ints arrive, where 120 loops over pairs and
# tolerates an empty list.
BACKPACK_REQ_TRANSMUTE_SOULFRAG, BACKPACK_RPLY_TRANSMUTE_SOULFRAG = 117, 118
# The padlock, shared by every equipment family (runes / soulmirrors / bloodpacts).
BACKPACK_REQ_EQUIP_LOCK, BACKPACK_RPLY_EQUIP_LOCK = 105, 106
# BackpackRpcCmd.DropItemRply -- "here is everything you just received", the ONE
# message that can show a multi-item popup. HandleDropItemRply (0x18EB9DC) needs
# EXACTLY ONE strarg, a `Dictionary<int,int>` of item id -> count, and builds one
# ItemStruct per entry into a single ItemPopupInfo. Reply 513 cannot do this: it reads
# intargs[2]/[3], a single pair, with no loop.
BACKPACK_RPLY_DROP_ITEM = 24
BACKPACK_CHANGE = 145


_shop_version = 0


def shop_version():
    """A ShopVersion that always differs from the one the client is holding.

    HandleLoginSync bails out of the whole rebuild when the incoming version equals
    PlayerShop.ShopVersion, so a constant value means the second and later syncs are
    no-ops -- including the one the store panel itself sends on open."""
    global _shop_version
    _shop_version += 1
    return _shop_version


def stage_execute_reply():
    """PlayerStage.HandleExecuteReplyCmd reads NO args -- it just raises
    StageEventType.EXECUTE_SUCCESS. Without it the client sits on "Data loading"
    forever after you pick a stage."""
    return uint64_msg(PLAYER_STAGE, STAGE_RPLY_EXECUTE, [], [])


def battle_msg(cmd, intargs=(), strargs=()):
    """PlayerBattleClientCmdRT.build is shape B: uint32[0][0]=cmd, sint32[0]=args,
    string[0]=strargs."""
    return sint_msg(bt.BATTLE_CLIENT_INDEX, cmd, intargs, strargs)


def start_battle_msg(battle):
    """cmd 1505 -> PlayerBattle.HandleServerStart. EXECUTE_SUCCESS alone only gets
    the client to start loading the battle scene; this is the handoff it then waits
    for, and the one that finally closes the "Data loading" overlay."""
    return battle_msg(bt.CMD_SERVER_START, [], [battle.battle_datas_json()])


def battle_sync_reply(st):
    """PlayerBattle.HandleSyncCmd (cmd 0x709=1801)'s reply, driven by whether this
    account has a saved in-progress battle (ps.saved_battle) -- decompiled from
    PlayerBattle.HandleSyncCmd (0x168a81c) AND AskBattleReconnect (0x168b67c), 2.2.7.

    reconnectCase (intargs[0]) has THREE meanings, not two -- easy to misread on a
    first pass (an earlier version of this function did, and shipped it, before a
    live device test showed no dialog ever appeared and this got re-checked):

      0 : no reconnect state. Ordinary sync.
      1 : "you have a LIVE battle -- rejoin?" AskBattleReconnect shows the actual
          interactive confirm dialog here, wired to CB_Reconnect (sends
          REQ_RECONNECT/0x2BD with intargs=[1]) and CB_ReconnectCancel (same RPC,
          intargs=[0]) -- battle_replies() already answers accept by replaying the
          start_battle_msg handoff a fresh stage entry uses. HandleSyncCmd's early
          return path (`if (v16 != 2) { dispatch SERVER_SYNC; return; }`) covers
          this case too, so it needs NOTHING beyond intargs[0]/[1] -- no stage id,
          no rune list, no strargs at all.
      2 : "a battle you were in already RESOLVED while you were gone." NOT a rejoin
          prompt -- AskBattleReconnect calls `PanelUtil.LaunchPanel("battle/
          panel_battle_result")` directly and clears reconnectCase back to 0, no
          confirm, no CB_Reconnect involved. THIS is the case that needs
          intargs[2]=CurStageID, intargs[3]=CurRuneIndex, strargs=[rune list JSON,
          price list JSON] -- they populate the result screen, not a live rejoin.
          We have no use for this case (we never let a battle "resolve while the
          player was gone" -- REQ_BATTLE_END only fires from a connected client).

    intargs[1] = battleType, read whenever intargs.size >= 2 -- picks which confirm-
    dialog string case 1's prompt shows (indexed battleType-1 into a 5-entry table;
    out of range falls back to a generic string 10064). We only ever resume a stage/
    campaign fight, so a fixed 1 is fine either way.
    """
    saved = ps.saved_battle(st) if st else None
    if not saved:
        return [sint_msg(0xFBC2FA08, 1801, [0], [])]
    return [sint_msg(0xFBC2FA08, 1801, [1, 1], [])]


def build_sync_replies(st):
    """Sync payloads are rendered from the persisted account state so that
    currency, stamina, characters and progress survive restarts and can be
    mutated by gameplay handlers."""
    return {
        # cmd 83 (HandleBackpackInfosRply) only stashes strargs[0] into syncBpInfosJson,
        # which SetBackpackInfo later parses as List<List<int>> -- so it must be a JSON
        # array, not an object, and it must be POSITIONALLY complete: entry i becomes
        # BackpackType i+1 and CheckAndShowReadyGoMsg looks up types 1/4/8 with no
        # containment check. Send it before the sub-backpacks.
        (0xC50AB056, 81): ("PlayerBackpack",
                           [backpack_msg(83, [], [ps.backpack_info_json(st)])] +
                           [backpack_msg(cmd, [1], [ps.backpack_json(st, cmd - 83)])
                            for cmd in (84, 85, 86, 87)]),
        # PlayerBattle.HandleSyncCmd (cmd 0x709=1801) -- see battle_sync_reply for the
        # reconnectCase wire contract this advertises whenever ps.saved_battle(st)
        # has something to offer. REQ_BATTLE_SYNC's own docstring explains why the
        # dispatch loop must never route this (index, cmd) pair to battle_replies.
        (bt.BATTLE_SERVER_INDEX, bt.REQ_BATTLE_SYNC): ("PlayerBattle",
                                                       battle_sync_reply(st)),
        # PlayerCurrency.HandleSyncCmd (cmd 512) deserializes strargs[0] into
        # Dictionary<CurrencyType,uint> and then raises CurrencyEventType.CURRENCY_SYNCED=1.
        (0xBD2055EA, 256): ("PlayerCurrency", [sint_msg(0xBC8FDA7C, 512, [], [ps.currency_json(st)])]),
        # PlayerChar.receivedSync (cmd 528) is chunked: intargs[0]=chunk, intargs[1]=total.
        # It appends strargs[0] to tempJsonStr and, when chunk==total, deserializes the
        # accumulated string into the PlayerCharData collection and raises CHAR_SYNCED=1.
        # 567 (sync_id_data) is sent here too, not just in answer to the Soulpedia's
        # request: receivedSyncIDData is what ASSIGNS PlayerCharData.charIDDic, and
        # nothing else does. Without it that dictionary stays null for the whole
        # session until the player happens to open Soulpedia, and every handler that
        # walks it throws -- receivedUpdateFriendly (552, the karma push after a gift)
        # dereferences it unconditionally, which is the `event execution error,
        # message=Object reference not set` logged on every single gift.
        (0x76B12CF8, 272): ("PlayerChar",
                            [uint_msg(0x771EA36E, 528, [1, 1], [ps.char_json(st)]),
                             uint_msg(0x771EA36E, 567, list(ps.book_progress(st)),
                                      [json.dumps(ps.char_id_table(st),
                                                  separators=(",", ":"))])]),
        # PlayerEnergy.HandleSyncCmd (cmd 512) deserializes strargs[0] into EnergySyncData
        # and immediately uses its `energies` dict (LuaTableConverter, key "energies") to
        # build Dictionary<EnergyType,Energy>, so the key must be present.
        (0xAFE7F2EF, 256): ("PlayerEnergy",
                            [uint_msg(0xAE487D79, 512, [], [ps.energy_json(st)])]),

        # --- remaining subsystems -------------------------------------------------
        # Reply cmd is consistently servercmd + 0x100 where the ranges allow it. The arg
        # INDICES below come from decompiling each handler (which m_Items[N] it reads);
        # the JSON shapes are first-pass and get corrected from the client's very
        # specific deserialization errors.
        # The uid in here is not cosmetic: PlayerBattle.HandleJudge only acts when its
        # strargs[0] equals PlayerGeneral.Uid, so an empty general sync means the
        # battle can never hand control to the player.
        (0x4DB7FD4B, 256): ("PlayerGeneral",
                            [uint_msg(0x4C1872DD, 512, [], [ps.general_json(st)])]),
        (0x4DB7FD4B, 257): ("PlayerGeneralTutorial",
                            [uint_msg(0x4C1872DD, 513, [], [])]),
        # HandleSyncGameRuleCmd (514) deserializes strargs[0] into
        # GeneralSyncGameRuleData and then walks FIVE List<string> fields with no null
        # check, building a List<Decimal> from each. "{}" deserializes fine but leaves
        # them null, so the first one threw the NullReference that had been sitting in
        # the login log since the beginning -- StackCore.poll swallowed it, which is
        # why it never had a stack trace. Empty arrays satisfy the loops (they exit on
        # size 0); the real magnification tables were live-ops data we do not have.
        # The wire keys are snake_case and do NOT match the C# field names.
        (0x4DB7FD4B, 259): ("PlayerGeneralGameRule",
                            [uint_msg(0x4C1872DD, 514, [], [ps.game_rule_json()])]),
        # PlayerLevel uses the DataSegment<T> chunking protocol (Game.Player.Common):
        # Recover() is a state machine -- the FIRST string is a header parsed as a JSON int
        # giving the number of data segments, and the next N strings are the payload, which
        # get concatenated and deserialized once N have arrived (RecoverData). Only then is
        # IsRecovered set and the event dispatched; sending just the header returns silently,
        # which is why Level sat in "still in syncing" with no error at all.
        (0x7FDE8871, 256): ("PlayerLevel", [
            uint_msg(0x7E7107E7, 512, [], ["1"]),                  # header: 1 data segment
            # levels is keyed by the LevelSetting uid, which LevelDefine..cctor sets to
            # "player_level" for LevelType.Player(1). An empty dict makes
            # PlayerLevel.GetUniqueLevel throw "level type 'Player' not found" from
            # UIMainTitleList.OnLevelUpdate as the main panel builds.
            uint_msg(0x7E7107E7, 512, [], [ps.level_json(st)]),     # LevelSyncData
        ]),
        # OFA: 257 reads strargs[0..1], 258 reads intargs[0]+strargs[0],
        # 259 reads intargs[0]+strargs[0..3].
        # strargs[0] = List<OFABannerData> -> StaticBannerDic (keyed by `id`, a
        # `oneforall` row); strargs[1] = QuestToOFAMapping, which UIMainAdBanner counts
        # for the lobby banner strip. See player_state.OFA_ENTRIES.
        (0xAE866295, 1): ("PlayerOFA",
                          [uint_msg(0xAF29ED03, 257, [],
                                    [ps.ofa_static_banner_json(),
                                     ps.ofa_quest_map_json()])]),
        # 258 is the EVENT banner list. It is NOT cosmetic: RefreshGroupedDic buckets it
        # by the `oneforall` row's `_group`, and that grouping is what gates the lobby
        # roulette button. See player_state.OFA_EVENT_ENTRIES.
        (0xAE866295, 2): ("PlayerOFA2",
                          [uint_msg(0xAF29ED03, 258, [0],
                                    [ps.ofa_event_banner_json()])]),
        # The lobby's ~10s global-chat poll. Answering with an EMPTY message list is a
        # clean no-op: ReviceServerPublicMsgList (0x18F22A0) walks strargs, and with
        # nothing to walk `obj` stays null and it returns before touching
        # GChatTimestamp or the chat panel. It does deref strargs itself, but
        # make_rpc always emits the wrapper for an empty group, so pop_string_array
        # yields an empty (non-null) list rather than throwing.
        # There is no chat on a single-player server, so an empty list is also correct
        # rather than merely safe.
        (CHATROOM_SERVER, CHATROOM_REQ_PUBLIC_LIST):
            ("PlayerChatRoomPublicList",
             [uint_msg(CHATROOM_CLIENT, CHATROOM_RPLY_PUBLIC_LIST, [], [])]),
        # Guild sync, sent once at login. **intargs[0] is the guild status and it
        # selects the branch** (disasm 0x19549F0..0x1954AA0):
        #   1 -> "in a guild": indexes strargs[0] and runs _deserializeGuildData
        #   2 -> a second data path, also reads strargs
        #   anything else -> skips strargs ENTIRELY, just sets gStatus and fires
        #                    GuildEvent
        # `HaveGuild()` is `gStatus == 1`, so **0 is the honest "no guild" answer** and
        # is also the only value that needs no payload. Claiming 1 with empty data would
        # leave the guild panels dereferencing a half-built object.
        # Guild is Tier 3 (multiplayer, dead on a private server); this exists so the
        # login sequence has no unanswered command, not to make guilds work.
        (GUILD_SERVER, GUILD_REQ_SYNC):
            ("PlayerGuild", [uint_msg(GUILD_CLIENT, GUILD_RPLY_SYNC, [0], [])]),
        # Stage is chunked like Char and additionally reads intargs[3].
        # Stage is chunked via tempJsonStr like Char (intargs[0]=chunk, [1]=total) and also
        # reads intargs[3]. StageSyncData's LuaTableConverter keys are entrance/stages/
        # bestrec/auto (again nothing like the field names) and must all be present.
        (PLAYER_STAGE_SERVER, STAGE_REQ_SYNC): ("PlayerStage", [stage_sync_msg(st)]),
        # Friend handlers take TWO string lists and walk them in parallel; a
        # non-empty first list with an empty second one indexes out of range. For a
        # new guest both must be empty, which skips the loop and reaches the
        # FRIEND_SYNCED dispatch (raised by cmd 529, ReviceServerFriendsData).
        # The request and reply enums pair up one-for-one -- FriendRpcServerCmd
        # helpers/friends/block_list/social = 272/273/274/275, FriendRpcClientCmd
        # helpers/friends/sync_block_list/social = 528/529/530/531. This table was
        # shifted by one (273->528, 274->529, 275->530), so every sync got the PREVIOUS
        # command's reply and `helpers` (272) went unanswered entirely -- the only
        # remaining "no handler" line in a clean session. Muted until now only because
        # all four payloads are empty pairs of lists.
        (0xB9FABC82, 272): ("PlayerFriendHelpers",
                            [twostr_msg(0xB8553314, 528, [], [])]),
        (0xB9FABC82, 273): ("PlayerFriendFriends",
                            [twostr_msg(0xB8553314, 529, [], [])]),
        (0xB9FABC82, 274): ("PlayerFriendBlock",
                            [twostr_msg(0xB8553314, 530, [], [])]),
        (0xB9FABC82, 275): ("PlayerFriendSocial",
                            [twostr_msg(0xB8553314, 531, [], [])]),
        # Mail cmd 2 reads intargs[1] and strargs[0] before calling SetMailList, which
        # deserializes that string as List<JObject>.
        # Mail waits on MailEventType.CHANGE_MAIL_COUNT = 2, which is raised by cmd 4
        # (set_UnreadMailCount, reads intargs[1]) -- NOT by cmd 2, whose SetMailList only
        # raises RECEIVE_MAIL_LIST = 1. So both messages are required.
        # intargs[1] is dataEnd and MUST be 1: SetMailList buffers the string per request
        # id and only parses the accumulated JSON when it sees 1. [0,0] meant the list was
        # never parsed at all -- invisible while we were sending "[]", fatal once we are
        # not. See player_state.mail_list_json for the "1".."10" element keys.
        (0x925A4AC7, 3): ("PlayerMail", [
            uint64_msg(0x93F5C551, 2, [0, 1], [ps.mail_list_json(st)]),   # MailListRply
            uint64_msg(0x93F5C551, 4, [0, ps.unread_mail_count(st), 0], []),
        ]),
        # Gacha waits on TWO events -- int[2]={17,18} = ROULETTE_DROP_SYNCED and
        # ROULETTE_SYNCED -- NOT GACHA_SYNC(1). Those come from cmds 305/306
        # (ReceiveSyncRouletteDrop reads strargs[0..1], ReceiveSyncRoulette strargs[0]).
        # ReceiveSyncGacha: strargs[0] = List<UnlimitGachaBox>, intargs = [TimeStamp,
        # PassSpLock]. It ends with PanelLoadingWaiting.Close(), so an exception here
        # leaves the gacha screen stuck on "Data Loading...". "[]" gave the panel an
        # empty box list to index.
        (0x5A8E8551, 1): ("PlayerGacha",
                          [uint_msg(0x5B210AC7, 257, [1, 1], [ps.gacha_json(st)])]),
        # Roulette. "{}" here left GetRouletteInfo returning null, which made
        # MenuBtnUpdater.UpdateRoulette bail before it could disable the second lobby
        # button -- the two-roulette-icons bug. See player_state.roulette_info_json.
        (0x5A8E8551, 49): ("PlayerGachaRouletteDrop",
                           [uint_msg(0x5B210AC7, 305, [],
                                     [ps.roulette_datas_json(),
                                      ps.roulette_bonus_datas_json()])]),
        (0x5A8E8551, 50): ("PlayerGachaRoulette",
                           [uint_msg(0x5B210AC7, 306, [],
                                     [ps.roulette_info_json(st)])]),
        # Shop's cmd 514 falls through to the "Unknown RPC cmd" path -- HandleLoginSync is
        # case 515. (Stacked case labels made 514/515 look like one handler.) It reads
        # strargs[0] as List<List<int>> -- the shop list, one [id, begin, end] row per
        # shop -- and strargs[1] as Dictionary<int,Dictionary<int,Dictionary<int,int>>>
        # (FreeGoodsDic). Those two were transposed in this comment before; see
        # player_state.shop_json for the full read-out.
        # intargs[1] is ShopVersion and it MUST NOT be 0. HandleLoginSync compares it
        # against PlayerShop.ShopVersion (which starts at 0) and, on a match, skips the
        # entire re-init -- leaving `_shopDic` NULL. That null then blows up far away:
        # AnalysisQuest calls CheckPreCase for every incomplete quest, 288 quests carry
        # `_pre_case 6`, and PlayerShop.GetLimitShopGoodsHasBuy dereferences _shopDic on
        # its first line. That NullReference aborted AnalysisQuest before QuestSort,
        # which is what left the goal-quest list unsorted and pinned the Goal panel to
        # step 210.
        # NOTE cmd 259 is NOT handled here -- it needs the request's intargs, which this
        # table cannot see. See the dedicated SHOP_REQ_SYNC branch in the dispatcher.
        # Ranking waits on RANKING_OLD_DATA_SYNCED = 17, raised by cmd 530
        # (HandleRankingOldDataSyncReply) -- not by 529. It bails unless intargs has
        # EXACTLY 5 entries and strargs EXACTLY 1 (strargs[0] = LastSeasonGroupID).
        (0xB7C131D3, 274): ("PlayerRanking",
                            [sint_msg(0xB66EBE45, 530, [0, 0, 0, 0, 0], ["0"])]),
        # PlayerQuest is NOT part of the login sync set -- the client asks for it from
        # PlayerStage.HandleSyncReplyCmd, which calls PlayerQuest.SendSyncCmd() right
        # after installing the stage data. Leaving it unanswered is why cmd 272 was
        # repeating in the log forever, and why the newbie tutorial could never get
        # past the 1-1 clear (see player_state.complete_stage_quests).
        (PLAYER_QUEST_SERVER, QUEST_REQ_SYNC): ("PlayerQuest",
                                                [quest_sync_msg(st)]),
        # PlayerLoginBonus asks with cmd 1 and wants cmd **17** back -- NOT 257; this
        # subsystem ignores the usual servercmd+0x100 convention (its switch is
        # 17/19/33/49). Build layout A, and HandleSyncCmd bails unless strargs has
        # EXACTLY one entry. Left unanswered this is the `Request LoginBonus` /
        # `DoSyncBonusData` timeout pair in logcat. See player_state.login_bonus_json.
        (PLAYER_LOGINBONUS_SERVER, LOGINBONUS_REQ_SYNC): (
            "PlayerLoginBonus",
            [uint64_msg(PLAYER_LOGINBONUS, LOGINBONUS_RPLY_SYNC, [],
                        [ps.login_bonus_json(st)]),
             uint64_msg(PLAYER_LOGINBONUS, LOGINBONUS_RPLY_RETURN_SYNC, [], []),
             # cmd 18 (DailyBonusRply) reads strargs[0] and strargs[1] UNCONDITIONALLY
             # -- both are indexed before any length check beyond the bounds assert, so
             # an empty list throws ArgumentOutOfRangeException ("Parameter name: index")
             # straight out of the handler. That aborts the whole login flow: the next
             # thing in logcat is `登錄流程Sync逾時,請確認流程DoSyncLoginReturnBonusData
             # 是否有異常`, and WaitReturnLoginDataSync is never cleared, so the client
             # re-requests the login bonus on a timer forever.
             #
             # "" is the client's own sentinel for "no data" (it compares each entry to
             # the empty string and nulls the corresponding list instead of running it
             # through JsonSerializationUtil), so two empty strings is the correct way
             # to say we have no daily bonus rather than a placeholder. With real data
             # they are List<List<int>>: [0] = CurrentDataList, [1] = NextDataList.
             # intargs stay [isLast, count, nextTime] and are read at 0/1/2.
             uint64_msg(PLAYER_LOGINBONUS, LOGINBONUS_RPLY_DAILY, [0, 0, 0],
                        ["", ""])]),
    }


def battle_end_reward(battle, state):
    """PlayerStage EndReward (cmd 23) -> HandleEndReward, which deserializes one
    string into a BattleReward and hands it to the results panel as
    PlayerBattle.RewardData.

    BattleReward is built through .ctor(List<List<int>> item_list, int[]
    caseParamsToOverride): `item_list` MUST be present (a null throws inside the
    ctor) and each entry is read as [_, item id, count] -- the ctor bounds-checks
    `_size > 2` and reads indices 1 and 2, never 0. The other lists come from
    [JsonProperty] names.

    Two reward streams reach this panel, and BOTH have to be granted server-side --
    the popup is display only:
      - Drops     -> `item_list`, the coin icons (see Battle.drops)
      - Ratings   -> the gems beside each condition, whose amounts live in the stage's
                     `_rating_datas` and are paid for the conditions just satisfied
                     (see Battle.rating_rewards). `rating_list` is index-aligned 1/0.
      - Exp Up!   -> `bar_list`, one BarData per party member. The XP curve IS known
                     (Formula.GetCharLVupNeedXP), but how much a stage PAYS is not in
                     the pack, so the amount is ours -- see stage_battle_xp.
    """
    won = battle.wave_cleared()
    drops = battle.drops()
    rating_rewards = battle.rating_rewards() if won else []
    bars = []
    if won:
        xp = ps.stage_battle_xp(battle.stage, battle.wave_max)
        bars = ps.grant_battle_xp(state, ps.battle_team(state), xp)
        if bars:
            ups = [b for b in bars if b["lv"] > b["olv"]]
            log(f"    -> battle xp: +{xp} each to {len(bars)} party members"
                + (f", {len(ups)} levelled up" if ups else ""))
    reward = {
        # index 0 is unread by the ctor; 1 keeps it looking like the gacha rows
        # Element [0] is ignored by BattleReward..ctor (it reads [1]=id, [2]=count), so
        # a rune drop shows its display item here and grants the real piece below.
        "item_list": [[1, d.display_item, d.count] if isinstance(d, bt.RuneDrop)
                      else [1, d[0], d[1]] for d in drops],
        "itembonus_list": [],
        "bar_list": bars,
        "rating_list": battle.rating_flags() if won else [],
        "helper_uid": "",
    }
    msgs = [uint64_msg(PLAYER_STAGE, STAGE_RPLY_END_REWARD, [],
                       [json.dumps(reward, separators=(",", ":"))])]
    if won:
        buckets = set()
        # A temple clear offers a CHOICE of shards; every other stage grants outright.
        rune_pick = [] if bt.starshard_temple_tier(battle.stage_id) else None
        for d in drops:
            if isinstance(d, bt.RuneDrop):
                # A starshard is an equipment INSTANCE, not a stackable item: it goes
                # into storage 2 with its own uid and rolled attr, which is what makes
                # it show up in the Starshards list.
                #
                # **On a Starshard Temple stage the drops are CANDIDATES, not prizes.**
                # The panel is titled "Starshards Select": the player is shown them and
                # keeps ONE. Roll now (the panel prints the attributes) and hold the
                # grant until cmd 508 says which.
                if rune_pick is not None:
                    e = ps.roll_rune(state, d.item_id, d.slot, d.level, d.enhance,
                                     reserved=rune_pick)
                    rune_pick.append(e)
                    continue
                r = ps.grant_rune(state, d.item_id, d.slot, d.level, d.enhance)
                log(f"    -> starshard: item {d.item_id} slot {d.slot} "
                    f"lv {d.level} uid {r['uid']}")
                buckets.add("equipment")
                continue
            buckets.add(ps.grant_reward(state, d[0], d[1]))
        for iid, cnt in rating_rewards:
            buckets.add(ps.grant_reward(state, iid, cnt))
        if drops or rating_rewards:
            log(f"    -> rewards: drops {drops}, rating {rating_rewards} "
                f"(turns {battle.round}, deaths {battle.deaths})")
        # The grant is only real once the client is told: currency/backpack are cached
        # from the login sync and nothing on the results panel updates them.
        if "currency" in buckets:
            msgs.append(sint_msg(0xBC8FDA7C, 512, [], [ps.currency_json(state)]))
        if "backpack" in buckets:
            msgs.append(backpack_msg(84, [1],
                                     [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
        if "equipment" in buckets:
            # Storage 2 = StorageEquipment, the Starshards inventory. Backpack reply
            # cmd is 83 + storage, so 85 here.
            msgs.append(backpack_msg(85, [1],
                                     [ps.backpack_json(state,
                                                       ps.BP_STORAGE_EQUIPMENT)]))
        # The tutorial fight also hands over Jacqueline, and she is NOT listed on the
        # results panel -- she just appears in the roster afterwards. Guarded on owning
        # her already so replaying 1-1 does not keep minting copies.
        if (battle.stage_id == NEWBIE_STAGE_ID
                and not any(e["id"] == TUTORIAL_GIFT_CHAR
                            for e in state["roster"].values())):
            ps.add_char(state, TUTORIAL_GIFT_CHAR,
                        lv=TUTORIAL_GIFT_LV, star=TUTORIAL_GIFT_STAR)
            msgs.append(uint_msg(0x771EA36E, 528, [1, 1], [ps.char_json(state)]))
            log(f"    -> tutorial gift: char {TUTORIAL_GIFT_CHAR} "
                f"{TUTORIAL_GIFT_STAR}* lv{TUTORIAL_GIFT_LV}")
        # Record the clear so progress persists -- and so the forced newbie tutorial
        # does not run again on a replay (bTutorial keys off this stage's rating).
        # **The rune panel is launched BY cmd 1507, and reads ONLY the list it carries.**
        # HandleRuneListCmd (0x168ABAC) deserializes strargs[0] as
        # List<BackpackItemData> into CurRuneList, takes intargs[0] as CurRuneIndex and
        # strargs[1] as a List<int> price list, then LaunchPanel()s the panel itself.
        # Putting the shards in the end-reward `item_list` (which is what an earlier fix
        # did) does NOT feed that list -- the panel opens with nothing, parks at
        # UpdateResultState(0) and hangs on the empty altar room.
        if rune_pick:
            state["_rune_pick"] = {"stage": battle.stage_id,
                                   "cands": [dict(e) for e in rune_pick]}
            # **intargs[0] is CurRuneIndex and it is 1-BASED.** OnPanelEnable computes
            # `_curSelectRune = base[count-1] + CurRuneIndex - 1`, where base is 1/2/4
            # for 1/2/3 candidates (dword_37037B4). Sending 0 with two candidates gives
            # slot 1 -- the SINGLE-rune layout's slot, which is not active in a two-rune
            # panel: the countdown then writes to a dead label (the button reads "(-s)")
            # and Claim operates on a slot that is not there. 1 selects the first real
            # candidate. The index the client sends BACK is 0-based over the candidates
            # (TransIdx, dword_37037A0), which is what the 508 handler indexes with.
            msgs.append(battle_msg(
                bt.CMD_RUNE_LIST, [1],
                [json.dumps(rune_pick, separators=(",", ":")),
                 json.dumps([0] * len(rune_pick), separators=(",", ":"))]))
            log(f"    -> starshard select: {len(rune_pick)} candidates "
                f"{[e['iid'] for e in rune_pick]}")
        state["stages"][str(battle.stage_id)] = 15
        # Best clear length, which is what the auto-play panel calls "Stage Clear
        # Record" and multiplies by 10s to estimate a sweep. With `bestrec` empty it
        # reads -1 Turn(s) and the estimate is nonsense.
        ps.record_stage_turns(state, battle.stage_id, battle.round)
        # Credit the "clear any stage of <family> N times" goals (`_case_id` 5). The
        # client cannot re-derive these -- GetQuestValue reads a server counter -- so
        # nothing bumping them left "Complete any Kizuna Quest 1 time" stuck at 0/1 no
        # matter how many Kizuna stages were cleared. quest_sync_msg below carries it.
        cat = ps.stage_category(battle.stage_id)
        touched = ps.bump_stage_category_quests(state, battle.stage_id)
        if touched:
            log(f"    -> stage family {cat}: quest counters {touched}")
        ps.save(state)
        # The client caches StageSyncData at login, so without this push it does not
        # learn about the clear until the next relaunch -- which is what made the
        # tutorial replay and would now leave the next stage locked. Re-pushing it
        # also makes the client re-request the quest sync by itself
        # (HandleSyncReplyCmd calls PlayerQuest.SendSyncCmd), but send the quest
        # update explicitly too so the ordering does not matter.
        # Account XP. PlayerLevel Update (513) carries ONE Level in strargs[0] and no
        # intargs; the client requires that uid to already exist from the login sync.
        acc_xp = ps.stage_player_xp(battle.stage)
        levelled, old_lv, new_lv = ps.grant_player_xp(state, acc_xp)
        msgs.append(uint_msg(0x7E7107E7, 513, [], [ps.level_update_json(state)]))
        log(f"    -> account xp +{acc_xp}"
            + (f" -- LEVEL UP {old_lv} -> {new_lv}" if levelled else
               f" (lv {new_lv}, {state['level']['xp']}/{state['level']['xp_cap']})"))
        # Battle XP changed lv/xp on the roster, and charDic is cached from login the
        # same way StageSyncData is -- without this the lobby keeps showing the old
        # levels until relaunch, even though the results panel just animated them.
        if bars:
            msgs.append(uint_msg(0x771EA36E, 528, [1, 1], [ps.char_json(state)]))
        msgs.append(stage_sync_msg(state))
        msgs.append(quest_sync_msg(state))
        log(f"    -> stage {battle.stage_id} cleared (rating pushed)")
    return msgs


def play_turn_msgs(battle, target_team):
    """Server-side move for the acting unit -> the CMD_ATTACK messages to send.

    Shared by the enemy turn and player auto-battle; see Battle.auto_move for why
    auto has to be driven from here.
    """
    move = battle.auto_move(target_team)
    if not move:
        return []
    attacker, defender, skill, slot = move
    body = battle_msg(bt.CMD_ATTACK, [],
                      [battle.attack_cmd_json(attacker, defender, skill)])
    battle.spend_skill(attacker, slot)
    battle.end_turn()
    return [body]


def battle_replies(battle, cmd, intargs, strargs, state=None, uid=""):
    """Answer one client->server battle RPC.

    The client drives the fight by asking; the server answers with the state it
    should render. Ready is sent by StartState.DoBattleStart once the scene is up
    (PlayerBattle.ServerRPCReady -> cmd 100, intargs=[auto flag]), and the fight
    only starts moving when we push the wave and then the first turn.
    """
    if cmd in (bt.REQ_READY, bt.REQ_START_TURN):
        # ReadyState.OnUpdate transits straight to TurnStart, which asks for 101 --
        # cmd 100 (Ready) is only sent on the auto-battle path -- so both mean "the
        # scene is up, give me something to do". The wave has to be opened first:
        # HandleWaveBegin is what runs SyncData and the wave-1 unit FX pass.
        if cmd == bt.REQ_READY and intargs:
            # **Ready's intargs[0] is the client's REMEMBERED auto-battle setting, and
            # ignoring it is why auto switched itself off every fight.**
            # `ServerRPCReady` (0x1687340) reads `ClientPrefs.GAME_SETTING.BattleAuto`
            # -- a client-side persisted preference, not per-battle state -- and sends
            # it here; it only forces 0 for PVP and BattleType 2/4, which is the game's
            # own rule that those modes may not auto. So the client remembers across
            # fights and TELLS us, and a fresh Battle defaulting to auto=False silently
            # overrode it: the player toggled auto on, the next battle's Ready said
            # `int=[1]`, we dropped it, and nothing auto-played until they toggled
            # again. Seen in the log as 501 int=[1] followed by 100 int=[1].
            #
            # Only REQ_READY carries this. REQ_START_TURN's intargs mean something
            # else entirely, so it must not touch `auto`.
            battle.auto = bool(intargs[0])
        if battle.turn_open:
            # TurnEnd re-asks while the previous answer is still being played out.
            # Answering again restarts the turn and makes the skill bar flicker.
            log("    (battle: turn already open, ignoring repeat request)")
            return []
        battle.turn_open = True
        msgs = []
        if not battle.wave_begun:
            battle.wave_begun = True
            msgs.append(battle_msg(bt.CMD_WAVE_BEGIN, [], [battle.battle_cmd_json()]))
        # intargs[0] picks the state AFTER the perform: 1 -> TurnEnd(8), anything
        # else -> SituationJudge(4). TurnEnd immediately asks for another turn, so
        # with an empty combo that spins. SituationJudge just idles until the server
        # judges, which is exactly the hand-off we want.
        msgs.append(battle_msg(bt.CMD_START_TURN, [0], [battle.battle_cmd_json()]))
        return msgs
    if cmd == bt.REQ_JUDGE and (battle.wave_cleared() or battle.party_wiped()):
        # The fight is decided, so answer the judge with the wave result instead of
        # another turn. intargs are [result, wave just fought, next AVG, end AVG,
        # start AVG]; the client sets BattleData.Wave from arg 1 and then increments
        # it itself in DoNextState, which either runs to the next wave (state 11,
        # which asks us for cmd 502) or finishes in GameWin.
        result = bt.WAVE_RESULT_WIN if battle.wave_cleared() else bt.WAVE_RESULT_LOSE
        # AVG ids: [2] is played by RushState during the run to the next room,
        # [3] after the battle, [4] is the next wave's pre-fight scene.
        next_avg = battle.avg(battle.interlude_avgs)
        end_avg = battle.avg(battle.after_avgs)
        start_avg = battle.avg(battle.before_avgs, battle.wave + 1)
        log(f"    -> wave {battle.wave}/{battle.wave_max} result {result} "
            f"avg next={next_avg} end={end_avg} start={start_avg}")
        return [battle_msg(bt.CMD_WAVE_END,
                           [result, battle.wave, next_avg, end_avg, start_avg],
                           # BtCollector -- the per-unit damage table behind the
                           # results screen's Result button. An EMPTY dmgTbl ends the
                           # wave cleanly but leaves AllDamageList empty, and
                           # PanelBattleRecord indexes it unguarded, so the button
                           # threw ArgumentOutOfRange and appeared dead.
                           [battle.collector_json()])]
    if cmd == bt.REQ_NEXT_WAVE:
        battle.advance_wave()
        log(f"    -> next wave {battle.wave}/{battle.wave_max}: "
            f"{[o for o, u in battle.units.items() if u.team == bt.TEAM_ENEMY]}")
        return [battle_msg(bt.CMD_NEXT_WAVE, [], battle.next_wave_strargs())]
    if cmd == bt.REQ_JUDGE:
        # PerformState.OnPerformEnd asks for the judge once the animation is done.
        # For a player unit, Judge (1200) transits to AttackState and shows the skill
        # bar; intargs are [action timeout, skill2 locked, skill3 locked, cd1..cd3]
        # and strargs[0] must equal PlayerGeneral.Uid or the handler silently no-ops.
        # For an enemy unit there is nobody to ask, so the server plays its move.
        if battle.player_turn():
            if not battle.auto:
                return [battle_msg(bt.CMD_JUDGE, battle.judge_args(), [uid])]
            # Auto-battle: the skill bar is hidden, so nobody can tap and the client
            # will sit in SituationJudge forever waiting on us. Send the Judge (the
            # panel still refreshes off it) and then immediately play the move.
            msgs = [battle_msg(bt.CMD_JUDGE, battle.judge_args(), [uid])]
            acting = battle.acting_unit()
            msgs += play_turn_msgs(battle, bt.TEAM_ENEMY)
            log(f"    -> auto turn for {acting.order if acting else '?'}")
            return msgs
        acting = battle.acting_unit()
        msgs = play_turn_msgs(battle, bt.TEAM_PLAYER)
        if msgs:
            log(f"    -> enemy {acting.order if acting else '?'} attacks")
        return msgs
    if cmd == bt.REQ_ATTACK:
        # PlayerBattle.ServerRPCAttack: intargs [skillKey, skillClass],
        # strargs [attacker order, defender order].
        attacker = strargs[0] if strargs else ""
        defender = strargs[1] if len(strargs) > 1 else ""
        # skillKey is the 1-based button slot (AttackState maps it to
        # CharData.SkillList[key-1]), not a skill id -- resolve it against the same
        # skill list we published in BattleDatas.
        skill_key = intargs[0] if intargs else 1
        unit = battle.units.get(attacker)
        skills = unit.skills if unit else []
        # A locked slot (on cooldown, gauge short, or ability_seal) is normally caught
        # by the disabled button client-side; re-check server-side so a raw/replayed
        # request can't bypass a seal.
        if unit and (skill_key - 1) not in battle.usable_slots(unit):
            skill_key = 1
        skill = skills[skill_key - 1] if 0 < skill_key <= len(skills) else 0
        log(f"    -> attack {attacker} -> {defender} "
            f"(slot {skill_key} = skill {skill})")
        battle.spend_skill(attacker, skill_key - 1)
        body = battle_msg(bt.CMD_ATTACK, [],
                          [battle.attack_cmd_json(attacker, defender, skill)])
        battle.end_turn()
        return [body]
    if cmd == bt.REQ_RETREAT:
        # Menu -> Retreat. `HandleRetreat` deserialises strargs[0] into a BtCollector,
        # sets BattleResultType = 2 (the loss/abandon value) and dispatches BattleEvent 4,
        # which is what actually tears the battle down. strargs must have >= 1 entry.
        # BtCollector's only [JsonProperty] is `type`; everything else arrives through its
        # ctor param `dmgTbl`, so send that explicitly rather than letting it come in null.
        log(f"    -> retreat (stage {battle.stage_id}, turn {battle.round})")
        return [battle_msg(bt.CMD_RETREAT, [],
                           [json.dumps({"type": 0, "dmgTbl": []},
                                       separators=(",", ":"))])]
    if cmd == bt.REQ_BATTLE_END:
        # Sent by GameWinState.ShowBattleResult once the victory cinematic ends; the
        # results panel then wants PlayerBattle.RewardData, which arrives on the
        # PlayerStage channel rather than the battle one.
        log(f"    -> battle end (stage {battle.stage_id}, "
            f"{'win' if battle.wave_cleared() else 'loss'})")
        return battle_end_reward(battle, state) if state else []
    if cmd == bt.REQ_AUTO:
        # ServerRPCSetAuto(set) -> intargs=[set], no strargs. HandleAutoSet
        # (0x1688FEC) does NOTHING unless strargs[0] equals PlayerGeneral.Uid
        # (+0x28) -- the whole body is inside that string comparison -- so the reply
        # must name the player. It then sets GameSetting.BattleAuto and fires
        # BattleEvent 6.
        want = intargs[0] if intargs else 0
        battle.auto = bool(want)
        log(f"    -> auto-battle {'on' if want else 'off'}")
        msgs = [battle_msg(bt.CMD_AUTO_SET, [want], [uid])]
        # Turning auto ON mid-turn: the client has already hidden the skill bar and is
        # idling in SituationJudge, so nothing will ask us for anything again. Kick the
        # turn here or the fight deadlocks until auto is switched back off.
        if battle.auto and battle.turn_open and battle.player_turn():
            msgs += play_turn_msgs(battle, bt.TEAM_ENEMY)
            log("    -> auto kick (turn was already open)")
        return msgs
    if cmd == bt.REQ_CHAR_INFO:
        # ServerRPCCharInfo(order) -> strargs=[order]. The in-battle unit-detail
        # popup. HandleCharCurInfoCmd (0x168A6C0) deserializes strargs[0] as
        # **BattleCharData** -- a different class from the LightBattleChar in
        # BattleDatas -- and raises BattleEvent 7. A parse failure just logs, so a
        # bad shape is silent; check the popup actually fills in.
        order = strargs[0] if strargs else ""
        unit = battle.units.get(order)
        if not unit:
            log(f"    (battle: char info for unknown order {order!r})")
            return []
        log(f"    -> char info for order {order} (char {unit.char_id})")
        return [battle_msg(bt.CMD_CHAR_INFO, [],
                           [json.dumps(unit.battle_char_data(uid),
                                       separators=(",", ":"))])]
    if cmd == bt.REQ_CHANGE_CHAR:
        # ServerRPCChangeChar(order) -> strargs=[order of the BACKUP coming in].
        # HandleChangeCharCmd (0x168AE34) reads strargs[0]=incoming,
        # strargs[1]=outgoing and, only when there are 3+, strargs[2] as a
        # List<string> that replaces BattleData.CharListByScv AND ActionOrderList.
        # It swaps the two units' order fields and tweens their positions.
        incoming = strargs[0] if strargs else ""
        acting = battle.acting_unit()
        outgoing = acting.order if acting else ""
        if not battle.swap_units(incoming, outgoing):
            log(f"    (battle: change char rejected, {incoming!r} not on the field)")
            return []
        log(f"    -> change char {outgoing} <-> {incoming}")
        return [battle_msg(bt.CMD_CHANGE_CHAR, [],
                           [incoming, outgoing,
                            json.dumps(battle.action_order(),
                                       separators=(",", ":"))])]
    if cmd == bt.REQ_SELECT_RUNE:
        # ServerRPCRuneSelect(idx) -> intargs=[idx], the candidate the player kept.
        # **Reply 1508 carries [itemID, itemCount], NOT the index.** HandleSelectRune
        # (0x168A5A8) re-raises the intargs as BattleEvent 12, and the listener is
        # PanelBattleRuneResult.OnBattleEnd (0x16BAB78), which returns early unless the
        # list has >= 1 entry and then reads [0] as ItemID and [1] as ItemCount for its
        # reward icon -- and only after that does it reach UpdateResultState(7), the
        # call that ends the sequence. Echoing the index alone leaves it hanging.
        pick = intargs[0] if intargs else 0
        held = (state or {}).get("_rune_pick") or {}
        cands = held.get("cands") or []
        if not cands:
            # **508 ARRIVES TWICE.** The panel's countdown auto-fires
            # ServerRPCRuneSelect on expiry (GetRuneTimer's MoveNext, 0x16BB658), so a
            # manual Claim followed by the timer running out sends it a second time.
            # Answering [0, 0] made the panel render item id 0. Replay the same grant
            # instead -- idempotent, and it keeps the reply honest.
            last = (state or {}).get("_rune_last")
            if not last:
                log(f"    -> rune select {pick}: nothing held, ignoring")
                return []
            log(f"    -> rune select {pick}: repeat, re-answering item {last[0]}")
            return [battle_msg(bt.CMD_SELECT_RUNE, [int(last[0]), int(last[1])], [])]
        entry = cands[pick] if 0 <= pick < len(cands) else cands[0]
        ps.store_rune(state, entry)
        state.pop("_rune_pick", None)
        state["_rune_last"] = [int(entry["iid"]), 1]
        ps.save(state)
        log(f"    -> starshard kept: item {entry['iid']} uid {entry['uid']} "
            f"(of {len(cands)} offered)")
        # **The storage sync alone leaves the COUNT stale.** Cmd 84-87 carries the item
        # list and raises BackpackEvent 4; the "Inventory n/999" figure comes from the
        # backpack INFO rows, which only cmd 83 and BACKPACK_CHANGE carry. Pushing just
        # the list showed the new shard in the grid while the counter still read the
        # login value -- 3 icons over "2/999". BACKPACK_CHANGE sends both, and raises
        # event 1, the one an already-open panel refreshes on.
        return [battle_msg(bt.CMD_SELECT_RUNE, [int(entry["iid"]), 1], []),
                backpack_msg(BACKPACK_CHANGE, [0],
                             [ps.backpacks_all_json(state,
                                                    {ps.BP_STORAGE_EQUIPMENT}),
                              ps.backpack_info_json(state)])]
    if cmd == bt.REQ_RECONNECT:
        # Same cmd for BOTH buttons on the "rejoin your battle?" prompt --
        # CB_Reconnect sends intargs=[1] on accept, CB_ReconnectCancel sends [0] on
        # decline. Accept: replay the same BattleDatas handoff the fight started with
        # (we hold the live Battle -- reconstructed at login if this is a fresh
        # process, see handle()'s LOGIN branch). Decline: send nothing back (forcing
        # the player into a battle they just said no to would be worse than the
        # prompt itself) -- the caller drops the saved snapshot for a decline the
        # same way it does for an explicit retreat, so the prompt does not nag again
        # on the next login.
        accept = bool(intargs) and intargs[0] == 1
        if not accept:
            log(f"    -> reconnect DECLINED (stage {battle.stage_id}) -- dropping "
                "the saved battle")
            return []
        log(f"    -> reconnect to stage {battle.stage_id}, wave "
            f"{battle.wave}/{battle.wave_max}")
        battle.turn_open = False
        return [start_battle_msg(battle)]
    if cmd in (bt.REQ_REPLAY, bt.REQ_REPLAY_LAST):
        # We keep no battle records, and HandleReplayBattleRecord (0x168B378) has a
        # first-class way to say so: intargs=[chunk, total] with the assembled
        # strargs equal to the sentinel makes it clear the buffer and show
        # ConfirmMsg 33 instead of entering ReplayMode on empty data.
        log("    -> replay unavailable (no records kept)")
        return [battle_msg(bt.CMD_REPLAY_RECORD, [1, 1], [bt.REPLAY_NONE])]
    if cmd == bt.REQ_REPORT_ERROR:
        # ServerRPCReportError(errSkillID) -> intargs=[skill id],
        # strargs=[tempReply]. Pure diagnostics: there is no ReportError in
        # BattleRpcClientCmd, so the client neither expects nor waits for anything.
        skill = intargs[0] if intargs else 0
        log(f"    (ack Battle.ReportError, skill {skill} -- no reply cmd exists)")
        return []
    log(f"    (battle: no handler for cmd {cmd})")
    return []


def heartbeat_reply(req_id):
    """PlayerSessionClientCmd cmd=17. HandleHeartbeatReply requires intargs of size
    3 or 4 ([server_unix_time, boot_time, reset_day_time], optional 4th==1 forces a
    battle-match check) and `id` must be the id from the client's request -- it is
    looked up via AsyncOpManager.TryGetManagedAsyncOp, and an unknown id is dropped
    silently, which is what "Heartbeat timeout" actually means.
    """
    return rpc_pack(make_rpc(PLAYER_SESSION,
                             uint32s=[[SESSION_HEARTBEAT_REPLY],
                                      [int(time.time()), BOOT_TIME, RESET_DAY_TIME]],
                             uint64s=[[req_id]],
                             strings=[[]]))


# ---- connection ----------------------------------------------------------

def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        d = conn.recv(n - len(buf))
        if not d:
            return None
        buf += d
    return buf


def handle(conn, addr):
    log(f"[+] CONNECT from {addr}")
    c2s, s2c = RC4(KEY_C2S), RC4(KEY_S2C)
    state, cur_battle = None, None
    conn.settimeout(120)

    def send(mtype, body):
        frame = make_header(mtype, len(body)) + body
        conn.sendall(s2c.crypt(frame))
        log(f"[>] type={mtype} size={len(body)} body={body.hex()}")

    try:
        while True:
            raw = recv_exact(conn, HDR_LEN)
            if raw is None:
                log(f"[-] {addr} closed by peer")
                return
            hdr = c2s.crypt(raw)
            chk, mtype = hdr[0], hdr[1]
            size = int.from_bytes(hdr[2:6], "big")
            if chk != chksum(mtype, size):
                log(f"[!] BAD CHECKSUM hdr={hdr.hex()} type={mtype} size={size} "
                    f"(expected {chksum(mtype, size):#04x}) -- stream desync, "
                    f"check KeyC2S")
                return
            body = c2s.crypt(recv_exact(conn, size)) if size else b""
            log(f"[<] type={mtype} size={size} body={body.hex()}")

            msg = pb_parse(body)
            if mtype == MSG_LOGIN:
                lg = pb_parse(msg[11][0]) if 11 in msg else {}
                user = lg.get(1, [b""])[0].decode("utf-8", "replace")
                jd = lg.get(3, [b""])[0].decode("utf-8", "replace")
                log(f"    LOGIN username={user!r} json_data={jd!r}")
                # username is the titan token "titan_token_<player_id>"
                pid = user.rsplit("_", 1)[-1] or "1000001"
                state = ps.load(pid)
                # Catch accounts that finished the tutorial in a prior session (or before
                # this reset existed): fold them to base before the login sync is built.
                if ps.maybe_reset_tutorial_casts(state):
                    ps.save(state)
                log(f"    state loaded for player {pid} from {ps.path_for(pid)}")
                # Rebuild a saved in-progress battle so it can actually answer
                # REQ_RECONNECT (which needs a live Battle to replay), not just
                # advertise reconnectCase=1 in the sync reply. A save that no longer
                # reconstructs (a stage pulled from design data, say) drops itself
                # rather than crashing the login -- an offer to resume that then
                # throws on acceptance is worse than no offer at all.
                saved = ps.saved_battle(state)
                if saved:
                    try:
                        cur_battle = bt.restore_battle(saved)
                        log(f"    -> resumed in-progress battle: stage "
                            f"{cur_battle.stage_id}, wave "
                            f"{cur_battle.wave}/{cur_battle.wave_max}")
                    except Exception:                             # noqa: BLE001
                        log("    -> saved battle could not be restored, dropping it")
                        ps.clear_battle(state)
                        ps.save(state)
                # TITAN_LOGIN_FAIL=<errno> replies failure instead -- a probe to prove
                # the client really parses our S2C stream (it should surface the errno).
                fail = os.environ.get("TITAN_LOGIN_FAIL")
                if fail:
                    send(MSG_LOGIN, login_reply(False, int(fail)))
                else:
                    send(MSG_LOGIN, login_reply(True, 0))
                    # The client sits idle after LoginReply -- it needs the session
                    # NotifyLoggedIn push to leave the title screen.
                    time.sleep(0.2)
                    send(MSG_RPC, notify_logged_in())
            elif mtype == MSG_RPC:
                for raw_rpc in msg.get(31, []):
                    index, cmd, rid, intargs, strargs, strargs2 = parse_rpc(raw_rpc)
                    log(f"    RPC index={index:#010x} cmd={cmd} id={rid} "
                        f"int={intargs} str={strargs}")
                    if index == PLAYER_SESSION_SERVER and cmd == SESSION_HEARTBEAT_REQUEST:
                        send(MSG_RPC, heartbeat_reply(rid))
                    elif index == PLAYER_STAGE_SERVER and cmd == STAGE_REQ_AVG_SYNC:
                        # HandleAVGSyncReplyCmd wants EXACTLY 4 ints and stores the
                        # first three in PlayerStage.mOptionTag.
                        #   [0] = the option already chosen for this scene (0 = none)
                        #   [1] = which options are UNLOCKED, encoded as decimal
                        #         digits: AvgUIOptions.UpdateAVGOptionLockState reads
                        #         digit i as `tag[1] / 10^i % 10` and unlocks
                        #         _btnOptions[digit - 1]. With 0 it locks the lot,
                        #         which is why every choice came up chained.
                        # 4321 therefore unlocks options 1..4; scenes with fewer
                        # buttons simply never read the higher digits.
                        # [0] is the option already locked in for this scene. A choice
                        # is PERMANENT per difficulty -- replaying a stage re-shows the
                        # scene with the previous pick set, and only a different
                        # difficulty lets you choose again -- so this has to be the
                        # stored answer, not a flat 0.
                        chosen = ps.avg_choice(state, rid)
                        log(f"    -> avg sync reply (avg {rid}, locked option {chosen})")
                        send(MSG_RPC, uint64_msg(PLAYER_STAGE, STAGE_RPLY_AVG_SYNC,
                                                 [chosen, AVG_OPTIONS_UNLOCKED, 0, 0],
                                                 [], req_id=rid))
                    elif index == PLAYER_STAGE_SERVER and cmd == STAGE_REQ_AVG_CHOICE:
                        # MUST be exactly 4 ints: HandleAVGChoice only calls
                        # PanelAvg.SetRewardInfo(currencySP, value, charID, favour)
                        # on that path, and SetRewardInfo is what lets the scene
                        # continue -- any other arg count hits a bare `return` and
                        # the AVG hangs on the choice.
                        #
                        # [currencyType, currencyValue, charID, fexp]. charID is a
                        # DesignRoleModelInfoForm row -- an ordinary char id, NOT the
                        # avg_role id an older note here guessed at.
                        #
                        # Which option pays what is NOT recoverable: the per-option
                        # values lived on the original server and the `avg` design form
                        # is not even in our pack. KARMA_REWARDS is therefore keyed off
                        # footage; anything not in it falls back to the default so the
                        # story still pays out and keeps moving. The avg id is logged so
                        # new decisions can be added as they are observed.
                        # Unlike the AVG *sync* request, which carries the scene in the
                        # request id, RequestServerAvgSelectOption puts BOTH values in
                        # intargs -- [avgID, optionIndex] -- and leaves the request id 0.
                        # The option index is 0-based.
                        avg_id = intargs[0] if intargs else 0
                        option = intargs[1] if len(intargs) > 1 else 0
                        cur_type, cur_val, char_id, fexp = ps.karma_reward(avg_id, option)
                        # A scene pays once. Re-deciding is only possible on another
                        # difficulty, and set_avg_choice is what enforces that.
                        first_time = ps.set_avg_choice(state, avg_id, option)
                        if not first_time:
                            cur_val = fexp = 0
                        if cur_val:
                            ps.grant_currency(state, cur_type, cur_val)
                        karma = ps.grant_karma(state, char_id, fexp) if fexp else None
                        ps.save(state)
                        log(f"    -> avg choice reply (avg {avg_id}, option {option}"
                            f"{'' if first_time else ', ALREADY DECIDED - no payout'}): "
                            f"currency {cur_type}x{cur_val}, char {char_id} +{fexp} "
                            f"karma -> {karma}")
                        send(MSG_RPC, uint64_msg(PLAYER_STAGE, STAGE_RPLY_AVG_CHOICE,
                                                 [cur_type, cur_val, char_id, fexp], [],
                                                 req_id=rid))
                        # the banner is display-only; the grant only sticks if the
                        # currency and the CharIDData karma are pushed back
                        if cur_val:
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                        if fexp:
                            send(MSG_RPC, uint_msg(0x771EA36E, 528, [1, 1],
                                                   [ps.char_json(state)]))
                    elif (index == PLAYER_GACHA_SERVER
                          and cmd == GACHA_REQ_DRAW_ROULETTE):
                        box_id = intargs[0] if intargs else 101
                        ok, results, why = ps.roulette_draw(state, box_id)
                        if ok:
                            ps.save(state)
                            log(f"    -> roulette {box_id} drew {results}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_GACHA, GACHA_RPLY_ROULETTE_OK, [box_id],
                                [json.dumps(results, separators=(",", ":")),
                                 json.dumps(ps.roulette_info(state, box_id),
                                            separators=(",", ":"))]))
                            # The winnings only exist client-side once we re-push the
                            # bucket they landed in; the panel updates neither by itself.
                            # Push both -- a slot can pay coin/diamond (currency) or
                            # scrolls and orbs (backpack).
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                        else:
                            log(f"    -> roulette {box_id} refused: {why}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_GACHA, GACHA_RPLY_ROULETTE_FAIL,
                                [box_id], []))
                    elif index == PLAYER_GACHA_SERVER and cmd == GACHA_REQ_DRAW:
                        # A redraw box rolls for free and commits on RedrawSave.
                        # TODO: the banner now offers four buttons (free daily / 600
                        # diamonds / 1 scroll / 10 scrolls) but we still always roll 10
                        # and charge scrolls in gacha_commit. Log the request so the
                        # box id + draw type + cost category can be read off a real
                        # press and the charge wired to the button actually used.
                        # intargs = [boxId, drawIndex], drawIndex 1-based into that
                        # box's cost_tbl -- so it names both the price AND whether this
                        # is a single or a ten-pull.
                        box_id = intargs[0] if intargs else ps.GACHA_BOX_ID
                        draw_ix = intargs[1] if len(intargs) > 1 else 1
                        cost_item, price, count = ps.gacha_cost_row(
                            state, box_id, draw_ix)
                        log(f"    -> gacha draw box {box_id} option {draw_ix}: "
                            f"{count} pull(s) for {price}x item {cost_item}")
                        if price == 0 and ps.gacha_free_available(state):
                            ps.use_gacha_free(state)
                            log("    -> that was the free daily pull")
                        results = ps.gacha_draw(state, count,
                                                cost=(cost_item, price),
                                                box_id=box_id)
                        # Only the tutorial box re-rolls. Every other banner has NO
                        # commit command of its own -- RedrawBoxDoGetDraw (20) is
                        # redraw-only -- so a regular pull has to be granted here or it
                        # is displayed and then silently dropped.
                        if not ps.gacha_is_redraw_box(box_id):
                            kept = ps.gacha_commit(state)
                            log(f"    -> gacha draw box {box_id} committed: {kept}")
                        else:
                            log(f"    -> gacha draw (pending): {results}")
                        ps.save(state)
                        send(MSG_RPC, uint_msg(
                            PLAYER_GACHA, GACHA_RPLY_DRAW, [1, 0, 0, 0, 0],
                            [ps.gacha_box_json(state, box_id),
                             json.dumps(results, separators=(",", ":"))]))
                        if not ps.gacha_is_redraw_box(box_id):
                            # Push whatever the pull actually changed.
                            if ps.gacha_is_soulmirror_box(box_id):
                                send(MSG_RPC, backpack_msg(
                                    86, [1],
                                    [ps.backpack_json(
                                        state, ps.BP_STORAGE_SOULFRAG)]))
                            else:
                                send(MSG_RPC, uint_msg(
                                    PLAYER_CHAR, 528, [1, 1],
                                    [ps.char_json(state)]))
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif index == PLAYER_GACHA_SERVER and cmd == GACHA_REQ_REDRAW_GET:
                        # Collect!: commit the pending roll, charge it, count the pull.
                        box_id = intargs[0] if intargs else ps.GACHA_BOX_ID
                        kept = ps.gacha_commit(state)
                        ps.save(state)
                        log(f"    -> redraw commit (box {box_id}): kept {len(kept)}, "
                            f"gacha count {state.get('gacha_count')}")
                        # Push the updated collections BEFORE the reply. The 277 is
                        # what sends the client back to the Goal panel, and although
                        # PanelGoalQuest.OnQuestSynced does MarkUIDirty, a sync that
                        # lands after the panel has already rebuilt is missed -- the
                        # step only appeared after leaving and re-entering.
                        send(MSG_RPC, uint_msg(PLAYER_CHAR, 528, [1, 1],
                                               [ps.char_json(state)]))
                        send(MSG_RPC, backpack_msg(84, [1],
                                                   [ps.backpack_json(state, 1)]))
                        send(MSG_RPC, quest_sync_msg(state))
                        send(MSG_RPC, uint_msg(PLAYER_GACHA,
                                               GACHA_RPLY_REDRAW_GET, [box_id], []))
                        # The gacha sync goes LAST. ReceiveRedrawBoxDoGetDraw looks the
                        # box up via GachaBoxIDToIndex[box_id], so pushing a box list
                        # that no longer contains it (the tutorial box is replaced by
                        # the standing banners once gacha_count > 0) makes the 277
                        # throw and Collect! silently do nothing. Sending it after also
                        # refreshes PanelGacha's scroll label, which only redraws off
                        # the gacha sync path (UpdateGachaToken).
                        send(MSG_RPC, uint_msg(PLAYER_GACHA, 257, [1, 1],
                                               [ps.gacha_json(state)]))
                    elif index == PLAYER_GACHA_SERVER and cmd == GACHA_REQ_REDRAW_SAVE:
                        box_id = intargs[0] if intargs else ps.GACHA_BOX_ID
                        hist = state.get("gacha_pending") or []
                        log(f"    -> redraw save-to-history (box {box_id})")
                        send(MSG_RPC, uint_msg(
                            PLAYER_GACHA, GACHA_RPLY_REDRAW_SAVE, [box_id],
                            [json.dumps(hist, separators=(",", ":"))]))
                    elif index == PLAYER_MAIL_SERVER and cmd == MAIL_REQ_LIST_PANEL:
                        # The Mail panel's own list request. Echo `rid` -- it waits on an
                        # AsyncOp keyed by it, and SetMailList also buffers the chunks
                        # under that id, finalising only when dataEnd (intargs[1]) is 1.
                        log(f"    -> mail list reply ({len(state.get('mail', []))} "
                            f"mails, {ps.unread_mail_count(state)} unread, id {rid})")
                        send(MSG_RPC, uint64_msg(PLAYER_MAIL, MAIL_RPLY_LIST, [0, 1],
                                                 [ps.mail_list_json(state)],
                                                 req_id=rid))
                        send(MSG_RPC, uint64_msg(
                            PLAYER_MAIL, MAIL_RPLY_UNREAD,
                            [0, ps.unread_mail_count(state), 0], [], req_id=rid))
                    elif index == PLAYER_MAIL_SERVER and cmd in (
                            MAIL_REQ_RECEIVE, MAIL_REQ_RECEIVE_ALL):
                        # cmd 5 claims one mail (uid in strargs[0]), cmd 7 claims every
                        # unread one. The reply is cmd 8 whose strargs[0] is the JSON
                        # list of uids actually claimed -- ReceiveAllAttachments marks
                        # exactly those read and pops the item display for them.
                        want = strargs if cmd == MAIL_REQ_RECEIVE and strargs else None
                        claimed, buckets = ps.claim_mail(state, want)
                        ps.save(state)
                        log(f"    -> mail receive {claimed} (buckets {sorted(buckets)})")
                        send(MSG_RPC, uint64_msg(
                            PLAYER_MAIL, MAIL_RPLY_RECEIVE, [0],
                            [json.dumps(claimed, separators=(",", ":"))], req_id=rid))
                        # the popup is display only -- push whatever the grant touched
                        if "backpack" in buckets:
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                        if "currency" in buckets:
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                        if "energy" in buckets:
                            send(MSG_RPC, uint_msg(0xAE487D79, 512, [],
                                                   [ps.energy_json(state)]))
                    elif index == PLAYER_QUEST_SERVER and cmd == QUEST_REQ_COMPLETED:
                        # intargs = the quest ids being claimed.
                        rewards, new_chars = ps.complete_quests(state, intargs)
                        ps.save(state)
                        log(f"    -> quests claimed: {rewards}")
                        # Claiming the last newbie quest ends the tutorial -> revert the
                        # boosted starter casts to base and re-sync so the lobby matches.
                        if ps.maybe_reset_tutorial_casts(state):
                            ps.save(state)
                            log("    -> tutorial complete: starter casts reset to base")
                            send(MSG_RPC, uint_msg(0x771EA36E, 528, [1, 1],
                                                   [ps.char_json(state)]))
                        # **A cast reward arrives through Char `create` (529), not the
                        # reward popup.** receivedCreateChar (0x16992F0) deserialises
                        # strargs[0] as Dictionary<uid, CharData>, AddChar's each one
                        # into charDic and dispatches CharEvent 4 -- that is what both
                        # grants the character and drives the single-pull reveal the
                        # live game plays on claim. The quest reply (513) only ever
                        # builds an ItemPopupInfo, so it cannot deliver a cast: its
                        # CharDatas field is never set on that path.
                        # Sent BEFORE the reward reply so the reveal leads and the item
                        # popup follows, which is the order the footage shows.
                        if new_chars:
                            log(f"    -> cast reward: granted {new_chars}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_CREATE, [],
                                [ps.char_create_json(state, new_chars)]))
                        triples = [v for r in rewards for v in r]
                        send(MSG_RPC, uint_msg(PLAYER_QUEST, QUEST_RPLY_REWARD,
                                               triples, []))
                        # A BUNDLE reward pays several things, and reply 513's triples
                        # carry only the headline. The live claim popup shows every
                        # line side by side (★4 Jacqueline AND Evolution Gem x1200), so
                        # follow up with the drop-item popup that can list them all --
                        # the same message the storefront bundles use.
                        for _q, rid, _c in rewards:
                            lines = ps.goods_payout_lines(
                                state, (bt.dd.row("quest", _q) or {}).get("_item_id"))
                            if len(lines) > 1:
                                send(MSG_RPC, backpack_msg(
                                    BACKPACK_RPLY_DROP_ITEM, [],
                                    [json.dumps({str(i): c for i, c in lines},
                                                separators=(",", ":"))]))
                        send(MSG_RPC, quest_sync_msg(state))
                        # The reward popup is display only and the client caches the bag
                        # and the currencies from the login sync, so without these the
                        # granted items exist ONLY server-side -- which is why the gacha
                        # still read 0 scrolls right after the 1-1 goal paid out 10.
                        # Count the bundle's LINES, not just its headline: a bundle
                        # whose head is a cast would otherwise report "backpack" for a
                        # payout that actually moved currency or energy.
                        paid = [(iid, cnt) for _q, iid, cnt in rewards if iid and cnt]
                        for _q, _rid, _c in rewards:
                            paid += ps.goods_payout_lines(
                                state, (bt.dd.row("quest", _q) or {}).get("_item_id"))
                        buckets = {ps.item_bucket(iid) for iid, cnt in paid if iid and cnt}
                        if "backpack" in buckets:
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                        if "currency" in buckets:
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                        if "energy" in buckets:
                            send(MSG_RPC, uint_msg(0xAE487D79, 512, [],
                                                   [ps.energy_json(state)]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_FORMATION:
                        # intargs = [teamIndex + 1, support, 1]; strargs = the slot
                        # uids. Persist it and echo the stored FormationData back --
                        # the client does not apply its own edit until this lands.
                        team_no = intargs[0] if intargs else 1
                        support = intargs[1] if len(intargs) > 1 else 0
                        data = ps.set_formation(state, team_no - 1, strargs, support)
                        log(f"    -> formation {team_no} set to {data['array']} "
                            f"(support {support})")
                        send(MSG_RPC, uint_msg(
                            PLAYER_CHAR, CHAR_RPLY_FORMATION, [team_no, 0],
                            [json.dumps(data, separators=(",", ":"))]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_SYNC_ID_DATA:
                        # The Soulpedia ("Cast -> Soulpedia", PanelCharacterList in
                        # illustration mode). OnEnterIllustrationCharacterList calls
                        # PlayerChar.RequestCharIDData() -> cmd 310, and the grid does
                        # not draw until the reply lands: receivedSyncIDData is what
                        # dispatches CharEvent 21, the event the panel repopulates on.
                        # Unanswered, the panel opens as an empty black page with only
                        # its frame -- no grid, no tabs, no completion label.
                        #
                        # Reply is cmd 567 with intargs [bookRank, bookSumXp,
                        # bookLeftXp] -- all three are indexed unconditionally, so a
                        # short list throws -- and strargs[0] = charIDDic. strargs may
                        # be empty (the handler skips the deserialize and still fires
                        # the event), but then the pedia cannot mark anything owned.
                        rank, sum_xp, left_xp = ps.book_progress(state)
                        id_tbl = ps.char_id_table(state)
                        log(f"    -> charIDData sync ({len(id_tbl)} owned, "
                            f"book rank {rank})")
                        send(MSG_RPC, uint_msg(
                            PLAYER_CHAR, CHAR_RPLY_SYNC_ID_DATA,
                            [rank, sum_xp, left_xp],
                            [json.dumps(id_tbl, separators=(",", ":"))]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_GIFT:
                        # Consonance -> Send Gifts. intargs =
                        # [charID, itemId, amount, itemId, amount, ...], no strargs.
                        # Identified by CHAR ID, not roster uid.
                        #
                        # giftReply reads ONLY intargs[0] and feeds it straight into
                        # UICharacterRoom._lbAddKizunaExpNum -- so that value is the
                        # KARMA XP GAINED (the floating "+N"), not the char id. It
                        # refreshes nothing else, which is why the karma rank on the
                        # surrounding page stays stale until the panel is reopened.
                        #
                        # The actual state has to be pushed separately with
                        # update_friendly (552): strargs[0] = {"<charId>": [flv, fxp]},
                        # which receivedUpdateFriendly writes into charIDDic (arr[0] ->
                        # friendlyLevel, arr[1] -> friendlyXp) and flags as updated.
                        # Send the data BEFORE the reply so the "+N" tween lands on
                        # already-current numbers.
                        char_id = intargs[0] if intargs else 0
                        pairs = list(zip(intargs[1::2], intargs[2::2]))
                        ok, karma_xp, used = ps.give_gifts(state, char_id, pairs)
                        if ok:
                            ps.save(state)
                            k = ps.karma_of(state, char_id)
                            log(f"    -> gifts to char {char_id}: {used} = +{karma_xp} "
                                f"karma -> rank {k['flv']} ({k['fxp']} xp)")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_UPDATE_FRIENDLY, [],
                                [json.dumps({str(char_id): [k["flv"], k["fxp"]]},
                                            separators=(",", ":"))]))
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                            # cmd 84 updates the bag data but dispatches BackpackEvent
                            # 4, which no open panel listens to. Only cmd 145
                            # (HandleBackpackChagne) dispatches BackpackEvent 1, which
                            # is what UICharacterRoom.OnCharGiftUpdate subscribes to --
                            # so this is what makes an already-open gift list redraw
                            # instead of correcting itself on reopen.
                            # cmd 145 (HandleBackpackChagne) is PARKED, not solved.
                            # It is the only command that dispatches BackpackEvent 1,
                            # the event an open panel refreshes on, so it is what would
                            # make gift counts tick down live rather than on reopen.
                            # It currently throws. What is established:
                            #
                            #   145 off             -> 0 NREs / 2 gifts
                            #   145 on, real data   -> 1 NRE  / 1 gift
                            #   145 on, EMPTY data  -> 2 NREs / 2 gifts   (payload is
                            #                          NOT the trigger)
                            #   145 on, at LOGIN with no panel open -> still throws
                            #                       => an always-on subscriber, not a
                            #                          character-room panel
                            #
                            # Ruled out: PlayerBackpack's ctor allocates _storageList
                            # and _backpackInfo, so UIItemConsole.OnBackpackUpdate's
                            # two dictionary derefs are not the null. BackpackEvent 1
                            # has ~20 subscribers (xrefs to
                            # Method$EventDispatcher_BackpackEvent_.AddListener) and
                            # the catch in TitanStack.StackCore.poll logs only
                            # e.Message, so the stack is lost. Frida cannot attach
                            # (the emulator's ARM translation layer breaks it), so the
                            # remaining approach is to enumerate the subscribers that
                            # are live at login and check each handler's derefs against
                            # what we sync.
                            #
                            # Left OFF: it throws AND does not refresh, so it buys
                            # nothing over the stale-until-reopen behaviour. Counts are
                            # correct -- cmd 84 above updates the data, it just
                            # dispatches BackpackEvent 4, which no open panel observes.
                        else:
                            log(f"    -> gift REFUSED for char {char_id} "
                                f"(pairs={pairs}) -- not held, or not a gift item")
                        send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_GIFT,
                                               [karma_xp], []))
                    elif index == PLAYER_JSAGENT_SERVER and cmd == JSAGENT_ULTRA_TRANSCEND:
                        # Ultra Transcend, routed through PlayerJSAgent (the super-limit
                        # UI is Puerts JS, so it never touches a CharRpc command).
                        #   strargs[0] group = ["PlayerChar"]  (JS module)
                        #   strargs[1] group = [target uid, ...duplicate material uids]
                        module = strargs[0] if strargs else "PlayerChar"
                        uids = list(strargs2 or [])
                        tgt = uids[0] if uids else ""
                        ok, used, lv, coins = ps.ultra_transcend(state, tgt, uids[1:])
                        if ok:
                            ps.save(state)
                            e = state["roster"].get(tgt, {})
                            log(f"    -> ultra transcend {tgt} -> super_limit "
                                f"{e.get('super_limit')} (+{lv} lv, ate {len(used)} "
                                f"dupes for {coins} coins)")
                            # 545 shares receivedOneCharAndRemove, so one message both
                            # updates the cast (super_limit rides in dbdata) and deletes
                            # the consumed duplicates from charDic.
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, 545, [],
                                [ps.char_data_json(state, tgt)] + used))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                        else:
                            log(f"    -> ultra transcend REFUSED tgt={tgt!r} "
                                f"mats={uids[1:]} -- not rarity 5, not duplicates, "
                                f"at cap, or not enough coins")
                        # Echo back on the JSAgent client index so the JS side of the
                        # panel stops waiting. PAYLOAD IS A GUESS: the listener lives in
                        # Puerts JS which does not ship in the APK, so the shape cannot
                        # be read -- mirroring the request (same module, same cmd, same
                        # uid list) is the most defensible thing to send.
                        send(MSG_RPC, jsagent_msg(JSAGENT_ULTRA_TRANSCEND, module,
                                                  [], uids))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_KIZUNA_SET:
                        # Soul Book Kizuna, equip. intargs = [charID, ...rowIds]; the
                        # reply mirrors it, and receivedBookKizunaSet rebuilds the list
                        # from those ids while reading each level out of the
                        # BookKizunaLvData it already holds from the char sync.
                        cid = intargs[0] if intargs else 0
                        rows = list(intargs[1:]) if len(intargs) > 1 else []
                        ok, stored = ps.set_kizuna(state, cid, rows)
                        if ok:
                            ps.save(state)
                            log(f"    -> kizuna set char {cid} -> {stored}")
                        else:
                            log(f"    -> kizuna set REFUSED char {cid} rows={rows} "
                                f"-- more than {ps.MAX_KIZUNA_EQUIP} equipped")
                        send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_KIZUNA_SET,
                                               [cid] + stored, []))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_KIZUNA_LVUP:
                        # Soul Book Kizuna, level up. intargs = [charID, rowID, nowLv]
                        # in and [charID, rowID, NEW level] back.
                        cid = intargs[0] if intargs else 0
                        row = intargs[1] if len(intargs) > 1 else 0
                        now = intargs[2] if len(intargs) > 2 else 0
                        ok, lv = ps.kizuna_level_up(state, cid, row, now)
                        if ok:
                            ps.save(state)
                            log(f"    -> kizuna lvup char {cid} row {row}: "
                                f"{now} -> {lv}")
                        else:
                            log(f"    -> kizuna lvup REFUSED char {cid} row {row} "
                                f"-- client says lv {now}, we have {lv}")
                        send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_KIZUNA_LVUP,
                                               [cid, row, lv], []))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_LIMIT_IMPART:
                        # Inherit. intargs = [costType] (1 = Inherit Gem, 2 = diamonds),
                        # strargs = [apprentice uid, mentor uid]. Reply 544 has its own
                        # handler, receivedLimitImpart, which writes exactly three ints:
                        # apprentice.limit_book, mentor.limit_book, mentor.limit_char.
                        in_uid = strargs[0] if strargs else ""
                        out_uid = strargs[1] if len(strargs) > 1 else ""
                        ctype = intargs[0] if intargs else ps.INHERIT_COST_ITEM
                        ok, a_book, m_book, m_char, why = \
                            ps.inherit_char(state, in_uid, out_uid, ctype)
                        if ok:
                            ps.save(state)
                            log(f"    -> inherit {out_uid} -> {in_uid} "
                                f"(costType {ctype}; apprentice book {a_book}, "
                                f"mentor {m_book}+{m_char})")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_LIMIT_IMPART,
                                [a_book, m_book, m_char], [in_uid, out_uid]))
                            # impartLimit lives in the General sync, so the "Number of
                            # Inherit Remaining" tip goes stale without a resync.
                            send(MSG_RPC, uint_msg(0x4C1872DD, 512, [],
                                                   [ps.general_json(state)]))
                            send(MSG_RPC, uint_msg(PLAYER_CHAR, 528, [1, 1],
                                                   [ps.char_json(state)]))
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_SYNC_ID_DATA,
                                list(ps.book_progress(state)),
                                [json.dumps(ps.char_id_table(state),
                                            separators=(",", ":"))]))
                            if ctype == ps.INHERIT_COST_DIAMOND:
                                send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                       [ps.currency_json(state)]))
                            else:
                                send(MSG_RPC, backpack_msg(
                                    84, [1],
                                    [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                        else:
                            # No reply at all would wedge the panel, so answer anyway --
                            # the client just rewrites the values it already has.
                            log(f"    -> inherit REFUSED {out_uid!r} -> {in_uid!r} "
                                f"(costType {ctype}) -- {why}")
                            a = state["roster"].get(in_uid, {})
                            m = state["roster"].get(out_uid, {})
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_LIMIT_IMPART,
                                [int(a.get("limit_book", 0)),
                                 int(m.get("limit_book", 0)),
                                 int(m.get("limit_char", 0))], [in_uid, out_uid]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_LIMIT_UP:
                        # Skill Up ("Skill Awaken"). Identical wire shape to plus up:
                        # strargs = [target uid, ...material uids], reply 537 is another
                        # receivedOneCharAndRemove (534/535/536/537/545 all share it).
                        uid = strargs[0] if strargs else ""
                        mats = list(strargs[1:]) if len(strargs) > 1 else []
                        ok, used, gained, coins = ps.skill_up(state, uid, mats)
                        if ok:
                            ps.bump_quest_counter(state, ps.QUEST_CASE_SKILL_UP)
                            ps.save(state)
                            send(MSG_RPC, quest_sync_msg(state))
                            e = state["roster"].get(uid, {})
                            log(f"    -> skill up {uid} -> limit "
                                f"{ps.char_limit(e)} (book {e.get('limit_book')} + "
                                f"char {e.get('limit_char')}, +{gained}, ate "
                                f"{len(used)} for {coins} coins)")
                        else:
                            log(f"    -> skill up REFUSED for {uid!r} mats={mats} -- "
                                f"in a formation, missing, at the limit cap, or not "
                                f"enough coins")
                        send(MSG_RPC, uint_msg(
                            PLAYER_CHAR, CHAR_RPLY_LIMIT_UP, [],
                            [ps.char_data_json(state, uid)] + (used if ok else [])))
                        if ok:
                            # Skill rank rides in charIDDic too (`skill` = char_limit),
                            # so the Soulpedia would go stale without a 567 refresh.
                            send(MSG_RPC, uint_msg(PLAYER_CHAR, 528, [1, 1],
                                                   [ps.char_json(state)]))
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_SYNC_ID_DATA,
                                list(ps.book_progress(state)),
                                [json.dumps(ps.char_id_table(state),
                                            separators=(",", ":"))]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_PLUS_UP:
                        # Transcend (plus up). strargs = [target uid, ...material uids].
                        # Reply 536 is receivedOneCharAndRemove: strargs[0] = updated
                        # CharData, strargs[1..] = the consumed materials to delete.
                        uid = strargs[0] if strargs else ""
                        mats = list(strargs[1:]) if len(strargs) > 1 else []
                        ok, used, pxp, coins = ps.transcend_char(state, uid, mats)
                        if ok:
                            ps.bump_quest_counter(state, ps.QUEST_CASE_TRANSCEND)
                            ps.save(state)
                            send(MSG_RPC, quest_sync_msg(state))
                            e = state["roster"].get(uid, {})
                            log(f"    -> transcend {uid} -> plus {e.get('plus')} "
                                f"(pxp {e.get('pxp')}, maxLv {ps.char_max_lv(e)}, "
                                f"ate {len(used)} casts for {pxp}pt / {coins} coins)")
                        else:
                            log(f"    -> transcend REFUSED for {uid!r} mats={mats} -- "
                                f"in a formation, missing, or not enough coins")
                        send(MSG_RPC, uint_msg(
                            PLAYER_CHAR, CHAR_RPLY_PLUS_UP, [],
                            [ps.char_data_json(state, uid)] + (used if ok else [])))
                        if ok:
                            send(MSG_RPC, uint_msg(PLAYER_CHAR, 528, [1, 1],
                                                   [ps.char_json(state)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_RANK_UP:
                        # Rank Up. strargs = [uid], no intargs. Reply 535 shares
                        # receivedOneCharAndRemove with level up (534), so strargs[0]
                        # is the updated CharData.
                        uid = strargs[0] if strargs else ""
                        ok, gems, coins = ps.rank_up_char(state, uid)
                        if ok:
                            ps.bump_quest_counter(state, ps.QUEST_CASE_RANK_UP)
                            ps.save(state)
                            send(MSG_RPC, quest_sync_msg(state))
                            e = state["roster"].get(uid, {})
                            log(f"    -> rank up {uid} -> star {e.get('star')} "
                                f"(maxLv {ps.char_max_lv(e)}, spent {gems} gems + "
                                f"{coins} coins)")
                        else:
                            log(f"    -> rank up REFUSED for {uid!r} -- star out of "
                                f"range, or not enough gems/coins")
                        # Always reply, or the panel waits forever (PanelSell pattern).
                        send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_RANK_UP, [],
                                               [ps.char_data_json(state, uid)]))
                        if ok:
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_LEVEL_UP:
                        # Level Training. intargs = how many of each trainer tier to
                        # feed (index 0..4 = items 101..105), strargs = [target uid].
                        #
                        # The reply is cmd 534, handled by receivedOneCharAndRemove:
                        # strargs[0] is the UPDATED CharData (deserialized and pushed
                        # through AddChar, replacing the entry in charDic) and any
                        # further strargs are uids to delete. Level training consumes
                        # items rather than casts, so there is nothing to delete here.
                        uid = strargs[0] if strargs else ""
                        ok, cost, used = ps.level_up_char(state, uid, intargs)
                        if not ok:
                            log(f"    -> level up REFUSED for {uid!r} "
                                f"(intargs={intargs}) -- missing trainers or coins")
                            # Still reply, or PanelCharacterUpgrade waits forever the
                            # same way PanelSell does.
                            send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_LEVEL_UP, [],
                                                   [ps.char_data_json(state, uid)]))
                        else:
                            ps.bump_quest_counter(state, ps.QUEST_CASE_POWER_UP)
                            ps.save(state)
                            send(MSG_RPC, quest_sync_msg(state))
                            entry = state["roster"].get(uid, {})
                            log(f"    -> level up {uid} -> lv {entry.get('lv')} "
                                f"xp {entry.get('xp')} (spent {cost} coins, {used})")
                            send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_LEVEL_UP, [],
                                                   [ps.char_data_json(state, uid)]))
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_LOCK:
                        # RequestServerLock(uids) sends strargs only -- NO intargs -- so
                        # the new state is ours to pick: it is a toggle. receivedLockChar
                        # applies ONE uid per reply and uses Dictionary.get_Item, which
                        # throws on an unknown uid, so send one message per uid we hold.
                        changed = ps.toggle_char_lock(state, strargs)
                        ps.save(state)
                        log(f"    -> lock toggled {changed}")
                        for uid, now in changed:
                            send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_LOCK,
                                                   [now], [uid]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_CHAR_MAX:
                        # RequestCharMax() sends no args. Case 533 assigns
                        # PlayerCharData.addCharCount = intargs[0], so this is the roster
                        # CAPACITY, not "max out a cast" as the name suggests.
                        cap = ps.char_capacity(state)
                        log(f"    -> char capacity add_char={cap}")
                        send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_CHAR_MAX,
                                               [cap], []))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_DECOMPOSE:
                        # RequestCharDecompose(List<string> decompose_uids). Same reply
                        # shape as Unsummon: strargs = uids to drop from charDic,
                        # intargs = FLAT [item id, amount, ...] for the reward popup.
                        done, gain = ps.decompose_chars(state, strargs)
                        if not done:
                            log(f"    -> decompose refused for {list(strargs)}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_DECOMPOSE_FAIL, [], []))
                        else:
                            ps.save(state)
                            log(f"    -> decompose {done} -> {gain}")
                            pairs = [v for i, c in gain for v in (i, c)]
                            send(MSG_RPC, uint_msg(PLAYER_CHAR,
                                                   CHAR_RPLY_DECOMPOSE, pairs, done))
                            send(MSG_RPC, uint_msg(PLAYER_CHAR, 528, [1, 1],
                                                   [ps.char_json(state)]))
                            buckets = {ps.item_bucket(i) for i, c in gain if i and c}
                            if "backpack" in buckets:
                                send(MSG_RPC, backpack_msg(
                                    84, [1],
                                    [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                            if "currency" in buckets:
                                send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                       [ps.currency_json(state)]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_SET_SORT:
                        # RequestSetSort(index, type, down). There is no set_sort in
                        # CharRpcClientCmd, so the client never waits -- just persist it
                        # so the next login sync returns the same ordering.
                        ix = intargs[0] if intargs else 0
                        ty = intargs[1] if len(intargs) > 1 else 0
                        dn = intargs[2] if len(intargs) > 2 else 0
                        ps.set_char_sort(state, ix, ty, dn)
                        ps.save(state)
                        log(f"    -> sort slot {ix} = {ty}_{dn} (no reply expected)")
                    elif (index == PLAYER_CHAR_SERVER
                          and cmd == CHAR_REQ_REMOVE_ALL_FORM):
                        # RequestServerRemoveFromAllFormation(uid). The reply carries the
                        # REBUILT formation dicts: strargs[0] normal, strargs[1] arena
                        # team. Both are indexed before any count check and both are
                        # dereferenced straight after deserializing, so send two real
                        # JSON objects -- "{}" for arena, which we do not model.
                        uid = strargs[0] if strargs else ""
                        forms = ps.remove_from_all_formations(state, uid)
                        ps.save(state)
                        log(f"    -> removed {uid} from all formations")
                        send(MSG_RPC, uint_msg(
                            PLAYER_CHAR, CHAR_RPLY_REMOVE_ALL_FORM, [],
                            [json.dumps(forms, separators=(",", ":")), "{}"]))
                    elif (index == PLAYER_CHAR_SERVER
                          and cmd == CHAR_REQ_WEAR_RUNE):
                        # RequestWearRune(char_uid, equips): NO intargs, and
                        # strargs = [...18 equip slots..., char_uid] with the uid LAST.
                        # The client has already applied its change to the array, so we
                        # validate and echo rather than trying to infer a slot.
                        char_uid = strargs[-1] if strargs else ""
                        want = list(strargs[:-1])
                        try:
                            slots = ps.wear_runes(state, char_uid, want)
                        except (KeyError, ValueError) as exc:
                            # No `char_wear_rune_fail` cmd exists, so there is nothing
                            # honest to answer with -- log it and leave the array alone.
                            log(f"    !! wear_rune refused for {char_uid!r}: {exc}")
                        else:
                            ps.save(state)
                            worn = [u for u in slots if u]
                            log(f"    -> {char_uid} wearing {len(worn)} equip(s): {worn}")
                            # Exactly CHAR_EQUIP_SLOTS + 1 strargs, uid last, or
                            # receivedUpdateEquip returns without a word.
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_UPDATE_EQUIP, [],
                                slots + [char_uid]))
                    elif (index == PLAYER_CHAR_SERVER
                          and cmd == CHAR_REQ_WEAR_BLOODPACT):
                        # RequestWearBloodPact(char_uid, equip): no intargs and
                        # strargs = [char_uid, equip_uid] -- uid FIRST, the reverse of
                        # 296. No slot index, so the server picks one of 12..14.
                        char_uid = strargs[0] if strargs else ""
                        equip_uid = strargs[1] if len(strargs) > 1 else ""
                        try:
                            slot, slots = ps.wear_bloodpact(
                                state, char_uid, equip_uid)
                        except (KeyError, ValueError) as exc:
                            log(f"    !! wear_bloodpact refused for "
                                f"{char_uid!r}: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> {char_uid} bloodpact slot {slot} = "
                                f"{equip_uid or '(cleared)'}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_UPDATE_EQUIP, [],
                                slots + [char_uid]))
                    elif (index == PLAYER_CHAR_SERVER
                          and cmd == CHAR_REQ_SET_HELPER):
                        # RequestServerSetHelper(char_uid): strargs=[uid], no intargs.
                        char_uid = strargs[0] if strargs else ""
                        try:
                            uid = ps.set_helper(state, char_uid)
                        except ValueError as exc:
                            log(f"    !! set_helper refused: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> helper = {uid}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_SET_HELPER, [], [uid]))
                    elif (index == PLAYER_CHAR_SERVER
                          and cmd == CHAR_REQ_SET_SHOWGIRL):
                        # RequestServerSetShowgirl(group, id, state, x, y, scale):
                        # intargs=[group, id, state], strargs=["x_y_scale"].
                        # The reply drops the group: receivedShowgirlData reads
                        # intargs[0]=id, intargs[1]=state, strargs[0]=offset.
                        sg_id = intargs[1] if len(intargs) > 1 else 0
                        sg_state = intargs[2] if len(intargs) > 2 else 0
                        offset = strargs[0] if strargs else ""
                        try:
                            sid, sstate, soff = ps.set_showgirl(
                                state, sg_id, sg_state, offset)
                        except ValueError as exc:
                            log(f"    !! set_showgirl refused: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> showgirl = {sid} state={sstate} "
                                f"offset={soff!r}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_SET_SHOWGIRL,
                                [sid, sstate], [soff]))
                    elif (index == PLAYER_CHAR_SERVER
                          and cmd == CHAR_REQ_UNLOCK_SKIN):
                        # RequestUnlockSkin: intargs=[skinType], strargs=[char_uid].
                        # The reply flips the flag on the per-CHARACTER-ID record, so it
                        # carries [charId, skinType] and NO strargs.
                        char_uid = strargs[0] if strargs else ""
                        skin_type = intargs[0] if intargs else 0
                        try:
                            char_id = ps.unlock_skin(state, char_uid, skin_type)
                        except (KeyError, ValueError) as exc:
                            log(f"    !! unlock_skin refused for "
                                f"{char_uid!r}: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> char {char_id} unlocked CharSoulType "
                                f"{skin_type}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_UNLOCK_SKIN,
                                [char_id, skin_type], []))
                            # The pedia record IS the unlock flag, so resend it or the
                            # gallery reverts on the next rebuild.
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_SYNC_ID_DATA, [],
                                [json.dumps(ps.char_id_table(state),
                                            separators=(",", ":"))]))
                    elif (index == PLAYER_CHAR_SERVER
                          and cmd == CHAR_REQ_SET_SKIN):
                        # RequestSetSkin: intargs=[skinType], strargs=[char_uid].
                        # receivedSetSkin echoes both back onto charDic[uid].dbChar.skin.
                        char_uid = strargs[0] if strargs else ""
                        skin_type = intargs[0] if intargs else 0
                        try:
                            chosen = ps.set_skin(state, char_uid, skin_type)
                        except (KeyError, ValueError) as exc:
                            log(f"    !! set_skin refused for {char_uid!r}: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> {char_uid} showing CharSoulType {chosen}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_SET_SKIN,
                                [chosen], [char_uid]))
                    elif (index == PLAYER_CHAR_SERVER
                          and cmd == CHAR_REQ_WEAR_SOULFRAG):
                        # RequestWearSoulFrag(char_uid, equip, itemAction):
                        # strargs = [equip_uid, char_uid], intargs = [slot index],
                        # where the index came from the client's own jump table at
                        # 0x3703540 (action 101..109 -> 6..11 / 15..17).
                        equip_uid = strargs[0] if strargs else ""
                        char_uid = strargs[1] if len(strargs) > 1 else ""
                        slot = intargs[0] if intargs else -1
                        try:
                            slots = ps.wear_soulmirror(
                                state, char_uid, equip_uid, slot)
                        except (KeyError, ValueError) as exc:
                            log(f"    !! wear_soulfrag refused for "
                                f"{char_uid!r}: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> {char_uid} soulmirror slot {slot} = "
                                f"{equip_uid or '(cleared)'}")
                            send(MSG_RPC, uint_msg(
                                PLAYER_CHAR, CHAR_RPLY_UPDATE_EQUIP, [],
                                slots + [char_uid]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_SELL:
                        # Unsummon -> "Mana Extract" (PanelSell action type 0).
                        # strargs = the selected cast uids.
                        #
                        # THIS REPLY ALSO UNWEDGES THE PANEL. PanelSell gates both
                        # CharListIconOnClickAction and OnCharSellClick on
                        # `_charSellFlag`, which it clears when it sends this request
                        # and only restores in OnCharUpdate / OnCharSellFailed -- i.e.
                        # on our reply. Leave 291 unanswered and the panel soft-locks:
                        # no selection, no button, and the lock survives leaving and
                        # re-entering the panel because the instance is cached. Only a
                        # game restart clears it.
                        #
                        # receivedCharSell reads strargs as the uids to drop from
                        # charDic, and intargs as FLAT [item id, amount, ...] pairs for
                        # the reward popup (it walks them two at a time).
                        sold, gain = ps.sell_chars(state, strargs)
                        ps.save(state)
                        log(f"    -> unsummon {sold} -> {gain}")
                        pairs = [v for item_id, amount in gain for v in (item_id, amount)]
                        send(MSG_RPC, uint_msg(PLAYER_CHAR, CHAR_RPLY_SELL,
                                               pairs, sold))
                        # The roster shrank and the payout has to reach the cached bag
                        # and currency, exactly as for quest rewards below.
                        send(MSG_RPC, uint_msg(PLAYER_CHAR, 528, [1, 1],
                                               [ps.char_json(state)]))
                        buckets = {ps.item_bucket(i) for i, c in gain if i and c}
                        if "backpack" in buckets:
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                        if "currency" in buckets:
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif index == PLAYER_CHAR_SERVER and cmd == CHAR_REQ_BOOK_RANK_UP:
                        # The Soulpedia's `RANK UP!` button. Soul Link rank is CLAIMED,
                        # not derived: the score accumulates on its own but the rank
                        # only moves when the player presses this, which is why live
                        # footage shows RANK 97 against a score already past rank 100.
                        # One press claims EVERY rank the score has earned, not one --
                        # confirmed against live footage. receivedBookRankUp agrees: it
                        # saves the OLD rank and dispatches CharEvent 22 with it so the
                        # UI can animate old -> new, which is only meaningful when the
                        # jump can be bigger than a single rank.
                        sum_xp = ps.book_sum_xp(state)
                        earned = ps.book_rank_for(sum_xp)
                        cur = int(state.get("book_rank", 0))
                        new = earned
                        if new != cur:
                            state["book_rank"] = new
                            ps.save(state)
                        rank, sum_xp, left_xp = ps.book_progress(state)
                        log(f"    -> soul link rank up {cur} -> {rank} "
                            f"(score {sum_xp}, earned rank {earned})")
                        # receivedBookRankUp indexes intargs 0..3, so all FOUR must be
                        # present; it applies [0] -> bookRank and [2] -> bookLeftXp.
                        send(MSG_RPC, uint_msg(
                            PLAYER_CHAR, CHAR_RPLY_BOOK_RANK_UP,
                            [rank, sum_xp, left_xp, 0], []))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_ENCHANT_GEM):
                        # SendEnchantGemReq(itemUID, cnt): intargs=[levels],
                        # strargs=[rune uid]. The reply (100) is a bare ack --
                        # HandleEnchantGemRply is a single RET -- so everything the
                        # client displays afterwards has to come from the cmd-145
                        # BackpackChange push and a currency sync.
                        uid = strargs[0] if strargs else ""
                        levels = intargs[0] if intargs else 0
                        try:
                            entry, gained, cost = ps.upgrade_rune(state, uid, levels)
                        except (LookupError, ValueError) as exc:
                            log(f"    !! gem upgrade refused for {uid!r}: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> {uid} +{gained} lv "
                                f"(now {entry['attr'][ps.RUNE_ATTR_LEVEL]}) "
                                f"for {cost} coins")
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_ENCHANT_GEM, [0], []))
                            # BackpackEvent 1 is the only event an already-open panel
                            # refreshes on; the storage sync (84-87) raises 4 instead.
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_CHANGE, [0],
                                [ps.backpacks_all_json(
                                    state, {ps.BP_STORAGE_EQUIPMENT}),
                                 ps.backpack_info_json(state)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_EQUIP_LOCK):
                        # SendEquipLockReq(equip_uid, toLock): intargs=[toLock],
                        # strargs=[uid]. Absolute, not a toggle -- the client already
                        # computed 1 - attr["l"].
                        uid = strargs[0] if strargs else ""
                        to_lock = intargs[0] if intargs else 0
                        try:
                            storage, entry = ps.set_equip_lock(state, uid, to_lock)
                        except LookupError as exc:
                            log(f"    !! equip lock refused: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> {uid} lock={to_lock} (storage {storage})")
                            # 106 only moves the client's _lockCount; the flag itself
                            # rides in attr["l"], so push the storage too.
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_EQUIP_LOCK, [to_lock], [uid]))
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_CHANGE, [0],
                                [ps.backpacks_all_json(state, {storage}),
                                 ps.backpack_info_json(state)]))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_DECOMPOSE_BLOODPACT):
                        # SendDecomposeBloodpactReq(uid_list): strargs = the pact uids,
                        # no intargs. Opens a PanelWaitingBlock, so always answer.
                        try:
                            reward, gone = ps.dismantle_bloodpacts(
                                state, list(strargs))
                        except (LookupError, ValueError) as exc:
                            log(f"    !! dismantle refused: {exc}")
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_DECOMPOSE_BLOODPACT, [0], ["[]"]))
                        else:
                            ps.save(state)
                            log(f"    -> dismantled {len(strargs)} pact(s) "
                                f"-> {reward}")
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_DECOMPOSE_BLOODPACT, [1],
                                [json.dumps(reward, separators=(",", ":"))]))
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_CHANGE, [0],
                                [ps.backpacks_all_json(
                                    state, {ps.BP_STORAGE_BLOODPACT,
                                            ps.BP_STORAGE_NORMAL},
                                    {ps.BP_STORAGE_BLOODPACT: gone}),
                                 ps.backpack_info_json(state)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_DECOMPOSE_SOULFRAG):
                        # SendDecomposeSoulFragReq(uid_list): strargs = the mirror uids,
                        # no intargs. ALWAYS answer -- the request opened a
                        # PanelWaitingBlock and only cmd 120 closes it.
                        try:
                            reward, gone = ps.dismantle_soulmirrors(
                                state, list(strargs))
                        except (LookupError, ValueError) as exc:
                            log(f"    !! soulmirror dismantle refused: {exc}")
                            # intargs[0] != 1 skips the popup and falls straight through
                            # to PanelWaitingBlock.Close(), which is the clean way to
                            # unblock the UI on a refusal.
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_DECOMPOSE_SOULFRAG, [0], []))
                        else:
                            ps.save(state)
                            log(f"    -> dismantled {len(strargs)} soulmirror(s) "
                                f"-> {reward}")
                            # HandleDecomposeSoulFragRply (0x18EBD60): intargs[0] must
                            # be 1, then it RemoveAt(0)s that flag and walks whatever is
                            # left in PAIRS -- [itemId, amount, itemId, amount, ...] --
                            # building one ItemStruct each for the reward popup. The
                            # pairs ride in intargs; unlike the bloodpact reply there is
                            # no JSON strarg at all, and it takes `size >> 1` pairs so a
                            # trailing odd element would be dropped silently.
                            flat = [n for pair in reward for n in pair]
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_DECOMPOSE_SOULFRAG, [1] + flat, []))
                            # 145 MERGES, so the dismantled slots need iid-0 tombstones
                            # or their icons stay on screen; storage 1 carries the
                            # refunded material.
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_CHANGE, [0],
                                [ps.backpacks_all_json(
                                    state, {ps.BP_STORAGE_SOULFRAG,
                                            ps.BP_STORAGE_NORMAL},
                                    {ps.BP_STORAGE_SOULFRAG: gone},
                                    only={ps.BP_STORAGE_SOULFRAG: []}),
                                 ps.backpack_info_json(state)]))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_TRANSMUTE_SOULFRAG):
                        # SendTransmuteSoulFragReq(uid_list): strargs = the mirrors to
                        # fuse, no intargs. PanelWaitingBlock again -- always answer.
                        try:
                            new, gone, coins = ps.fuse_soulmirrors(
                                state, list(strargs))
                        except (LookupError, ValueError) as exc:
                            log(f"    !! soulmirror fuse refused: {exc}")
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_TRANSMUTE_SOULFRAG, [0], []))
                        else:
                            ps.save(state)
                            log(f"    -> fused {len(strargs)} soulmirror(s) for "
                                f"{coins} coins -> item {new['iid']} ({new['uid']})")
                            # HandleTransmuteSoulFragRply (0x18EBF9C): intargs[0] == 1,
                            # then it reads intargs[1] as the item id and intargs[2] as
                            # the amount -- ONE ItemStruct, no loop. **All three ints
                            # must be present**: it indexes [1] and [2] behind explicit
                            # size checks that throw ArgumentOutOfRange, unlike 120
                            # which tolerates a bare [1].
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_TRANSMUTE_SOULFRAG,
                                [1, int(new["iid"]), 1], []))
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_CHANGE, [0],
                                [ps.backpacks_all_json(
                                    state, {ps.BP_STORAGE_SOULFRAG},
                                    {ps.BP_STORAGE_SOULFRAG: gone},
                                    only={ps.BP_STORAGE_SOULFRAG: [new["sid"]]}),
                                 ps.backpack_info_json(state)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_MIX_BLOODPACT):
                        # SendMixBloodpactReq(fromUID, toUID, fromIndex, toIndex):
                        # intargs=[fromIndex, toIndex], strargs=[fromUID, toUID],
                        # slot indices 1-based onto bid_N.
                        from_uid = strargs[0] if strargs else ""
                        to_uid = strargs[1] if len(strargs) > 1 else ""
                        from_i = intargs[0] if intargs else 0
                        to_i = intargs[1] if len(intargs) > 1 else 0
                        try:
                            tgt, skill, coins, gone = ps.mix_bloodpact(
                                state, from_uid, to_uid, from_i, to_i)
                        except (LookupError, ValueError) as exc:
                            log(f"    !! forge refused: {exc}")
                            # Still answer, or PanelWaitingBlock never closes.
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_MIX_BLOODPACT, [0], []))
                        else:
                            ps.save(state)
                            log(f"    -> forged skill {skill} from {from_uid}[{from_i}]"
                                f" into {to_uid}[{to_i}] for {coins} coins")
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_MIX_BLOODPACT, [1], []))
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_CHANGE, [0],
                                [ps.backpacks_all_json(
                                    state, {ps.BP_STORAGE_BLOODPACT},
                                    {ps.BP_STORAGE_BLOODPACT: [gone]}),
                                 ps.backpack_info_json(state)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_ENHANCE_BLOODPACT):
                        # SendEnhanceBloodpactReq(itemUID, cnt): intargs=[levels],
                        # strargs=[pact uid]. Reply is a bare ack; the client only
                        # updates from the 145 push and the currency sync.
                        uid = strargs[0] if strargs else ""
                        levels = intargs[0] if intargs else 0
                        try:
                            entry, gained, coins = ps.upgrade_bloodpact(
                                state, uid, levels)
                        except (LookupError, ValueError) as exc:
                            log(f"    !! bloodpact upgrade refused for "
                                f"{uid!r}: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> {uid} +{gained} lv "
                                f"(now {entry['attr'][ps.RUNE_ATTR_LEVEL]}) "
                                f"for {coins} coins")
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_ENHANCE_BLOODPACT, [0], []))
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_CHANGE, [0],
                                [ps.backpacks_all_json(
                                    state, {ps.BP_STORAGE_BLOODPACT}),
                                 ps.backpack_info_json(state)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_ENHANCE_SOULFRAG):
                        # SendEnhanceSoulFragReq(itemUID, cnt): intargs=[levels],
                        # strargs=[mirror uid]. Storage 3 AND storage 1 both change
                        # (the mirror levels, the Soul Essence is spent), so the 145
                        # push carries both.
                        uid = strargs[0] if strargs else ""
                        levels = intargs[0] if intargs else 0
                        try:
                            entry, gained, coins, essence = ps.upgrade_soulmirror(
                                state, uid, levels)
                        except (LookupError, ValueError) as exc:
                            log(f"    !! soulmirror upgrade refused for "
                                f"{uid!r}: {exc}")
                        else:
                            ps.save(state)
                            log(f"    -> {uid} +{gained} lv "
                                f"(now {entry['attr'][ps.RUNE_ATTR_LEVEL]}) for "
                                f"{coins} coins + {essence} essence")
                            # **intargs[0] MUST be 1.** HandleEnhanceSoulFragRply
                            # (0x18EBC14) treats anything else as failure: it skips
                            # straight to PanelWaitingBlock.Close() and returns, which
                            # looks exactly like the click doing nothing. On 1 it
                            # deserializes strargs[0] as a BackpackItemData, converts it
                            # with BackpackItemDataToItemStructGem and dispatches
                            # BackpackEvent 7 -- the event ShowEnhanceResult listens on.
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_ENHANCE_SOULFRAG, [1],
                                [json.dumps(entry, separators=(",", ":"))]))
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_CHANGE, [0],
                                [ps.backpacks_all_json(
                                    state,
                                    {ps.BP_STORAGE_SOULFRAG, ps.BP_STORAGE_NORMAL}),
                                 ps.backpack_info_json(state)]))
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                    elif (index == BACKPACK_SERVER
                          and cmd == BACKPACK_REQ_QUERY_BOX):
                        # RequesQueryBoxList(itemId) -> intargs=[item id].
                        # HandleQueryBoxRply (0x18EC148) reads strargs[0] as
                        # List<List<uint>> and calls
                        # PanelItemInfo.ShowBoxItemInfoPopup(intargs[0], list) --
                        # note intargs[0] there is a **box_type**, not the item id.
                        #
                        # Box CONTENTS are not in the client data. item 1001 is
                        # `_class 2` (box) with `_param1 5211`, and 5211 resolves to an
                        # equipment row whose suit has 257 members -- the actual drop
                        # table was live-ops, like the gacha boxes and the roulette. So
                        # answer with an EMPTY list: ShowMultipleItemInfo takes the
                        # multi-item path (a null list would throw, an empty one is
                        # fine), the popup opens with the item's own name/description
                        # and an empty grid, and it can be dismissed. Inventing plausible
                        # contents would show the player a preview that is simply wrong.
                        item_id = intargs[0] if intargs else 0
                        # Box contents are OUR data -- they were live-ops and are in no
                        # design form -- so answer from the same tables a purchase pays
                        # out of. That is what keeps Drop Info honest: what the preview
                        # lists is exactly what buying hands over. An unknown box still
                        # answers with an empty list, which opens the popup with the
                        # item's own name and an empty grid rather than throwing.
                        contents = ps.box_contents(item_id)
                        log(f"    -> box {item_id}: {len(contents)} entries")
                        send(MSG_RPC, backpack_msg(
                            BACKPACK_RPLY_QUERY_BOX, [item_id],
                            [json.dumps(contents, separators=(",", ":"))]))
                    elif index == OFA_SERVER and cmd == OFA_REQ_CONTENT:
                        # Opening any OFA banner (`RequestServerOFAContent: <id>` in
                        # logcat) asks for its content; unanswered, the bulletin panel
                        # opens empty -- the black screen behind the roulette entry.
                        # HandleSyncOFAContent, disassembled at 0x1960978:
                        #   intargs[0] = the OFAID, echoed back. It also flips that
                        #     banner's Status to 2 in Static/EventBannerDic.
                        #   strargs[0] = a SHOP ID as a decimal string -- Int32.Parse'd,
                        #     then PlayerShop.ShopDic is given an empty ShopData if it has
                        #     no entry for it. The `oneforall` row's `_shop_cond` is that
                        #     id, and it is **0** for the roulette entry (200019).
                        #   strargs[1] = REQUIRED (it indexes [1] before checking) and is
                        #     the goods-bought dict; the empty string is special-cased to
                        #     mean "clear it", which is what we want.
                        # Send EXACTLY two strargs: counts of 3 and >=4 take extra
                        # branches that consume strargs[2] and beyond.
                        ofa_id = intargs[0] if intargs else 0
                        shop_id = ps.ofa_shop_cond(ofa_id)
                        log(f"    -> OFA content for {ofa_id} (shop {shop_id})")
                        send(MSG_RPC, uint_msg(
                            OFA_CLIENT, OFA_RPLY_CONTENT, [ofa_id],
                            [str(shop_id), ""]))
                    elif index == SHOP_SERVER and cmd == SHOP_REQ_SYNC:
                        # intargs = [refreshFlag, ShopVersion], and the client tells us
                        # which case it wants: LOGIN sends [0, 0], the store panel sends
                        # [1, 1]. Reply intargs[0] picks the event HandleLoginSync
                        # dispatches -- 0 -> OPEN_SHOP_FINISH(1), non-zero ->
                        # UPDATE_SHOP_LIST(12). The login sync WAITS on
                        # OPEN_SHOP_FINISH, so hardcoding 1 hangs the whole login on
                        # "Subsystem 'PlayerShop' still in syncing"; but the store panel
                        # needs UPDATE_SHOP_LIST or it never rebuilds its banner list.
                        # Echoing the flag serves both.
                        #
                        # ShopVersion must DIFFER from the client's current value or
                        # HandleLoginSync skips the rebuild entirely, so bump it.
                        flag = intargs[0] if intargs else 0
                        log(f"    -> shop sync (flag {flag} -> ShopEvent "
                            f"{'UPDATE_SHOP_LIST' if flag else 'OPEN_SHOP_FINISH'})")
                        send(MSG_RPC, uint_msg(
                            SHOP_CLIENT, SHOP_RPLY_SYNC, [flag, shop_version()],
                            [ps.shop_json(), "{}"]))
                    elif index == SHOP_SERVER and cmd == SHOP_REQ_BUY:
                        # SendBuyCmd(goodsID, count) -- no shop id, goods ids are global.
                        gid = intargs[0] if intargs else 0
                        cnt = intargs[1] if len(intargs) > 1 else 1
                        ok, shop_id, why, new_chars = ps.buy_shop_goods(
                            state, gid, cnt)
                        if ok:
                            ps.save(state)
                            log(f"    -> bought {cnt}x goods {gid} from shop {shop_id}"
                                + (f", cast reward {new_chars}" if new_chars else ""))
                        else:
                            log(f"    -> buy REFUSED goods {gid} x{cnt} -- {why}")
                        # Reply 513 (case 513 in PlayerShop.OnClientCmdReceived):
                        #   intargs[0] = SHOP id   -- looked up in _shopDic
                        #   intargs[1] = goods id
                        #   intargs[2] = granted ITEM id    } only read when Count >= 3;
                        #   intargs[3] = granted ITEM count } they build the List<ItemStruct>
                        #                                     behind the "you received"
                        #                                     popup, which is why a
                        #                                     2-int reply bought the item
                        #                                     silently with no animation.
                        #   strargs[0] = updated Dictionary<int, GoodsBuyData>
                        item_id, item_cnt = ps.goods_reward(state, gid, cnt)
                        # A BUNDLE pays several things, so its confirmation goes out as
                        # a drop-item popup listing every line. Reply 513 then carries
                        # only two intargs -- with fewer than three it deliberately
                        # shows nothing, which is what stops the two popups stacking.
                        lines = ps.goods_bundle_lines(state, gid) if ok else []
                        send(MSG_RPC, uint_msg(
                            SHOP_CLIENT, SHOP_RPLY_BUY,
                            ([shop_id or 0, gid] if len(lines) > 1
                             else [shop_id or 0, gid, item_id, item_cnt]),
                            [ps.shop_bought_json(state, shop_id or 0)]))
                        if len(lines) > 1:
                            send(MSG_RPC, backpack_msg(
                                BACKPACK_RPLY_DROP_ITEM, [],
                                [json.dumps({str(i): c for i, c in lines},
                                            separators=(",", ":"))]))
                        if ok:
                            send(MSG_RPC, sint_msg(0xBC8FDA7C, 512, [],
                                                   [ps.currency_json(state)]))
                            send(MSG_RPC, backpack_msg(
                                84, [1],
                                [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                            # A ★5 card on the Medal of Pride tab is an `_action 1`
                            # CAST, so it arrives the same way a quest cast reward
                            # does -- Char `create`, which stores it and plays the
                            # single-pull reveal. See CHAR_RPLY_CREATE.
                            if new_chars:
                                send(MSG_RPC, uint_msg(
                                    PLAYER_CHAR, CHAR_RPLY_CREATE, [],
                                    [ps.char_create_json(state, new_chars)]))
                            # Stamina bundles pay ENERGY, which the client caches
                            # from the login sync like everything else.
                            send(MSG_RPC, uint_msg(0xAE487D79, 512, [],
                                                   [ps.energy_json(state)]))
                            # buy_shop_goods credited any "go and exchange X" quest
                            # watching this goods id. The counter only becomes visible
                            # -- and the goal only becomes claimable -- once the client
                            # is told: AnalysisQuest rebuilds the claimable list from
                            # this payload. Without it the player buys the item and the
                            # goal sits there unchanged.
                            send(MSG_RPC, quest_sync_msg(state))
                    elif index == SHOP_SERVER and cmd == SHOP_REQ_GOODS_TO_SHOP:
                        # "Which shop sells goods N, and which tab is it on?"
                        # EnterSpecificStore needs BOTH, and the handler bails unless
                        # intargs has two entries -- one is not a partial answer, it is
                        # no answer. A goods id we do not sell gets no reply, since
                        # navigating somewhere arbitrary is worse than not moving.
                        goods_id = intargs[0] if intargs else 0
                        shop_id, row = ps.find_shop_goods(state, goods_id)
                        if row:
                            log(f"    -> goods {goods_id} is in shop {shop_id} "
                                f"tab {row[9]}")
                            send(MSG_RPC, uint_msg(
                                SHOP_CLIENT, SHOP_RPLY_GOODS_TO_SHOP,
                                [shop_id, row[9]], []))
                        else:
                            log(f"    -> goods {goods_id} is in no shop we serve")
                    elif index == SHOP_SERVER and cmd == SHOP_REQ_QUERY_COUPON:
                        # Drop Info on a selector. UNANSWERED THIS LOCKS THE CLIENT:
                        # the popup is already open behind a modal overlay and only
                        # the reply builds its contents, so the player is left with an
                        # undismissable dark screen. Reply even when we do not model
                        # that selector -- an empty list opens the popup with the
                        # item's own name and an empty grid, which closes normally.
                        item_id = intargs[0] if intargs else 0
                        contents = ps.box_contents(item_id)
                        log(f"    -> selector {item_id}: {len(contents)} choices")
                        send(MSG_RPC, uint_msg(
                            SHOP_CLIENT, SHOP_RPLY_QUERY_COUPON, [item_id],
                            [json.dumps(contents, separators=(",", ":"))]))
                    elif index == SHOP_SERVER and cmd == SHOP_REQ_SYNC_GOODS:
                        # Opening a store tab. Without this the panel calls
                        # PanelWaitingBlock.Open(-1.0, 0) -- an indefinite, input-blocking
                        # overlay with NO timeout -- and waits here forever, which locks
                        # the whole client.
                        shop_id = intargs[0] if intargs else 0
                        sync_bought = intargs[1] if len(intargs) > 1 else 0
                        log(f"    -> shop goods sync for shop {shop_id}")
                        send(MSG_RPC, uint_msg(
                            SHOP_CLIENT, SHOP_RPLY_SYNC_GOODS,
                            [shop_id, sync_bought],
                            ps.shop_goods_json(state, shop_id)))
                    elif (index == PLAYER_STAGE_SERVER
                          and cmd in (STAGE_REQ_AUTO_START, STAGE_REQ_AUTO_SYNC,
                                      STAGE_REQ_AUTO_STOP)):
                        now = int(time.time())
                        if cmd == STAGE_REQ_AUTO_START:
                            stage_id = intargs[0] if intargs else 0
                            count = intargs[1] if len(intargs) > 1 else 1
                            coupon = bool(intargs[2]) if len(intargs) > 2 else False
                            ok, why = ps.autorun_start(state, stage_id, count,
                                                       coupon, now)
                            if not ok:
                                log(f"    -> auto play REFUSED {stage_id} x{count} "
                                    f"-- {why}")
                                send(MSG_RPC, stage_sync_msg(state))
                                continue
                            ps.save(state)
                            job = state["autorun"]
                            log(f"    -> auto play started: stage {stage_id} x{count}"
                                f"{' (express)' if coupon else ''}, due in "
                                f"{job['duetime'] - now}s")
                            # NO toast here -- 401 says "Completed", which is a lie
                            # until the timer runs out. The syncs below are what tell
                            # the panel a sweep is running.
                        elif cmd == STAGE_REQ_AUTO_STOP:
                            stopped = ps.autorun_cancel(state)
                            if stopped:
                                elapsed = max(0, now - int(stopped["starttime"]))
                                done = ps.autorun_runs_elapsed(stopped, now)
                                paid = autorun_payout(state, stopped["stage_id"], done)
                                ps.save(state)
                                log(f"    -> auto play stopped: stage "
                                    f"{stopped['stage_id']} after {done}/"
                                    f"{stopped['count']} runs ({elapsed}s)")
                                # EXACTLY three intargs, or HandleAutoStop bails.
                                send(MSG_RPC, uint64_msg(
                                    PLAYER_STAGE, STAGE_RPLY_AUTO_STOP,
                                    [int(stopped["stage_id"]), int(done),
                                     int(elapsed)], []))
                                for m in paid:
                                    send(MSG_RPC, m)
                        # Settle a finished sweep before answering, so the client is
                        # told about the rewards in the same breath as the new state.
                        done = autorun_settle(state, now)
                        send(MSG_RPC, stage_sync_msg(state))
                        send(MSG_RPC, auto_sync_msg(state))
                        for m in done:
                            send(MSG_RPC, m)
                    elif index == PLAYER_STAGE_SERVER and cmd == STAGE_REQ_GET_DROPS:
                        stage_id = intargs[0] if intargs else 0
                        drops = bt.stage_drop_preview(stage_id)
                        log(f"    -> drop info for stage {stage_id}: {drops}")
                        send(MSG_RPC, uint64_msg(
                            PLAYER_STAGE, STAGE_RPLY_GET_DROPS, [stage_id],
                            [json.dumps(drops, separators=(",", ":"))]))
                    elif index == PLAYER_STAGE_SERVER and cmd in (
                            STAGE_REQ_EXECUTE, STAGE_REQ_NEWBIE):
                        # Execute carries [stage_id, team]; the newbie variant sends no
                        # args at all and always means the tutorial stage.
                        stage_id = intargs[0] if intargs else NEWBIE_STAGE_ID
                        # A daily-dungeon stage (_ap_type 2) costs one of item _ap_v1 --
                        # the Training Gym Pass and friends. Charge it here, or every
                        # run is free and the counter never moves.
                        cost = ps.stage_ap_cost(bt.dd.row("stage", stage_id) or {})
                        if cost:
                            iid, n = cost
                            if ps.spend_item(state, iid, n):
                                ps.save(state)
                                log(f"    -> charged {n}x item {iid} to enter "
                                    f"stage {stage_id} (left {ps.item_count(state, iid)})")
                                send(MSG_RPC, backpack_msg(
                                    84, [1],
                                    [ps.backpack_json(state, ps.BP_STORAGE_NORMAL)]))
                            else:
                                log(f"    -> stage {stage_id} entry REFUSED -- no "
                                    f"item {iid}")
                        log(f"    -> stage execute reply (stage {stage_id})")
                        send(MSG_RPC, stage_execute_reply())
                        cur_battle = bt.Battle(stage_id, ps.battle_team(state),
                                               state.get("team_level", 1),
                                               state.get("team_star"),
                                               state.get("team_super_star", 0),
                                               int(state.get("book_rank", 0)))
                        # Persist immediately: a server restart between this and the
                        # FIRST attack must still have something to resume, not just
                        # ones after the player's first action.
                        ps.save_battle(state, cur_battle)
                        ps.save(state)
                        log(f"    -> start battle: wave 1/{cur_battle.wave_max}, "
                            f"units {sorted(cur_battle.units)}")
                        send(MSG_RPC, start_battle_msg(cur_battle))
                    elif (index == bt.BATTLE_SERVER_INDEX and cur_battle
                          and cmd != bt.REQ_BATTLE_SYNC):
                        for body in battle_replies(cur_battle, cmd, intargs, strargs,
                                                   state, ps.uid(state)):
                            send(MSG_RPC, body)
                        # Persist after EVERY battle RPC (attacks, wave transitions,
                        # auto-toggle, ...) so a killed/restarted server always has
                        # something to resume -- except the cmds that mean the fight
                        # is genuinely OVER (won/lost, retreated, or the player just
                        # declined the "rejoin?" prompt), which must drop the
                        # snapshot rather than re-save a finished battle as live.
                        declined_reconnect = (cmd == bt.REQ_RECONNECT
                                             and not (intargs and intargs[0] == 1))
                        if state:
                            if cmd in (bt.REQ_BATTLE_END, bt.REQ_RETREAT) \
                                    or declined_reconnect:
                                # Mark it, not just clear it: RPCs still arrive AFTER
                                # the fight is over and the else-branch below would
                                # re-save the corpse as live. A Starshard Temple clear
                                # does exactly that -- 505 ends the fight, then the
                                # player's shard pick (508) lands afterwards, and the
                                # finished battle went straight back into the save.
                                # Every restart then offered to "Continue the Fight",
                                # and accepting replayed a fight already won.
                                if cur_battle is not None:
                                    cur_battle.finished = True
                                ps.clear_battle(state)
                            elif getattr(cur_battle, "finished", False):
                                pass          # never re-save a finished fight
                            else:
                                ps.save_battle(state, cur_battle)
                            ps.save(state)
                    elif state and (index, cmd) in build_sync_replies(state):
                        # Rebuild per request rather than caching at login: the payloads
                        # are rendered from `state`, so a table built once at login goes
                        # stale the moment anything changes. That is what made the gacha
                        # screen fall back to the tutorial box on re-entry even though
                        # gacha_count had already advanced.
                        name, msgs = build_sync_replies(state)[(index, cmd)]
                        log(f"    -> sync sequence for {name} ({len(msgs)} msgs)")
                        for body in msgs:
                            send(MSG_RPC, body)
                    elif (index, cmd) in FIRE_AND_FORGET:
                        log(f"    (ack {FIRE_AND_FORGET[(index, cmd)]}"
                            f" -- no reply cmd exists)")
                    else:
                        log(f"    (no handler for index={index:#010x} cmd={cmd})")
            else:
                log(f"    (unhandled message type {mtype}: {msg})")
    except socket.timeout:
        log(f"[t] {addr} idle timeout")
    except Exception as e:
        # Full traceback, not just the type -- a handler crash drops the socket
        # ("connection interrupted" on the client), and the one-line form gave no
        # way to see WHERE (e.g. which int(None) in which subsystem handler).
        import traceback
        log(f"[!] {addr} {type(e).__name__}: {e}\n" + traceback.format_exc())
    finally:
        conn.close()


# The listening socket, so an embedding host (the Android app) can stop us. Closing
# it is what breaks accept() out of its loop -- there is no other exit.
_listener = None


def main(port=None):
    global _listener
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port or PORT))
    srv.listen(8)
    _listener = srv
    log(f"[*] TitanStack server listening on 0.0.0.0:{port or PORT}")
    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
    except OSError:
        log("[*] TitanStack listener closed")
    finally:
        _listener = None


def shutdown():
    """Close the listener; a blocked accept() then raises OSError and main() returns."""
    srv = _listener
    if srv is not None:
        try:
            srv.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()

# The battle result panel, and why a lost guild fight hangs

Written 2026-09-19 after two sessions on one symptom: after a Guild Weekly defeat the
client shows the BATTLE ENDS banner and the Record page, and then every tap does
nothing. Four server-side fixes were shipped against it and all four were wrong. This
records what is now *verified*, so nobody pays that cost twice.

**The client is not the broken part.** It ran against the real server for years. If it
hangs, we are sending something retail did not, or failing to send something it did.
Every conclusion below is written with that in mind.

---

## 1. The chain that ends in a working tap

All of it read out of `libil2cpp.so` (EN 2.2.7) by disassembly, not inferred:

    1  PlayerStage.HandleEndReward            cmd 23 on the PlayerStage channel
         DeserializeObject<BattleReward>(strargs[0], &obj)
         PlayerBattle+0x68 = obj              RewardData
         PlayerBattle+0x70 = obj.helper_uid   HelperUID
         dispatch StageEvent
         ** if `obj` is null it RETURNS BEFORE THE DISPATCH -- see section 5 **

    2  GameWinState.OnBattleEnd               StageEvent listener, 0x1969cf0
         MOV W8,#1 ; STRB W8,[X0,#0xA0]       IsBattleEndReady = true

    3  PanelBattleResult.coInitResultData     0x16b82c4
         LDRB W8,[X0,#0xA0] ; CBZ -> park     polls, then SetResultData()

    4  PanelBattleResult.ShowResultPageInAnimation
         LDRB W8,[X0,#0xA0] ; CBZ -> park     polls, then plays _ResultTween

    5  PtRewardCb / PtEvaCb / PtCharLevelCb   isSkipEnabled = 1

    6  OnClickNextStep                        its ENTIRE body is inside
                                              `if (isSkipEnabled)`, so until step 5
                                              runs, every tap is a silent no-op

Counterpart, and the reason a defeat breaks the chain at step 1:

    PlayerBattle.ServerRPCBattleEnd           0x1687be0
      if (!runeSel) STRB WZR,[X19,#0xA0]      clears IsBattleEndReady
      sends 505 ONLY when
        BattleData.BattleType       (+0x14) == 0   and
        BattleData.BattleResultType (+0xA8) == 1   i.e. a WIN

So on a defeat the client clears the flag, never asks for the reward, and both panel
coroutines park forever. The panel is not "broken" -- it is waiting, exactly as written.

### Offsets

| Object | Offset | Field |
|---|---|---|
| PlayerBattle | 0x68 | `RewardData` |
| PlayerBattle | 0x70 | `HelperUID` |
| PlayerBattle | **0xA0** | **`IsBattleEndReady`** |
| BattleDatas | 0x10 | `stageID` |
| BattleDatas | 0x14 | `BattleType` |
| BattleDatas | 0xA8 | `BattleResultType` |
| PanelBattleResult | 0x48 | `_curResultState` |
| PanelBattleResult | 0x144 | `isEvaMode` |
| PanelBattleResult | 0x146 | `isCharLevelMode` |
| PanelBattleResult | **0x147** | **`isSkipEnabled`** |

`eResultState`: `Init=0 Evaluation=1 Reward=2 CharLevel=3 KizunaLevelUp=4 WaitClose=5
BattleWin=6`. `OnEnterResultState` hooks a tween-finished callback for 1/2/3/6 ONLY;
`OnAllUIFxLoadComplete` ends by entering `Init`, which hooks nothing. Nothing can arm
the tap while the panel sits in `Init`.

---

## 2. Why a story defeat is fine and a guild defeat is not

`TurnEndState.OnWaveEnd` tests `IsBossStage()` **before** it looks at the battle type:

    boss stage      -> GameWinState    (result panel; the broken path)
    ordinary stage  -> GameLoseState   (fail screen, exits cleanly)

Verified on a device by deliberately losing a story fight: fail screen, straight back
to the lobby, no hang.

`GameLoseState` never contacts the server at all -- it plays a voice line, calls
`DoLoserShow` (close menu, restore timescale, switch on a panel) and `QuitBattle`
(clear cache, leave the sub-scene). `ServerRPCBattleEnd` is reached only from the WIN
state, which is why a defeat never sends 505 and why the server must settle a lost
fight itself rather than wait to be told.

### `IsBossStage` reads `_book`, at stage-row offset 0xB8

    LDR W8,[X0,#0xB8] ; CMP W8,#8

28 stages carry `_book == 8` -- 7 bosses x 4 tiers -- which is exactly the Guild Weekly
set, and it matches the observed behaviour split. `GameWinState.OnEnter` reads the SAME
offset into its `stageType`.

**Trap:** the C# dump (section 4) names offset 0xB8 `_ap_v1`. In OUR pack `_ap_v1` is 0
on every guild stage and no stage anywhere has `_ap_v1 == 8`. The dump's stage-row
layout is from a different build. Do not map dump field NAMES onto our design rows.

---

## 3. Nobody had ever lost

82 wave results across the device's entire log history before 2026-09-13, every one of
them `result 1`. The party was unkillable -- see the alive-count gate (49697d6) and the
sentence-scoped conditions (c6b5355) -- so the loss path had never executed once. The
first real defeat hung the game.

Two bugs were hiding behind that: a lost run banked no score at all (fixed, b8231ee /
8db840d), and this one.

---

## 4. The C# "decomp" in Downloads: what it is worth

It is an **Il2CppDumper DummyDll export run through ILSpy**. 2,362 files. Method bodies
are stubs (`return default(int)`), so it contains no logic.

**Safe and very useful:** field offsets, enum names and values, type and method
inventories. It independently confirmed four offsets derived by hand here
(`IsBattleEndReady` 0xA0, `stageID` 0x10, `BattleType` 0x14, `BattleResultType` 0xA8)
and it is where `eResultState` and `BattleType` (`Stage=0 Arena=1 Challenge=2 Raid=3
FreePK=4 ArenaSP=5 ArenaTeam=6 Campaign=7 Village=8 GMTest=100 None=255`) come from.
Those decode branches that otherwise read as magic numbers.

**Its method lists ARE accurate** -- this doc originally said otherwise and was wrong.
`ShowBattleWin`, `ShowEvaResult`, `ShowRewardResult`, `ShowCharLevelResult`,
`ShowKizunaLevelUp`, `ShowFinalResult` and `InitUIResult` all exist in our binary. They
do not appear in IDA's function list because **IDA folded them into `CheckAppsFlyer`**
(0x16b4054, size 0xed4, which spans 0x16b4054-0x16b4f28); they are named branch targets
inside it. `list_funcs` returning 44 methods was an artefact of that, not evidence about
the build. If a method the dump names seems missing, disassemble the neighbourhood
before concluding it is absent.

**Not safe:** design-row layouts. The dump calls stage-row offset 0xB8 `_ap_v1`; in our
pack `_ap_v1` is 0 on every guild stage and nothing anywhere has `_ap_v1 == 8`, while
`_book == 8` picks out exactly the 28 guild stages and matches observed client behaviour
(section 2). Verified against the pack, so this one is solid.

Rule of thumb: **trust it for runtime-class offsets, enums and method names; verify
anything design-row shaped against the pack; it has no method bodies, so control flow
always comes from the binary.**

It is ~15 MB of extracted game material and belongs with the IDA databases and
`patch_root` -- useful locally, never committed.

---

## 5. Where the bug actually stands

The server now reports a boss-stage wipe as `WAVE_RESULT_WIN` (e93656198d9a3ba3), which
makes the client send 505 of its own accord and take the path logged working on guild
stages 1000025 and 1000027. The server is not fooled: `wave_cleared()` is still False,
so no xp, drops or ratings are paid, and the log still records `battle end (..., loss)`.
The banner is honest either way -- "BATTLE ENDS!" is what a guild VICTORY shows too.

Confirmed on device: 505 now arrives, the settlement runs, the score banks. **The tap is
still dead.**

Since every step of section 1 should then fire, the break is at step 1's bail:

    if ( !obj || !Instance ) goto LABEL_22;   // returns BEFORE dispatching StageEvent

If `DeserializeObject<BattleReward>` yields null, no StageEvent is dispatched, the flag
is never set, and both coroutines park. That is server-side and testable.

### The payload is NOT the problem -- settled 2026-09-19

The obvious suspicion was the payload, since the loss reply differs from a working win
reply in exactly three fields, all empty-versus-populated:

    WIN   item_list=[[1,101,2]]  bar_list=5 rows  rating_list=[0,0,0,0]
    LOSS  item_list=[]           bar_list=0 rows  rating_list=[]

`BattleReward` is `[JsonObject]` with ONE constructor and no default:

    public BattleReward(List<List<int>> item_list, params int[] caseParamsToOverride)

and `items` carries NO `[JsonProperty]`, so it cannot come from JSON -- it can only be
built inside that constructor. So "what does the ctor do with an empty item_list" was
the precise question. Decompiled (`BattleReward$$.ctor`, 0x1685f3c), it is fully
defensive:

    itembonusList = new List<int>()          all four allocated unconditionally,
    barValues     = new List<BarData>()      BEFORE item_list is even looked at
    ratingList    = new List<int>()
    items         = new List<RewardData>()
    if (!item_list) goto end                 a NULL item_list is safe
    if (size >= 1) { ...loop... }            an EMPTY one just skips the loop

So an all-empty payload deserialises fine, `obj` is non-null, `HandleEndReward` reaches
its dispatch, and the flag should be set. **Empty lists are exonerated.** Do not spend
another device run on them.

### What is left

Everything the server sends is now accounted for and correct, so the break is in
whether the client-side plumbing is live on this path:

1. **Is `GameWinState.OnBattleEnd` subscribed?** It has no registration method of its
   own and `OnEnter` does not register it, so the subscription is in `StateBase` or the
   state machine. Note there are FOUR identical 0x5c `OnBattleEnd` variants on
   GameWinState (0x1969cf0, _26647884, _26647976, _26648068), which suggests several
   dispatchers or generic instantiations -- possibly only one of which is wired up.
2. ~~Is `coInitResultData` ever started?~~ **Answered: yes.** `OnEnterResultState` does
   not end where Hex-Rays shows it -- it tail-jumps to a switch on `_curResultState`
   (jump table at 0x16B484C) that calls a handler per state:

       Init 0 -> InitUIResult      Evaluation 1 -> ShowEvaResult
       Reward 2 -> ShowRewardResult   CharLevel 3 -> ShowCharLevelResult
       KizunaLevelUp 4 -> ShowKizunaLevelUp   WaitClose 5 -> ShowFinalResult
       BattleWin 6 -> ShowBattleWin

   and `InitUIResult` (0x16b4870) immediately does `BL coInitResultData`. So the panel
   enters `Init`, starts the coroutine, and polls `IsBattleEndReady` exactly as designed.

3. **Is `GameWinState.OnBattleEnd` subscribed?** Traced as far as it can be offline:

       BattleStateMachine.AddListener()        (0x17f74e4, called from Init)
       BattleStateMachine.OnBattleEnd(evt)     (0x17f8acc + 3 more, one per event type)
         _stateDic[9]  -- StateKey 9 IS GameWinState, type-checked
         -> GameWinState.OnBattleEnd(evt)      -> IsBattleEndReady = 1

   and there are FOUR overloads on each side, one per event type: `StageEvent`,
   `ArenaEvent`, `ChallengeEvent`, `BattleEvent`. Note `ChallengeEvent` -- reply 786
   dispatches one of those and we KNOW it lands, because it is what fills the Record
   labels via `UpdateGuildRewardData`. So on a guild defeat the flag has two
   independent routes to being set, and neither appears to fire.

4. **Ruled out:** `GameWinState.OnEnter`'s early exit. It reads

       if (BattleType == 4 /* FreePK */ || PlayerBattle+0xB0 /* ReplayMode */)
             OnResultEnd()      // skips the result panel entirely

   We send `BattleType` 0 and never set `ReplayMode`, so the panel path is taken --
   which matches the observed behaviour (the panel does appear).

### The state as of 2026-09-19

Every link is confirmed present and correct: the panel opens, enters `Init`, starts
`coInitResultData`, and polls. The server sends a well-formed EndReward (the ctor
tolerates our empty lists) and a 786 that demonstrably reaches ChallengeEvent listeners.
The forwarding path from either event to `IsBattleEndReady = 1` is wired. And the tap is
still dead.

**That hypothesis is now disproved.** `SetResultData` and all five page builders were
read end to end on 2026-09-19 (below). Not one of them touches a party member, alive or
dead. The panel is built entirely from the stage row, `BattleReward`, and serialized
prefab references.

### The panel, read end to end

`PanelBattleResult` methods live inside IDA-folded ranges; the addresses below are ours
(EN 2.2.7). `InitUIResult` starts the coroutine and *then* enters state 6, synchronously:

    InitUIResult              0x16b4870   RunCoroutine(coInitResultData); OnEnterResultState(6)
    coInitResultData MoveNext 0x16b82c4   spin-waits on PlayerBattle.IsBattleEndReady (+0xA0),
                                          then and only then calls SetResultData
    SetResultData             0x16b60bc
    SetKizunaUpResultData     0x16b6398
    SetCharLevelResultData    0x16b6494
    SetEvaResultData          0x16b6614
    SetRewardResultData       0x16b6854   -> UpdateGuildRewardData 0x16b68cc  (IsBossStage)
                                          -> UpdateRegularRewardData 0x16b699c (otherwise)

`SetResultData` sets four panel fields and then tail-calls the page builders:

    panel[0x140] = stageRow[0xB8]          ; _book
    panel[0x144] = IsEvaResultOn()         ; isEvaMode
    panel[0x145] = !IsBossStage()
    panel[0x146] = RewardData.barValues.Count >= 1   ; isCharLevelMode
    ... UpdateLive2d, SetKizunaUp, SetCharLevel, SetEva, tail-call SetRewardResultData

`IsEvaResultOn` returns true only when the stage has `_rating_datas` AND at least one
entry of `GetRewardRatingList()` (our `rating_list`) is **non-zero**. We send `[0,0,0,0]`
on a guild win and `[]` on a loss, so it is false either way.

That makes the state the panel lands in purely a function of our payload:

    PtBattleWinCb: isCharLevelMode ? Enter(3 CharLevel) : Enter(isEvaMode ? 1 Eva : 2 Reward)

    guild WIN   bar_list non-empty -> isCharLevelMode -> state 3
    guild LOSS  bar_list empty     -> neither flag    -> state 2

**The dead tap is `isSkipEnabled` (panel+0x147), and nothing else.** `OnClickNextStep`
(0x16b3970) is:

    if (this.isSkipEnabled == 0) return;        // <- the dead tap, exactly
    this.isSkipEnabled = false;
    Timing.KillCoroutines(tag); SkipResult(this._curResultState);

`isSkipEnabled` is written true in exactly three places -- `PtEvaCb`, `PtRewardCb`,
`PtCharLevelCb` -- and each is a one-shot `onFinished` callback on tween `panel[0xA8]`.
So the tap can only ever arm once a result *page* tween has finished. The "BATTLE ENDS"
banner is a different tween, `panel[0x60]`, played by `ShowBattleWin`. Banner up with a
dead tap and an empty background is therefore the exact signature of **`SetResultData`
never having been called at all** -- the coroutine is still spinning on
`IsBattleEndReady`.

One report is closed by this read and is not a bug: `ShowBattleWin` (0x16b4900) picks the
banner with `IsBossStage() ? panel[0x70] : panel[0x68]`, never by win/loss. A guild
victory showing "BATTLE ENDS" is what retail does.

### Where `IsBattleEndReady` is set -- and the boss/story fork

**A guild fight and a story fight take different code, not just different data.** The
fork is in `GameWinState.OnEnter` (0x1969220) at 0x1969414:

    IsBossStage()  -> ShowBattleResult()            ; guild: straight to the RPC
    else if (BattleData.BattleType != 0)
                   -> ShowBattleResult()
    else           -> BattleEnd_Stage()             ; story: victory cinematic first

So `BattleEnd_Stage` -- the long prologue with the end-cinematic clone, the
`GetTeamMembers` pose list and the wave-box lookup -- **is never executed for a guild
stage**. Nothing in it can explain the guild hang. (Both of its data-driven throw sites
were chased down and eliminated anyway: `MapManager.boxs` is filled by
`BattleDataInitializer.CloneItems` from a *stage-independent* static name list, so its
count is the same constant for every stage and the `InterludeList` box index cannot go
out of range on a guild stage alone; and the `sceXXXX_clear` directors are absent from
the scene bundles for story stages too, so their lookup is symmetric.)

`ShowBattleResult` (0x1969604) is short and does the one thing that matters:

    book = this[0x24]                                  ; stage _book, stored in OnEnter
    PlayerBattle.ServerRPCBattleEnd(runeSel: book == 2)

and `ServerRPCBattleEnd` (0x1687cec) is a trap:

    IsBattleEndReady = 0                               ; cleared FIRST, unconditionally
    if (BattleData == null)               return;      ; bare RET -- no RPC
    if (BattleData.BattleType != 0)       return;      ; bare RET -- no RPC
    if (BattleData.BattleResultType != 1) return;      ; bare RET -- no RPC   (+0xA8)
    ...
    ServerRpc.PlayerBattleServerCmd(0x1F9 = 505, ...)

Miss either gate and the flag has been cleared with nothing left that will ever set it:
`coInitResultData` spins forever, `SetResultData` is never called, and the panel sits on
the banner with a dead tap. That is the failure shape, and it is why
`WAVE_RESULT_WIN`-on-a-boss-wipe (which drives `HandleWaveEnd` to write
`BattleResultType = 1`) is load-bearing rather than cosmetic.

The flag is re-armed only by `GameWinState.OnBattleEnd` -- four identical generic
instantiations (0x1969cf0 / d4c / da8 / e04), each simply
`PlayerBattle.Instance.IsBattleEndReady = 1`. They are reached through
`BattleStateMachine.OnBattleEnd`, which looks up `_stateDic[9]`, type-checks
`GameWinState` and forwards with **no active-state guard**. `BattleStateMachine.AddListener`
(0x17f74e4) registers them on three dispatchers:

    StageEvent      type 3
    ChallengeEvent  type 4     (ChallengeEventType.Challenge_BATTLE_END)
    ArenaEvent      type 7

For a guild fight the live route is the ChallengeEvent one, and it is reply **786**:

    ChallengeBattleRewardReply (0x1690428)
      if (intargs.Count != 3) return;                  ; silent -- no dispatch at all
      info.damage/bonus/total_damage = intargs[0..2]
      PlayerChallenge.Event.Dispatch(4, new ChallengeEvent())   -> IsBattleEndReady = 1

We send exactly three ints, from the 505 handler, i.e. *after* `ServerRPCBattleEnd`
cleared the flag. So the ordering is right and the payload is right.

### Resolved 2026-09-20: the tap was never dead -- it was crashing

One device run with logcat settled it. The server side was perfect (505 in, EndReward +
786 out, three ints), and the client threw on **every tap**:

    ArgumentOutOfRangeException: Index was out of range.
      at System.ThrowHelper.ThrowArgumentOutOfRangeException ()
      at Game.Gui.Panel.PanelBattleResult.CheckAppsFlyer ()
      at Game.Gui.Panel.PanelBattleResult.OnClickResultEnd ()
      at UIEventListener.OnSafeClick ()

`OnClickResultEnd` -- the Tap to End handler -- is `CheckAppsFlyer(); QuitBattle()`, and
`CheckAppsFlyer` (0x16b4054) opens with an **unconditional read of `rating_list[3]`**:

    list = PlayerBattle.GetRewardRatingList()
    if (list._size <= 3)  -> ThrowArgumentOutOfRangeException
    if (list[3] == 1)     -> AppsFlyer analytics for a few hardcoded stage ids
    else                  -> return

A win sent `rating_list=[0,0,0,0]` -> index safe -> `QuitBattle` ran -> worked. A loss
sent `[]` -> the throw killed the handler before `QuitBattle`, every tap, forever. That
is the exact reported symptom ("works on a win, dead on a loss") and none of it involved
`IsBattleEndReady` -- the panel had reached its final state normally.

**Fix:** `battle_end_reward` now sends `battle.rating_flags()` on both outcomes; on a
loss that is four honest zeros (every condition is gated on `cleared`). One line.
Verified on the device the same morning: wipe, banner, tap, clean exit to the Record
page, zero exceptions in logcat.

The `IsBattleEndReady` chain documented above stays: it is all true, it is why the
wipe-as-win WaveEnd is load-bearing, and it is the map the next result-panel bug will
need. But the lesson of this hunt is the doc's closing line made real: the four wrong
fixes below were all payload guesses, the fifth "fix" (the flag chain) was a correct
read of the wrong code path, and the actual bug fell out of **one logcat trace** the
moment a device was attached. Exceptions first, disassembly second.

### Four wrong fixes, and why each was wrong

Recorded because each looked reasonable:

1. *Push an empty EndReward with the wave end.* The panel needs it, but sending it at
   the same time loses to `ServerRPCBattleEnd` clearing the flag afterwards.
2. *Populate `bar_list`.* Live footage shows the retail guild result screen has NO
   experience bar. Invented a page the real game does not show.
3. *Defer the EndReward to the next request.* Correct that ordering mattered, wrong
   about the mechanism -- it assumed `HandleEndReward` set the ready flag. It does not;
   `GameWinState.OnBattleEnd` does.
4. *Report the wipe as a win.* Genuinely right -- it produces the 505 round-trip -- but
   not sufficient on its own.

The common thread: reasoning about a client state machine from partial decompilation
without verifying the field a branch actually reads. `LOBYTE(this[4].klass)` in
Hex-Rays output is not a field name. Disassemble and read the offset.

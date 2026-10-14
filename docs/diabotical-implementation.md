# Diabotical Wipeout Auto-Reporting — Implementation Reference

> Reference for debugging and extending the Diabotical Wipeout auto-reporting
> added to PUBobot2. Original PUBobot2 is Copyright (C) 2020 Leshaka; this
> modification is (C) 2026, distributed under GPLv3.

## 1. What this does

PUBobot2 runs pickup matches. One Pubobot **Match** is a **best-of-3 series** of
Diabotical Wipeout team games. The Diabotical API returns **one game per
`match_id`**, so this package reconstructs the series from the games and files
the **series score** through the existing report path.

The goal is a self-hosted replacement for the public PUBobot in
GetCracked! Tournaments `#pug`. Both the automatic path and the manual
`/diabotical confirm` fallback replace the old `/report` workflow.

## 2. Where the code lives

| File | Responsibility |
|------|----------------|
| `bot/diabotical/client.py` | Unauthenticated Diabotical API client. Stdlib-only (urllib with a `requests` fallback if present). Rate-limit + retry/backoff. |
| `bot/diabotical/matcher.py` | **Pure-stdlib** match-finding logic. No bot/nextcord/db imports → unit-testable in a bare environment. |
| `bot/diabotical/report.py` | DB link table, `/link`-family persistence, scoreboard/summary embeds, the `DiaboticalPoller`, and `start_poller`/`stop_poller` integration. |
| `bot/commands/diabotical.py` | `/link`, `/unlink`, `/link_status` command handlers. |
| `bot/context/slash/commands.py` | Slash-command registrations (includes `/diabotical`). |
| `bot/match/match.py` | `MATCH` integration: poller lifecycle, `confirm_series`, `pending_series_*` state. |

## 3. The core problem: one Match = a Bo3 series, one API call = one game

`register_match_ranked` iterates `while n < m.scores[0] or n < m.scores[1]`, so
`m.scores` is a **series score** — one rating cycle is applied per game won.
Each Diabotical `match_id` is a single game, so reporting a single game as if it
were the whole series would be wrong.

The fix: group the finished Wipeout games that make up the series, count how
many each team won, and hand `[team0_wins, team1_wins]` to `report_scores`.

### Why team-level (not per-user) agreement

A Bo3 series has players who **play opposite sides from game to game** (team_idx
swaps). So the agreement check must be at the **team level**, not per-user:

- The present linked players split into a Diabotical team-0 group and a
  Diabotical team-1 group.
- Those two groups must match the bot's two teams **either directly or swapped**.

Per-user agreement rejected ~21/25 fixtures, which is how we discovered the
side-swapping. This is the single most important matcher concept to remember
when debugging a "series not found" report.

## 4. The matcher (`matcher.py`)

Pure functions, all values from the API treated as **opaque data** (never fed to
shell, eval, or the command parser).

Constants:
- `majority(count, total)` — helper returning `count * 2 > total`. The linked-player
  threshold is team-size **agnostic**: a game (or the poller itself) is trusted when
  **over half** of the relevant players are linked, instead of a fixed "6 of 8" floor.
  This is what lets 3v3, 4v4, and any team size auto-report.
- `MIN_SHARED_PLAYERS = 2` — a `match_id` must appear in the recent list of ≥2 distinct linked players before it is fetched/verified (guards against false positives from the short recent-match list).
- `CLINCH_WINS = 2` — Bo3 clinches at first-to-2.
- `FINISHED_STATES = (3, 4)` — Diabotical `match_state` values meaning "concluded".

### `candidate_game(match, linked_ids, pickup_ts, bot_teams) -> verdict`

`bot_teams = [frozenset(team0_ids), frozenset(team1_ids)]` — the teams the bot
assigned from the draft.

Rejects (and why) — each is logged in `poller.reasons`:
- `match_mode != "wipeout"` — wrong game mode.
- `team_count != 2` — not a team game.
- not finished (`match_state` not in `(3,4)`, no `finished=True`, no team `finished`).
- `create_ts` before the pickup start (`pickup_ts`).
- fewer than **half** of the game's players are linked to the match (logged as `only N of Y players linked`). Over-half keeps it team-size agnostic.
- team_idx disagreement with the bot draft (present players don't split cleanly
  direct-or-swapped onto the bot's teams).
- equal scores (a tie game is not a win for either side).

On success returns a verdict dict with `id`, `diag_winner`, `diag_team0_ids`,
`diag_team1_ids`, `map_direct` (bool), `bot_winner` (the Diabotical winner
re-mapped onto the bot's team order, honoring the swap), and `create_ts`.

### `series_signature(game) -> frozenset`

Grouping key: the unordered pair of the two teams' rosters. Two games belong to
the same series when they feature the same two teams regardless of side.

### `aggregate_series(games) -> {complete, scores, clinch_game_id, games}`

Sorts games by `create_ts`, counts `bot_winner` per game, and returns
`complete=True` as soon as one team reaches `CLINCH_WINS`, with the clinching
game id and the ordered list of game ids. `[1,1]` (incomplete) is not reported.

## 5. The poller (`report.py` — `DiaboticalPoller`)

Owned by `report.py`, driven by `MATCH.think` every cycle.

State machine essentials:
- `__init__`: sets `self.auto` from `DIABOTICAL_AUTO_REPORT`, `self.pickup_ts`
  from `match.start_time`, and initializes the seen/fetched/games/consumed maps.
- `setup(links)`: resolves drafted+linked players to Diabotical ids and builds
  `bot_teams` as frozensets. `local_teams` is built as mutable lists and only
  converted to frozensets **after** the loop.
- `think(now)` → `_run(now)`:
  - Stops itself if the match is no longer `WAITING_REPORT` or not in
    `bot.active_matches`.
  - Calls `_maybe_poll(now)` then `_evaluate(now)`.

### Rate-limit safety (critical)

The `/users/{id}/matches` path is hard-capped at **3 requests per reset window**
on Diabotical. Two guards:
- `MIN_PLAYER_INTERVAL = 30.0` — seconds between polls of the *same* player.
- `GLOBAL_MIN_GAP = 25.0` — seconds between *any* two `/users/*` requests.

`_maybe_poll` picks the oldest-due linked player (respecting both). The
`_last_user_poll` dict and `last_user_poll` scalar track this. **If you change
these numbers, stay well above the 3/reset hard cap** or you'll get rate-limit
rejections and the series won't be found.

### Fetch caching

`_verify(mid, now)` caches the fetched match in `self.fetched[mid] = (match, ts)`
for 30s (`(now - ts) > 30` refetches). This avoids hammering the `/match/{id}`
path (100 req/reset) on every poll tick. The recent-match list is cached in
`self.seen` keyed by player index.

### Evaluation flow (`_evaluate`)

1. **Candidate selection:** any `match_id` in `self.seen` with ≥
   `MIN_SHARED_PLAYERS` distinct linked players, not already `consumed`.
2. **Verify:** `_verify` → `matcher.candidate_game`. If `ok`, record the verdict
   under its `series_signature` and mark consumed. In non-auto mode, also posts
   a per-game scoreboard embed. If not `ok`, appends `"{id}: {reason}"` to
   `self.reasons`.
3. **Series aggregation:** for each series signature not yet in
   `reported_series`, run `matcher.aggregate_series`. If `complete`, mark
   reported, mark its games consumed, and call `_act`.

### Auto vs off mode (`_act`)

- `auto=True` → `_file_auto`: calls `MATCH.report_scores(ctx, scores)`. On
  failure it **discards** the series from `reported_series` and re-raises so it
  retries.
- `auto=False` → `_post_summary`: posts the series summary embed AND persists
  `self.match.pending_series_score` and `pending_series_clinch_game_id` so the
  operator can confirm later. Then stops the poller.

## 6. MATCH integration (`match.py`)

### Poller lifecycle

- `start_waiting_report()`: `import asyncio; loop = asyncio.get_running_loop()`
  then `loop.create_task(bot.diabotical.report.start_poller(self))`.
- `think()`: `elif self.state == self.WAITING_REPORT:` → ticks
  `poller.think(frame_time)` (poller only exists in this state).
- `finish_match()`: `stop_poller(self)`.
- `cancel()`: sets `diabotical_poller = None` then `stop_poller(self)`.
- Timeout (`think`, `frame_time > lifetime + start_time`): notices, then
  `cancel()` (which stops the poller).

### The confirm flow (off-mode)

`MATCH.__init__` sets:
```python
self.pending_series_score = None
self.pending_series_clinch_game_id = None
```

`confirm_series(ctx)` reads `pending_series_score`; if set, clears the pending
state and calls `report_scores(ctx, scores)`. This is the operator's safety net
when auto-report is off: the poller posts the summary, the operator reviews, and
`/diabotical confirm` files the exact score.

### Slash command

`/diabotical confirm` (in `commands.py`) finds the match for `ctx.author` in
`bot.active_matches` and calls `confirm_series`. Raises
`bot.Exc.NotFoundError` if the author isn't in an active match.

## 7. DB: `diabotical_links` table

Created at module import in `report.py`:
```sql
diabotical_links (guild_id, user_id, diabotical_id, PRIMARY KEY (guild_id, user_id))
```
`db.ensure_table(...)` runs at import time. This is safe because
`Pubobot2.py` calls `db.connect()` before `import bot`. If the connection isn't
live yet, the import will fail — the ordering must stay `db.connect()` first.

Functions: `get_links`, `set_link` (insert or update by PK), `delete_link`.
`db.types.int` / `db.types.str` must exist on the real `db` object (they do in
`cfg_factory.py`; only the bare test stub needs them faked).

## 8. The `/link` flow (`commands/diabotical.py`)

1. Validate the id against the Diabotical `users/{id}` endpoint (`validate_user`).
2. Store `(guild_id, discord_id, diabotical_id)`.
3. Only members drafted into a match are considered by the poller, so an
   unlinked player on a team simply doesn't participate in matching.

Matching keys **only** on Diabotical user id — never nicknames.

## 9. Config flag

```ini
# config.cfg
DIABOTICAL_AUTO_REPORT = False   # default: off
```

Read via `getattr(_global_cfg, "diabotical_auto_report", 0)` in
`report.py` (`_global_cfg` = `from core.config import cfg`). If the flag is
missing/mis-cased, it defaults to off → poller posts summaries and waits for
`/diabotical confirm`.

## 10. Testing

`tests/test_diabotical_matcher.py` — 10 unit tests, run with:
```bash
python3 tests/test_diabotical_matcher.py
```
Fixtures: 39 match JSON files in `tests/fixtures/` (25 real games + `dankjr_matches.json`
etc.). They exercise: valid series, tie rejection, first-to-two clinch,
start-before-pickup rejection, wrong-roster rejection, partial overlap,
non-wipeout rejection, and all-fixtures aggregate-clean-series.

Note: `matcher.py` is pure stdlib — the tests import it directly without
bot/nextcord/db. `report.py` and `client.py` need the bot env; smoke-test them
against stubbed `bot`/`core` modules.

## 11. Debugging checklist (when a series isn't reported)

1. **Was the poller started?** Check logs for `Started Diabotical poller for
   match {id}`. If it says `Skipping Diabotical poll ... only N of Y drafted
   player(s) linked`, fewer than half of the drafted players are linked.
2. **Did any candidate get rejected?** Read `poller.reasons` (logged). Common
   reasons: `not a wipeout match`, `team_count is X`, `not finished`,
   `before the pickup`, `only N of Y players linked`, `team_idx disagreement`,
   `tie`.
3. **team_idx disagreement?** This is the side-swapping trap. The present
   linked players' Diabotical partition must match the bot draft direct-or-
   swapped. If 3+ players disagree on their team assignment, no candidate
   survives.
4. **Rate-limited?** `/users/*` is 3/reset. If polls are too frequent you get
   `Diabotical user poll failed`. Verify `MIN_PLAYER_INTERVAL`/`GLOBAL_MIN_GAP`.
5. **`create_ts` before pickup?** The pickup must have started before the game.
   If the game's `create_ts` < `match.start_time`, it's rejected.
6. **Incomplete series?** A `[1,1]` series never clinches and is never reported
   by design. Wait for the third game or accept it as unresolved.
7. **Auto mode failing to file?** `_file_auto` re-raises on error and unmarks
   the series so it retries. Check `register_match_ranked` isn't erroring on the
   series score.

## 12. Gotchas learned

- **Build `local_teams` as mutable lists, convert to frozensets after the loop.**
  Mutating a frozenset during setup is an error.
- **The poller only runs inside the bot's event loop.** `asyncio_run` creates a
  task; if there's no running loop it returns `None` (no-op).
- **`find` is re-exported from `nextcord.utils`** via `core.utils`. LSP may flag
  it as missing (nextcord isn't installed in bare envs) — it works at runtime.
- **Every API value is opaque data.** Never interpolate match data into shell,
  eval, or the command parser.

## 13. Deployment notes

- Deploy in the GetCracked! Tournaments guild.
- Add `/link`, `/unlink`, `/link_status`, `/diabotical` to the `#pug` channel.
- The bot needs channel access to post scoreboards/summaries.
- For auto-reporting: set `DIABOTICAL_AUTO_REPORT = True`.
- For manual confirm: leave it `False`; operators run `/diabotical confirm`
  after reviewing the posted series summary.

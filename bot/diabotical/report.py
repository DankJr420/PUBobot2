# -*- coding: utf-8 -*-
"""Diabotical Wipeout result reporting for PUBobot2.

A single PUBobot *Match* is a best-of-3 series of Wipeout team games. The
Diabotical API returns one game per ``match_id``; this module reconstructs the
series by grouping finished wipeout team games that share the same two-team
roster and aggregating the games each team won into ``[team0_wins, team1_wins]``.

This module adds:
  * a per-guild ``diabotical_links`` table mapping a Discord snowflake to a
    Diabotical user id,
  * ``/link`` and ``/unlink`` slash commands to manage that mapping (wired in
    ``bot.commands.diabotical``),
  * a background poller, started when a ranked match reaches the *waiting report*
    stage, that watches Diabotical for the games of the series and either:
      - files the aggregated series result through the existing
        ``report_scores`` path (so ``finish_match`` and
        ``register_match_ranked`` still run) when
        ``diabotical_auto_report`` is true, or
      - posts a per-game scoreboard as each game finishes and a series summary
        when the series clinches, then waits for the operator's normal
        ``/report`` to confirm when ``diabotical_auto_report`` is false.

The matcher logic lives in :mod:`bot.diabotical.matcher` (pure, unit-tested);
this module owns the database links, the async poller and the embeds.

Every value that comes back from the Diabotical API is treated as opaque data:
it is never evaluated, run through a shell, or fed into the bot's command
parser.
"""
import time
import asyncio

from nextcord.utils import find
from core.utils import get_nick
from core.config import cfg as _global_cfg
from core.console import log
from core.database import db

import bot
from . import client as _client
from . import matcher

# Ensure the link table exists before anything reads it.
db.ensure_table(dict(
    tname="diabotical_links",
    columns=[
        dict(cname="guild_id", ctype=db.types.int),
        dict(cname="user_id", ctype=db.types.int),
        dict(cname="diabotical_id", ctype=db.types.str),
    ],
    primary_keys=["guild_id", "user_id"]
))

# Rate-limit safety for the /users/* path (hard-capped at 3 requests/reset
# window on Diabotical). Keep polls well spaced.
MIN_PLAYER_INTERVAL = 30.0   # seconds between polls of the same player
GLOBAL_MIN_GAP = 25.0        # seconds between any two /users/* requests


# --------------------------------------------------------------------------
# Link persistence
# --------------------------------------------------------------------------

async def get_links(guild_id: int) -> list:
    """Return ``[{discord_id, diabotical_id}, ...]`` for a guild."""
    rows = await db.select(
        ("user_id", "diabotical_id"), "diabotical_links",
        where={"guild_id": guild_id}, order_by="user_id"
    )
    return [{"discord_id": r["user_id"], "diabotical_id": r["diabotical_id"]} for r in rows]


async def set_link(guild_id: int, discord_id: int, diabotical_id) -> None:
    row = {"guild_id": guild_id, "user_id": discord_id, "diabotical_id": str(diabotical_id)}
    existing = await db.select_one(("user_id",), "diabotical_links",
                                   where={"guild_id": guild_id, "user_id": discord_id})
    if existing:
        await db.update("diabotical_links", row, keys={"guild_id": guild_id, "user_id": discord_id})
    else:
        await db.insert("diabotical_links", row)


async def delete_link(guild_id: int, discord_id: int) -> bool:
    existing = await db.select_one(("user_id",), "diabotical_links",
                                   where={"guild_id": guild_id, "user_id": discord_id})
    if existing:
        await db.delete("diabotical_links", where={"guild_id": guild_id, "user_id": discord_id})
        return True
    return False


def get_name_only(name):
    return str(name) if name is not None else "?"


# --------------------------------------------------------------------------
# Scoreboard embeds
# --------------------------------------------------------------------------

def build_board_embed(match, winner_team_idx, draw):
    """Build a nextcord Embed describing one matched Diabotical game."""
    from nextcord import Embed, Colour

    teams = match.get("teams", [])
    clients = match.get("clients", [])
    match_id = match.get("match_id")
    match_map = match.get("match_map") or match.get("match_map_name") or "?"

    embed = Embed(colour=Colour(0x408ad9))
    if draw:
        embed.title = "Diabotical Wipeout game — draw"
    elif winner_team_idx is not None:
        embed.title = "Diabotical Wipeout game — winner"
    else:
        embed.title = "Diabotical Wipeout game"
    embed.add_field(name="Match", value=f"`{match_id}`", inline=True)
    embed.add_field(name="Map", value=f"**{match_map}**", inline=True)

    for idx, team in enumerate(teams):
        label = f"**{team.get('name', 'Team ' + str(idx + 1))}**"
        if winner_team_idx == idx:
            label += "  ✅"
        elif draw:
            label += "  🤝"
        members = [c for c in clients if c.get("team_idx") == idx]
        names = ", ".join(get_name_only(c.get("name")) for c in members)
        frags = ", ".join(
            f"{get_name_only(c.get('name'))}={c.get('stats', {}).get('f', 0)}" for c in members
        )
        damage = ", ".join(
            f"{get_name_only(c.get('name'))}={c.get('stats', {}).get('di', 0)}" for c in members
        )
        value = (
            f"score={team.get('score', 0)}\n"
            f"players: {names}\n"
            f"frags: {frags}\n"
            f"damage: {damage}"
        )
        embed.add_field(name=label, value=value, inline=False)

    embed.set_footer(text=f"diabotical.cool/match/{match_id}")
    embed.url = f"https://diabotical.cool/match/{match_id}"
    return embed


def build_summary_embed(games, agg, bot_winner):
    """Build a nextcord Embed summarising a clinched best-of-3 series."""
    from nextcord import Embed, Colour

    title = "Diabotical Wipeout series complete"
    if bot_winner is not None:
        title += f" — {bot_winner + 1} team wins {agg['scores'][0]}-{agg['scores'][1]}"
    else:
        title += " — draw"
    embed = Embed(colour=Colour(0x408ad9))
    embed.title = title
    lines = [f"Games won: **{agg['scores'][0]}** :vs: **{agg['scores'][1]}**"]
    for i, mid in enumerate(agg["games"], 1):
        lines.append(f"{i}. `{mid}`")
    embed.add_field(name="Diabotical games", value="\n".join(lines), inline=False)
    clinch = agg["clinch_game_id"]
    if clinch:
        embed.set_footer(text=f"diabotical.cool/match/{clinch}")
        embed.url = f"https://diabotical.cool/match/{clinch}"
    return embed


# --------------------------------------------------------------------------
# Background poller
# --------------------------------------------------------------------------

class DiaboticalPoller:
    """Watches Diabotical for the best-of-3 series a WAITING_REPORT game is.

    Driven by ``Match.think`` every cycle; exits on its own once the match
    leaves the active set (finished, cancelled, timed out) or ``stop()`` runs.
    """

    def __init__(self, match):
        self.match = match
        self.qc = match.qc
        self.client = _client.DiaboticalClient()
        self.auto = bool(getattr(_global_cfg, "diabotical_auto_report", 0))
        self.stop_flag = False
        self.linked_ids = []            # list of diabotical ids drafted in this match
        self.bot_teams = [[], []]       # mutable during setup -> frozensets
        self.pickup_ts = getattr(match, "start_time", 0) or 0
        self._last_user_poll = {}       # {linked_index: epoch}
        self.last_user_poll = 0.0
        self.seen = {}                  # {match_id: set(player indices)}
        self.fetched = {}               # {match_id: (full_match, epoch)}
        self.games = {}                 # {series_signature: [verdict, ...]}
        self.consumed = set()           # match_ids already incorporated
        self.reported_series = set()    # series signatures already acted on
        self.reasons = []               # human-readable rejection log

    # ---- setup ---------------------------------------------------------

    async def setup(self, links):
        """Resolve the drafted+linked players to Diabotical ids and bot teams."""
        teams = self.match.teams
        local_teams = [set(), set()]
        for link in links:
            discord_id = link["discord_id"]
            member = find(lambda p: p.id == discord_id, self.match.players)
            if member is None:
                continue
            team_idx = None
            if member in teams[0]:
                team_idx = 0
            elif member in teams[1]:
                team_idx = 1
            if team_idx is None:
                continue
            local_teams[team_idx].add(link["diabotical_id"])
            self.linked_ids.append(link["diabotical_id"])
        self.bot_teams = [frozenset(local_teams[0]), frozenset(local_teams[1])]

    # ---- main loop -----------------------------------------------------

    async def think(self, now):
        try:
            await self._run(now)
        except Exception as e:
            log.error(f"Diabotical poller error on match {self.match.id}: {e}")
            self.stop()

    async def _run(self, now):
        if self.stop_flag:
            return
        if self.match.state != bot.Match.WAITING_REPORT or self.match not in bot.active_matches:
            self.stop()
            return

        self._maybe_poll(now)
        self._evaluate(now)

    def _maybe_poll(self, now):
        """Poll the oldest-due linked player, respecting rate limits."""
        due_idx = None
        oldest = None
        for idx, _pid in enumerate(self.linked_ids):
            last = self._last_user_poll.get(idx, 0)
            if (now - last >= MIN_PLAYER_INTERVAL and
                    now - self.last_user_poll >= GLOBAL_MIN_GAP):
                if oldest is None or last < oldest:
                    oldest = last
                    due_idx = idx
        if due_idx is None:
            return
        pid = self.linked_ids[due_idx]
        try:
            matches = self.client.get_user_matches(pid)
        except _client.DiaboticalAPIError as e:
            log.error(f"Diabotical user poll failed ({pid}): {e}")
            return
        self._last_user_poll[due_idx] = now
        self.last_user_poll = now
        for mm in matches:
            mid = mm.get("match_id")
            if mid:
                self.seen.setdefault(mid, set()).add(due_idx)

    def _evaluate(self, now):
        candidates = [mid for mid, idxs in self.seen.items()
                      if len(idxs) >= matcher.MIN_SHARED_PLAYERS and mid not in self.consumed]
        for mid in candidates:
            verdict = self._verify(mid, now)
            if verdict["ok"]:
                sig = matcher.series_signature(verdict)
                self.games.setdefault(sig, []).append(verdict)
                self.consumed.add(mid)
                if not self.auto:
                    # Post a per-game scoreboard as each game finishes.
                    asyncio_run(self._post_game_embed(verdict))
            else:
                self.reasons.append(f"{mid}: {verdict['reason']}")

        for sig, gs in list(self.games.items()):
            if sig in self.reported_series:
                continue
            agg = matcher.aggregate_series(gs)
            if agg["complete"]:
                self.reported_series.add(sig)
                self.consumed.update(agg["games"])
                asyncio_run(self._act(sig, gs, agg))

    async def _verify(self, mid, now):
        match, ts = self.fetched.get(mid, (None, 0))
        if match is None or (now - ts) > 30:
            try:
                match = self.client.get_match(mid)
            except _client.DiaboticalAPIError as e:
                return matcher._reject(f"fetch failed: {e}")
            self.fetched[mid] = (match, now)
        return matcher.candidate_game(
            match, set(self.linked_ids), self.pickup_ts, self.bot_teams
        )

    async def _act(self, sig, games, agg):
        bot_winner = None
        for g in games:
            if g["id"] == agg["clinch_game_id"]:
                bot_winner = g["bot_winner"]
                break
        if self.auto:
            await self._file_auto(sig, agg)
        else:
            await self._post_summary(games, agg, bot_winner)

    async def _file_auto(self, sig, agg):
        log.info(f"Diabotical series clinched for match {self.match.id}: {agg['scores']} "
                 f"(games: {agg['games']})")
        ctx = bot.SystemContext(self.qc)
        try:
            await self.match.report_scores(ctx, agg["scores"])
        except Exception as e:
            log.error(f"Failed to file Diabotical report for series on match {self.match.id}: {e}")
            self.reported_series.discard(sig)
            raise

    async def _post_game_embed(self, verdict):
        match, ts = self.fetched.get(verdict["id"], (None, 0))
        if match is None:
            try:
                match = self.client.get_match(verdict["id"])
            except _client.DiaboticalAPIError as e:
                log.error(f"Diabotical scoreboard fetch failed ({verdict['id']}): {e}")
                return
        ctx = bot.SystemContext(self.qc)
        try:
            await ctx.notice(embed=build_board_embed(match, verdict["bot_winner"], None))
        except Exception as e:
            log.error(f"Failed to post Diabotical scoreboard: {e}")

    async def _post_summary(self, games, agg, bot_winner):
        ctx = bot.SystemContext(self.qc)
        # Persist the aggregated series score so the operator can confirm it
        # through a normal path after reviewing the summary.
        self.match.pending_series_score = list(agg["scores"])
        self.match.pending_series_clinch_game_id = agg["clinch_game_id"]
        try:
            await ctx.notice(embed=build_summary_embed(games, agg, bot_winner))
        except Exception as e:
            log.error(f"Failed to post Diabotical series summary: {e}")
        self.stop()

    def stop(self):
        self.stop_flag = True


def asyncio_run(coro):
    """Schedule a coroutine on the running event loop without blocking.

    The poller only ever runs inside the bot's event loop, so this just creates
    a task.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(coro)


# --------------------------------------------------------------------------
# Integration helpers
# --------------------------------------------------------------------------

async def start_poller(match):
    """Attach a poller to a match that reached WAITING_REPORT, if applicable."""
    qc = match.qc
    links = await get_links(qc.id)
    poller = DiaboticalPoller(match)
    await poller.setup(links)
    # Team-size-agnostic: only start polling when over half of the drafted
    # players are linked (so 3v3, 4v4, etc. all work).
    if not matcher.majority(len(poller.linked_ids), len(match.players)):
        log.info(f"Skipping Diabotical poll for match {match.id}: only "
                 f"{len(poller.linked_ids)} of {len(match.players)} drafted "
                 f"player(s) linked.")
        return
    match.diabotical_poller = poller
    log.info(f"Started Diabotical poller for match {match.id} "
             f"({len(poller.linked_ids)} linked players, auto_report={poller.auto}).")


async def stop_poller(match):
    poller = getattr(match, "diabotical_poller", None)
    if poller is not None:
        poller.stop()

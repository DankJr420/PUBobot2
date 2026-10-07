# -*- coding: utf-8 -*-
"""Pure match-finding logic for Diabotical Wipeout result reporting.

This module is intentionally free of any bot / nextcord / database imports so it
can be unit-tested in a bare environment. It only depends on the Diabotical API
client (:mod:`bot.diabotical.client`), which itself is stdlib-only.

A single PUBobot *Match* corresponds to a best-of-3 series of Wipeout team games.
The Diabotical API returns one game per ``match_id``; the series is reconstructed
by grouping finished wipeout team games that share the same two-team roster and
aggregating the games each team won into ``[team0_wins, team1_wins]``.

Every value that comes back from the Diabotical API is treated as opaque data:
it is never evaluated, run through a shell, or fed into the bot's command parser.
"""
import logging

from . import client as _client

log = logging.getLogger("pubobot.diabotical")

# Minimum number of linked players that must be present in a single game before
# it is trusted as part of the series. Mirrors "at least 6 of 8" for a full
# 4v4 team game.
MIN_SHARED_IDS = 6

# A candidate match_id must appear in the recent match list of at least this many
# distinct linked players before it is fetched/verified (guards against false
# positives from the short recent-match list).
MIN_SHARED_PLAYERS = 2

# Best-of-3 -> a series is clinched as soon as one team reaches this many wins.
CLINCH_WINS = 2

# Diabotical match_state values that mean the game has concluded.
FINISHED_STATES = (3, 4)


def _match_finished(match) -> bool:
    """True if the game has concluded (any of several API shape variants)."""
    if match.get("match_state") in FINISHED_STATES:
        return True
    if match.get("finished") is True:
        return True
    return any(t.get("finished") for t in match.get("teams", []))


def is_wipeout_team_match(match) -> bool:
    """A Wipeout team game is team_count == 2 with two scored, non-equal teams."""
    if match.get("match_mode") != "wipeout":
        return False
    if match.get("team_count") != 2:
        return False
    teams = match.get("teams", [])
    if len(teams) != 2:
        return False
    scores = [t.get("score", 0) or 0 for t in teams]
    if scores[0] == scores[1]:
        return False
    return max(scores) > 0


def _reject(reason):
    return {"ok": False, "reason": reason}


def candidate_game(match, linked_ids, pickup_ts, bot_teams):
    """Decide whether one full match object is a valid game of this series.

    Parameters
    ----------
    match:
        The full match object fetched from ``GET /match/{id}``.
    linked_ids:
        Diabotical user ids linked to this guild (a set of opaque ids).
    pickup_ts:
        Epoch seconds when the pickup/match started.
    bot_teams:
        ``[frozenset(bot_team0_ids), frozenset(bot_team1_ids)]`` — the two teams
        the bot assigned for this match, built from the draft. Used for the draft
        team_idx agreement check and to map Diabotical team order onto the bot's
        team order.

    Returns
    -------
    A verdict dict with keys ``ok`` and, when true, ``id``, ``diag_winner``,
    ``diag_team0_ids``, ``diag_team1_ids``, ``map_direct``, ``bot_winner`` and
    ``create_ts``. When false it has keys ``ok`` and ``reason``.

    Note on team agreement: a single bot Match is a best-of-3 series, and players
    may play opposite sides from game to game. The check is therefore *team-level*,
    not per-user: the present linked players' Diabotical team partition must match
    the bot's team partition either directly (Diabotical 0 == bot 0) or swapped
    (Diabotical 0 == bot 1) for the whole series.
    """
    if match.get("match_mode") != "wipeout":
        return _reject(f"not a wipeout match (mode={match.get('match_mode')!r})")
    if match.get("team_count") != 2:
        return _reject(f"team_count is {match.get('team_count')}, expected 2")
    if not _match_finished(match):
        return _reject(f"match not finished (state={match.get('match_state')})")

    create_dt = _client.DiaboticalClient.parse_ts(match.get("create_ts"))
    if create_dt is None:
        return _reject("could not parse create_ts")
    create_ts = create_dt.replace(tzinfo=_client.timezone.utc)
    if create_ts.timestamp() < pickup_ts:
        return _reject("match started before the pickup")

    clients = match.get("clients", [])
    present = [c for c in clients if c.get("user_id") in linked_ids]
    if len(present) < MIN_SHARED_IDS:
        return _reject(f"only {len(present)} of {len(linked_ids)} linked ids in the match")

    # Draft team_idx agreement (team-level). The present linked players split into
    # a Diabotical team-0 group and a Diabotical team-1 group. Those two groups
    # must match the bot's two teams, either directly or swapped.
    bot_team0 = bot_teams[0]
    bot_team1 = bot_teams[1]
    present_ids = {c.get("user_id") for c in present}
    p0 = present_ids & bot_team0
    p1 = present_ids & bot_team1
    if not (p0 or p1):
        return _reject("no present linked players match the bot's teams")

    diag_team0_ids = frozenset(
        c.get("user_id") for c in clients if c.get("team_idx") == 0
    )
    diag_team1_ids = frozenset(
        c.get("user_id") for c in clients if c.get("team_idx") == 1
    )

    # Direct: bot team 0 members are on Diabotical team 0.
    if p0 and p0 <= diag_team0_ids and p1 and p1 <= diag_team1_ids:
        map_direct = True
    elif p0 and p0 <= diag_team1_ids and p1 and p1 <= diag_team0_ids:
        map_direct = False
    else:
        return _reject("team_idx disagreement with the bot draft")

    teams = match.get("teams", [])
    s0 = teams[0].get("score", 0) or 0
    s1 = teams[1].get("score", 0) or 0
    if s0 == s1:
        return _reject("game ended in a tie (equal scores)")
    diag_winner = 0 if s0 > s1 else 1
    bot_winner = diag_winner if map_direct else 1 - diag_winner

    return {
        "ok": True,
        "reason": None,
        "id": match.get("match_id"),
        "diag_score0": s0,
        "diag_score1": s1,
        "diag_winner": diag_winner,
        "diag_team0_ids": diag_team0_ids,
        "diag_team1_ids": diag_team1_ids,
        "map_direct": map_direct,
        "bot_winner": bot_winner,
        "create_ts": create_ts,
    }


def series_signature(game) -> frozenset:
    """Grouping key: the unordered pair of the two teams' rosters.

    Two games belong to the same best-of-3 series when they feature the same two
    teams (same eight players split the same way), regardless of which side each
    team played on.
    """
    return frozenset([game["diag_team0_ids"], game["diag_team1_ids"]])


def aggregate_series(games) -> dict:
    """Aggregate the games of one roster-pair (best-of-3) into a series score.

    Parameters
    ----------
    games:
        Verdict dicts from :func:`candidate_game` (each ``ok``), any order.

    Returns
    -------
    ``{"complete": bool, "scores": [w0, w1], "clinch_game_id": id|None,
    "games": [ordered game ids]}``. ``scores`` is the series score to hand to
    ``report_scores`` (one rating cycle per game won).
    """
    ordered = sorted(games, key=lambda g: g["create_ts"])
    w0 = w1 = 0
    for g in ordered:
        if g["diag_winner"] is None:
            continue
        if g["bot_winner"] == 0:
            w0 += 1
        else:
            w1 += 1
        if w0 == CLINCH_WINS or w1 == CLINCH_WINS:
            return {
                "complete": True,
                "scores": [w0, w1],
                "clinch_game_id": g["id"],
                "games": [x["id"] for x in ordered],
            }
    return {
        "complete": False,
        "scores": [w0, w1],
        "clinch_game_id": None,
        "games": [x["id"] for x in ordered],
    }

# -*- coding: utf-8 -*-
"""Unit tests for bot.diabotical.matcher (pure, no bot/nextcord needed).

Run from the PUBobot2 project root:

    python3 tests/test_diabotical_matcher.py

The tests build small in-memory match objects so they do not depend on any
installed package. The real saved fixtures live in tests/fixtures/.
"""
import os
import sys
import json
import glob
import importlib.util
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def _load():
    """Load matcher + client as standalone modules (no `import bot`)."""
    client_path = os.path.join(ROOT, "bot", "diabotical", "client.py")
    matcher_path = os.path.join(ROOT, "bot", "diabotical", "matcher.py")

    spec = importlib.util.spec_from_file_location("diabotical_client_mod", client_path)
    client = importlib.util.module_from_spec(spec)
    sys.modules["diabotical_client_mod"] = client
    spec.loader.exec_module(client)

    pkg = types.ModuleType("bot_diabotical")
    pkg.__path__ = [os.path.join(ROOT, "bot", "diabotical")]
    sys.modules["bot_diabotical"] = pkg
    cpkg = types.ModuleType("bot_diabotical.client")
    cpkg.__dict__.update(client.__dict__)
    sys.modules["bot_diabotical.client"] = cpkg

    spec = importlib.util.spec_from_file_location(
        "bot_diabotical.matcher", matcher_path, submodule_search_locations=[pkg.__path__[0]]
    )
    matcher = importlib.util.module_from_spec(spec)
    sys.modules["bot_diabotical.matcher"] = matcher
    spec.loader.exec_module(matcher)
    return matcher


matcher = _load()


# --------------------------------------------------------------------------
# Helpers to build match objects
# --------------------------------------------------------------------------

def _match(match_id="m1", score0=4, score1=1, map_name="woa_map",
           team0_ids=("a", "b", "c", "d"), team1_ids=("e", "f", "g", "h"),
           state=4, create_ts="2026-10-06T04:48:21.000Z"):
    """Build a Diabotical match object (opaque data)."""
    clients = []
    for uid in team0_ids:
        clients.append({"user_id": uid, "name": uid.upper(), "team_idx": 0,
                        "stats": {"f": 3, "di": 100}})
    for uid in team1_ids:
        clients.append({"user_id": uid, "name": uid.upper(), "team_idx": 1,
                        "stats": {"f": 1, "di": 50}})
    return {
        "match_id": match_id,
        "match_mode": "wipeout",
        "match_type": 2,
        "match_state": state,
        "match_private": False,
        "match_map": map_name,
        "team_count": 2,
        "team_size": 4,
        "score_limit": 4,
        "create_ts": create_ts,
        "teams": [
            {"name": "Team 1", "score": score0, "team_idx": 0, "placement": 1, "finished": True},
            {"name": "Team 2", "score": score1, "team_idx": 1, "placement": 2, "finished": True},
        ],
        "clients": clients,
    }


def _bot_teams(team0_ids, team1_ids):
    return [frozenset(team0_ids), frozenset(team1_ids)]


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_valid_wipeout_series():
    # Two games, same two teams, one direct and one swapped (Bo3).
    g1 = _match("m1", 4, 1, team0_ids=("a", "b", "c", "d"), team1_ids=("e", "f", "g", "h"))
    g2 = _match("m2", 1, 4, team0_ids=("e", "f", "g", "h"), team1_ids=("a", "b", "c", "d"))
    bot = _bot_teams(("a", "b", "c", "d"), ("e", "f", "g", "h"))
    linked = frozenset("abcdefgh")

    v1 = matcher.candidate_game(g1, linked, pickup_ts=0, bot_teams=bot)
    v2 = matcher.candidate_game(g2, linked, pickup_ts=0, bot_teams=bot)
    assert v1["ok"], v1
    assert v2["ok"], v2
    # g1: team 0 wins -> bot 0. g2: swapped (diag winner is 1) -> bot 0.
    assert v1["bot_winner"] == 0
    assert v2["bot_winner"] == 0
    agg = matcher.aggregate_series([v1, v2])
    assert agg["complete"] is True
    assert agg["scores"] == [2, 0], agg


def test_tie_is_rejected():
    m = _match("m1", 2, 2)  # equal scores
    v = matcher.candidate_game(m, frozenset("abcdefgh"), 0, _bot_teams(("a", "b", "c", "d"), ("e", "f", "g", "h")))
    assert v["ok"] is False
    assert "tie" in v["reason"].lower() or "equal" in v["reason"].lower()


def test_wrong_roster_rejected():
    # Only 3 of our linked players present (a,b,c) plus an unlinked player z.
    m = _match("m1", 4, 1,
               team0_ids=("a", "b", "c", "z"),   # z is not linked
               team1_ids=("q", "w", "r", "t"))  # none linked
    linked = frozenset("abcdefgh")
    v = matcher.candidate_game(m, linked, 0, _bot_teams(("a", "b", "c", "d"), ("e", "f", "g", "h")))
    assert v["ok"] is False
    assert "only 3 of 8" in v["reason"]


def test_wrong_roster_partial_overlap():
    # 6 linked players present, but split across teams so neither direct nor
    # swapped alignment matches the bot draft (a,b,c,d,e,f all on Diabotical team
    # 0, none on team 1) -> team-level disagreement.
    m = _match("m1", 4, 1,
               team0_ids=("a", "b", "c", "d", "e", "f"),
               team1_ids=("g", "h", "w", "r"))
    linked = frozenset("abcdefgh")
    v = matcher.candidate_game(m, linked, 0, _bot_teams(("a", "b", "c", "d"), ("e", "f", "g", "h")))
    assert v["ok"] is False
    assert "team_idx disagreement" in v["reason"]


def test_team_idx_disagreement_rejected():
    # Bot draft says team 0 = {a,b,c,d} but in the match a,b are on team 1.
    m = _match("m1", 4, 1, team0_ids=("a", "b", "c", "d"), team1_ids=("e", "f", "g", "h"))
    bot_wrong = _bot_teams(("e", "f", "g", "h"), ("a", "b", "c", "d"))  # correct actually
    # Force a real disagreement: bot claims a,b are on the losing side that is team 0,
    # but they're on Diabotical team 0 which won. Use bot where team0 = {a,b, e,f}.
    bot = _bot_teams(("a", "b", "e", "f"), ("c", "d", "g", "h"))
    v = matcher.candidate_game(m, frozenset("abcdefgh"), 0, bot)
    assert v["ok"] is False
    assert "team_idx disagreement" in v["reason"]


def test_start_before_pickup_rejected():
    m = _match("m1", 4, 1, create_ts="2026-10-06T04:48:21.000Z")
    v = matcher.candidate_game(m, frozenset("abcdefgh"), pickup_ts=9999999999.0,
                               bot_teams=_bot_teams(("a", "b", "c", "d"), ("e", "f", "g", "h")))
    assert v["ok"] is False
    assert "before the pickup" in v["reason"]


def test_non_wipeout_rejected():
    m = _match("m1", 4, 1)
    m["match_mode"] = "ffa"
    v = matcher.candidate_game(m, frozenset("abcdefgh"), 0, _bot_teams(("a", "b", "c", "d"), ("e", "f", "g", "h")))
    assert v["ok"] is False
    assert "wipeout" in v["reason"]


def test_draw_series_incomplete():
    # Two direct games, one won by each team, no clinch -> incomplete [1,1].
    g1 = _match("m1", 4, 1, team0_ids=("a", "b", "c", "d"), team1_ids=("e", "f", "g", "h"))
    g2 = _match("m2", 1, 4, team0_ids=("a", "b", "c", "d"), team1_ids=("e", "f", "g", "h"))
    bot = _bot_teams(("a", "b", "c", "d"), ("e", "f", "g", "h"))
    linked = frozenset("abcdefgh")
    v1 = matcher.candidate_game(g1, linked, 0, bot)
    v2 = matcher.candidate_game(g2, linked, 0, bot)
    agg = matcher.aggregate_series([v1, v2])
    assert agg["complete"] is False
    assert agg["scores"] == [1, 1], agg
    assert agg["clinch_game_id"] is None


def test_first_to_two_clinches():
    # Best-of-3: two wins end it on the second game.
    g1 = _match("m1", 4, 1, team0_ids=("a", "b", "c", "d"), team1_ids=("e", "f", "g", "h"))
    g2 = _match("m2", 4, 1, team0_ids=("a", "b", "c", "d"), team1_ids=("e", "f", "g", "h"))
    g3 = _match("m3", 4, 1, team0_ids=("a", "b", "c", "d"), team1_ids=("e", "f", "g", "h"))
    bot = _bot_teams(("a", "b", "c", "d"), ("e", "f", "g", "h"))
    linked = frozenset("abcdefgh")
    vs = [matcher.candidate_game(g, linked, 0, bot) for g in (g1, g2, g3)]
    agg = matcher.aggregate_series(vs)
    assert agg["complete"] is True
    assert agg["scores"] == [2, 0], agg
    assert agg["clinch_game_id"] == "m2"


def _load_fixtures():
    out = []
    for f in sorted(glob.glob(os.path.join(HERE, "fixtures", "match_*.json"))):
        out.append(json.load(open(f)).get("match", None))
    return out


def test_all_fixtures_aggregate_clean_series():
    """Every saved wipeout game must be a valid candidate, grouping into clean series."""
    games = _load_fixtures()
    assert len(games) >= 20, f"expected fixtures, got {len(games)}"

    # Group by diag roster-pair and build a per-series draft from the first game.
    def diag_pair(m):
        c0 = frozenset(c["user_id"] for c in m["clients"] if c["team_idx"] == 0)
        c1 = frozenset(c["user_id"] for c in m["clients"] if c["team_idx"] == 1)
        return frozenset([c0, c1])

    from collections import defaultdict
    by_pair = defaultdict(list)
    for m in games:
        by_pair[diag_pair(m)].append(m)

    drafts = {}
    for pair, ms in by_pair.items():
        ms = sorted(ms, key=lambda x: x["create_ts"])
        b0 = frozenset(c["user_id"] for c in ms[0]["clients"] if c["team_idx"] == 0)
        b1 = frozenset(c["user_id"] for c in ms[0]["clients"] if c["team_idx"] == 1)
        drafts[pair] = [b0, b1]

    linked = set()
    for m in games:
        for c in m["clients"]:
            linked.add(c["user_id"])

    verdicts = []
    for m in games:
        v = matcher.candidate_game(m, linked, 0.0, drafts[diag_pair(m)])
        assert v["ok"], f"fixture rejected: {m['match_id']} {v}"
        verdicts.append(v)

    series = defaultdict(list)
    for v in verdicts:
        series[matcher.series_signature(v)].append(v)

    for pair, gs in series.items():
        agg = matcher.aggregate_series(gs)
        # A series is either clinched, or genuinely tied 1-1 with no third game.
        if not agg["complete"]:
            assert agg["scores"] == [1, 1], agg
    print(f"\n[test_all_fixtures] {len(games)} games across {len(series)} series all aggregate cleanly")


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)

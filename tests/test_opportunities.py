"""Tests for opportunity detection."""
from wcf import opportunities
from wcf.models import Player, PlayerProjection


def proj(pid, ep, pos="MID", nation="Spain", price=6.0, opp="Foe"):
    return PlayerProjection(player_id=pid, name=pid, nation=nation, position=pos,
                            price=price, match_id="m", opponent=opp,
                            p_start=0.9, exp_minutes=80, exp_points=ep)


def player(pid, status="playing", own=10.0, pos="MID", nation="Spain", price=6.0):
    return Player(id=pid, name=pid, nation=nation, position=pos, price=price,
                  status=status, ownership=own)


def base_team():
    return {
        "starters": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "bench": [{"id": "d"}],
        "captain": "a", "vice": "b",
    }


def kinds(alerts):
    return {al.kind for al in alerts}


def test_flags_unavailable_owned_player():
    team = base_team()
    projs = [proj("a", 5), proj("b", 4), proj("c", 3), proj("d", 2)]
    players = [player("a"), player("b", status="injured"), player("c"), player("d")]
    alerts = opportunities.detect(team, projs, players)
    assert "unavailable" in kinds(alerts)
    assert any("b" in a.message for a in alerts if a.kind == "unavailable")


def test_flags_low_owned_differential():
    team = base_team()
    projs = [proj("a", 5), proj("b", 4), proj("c", 3), proj("d", 2),
             proj("star", 9, nation="Brazil")]          # not owned, high EP
    players = [player("a"), player("b"), player("c"), player("d"),
               player("star", own=3.0, nation="Brazil")]   # 3% owned -> differential
    alerts = opportunities.detect(team, projs, players)
    assert "differential" in kinds(alerts)
    assert any("star" in a.message for a in alerts if a.kind == "differential")


def test_flags_captain_not_in_confirmed_xi():
    team = base_team()                      # captain = "a" (Spain)
    projs = [proj("a", 5), proj("b", 4), proj("c", 3), proj("d", 2)]
    players = [player("a"), player("b"), player("c"), player("d")]
    # Spain's confirmed XI does NOT include player "a".
    confirmed = {"Spain": {"someone else", "another player"}}
    alerts = opportunities.detect(team, projs, players, confirmed_by_nation=confirmed)
    assert "captain_benched" in kinds(alerts)


def test_no_false_alerts_when_all_fine():
    team = base_team()
    projs = [proj("a", 5), proj("b", 4), proj("c", 3), proj("d", 2)]
    players = [player("a"), player("b"), player("c"), player("d")]
    alerts = opportunities.detect(team, projs, players)
    # No unavailable, no captain issue (no confirmed lineups passed).
    assert "unavailable" not in kinds(alerts)
    assert "captain_benched" not in kinds(alerts)


def test_momentum_riser_and_faller():
    team = base_team()                      # owns a, b, c, d
    projs = [proj("a", 5), proj("b", 4), proj("c", 3), proj("d", 2),
             proj("riser", 6, nation="Italy")]
    players = [player("a"), player("b"), player("c"), player("d"),
               player("riser", own=4.0, nation="Italy")]
    momentum = {
        "a": {"now": 2.0, "prev": 5.0, "delta": -3.0, "rel": -0.60},   # owned, falling
        "riser": {"now": 4.0, "prev": 1.0, "delta": 3.0, "rel": 3.0},  # surging
    }
    alerts = opportunities.detect(team, projs, players, momentum=momentum)
    assert "ownership_falling" in kinds(alerts)
    assert "ownership_rising" in kinds(alerts)

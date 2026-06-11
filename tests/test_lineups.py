"""Tests for the start-probability prior (price backbone + ownership direction)."""
from wcf.models import Player
from wcf.providers.lineups import from_pool


def _pool():
    return [
        Player(id="star", name="Star", nation="X", position="MID", price=9.0, ownership=20),
        Player(id="faller", name="Faller", nation="X", position="MID", price=8.5, ownership=15),
        Player(id="s3", name="S3", nation="X", position="MID", price=6.0, ownership=5),
        Player(id="s4", name="S4", nation="X", position="MID", price=5.5, ownership=4),
        Player(id="riser", name="Riser", nation="X", position="MID", price=4.5, ownership=3),
    ]


def test_cold_start_uses_level_no_direction():
    # No history: a high price+ownership player is a nailed starter; momentum ignored.
    mom = {"faller": {"rel": -0.5, "now": 8, "prev": 15}}
    ln = from_pool(_pool(), momentum=mom, trend_available=False)
    assert ln.for_player("faller")["p_start"] > 0.8     # not penalised at cold start


def test_trend_penalises_fallers_and_lifts_risers():
    mom = {"faller": {"rel": -0.5, "now": 8, "prev": 15},
           "riser": {"rel": 1.2, "now": 3, "prev": 1}}
    ln = from_pool(_pool(), momentum=mom, trend_available=True)
    # Falling ownership -> treated as a doubt (auto-deweighted, no manual override).
    assert ln.for_player("faller")["p_start"] <= 0.20
    # Rising ownership -> lifted out of the fringe.
    assert ln.for_player("riser")["p_start"] >= 0.55


def test_unavailable_always_zero():
    pool = _pool() + [Player(id="out", name="Out", nation="X", position="MID",
                             price=9.0, ownership=30, status="transferred")]
    ln = from_pool(pool, trend_available=False)
    assert ln.for_player("out")["p_start"] == 0.0

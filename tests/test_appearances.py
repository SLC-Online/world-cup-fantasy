"""Tests for the 'who actually played' learning signal."""
from wcf.models import Player
from wcf.providers import lineups as lp
from wcf import persistence


def _pl(pid, name, nation, pos, price, own=0.0):
    return Player(id=pid, name=name, nation=nation, position=pos,
                  price=price, ownership=own)


def test_from_pool_appearance_override_beats_name_prior():
    """The exact bug we hit: a pricier/famous backup keeper is rated a nailed
    starter by the prior, but once we know who actually played it flips."""
    rangel = _pl("rangel", "Raul Rangel", "Mexico", "GK", 3.9, own=4.9)
    ochoa = _pl("ochoa", "Guillermo Ochoa", "Mexico", "GK", 4.2, own=4.8)
    players = [rangel, ochoa]

    # Cold prior: the dearer veteran is (wrongly) rated the higher starter.
    prior = lp.from_pool(players)
    assert prior.for_player("ochoa")["p_start"] > prior.for_player("rangel")["p_start"]

    # With real appearances, reality overrides: Rangel started, Ochoa benched.
    learned = lp.from_pool(players, appearances={"rangel": "started",
                                                 "ochoa": "benched"})
    assert learned.for_player("rangel")["p_start"] >= 0.9
    assert learned.for_player("ochoa")["p_start"] <= 0.2


def test_appearance_signal_latest_status_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "APPEARANCES_FILE", tmp_path / "app.csv")
    players = [_pl("a", "Starter", "Mexico", "FWD", 6.0),
               _pl("b", "Backup", "Mexico", "GK", 4.0),
               _pl("c", "Unplayed", "Brazil", "FWD", 8.0)]
    # From the live matchStatus: a started, b was a named sub; Brazil hasn't played.
    persistence.record_appearances("MD1", {"a": "started", "b": "benched"})
    sig = persistence.appearance_signal(players)
    assert sig["a"] == "started"
    assert sig["b"] == "benched"
    assert "c" not in sig                  # Brazil unknown until they play

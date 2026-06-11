"""Tests for the odds<->player name matcher (the data-integrity backbone)."""
from wcf.models import Player
from wcf import projections as P


def _pl(pid, name, nation, pos, full=""):
    return Player(id=pid, name=name, nation=nation, position=pos,
                  price=5.0, full_name=full)


def test_legal_name_vs_known_name():
    """Bookmakers use full legal names; FIFA uses known/mononyms."""
    roster = [
        _pl("1", "Raphinha", "Brazil", "MID", "Raphael Dias Belloli"),
        _pl("2", "Vinícius Júnior", "Brazil", "MID",
            "Vinícius José Paixão de Oliveira Júnior"),
    ]
    match = {"match_id": "m", "home": "Brazil", "away": "Morocco",
             "anytime": {"Raphael Dias Belloli": 2.75, "Vinicius Junior": 2.5}}
    out = P.match_market(match, roster)
    assert out == {"1": 2.75, "2": 2.5}


def test_reversed_order_and_two_same_first_name():
    """Reversed token order, and two squad-mates sharing a first name."""
    roster = [
        _pl("a", "Gabriel Magalhães", "Brazil", "DEF",
            "Gabriel dos Santos Magalhães"),
        _pl("b", "Danilo", "Brazil", "DEF", "Danilo Luiz da Silva"),
        _pl("c", "Danilo", "Brazil", "MID", "Danilo dos Santos de Oliveira"),
    ]
    match = {"match_id": "m", "home": "Brazil", "away": "X", "anytime": {
        "Magalhaes Gabriel": 8.0,
        "Danilo Luiz da Silva": 9.0,
        "Danilo dos Santos de Oliveira": 12.0,
    }}
    out = P.match_market(match, roster)
    assert out["a"] == 8.0          # reversed order matched
    assert out["b"] == 9.0          # the two Danilos are disambiguated
    assert out["c"] == 12.0


def test_transliteration_and_collapsed_tokens():
    """ß->ss and hyphen/concatenation differences."""
    gross = [_pl("g", "Pascal Groß", "Germany", "MID", "Pascal Groß")]
    m1 = {"match_id": "m1", "home": "Germany", "away": "X",
          "anytime": {"Pascal Gross": 6.0}}
    assert P.match_market(m1, gross) == {"g": 6.0}

    lee = [_pl("k", "Lee Kang-In", "Korea Republic", "MID", "Kang-In Lee")]
    m2 = {"match_id": "m2", "home": "Korea Republic", "away": "Y",
          "anytime": {"Kangin Lee": 7.0}}
    assert P.match_market(m2, lee) == {"k": 7.0}


def test_no_false_positive_for_absent_player():
    roster = [_pl("1", "Harry Kane", "England", "FWD", "Harry Kane")]
    match = {"match_id": "m", "home": "England", "away": "X",
             "anytime": {"Totally Different": 3.0}}
    assert P.match_market(match, roster) == {}


def test_alias_table_is_authoritative():
    """A verified alias links a name the fuzzy matcher could never resolve."""
    roster = [_pl("y", "Nickname", "X", "FWD", "Legal Long Name")]
    match = {"match_id": "m", "home": "X", "away": "Z",
             "anytime": {"Cryptic Bookmaker Handle": 4.0}}
    assert P.match_market(match, roster) == {}          # fuzzy can't
    aliases = {"y": {P._norm("Cryptic Bookmaker Handle")}}
    assert P.match_market(match, roster, aliases) == {"y": 4.0}   # alias can


def test_audit_flags_unlinked_popular_attacker():
    from wcf.models import Player
    star = Player(id="9", name="Star Striker", nation="Brazil", position="FWD",
                  price=9.0, ownership=20.0)
    match = {"match_id": "m", "home": "Brazil", "away": "X", "commence_time": "1",
             "anytime": {"Someone Else Entirely": 2.0}}
    a = P.audit_market([star], [match], aliases={})
    assert a["rate"] == 0.0
    assert any(n == "Star Striker" for n, *_ in a["popular_unlinked"])

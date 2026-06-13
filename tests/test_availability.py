"""Tests for the availability (injury) layer — round-aware exclusion."""
from wcf.providers import availability


def test_availability_round_aware(tmp_path, monkeypatch):
    f = tmp_path / "av.csv"
    f.write_text(
        "# comment line that must be skipped\n"
        "name,nation,status,available_from,note\n"
        "Alphonso Davies,Canada,out,R32,hamstring\n"
        "Xavi Simons,Netherlands,out,,out whole tournament\n",
        encoding="utf-8")
    monkeypatch.setattr(availability, "AVAILABILITY_FILE", f)

    # Davies is out for the group stage but back from R32.
    assert "alphonso davies" in availability.out_tokens_for_round("MD1")
    assert "alphonso davies" in availability.out_tokens_for_round("MD3")
    assert "alphonso davies" not in availability.out_tokens_for_round("R32")
    # Blank available_from = out for the whole tournament.
    assert "xavi simons" in availability.out_tokens_for_round("FIN")

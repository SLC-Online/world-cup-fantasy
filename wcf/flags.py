"""Nation -> flag emoji, for the dashboard."""
from __future__ import annotations

# FIFA squad name -> ISO 3166-1 alpha-2 (for regional-indicator flag emoji).
_ISO2 = {
    "Algeria": "DZ", "Argentina": "AR", "Australia": "AU", "Austria": "AT",
    "Belgium": "BE", "Bosnia and Herzegovina": "BA", "Brazil": "BR",
    "Cabo Verde": "CV", "Canada": "CA", "Colombia": "CO", "Congo DR": "CD",
    "Croatia": "HR", "Curaçao": "CW", "Czechia": "CZ", "Côte d'Ivoire": "CI",
    "Ecuador": "EC", "Egypt": "EG", "France": "FR", "Germany": "DE",
    "Ghana": "GH", "Haiti": "HT", "IR Iran": "IR", "Iraq": "IQ", "Japan": "JP",
    "Jordan": "JO", "Korea Republic": "KR", "Mexico": "MX", "Morocco": "MA",
    "Netherlands": "NL", "New Zealand": "NZ", "Norway": "NO", "Panama": "PA",
    "Paraguay": "PY", "Portugal": "PT", "Qatar": "QA", "Saudi Arabia": "SA",
    "Senegal": "SN", "South Africa": "ZA", "Spain": "ES", "Sweden": "SE",
    "Switzerland": "CH", "Tunisia": "TN", "Türkiye": "TR", "USA": "US",
    "Uruguay": "UY", "Uzbekistan": "UZ",
}
# Home-nation subdivision flags (no ISO2 country code).
_SPECIAL = {"England": "🏴\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F",
            "Scotland": "🏴\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F"}


def flag(nation: str) -> str:
    if nation in _SPECIAL:
        return _SPECIAL[nation]
    iso = _ISO2.get(nation)
    if not iso:
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)

"""World Cup 2026 Fantasy optimizer and tracker.

A small, sustainable toolkit to:
  * hold the (fixed) FIFA player pool,
  * pull betting odds each round,
  * turn odds into expected fantasy points per player,
  * pick the optimal 15 / XI / captain under the game's constraints, and
  * track how the team does across the 8 rounds.
"""

__version__ = "0.1.0"

# Silence the harmless urllib3 v2 / LibreSSL notice on macOS system Python.
# Filter by message (registered before urllib3 is imported) so it never prints.
import warnings as _warnings  # noqa: E402
_warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL")

#!/usr/bin/env bash
# Builds "World Cup Fantasy.app" into ~/Applications. Double-clicking it launches
# the dashboard (Streamlit) and opens it in your browser. Re-run to rebuild.
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$HOME/Applications/World Cup Fantasy.app"
ARCH="$(uname -m)"   # bake native arch so Finder can't launch it under Rosetta

mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>World Cup Fantasy</string>
    <key>CFBundleDisplayName</key><string>World Cup Fantasy</string>
    <key>CFBundleIdentifier</key><string>com.wcf.dashboard</string>
    <key>CFBundleExecutable</key><string>run</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict>
</plist>
PLIST

# The executable: launch the dashboard from the project venv and open the browser.
# `python -E -s -m streamlit` forces venv-only packages (ignores PYTHONPATH and
# user site-packages), so a stray numpy/pandas in ~/Library/Python can't shadow.
cat > "$APP/Contents/MacOS/run" <<RUN
#!/bin/bash
cd "$PROJ" || exit 1
unset PYTHONPATH
# Stop any stale dashboard instance (e.g. an earlier failed launch) so the port is free.
pkill -f "streamlit run dashboard.py" 2>/dev/null || true
# Suppress Streamlit's first-run email prompt (which would block a double-click).
mkdir -p "\$HOME/.streamlit"
[ -f "\$HOME/.streamlit/credentials.toml" ] || \\
    printf '[general]\nemail = ""\n' > "\$HOME/.streamlit/credentials.toml"
exec arch -$ARCH ./.venv/bin/python -E -s -m streamlit run dashboard.py \\
    --server.port=8766 --server.headless=false \\
    --browser.gatherUsageStats=false
RUN
chmod +x "$APP/Contents/MacOS/run"

echo "Built: $APP"
echo "Double-click it in ~/Applications (or Spotlight 'World Cup Fantasy')."
echo "If macOS blocks it the first time: right-click -> Open."

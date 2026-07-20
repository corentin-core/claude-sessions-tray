#!/usr/bin/env bash
# Install the Claude sessions tray + the GNOME window-focus extension.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
EXT_UUID="claude-focus@corentin-core.github.io"
TRAY_DEST="$HOME/.local/share/claude-sessions"
EXT_DEST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
CLAUDE_ICON="/usr/share/icons/hicolor/256x256/apps/claude-desktop.png"

echo "→ Tray applet"
mkdir -p "$TRAY_DEST/icons"
cp "$HERE/tray/tray.py" "$TRAY_DEST/tray.py"
chmod +x "$TRAY_DEST/tray.py"

# Icon: the Claude logo from a local desktop install if present, otherwise the
# bundled fallback (generic spark). new_with_path resolves "claude" to .png or .svg.
rm -f "$TRAY_DEST/icons/claude.png" "$TRAY_DEST/icons/claude.svg"
if [ -f "$CLAUDE_ICON" ]; then
    cp "$CLAUDE_ICON" "$TRAY_DEST/icons/claude.png"
else
    cp "$HERE/assets/claude-fallback.svg" "$TRAY_DEST/icons/claude.svg"
    echo "  (Claude desktop logo not found → fallback icon)"
fi

echo "→ GNOME extension ($EXT_UUID)"
mkdir -p "$EXT_DEST/schemas"
cp "$HERE/gnome-extension/$EXT_UUID/metadata.json" "$EXT_DEST/"
cp "$HERE/gnome-extension/$EXT_UUID/extension.js" "$EXT_DEST/"
cp "$HERE/gnome-extension/$EXT_UUID/schemas/"*.gschema.xml "$EXT_DEST/schemas/"
glib-compile-schemas "$EXT_DEST/schemas/"

echo "→ Autostart at login"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/claude-sessions-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Claude Sessions
Comment=Claude Code sessions waiting for an instruction
Exec=/usr/bin/python3 $TRAY_DEST/tray.py
Icon=$TRAY_DEST/icons/claude.png
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

cat <<EOF

✓ Installed.

Final steps (once):
  1. Enable the extension:
       gnome-extensions enable $EXT_UUID
  2. Log out and back in
     (Wayland only loads a new extension at session start).
  3. Start the tray now:
       setsid /usr/bin/python3 $TRAY_DEST/tray.py </dev/null >/dev/null 2>&1 &
     (on later logins, autostart handles it)

Conversation palette: Super+K from anywhere (after the relog).
To change the shortcut:
  gsettings --schemadir "$EXT_DEST/schemas" \\
    set org.gnome.shell.extensions.claude-focus toggle-palette "['<Super>k']"

Focus check (a VSCode window "myproject" open in the background):
  gdbus call --session --dest io.github.corentin_core.ClaudeFocus \\
    --object-path /io/github/corentin_core/ClaudeFocus \\
    --method io.github.corentin_core.ClaudeFocus.FocusWindow "myproject"
EOF

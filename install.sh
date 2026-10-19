#!/bin/bash
# WiFi Optimizer - Decky Plugin Installer
# Usage: curl -sL https://github.com/ArcadaLabs-Jason/WifiOptimizer/raw/main/install.sh -o /tmp/wifi-opt-install.sh && sudo bash /tmp/wifi-opt-install.sh

set -e

PLUGIN_NAME="WiFi Optimizer"

# Check for root (needed to write to plugin dir and restart service)
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run with sudo."
    echo "Run: sudo bash $0"
    exit 1
fi

# Resolve the real user's home directory (same method as Decky's own installer)
DECK_USER="${SUDO_USER:-$(logname 2>/dev/null || echo deck)}"
USER_HOME="$(getent passwd "$DECK_USER" | cut -d: -f6)"
if [ -z "$USER_HOME" ]; then
    USER_HOME="/home/$DECK_USER"
fi

PLUGIN_BASE="$USER_HOME/homebrew/plugins"
PLUGIN_DIR="$PLUGIN_BASE/$PLUGIN_NAME"

# Check Decky is installed
if [ ! -d "$PLUGIN_BASE" ]; then
    echo "Error: Decky Loader not found at $PLUGIN_BASE"
    echo "Install it first: https://decky.xyz"
    exit 1
fi

# Fetch latest release tag from GitHub
echo "Checking for latest release..."
LATEST_TAG=$(curl -sL "https://api.github.com/repos/ArcadaLabs-Jason/WifiOptimizer/releases/latest" | grep '"tag_name"' | head -1 | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')

if [ -z "$LATEST_TAG" ]; then
    echo "Warning: Couldn't fetch latest release, falling back to main branch"
    REPO_URL="https://github.com/ArcadaLabs-Jason/WifiOptimizer/archive/refs/heads/main.tar.gz"
    DIR_NAME="WifiOptimizer-main"
else
    echo "Latest release: $LATEST_TAG"
    REPO_URL="https://github.com/ArcadaLabs-Jason/WifiOptimizer/archive/refs/tags/${LATEST_TAG}.tar.gz"
    DIR_NAME="WifiOptimizer-${LATEST_TAG#v}"
fi

TMP_DIR=$(mktemp -d)
# Staging and the previous copy live beside the plugins folder, not in it:
# Decky loads every folder there that has a plugin.json. Same filesystem, so
# each move below is a rename rather than a copy.
STAGE_DIR="$USER_HOME/homebrew/.wifi-optimizer-stage"
OLD_DIR="$USER_HOME/homebrew/.wifi-optimizer-previous"

cleanup() {
    rm -rf "$TMP_DIR" "$STAGE_DIR"
}
trap cleanup EXIT

echo "Installing $PLUGIN_NAME..."

# Download and extract
echo "Downloading..."
curl -sSL --fail --connect-timeout 15 --max-time 300 "$REPO_URL" -o "$TMP_DIR/plugin.tar.gz"
tar xzf "$TMP_DIR/plugin.tar.gz" -C "$TMP_DIR"

SRC="$TMP_DIR/$DIR_NAME"
for f in plugin.json package.json main.py decky.pyi dist/index.js defaults/dispatcher.sh.tmpl; do
    if [ ! -f "$SRC/$f" ]; then
        echo "Error: Download failed or repo structure changed (missing $f)."
        exit 1
    fi
done

# Build the new copy completely before touching the installed one, so a
# failure part way (a full disk, a closed terminal) leaves it working.
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/dist" "$STAGE_DIR/defaults" "$STAGE_DIR/py_modules"
cp "$SRC/plugin.json" "$SRC/package.json" "$SRC/main.py" "$SRC/decky.pyi" "$STAGE_DIR/"
cp "$SRC/dist/index.js" "$STAGE_DIR/dist/"
cp "$SRC/dist/index.js.map" "$STAGE_DIR/dist/" 2>/dev/null || true
cp "$SRC/defaults/dispatcher.sh.tmpl" "$STAGE_DIR/defaults/"
touch "$STAGE_DIR/py_modules/.keep"

# Install
if [ -d "$PLUGIN_DIR" ]; then
    echo "Upgrading existing installation..."
    UPGRADING=true
    rm -rf "$OLD_DIR"
    mv "$PLUGIN_DIR" "$OLD_DIR"
else
    echo "Installing to $PLUGIN_DIR..."
    UPGRADING=false
fi
if ! mv "$STAGE_DIR" "$PLUGIN_DIR"; then
    echo "Error: Couldn't move the new version into place."
    if [ "$UPGRADING" = true ]; then
        mv "$OLD_DIR" "$PLUGIN_DIR" && echo "The previous version was put back."
    fi
    exit 1
fi
rm -rf "$OLD_DIR"

# Restart Decky
echo "Restarting Decky Loader..."
if ! systemctl restart plugin_loader; then
    echo ""
    echo "Installed, but Decky Loader did not restart. Restart the Deck to load it."
    exit 1
fi

echo ""
if [ "$UPGRADING" = true ]; then
    echo "WiFi Optimizer updated! Your settings have been preserved."
else
    echo "WiFi Optimizer installed! Open the Quick Access Menu to configure."
fi

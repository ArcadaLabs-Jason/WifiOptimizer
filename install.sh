#!/bin/bash
# WiFi Optimizer - Decky Plugin Installer
# Usage: curl -sL https://github.com/ArcadaLabs-Jason/WifiOptimizer/raw/main/install.sh -o /tmp/wifi-opt-install.sh && sudo bash /tmp/wifi-opt-install.sh

set -e

PLUGIN_NAME="WiFi Optimizer"
REPO_URL="https://github.com/ArcadaLabs-Jason/WifiOptimizer/archive/refs/heads/main.tar.gz"
TMP_DIR=$(mktemp -d)

# Check for root (needed to write to plugin dir and restart service)
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run with sudo."
    echo "Run: sudo bash $0"
    exit 1
fi

# Resolve the real user's home directory (same method as Decky's own installer)
DECK_USER="${SUDO_USER:-deck}"
USER_HOME="$(getent passwd "$DECK_USER" | cut -d: -f6)"
if [ -z "$USER_HOME" ]; then
    USER_HOME="/home/deck"
fi

PLUGIN_BASE="$USER_HOME/homebrew/plugins"
PLUGIN_DIR="$PLUGIN_BASE/$PLUGIN_NAME"

# Check Decky is installed
if [ ! -d "$PLUGIN_BASE" ]; then
    echo "Error: Decky Loader not found at $PLUGIN_BASE"
    echo "Install it first: https://decky.xyz"
    exit 1
fi

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "Installing $PLUGIN_NAME..."

# Download and extract
echo "Downloading..."
curl -sL "$REPO_URL" -o "$TMP_DIR/plugin.tar.gz"
tar xzf "$TMP_DIR/plugin.tar.gz" -C "$TMP_DIR"

SRC="$TMP_DIR/WifiOptimizer-main"
if [ ! -f "$SRC/plugin.json" ]; then
    echo "Error: Download failed or repo structure changed."
    exit 1
fi

# Install
if [ -d "$PLUGIN_DIR" ]; then
    echo "Upgrading existing installation..."
    UPGRADING=true
else
    echo "Installing to $PLUGIN_DIR..."
    UPGRADING=false
fi
rm -rf "$PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR/dist" "$PLUGIN_DIR/defaults" "$PLUGIN_DIR/py_modules"

cp "$SRC/plugin.json" "$PLUGIN_DIR/"
cp "$SRC/package.json" "$PLUGIN_DIR/"
cp "$SRC/main.py" "$PLUGIN_DIR/"
cp "$SRC/decky.pyi" "$PLUGIN_DIR/"
cp "$SRC/dist/index.js" "$PLUGIN_DIR/dist/"
cp "$SRC/dist/index.js.map" "$PLUGIN_DIR/dist/" 2>/dev/null || true
cp "$SRC/defaults/dispatcher.sh.tmpl" "$PLUGIN_DIR/defaults/"
touch "$PLUGIN_DIR/py_modules/.keep"

# Restart Decky
echo "Restarting Decky Loader..."
systemctl restart plugin_loader 2>/dev/null || true

echo ""
if [ "$UPGRADING" = true ]; then
    echo "WiFi Optimizer updated! Your settings have been preserved."
else
    echo "WiFi Optimizer installed! Open the Quick Access Menu to configure."
fi

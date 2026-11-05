#!/usr/bin/env bash
set -e

# multigravity installer
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing mgy to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$HOME/.config/multigravity-profiles"

cp "$SCRIPT_DIR/bin/mgy" "$INSTALL_DIR/mgy"
chmod +x "$INSTALL_DIR/mgy"
ln -sf mgy "$INSTALL_DIR/multigravity"

if [ -f "$SCRIPT_DIR/bin/mgy-quota" ]; then
    cp "$SCRIPT_DIR/bin/mgy-quota" "$INSTALL_DIR/mgy-quota"
    chmod +x "$INSTALL_DIR/mgy-quota"
fi

echo "==> Successfully installed mgy, multigravity, and mgy-quota to $INSTALL_DIR"

# Check if INSTALL_DIR is in PATH
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    echo ""
    echo "Notice: $INSTALL_DIR is not currently in your PATH."
    echo "Add the following line to your ~/.zshrc or ~/.bashrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "Get started by running:"
echo "  mgy list"
echo "  mgy new <profile_name>"

#!/usr/bin/env bash
set -e

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"

for bin in mgy multigravity mgy-quota; do
    if [ -f "$INSTALL_DIR/$bin" ] || [ -L "$INSTALL_DIR/$bin" ]; then
        rm -f "$INSTALL_DIR/$bin"
        echo "Removed $INSTALL_DIR/$bin."
    fi
done

echo ""
echo "Note: Profile data in ~/.config/multigravity-profiles was preserved."
echo "If you also wish to delete all profiles and tokens, run:"
echo "  rm -rf ~/.config/multigravity-profiles"

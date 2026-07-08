#!/usr/bin/env bash
# Installs the latest Node LTS via nvm and repoints ~/.local/bin/yt-dlp-node
# at it. All download-*-agendas.py scripts pass --js-runtimes pointed at
# that symlink, so this is the only step needed when yt-dlp raises its
# minimum supported Node version again (check by running a script and
# watching for "No supported JavaScript runtime could be found").
#
# Usage: bash scripts/update-yt-dlp-node.sh

set -euo pipefail

export NVM_DIR="$HOME/.nvm"
# shellcheck source=/dev/null
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

nvm install --lts
NODE_BIN="$(nvm which lts/*)"

mkdir -p "$HOME/.local/bin"
ln -sf "$NODE_BIN" "$HOME/.local/bin/yt-dlp-node"

echo "yt-dlp-node -> $NODE_BIN"
"$HOME/.local/bin/yt-dlp-node" --version

#!/usr/bin/env bash
set -euo pipefail

# Prefer Homebrew if available
if command -v /home/linuxbrew/.linuxbrew/bin/brew >/dev/null 2>&1 || command -v brew >/dev/null 2>&1; then
  if command -v /home/linuxbrew/.linuxbrew/bin/brew >/dev/null 2>&1; then
    eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
  else
    eval "$(brew shellenv)"
  fi
  echo "[kind.sh] Installing kind via brew…"
  brew install kind || true
  echo "[kind.sh] Done."
  exit 0
fi

# Fallback: install prebuilt Kind binary (no Go needed)
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$ARCH" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
esac
URL="https://kind.sigs.k8s.io/dl/v0.23.0/kind-${OS}-${ARCH}"

echo "[kind.sh] Installing kind from ${URL}"
TMP=/tmp/kind
curl -fsSL "${URL}" -o "${TMP}"
chmod +x "${TMP}"

# Prefer /usr/local/bin if sudo works non-interactively; else ~/.local/bin
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  sudo mv "${TMP}" /usr/local/bin/kind
else
  mkdir -p "$HOME/.local/bin"
  mv "${TMP}" "$HOME/.local/bin/kind"
  case ":$PATH:" in *":$HOME/.local/bin:"*) : ;; *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc";; esac
fi

echo "[kind.sh] kind installed: $(kind --version || true)"

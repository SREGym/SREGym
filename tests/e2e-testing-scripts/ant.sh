#!/usr/bin/env bash
set -euo pipefail

# Prefer Homebrew if available
if command -v /home/linuxbrew/.linuxbrew/bin/brew >/dev/null 2>&1 || command -v brew >/dev/null 2>&1; then
  if command -v /home/linuxbrew/.linuxbrew/bin/brew >/dev/null 2>&1; then
    eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
  else
    eval "$(brew shellenv)"
  fi
  echo "[ant.sh] Installing ant via brew…"
  brew install ant || true
  echo "[ant.sh] Done."
  exit 0
fi

# Fallback: apt-get on Debian/Ubuntu
if command -v apt-get >/dev/null 2>&1; then
  echo "[ant.sh] Installing ant via apt-get…"
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo apt-get install -y ant
  else
    apt-get install -y ant
  fi
  echo "[ant.sh] ant installed: $(ant -version || true)"
  exit 0
fi

# Fallback: download Apache Ant binary tarball
ANT_VERSION="1.10.15"
URL="https://archive.apache.org/dist/ant/binaries/apache-ant-${ANT_VERSION}-bin.tar.gz"

echo "[ant.sh] Installing Apache Ant ${ANT_VERSION} from ${URL}"
TMP_DIR=$(mktemp -d)
curl -fsSL "${URL}" -o "${TMP_DIR}/ant.tar.gz"
tar -xzf "${TMP_DIR}/ant.tar.gz" -C "${TMP_DIR}"

INSTALL_DIR="/usr/local/apache-ant-${ANT_VERSION}"
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  sudo mv "${TMP_DIR}/apache-ant-${ANT_VERSION}" "${INSTALL_DIR}"
  sudo ln -sf "${INSTALL_DIR}/bin/ant" /usr/local/bin/ant
else
  mkdir -p "$HOME/.local"
  mv "${TMP_DIR}/apache-ant-${ANT_VERSION}" "$HOME/.local/apache-ant-${ANT_VERSION}"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$HOME/.local/apache-ant-${ANT_VERSION}/bin/ant" "$HOME/.local/bin/ant"
  case ":$PATH:" in *":$HOME/.local/bin:"*) : ;; *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc";; esac
fi

rm -rf "${TMP_DIR}"
echo "[ant.sh] ant installed: $(ant -version || true)"

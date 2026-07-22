#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/root/geng-agent-task-driven}"
CONDA_ROOT="${CONDA_ROOT:-/root/miniconda3}"
ENV_PREFIX="${ENV_PREFIX:-/root/.venvs/geng-agent}"
CASES_ROOT="${GENG_CASES_ROOT:-/root/geng-agent-cases}"
NODE_VERSION="${NODE_VERSION:-22.22.1}"
PYTORCH_VERSION="${PYTORCH_VERSION:-2.11.0}"
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"

if [[ ! -f "$PROJECT_ROOT/pyproject.toml" ]]; then
  echo "project mirror is missing: $PROJECT_ROOT" >&2
  exit 2
fi

mkdir -p /root/.local/bin /root/.local/opt /root/.config/geng-agent /root/.venvs "$CASES_ROOT"
export PATH="/root/.local/bin:$PATH"

if [[ ! -x "$CONDA_ROOT/bin/conda" ]]; then
  installer='/tmp/Miniconda3-latest-Linux-x86_64.sh'
  checksum="${installer}.sha256"
  curl -fsSLo "$installer" https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
  curl -fsSLo "$checksum" https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh.sha256
  expected="$(awk '{print $1}' "$checksum")"
  actual="$(sha256sum "$installer" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || { echo 'Miniconda checksum mismatch' >&2; exit 3; }
  bash "$installer" -b -p "$CONDA_ROOT"
  rm -f "$installer" "$checksum"
fi

if [[ ! -x "$ENV_PREFIX/bin/python" ]]; then
  "$CONDA_ROOT/bin/conda" create -y -p "$ENV_PREFIX" python=3.11 pip
fi

PYTHON="$ENV_PREFIX/bin/python"
"$PYTHON" -m pip install --upgrade pip setuptools wheel
"$PYTHON" -m pip install --upgrade "torch==$PYTORCH_VERSION" --index-url "$PYTORCH_INDEX_URL"
"$PYTHON" -m pip install --upgrade -e "$PROJECT_ROOT[repro,web]" httpx2 pytest pytest-subtests

node_root="/root/.local/opt/node-v$NODE_VERSION-linux-x64"
if [[ ! -x "$node_root/bin/node" ]]; then
  node_archive="node-v$NODE_VERSION-linux-x64.tar.xz"
  curl -fsSLo "/tmp/$node_archive" "https://nodejs.org/dist/v$NODE_VERSION/$node_archive"
  curl -fsSLo /tmp/node-SHASUMS256.txt "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt"
  expected="$(awk -v file="$node_archive" '$2 == file {print $1}' /tmp/node-SHASUMS256.txt)"
  actual="$(sha256sum "/tmp/$node_archive" | awk '{print $1}')"
  [[ -n "$expected" && "$actual" == "$expected" ]] || { echo 'Node.js checksum mismatch' >&2; exit 4; }
  rm -rf -- "$node_root"
  tar -xJf "/tmp/$node_archive" -C /root/.local/opt
  rm -f "/tmp/$node_archive" /tmp/node-SHASUMS256.txt
fi

for binary in node npm npx corepack; do
  if [[ -x "$node_root/bin/$binary" ]]; then
    ln -sfn "$node_root/bin/$binary" "/root/.local/bin/$binary"
  fi
done

frontend="$PROJECT_ROOT/geng_agent/web/frontend"
if [[ -f "$frontend/package-lock.json" ]]; then
  (cd "$frontend" && /root/.local/bin/npm ci && /root/.local/bin/npm run build)
fi

if ! command -v codex >/dev/null 2>&1; then
  codex_installer='/tmp/codex-install.sh'
  if timeout 90 curl -fsSLo "$codex_installer" https://chatgpt.com/codex/install.sh \
    && [[ -s "$codex_installer" ]] \
    && CODEX_NON_INTERACTIVE=1 sh "$codex_installer"; then
    rm -f "$codex_installer"
  else
    rm -f "$codex_installer"
    echo 'standalone Codex installer unavailable; falling back to the official npm package' >&2
    npm install --global --prefix /root/.local @openai/codex@latest
  fi
fi

cat > /root/.config/geng-agent/env.sh <<EOF
export PATH=/root/.local/bin:$ENV_PREFIX/bin:\$PATH
export GENG_PYTHON=$ENV_PREFIX/bin/python
export GENG_CASES_ROOT=$CASES_ROOT
export GENG_CODEX_CMD=/root/.local/bin/codex
if [[ -f /etc/network_turbo ]]; then
  source /etc/network_turbo >/dev/null 2>&1
fi
EOF

profile_line='source /root/.config/geng-agent/env.sh'
grep -qxF "$profile_line" /root/.bashrc 2>/dev/null || printf '\n%s\n' "$profile_line" >> /root/.bashrc

source /root/.config/geng-agent/env.sh

printf '%s\n' '== installed runtime =='
python --version
python -m pip --version
node --version
npm --version
codex --version
python - <<'PY'
import torch
print('torch', torch.__version__)
print('cuda_available', torch.cuda.is_available())
print('cuda_version', torch.version.cuda)
print('device', torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)
PY

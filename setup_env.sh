#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# 8-cut environment setup — supports conda (miniforge) or python venv
#
# Usage:
#   ./setup_env.sh              # auto-detect (prefers conda if available)
#   ./setup_env.sh --conda      # force conda
#   ./setup_env.sh --venv       # force python venv
# ─��────────────────────────────��───────────────────────────────────────

ENV_NAME="8cut"
PYTHON_VERSION="3.12"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Auto-detect GPU for PyTorch index URL
if command -v nvidia-smi &>/dev/null; then
    TORCH_INDEX="https://download.pytorch.org/whl/cu128"
    echo "NVIDIA GPU detected — will install PyTorch with CUDA 12.8"
else
    TORCH_INDEX="https://download.pytorch.org/whl/cpu"
    echo "No NVIDIA GPU detected — will install CPU-only PyTorch"
fi

# ── Parse args ────────────────────────────────────────────────────────

MODE=""
for arg in "$@"; do
    case "$arg" in
        --conda) MODE="conda" ;;
        --venv)  MODE="venv"  ;;
        *)       echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

if [ -z "$MODE" ]; then
    if command -v conda &>/dev/null; then
        MODE="conda"
    else
        MODE="venv"
    fi
    echo "Auto-detected mode: $MODE"
fi

# ── Conda setup ─────────────��─────────────────────────────────────────

setup_conda() {
    echo "==> Setting up conda environment: $ENV_NAME"

    # Source conda shell hooks if not already active
    if ! command -v conda &>/dev/null; then
        echo "conda not found in PATH"
        exit 1
    fi
    eval "$(conda shell.bash hook)"

    if conda env list | grep -qw "$ENV_NAME"; then
        echo "  Environment '$ENV_NAME' already exists, updating..."
        conda activate "$ENV_NAME"
    else
        echo "  Creating environment '$ENV_NAME' with Python $PYTHON_VERSION..."
        conda create -y -n "$ENV_NAME" python="$PYTHON_VERSION"
        conda activate "$ENV_NAME"
    fi

    echo "  Installing PyTorch + torchaudio (CUDA 12.8)..."
    pip install torch torchaudio torchvision --index-url "$TORCH_INDEX"

    echo "  Installing project dependencies..."
    pip install -r "$SCRIPT_DIR/requirements.txt" --extra-index-url "$TORCH_INDEX"

    echo ""
    echo "Done! Activate with:"
    echo "  conda activate $ENV_NAME"
}

# ── Venv setup ───────��────────────────────────────────────────────────

setup_venv() {
    echo "==> Setting up Python venv at: $VENV_DIR"

    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        echo "  Created venv"
    else
        echo "  Venv already exists, updating..."
    fi

    source "$VENV_DIR/bin/activate"

    echo "  Installing PyTorch + torchaudio (CUDA 12.8)..."
    pip install torch torchaudio torchvision --index-url "$TORCH_INDEX"

    echo "  Installing project dependencies..."
    pip install -r "$SCRIPT_DIR/requirements.txt" --extra-index-url "$TORCH_INDEX"

    echo ""
    echo "Done! Activate with:"
    echo "  source $VENV_DIR/bin/activate"
}

# ── Run ───────────────────────────────────────────────────────────────

case "$MODE" in
    conda) setup_conda ;;
    venv)  setup_venv  ;;
esac

echo ""
echo "Verify with:"
echo "  python -c \"import torch; print('PyTorch', torch.__version__, 'CUDA', torch.version.cuda)\""
echo "  python -c \"import librosa, torchaudio, sklearn; print('All imports OK')\""

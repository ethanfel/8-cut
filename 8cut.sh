#!/bin/bash
# Launch 8-cut with auto-detected venv/conda environment
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_NAME="8cut"
CONDA_PREFIX_BASE="/media/p5/miniforge3"

# 1. Try .venv in project dir
if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
    exec python "$SCRIPT_DIR/main.py" "$@"
fi

# 2. Try conda env (works without shell init)
CONDA_PYTHON="$CONDA_PREFIX_BASE/envs/$ENV_NAME/bin/python"
if [ -x "$CONDA_PYTHON" ]; then
    exec "$CONDA_PYTHON" "$SCRIPT_DIR/main.py" "$@"
fi

# 3. Try conda via shell hook (interactive shells)
if command -v conda &>/dev/null; then
    eval "$(conda shell.bash hook 2>/dev/null)"
    if conda env list 2>/dev/null | grep -qw "$ENV_NAME"; then
        conda activate "$ENV_NAME"
        exec python "$SCRIPT_DIR/main.py" "$@"
    fi
fi

# 4. Fallback to system Python
exec python3 "$SCRIPT_DIR/main.py" "$@"

#!/bin/bash
# Spyoncino Security System - Professional Launcher
# Checks dependencies, creates environment, and runs the system

echo "========================================"
echo "Spyoncino Security System Launcher"
echo "========================================"
echo

# Configuration
ENV_PATH=".venv"
MIN_PYTHON_VERSION="3.12"

# ========================================
# Step 1: Check Python version
# ========================================
echo "[1/5] Checking Python version..."
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed or not in PATH!"
    echo "Please install Python $MIN_PYTHON_VERSION or higher"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo "Found Python $PYTHON_VERSION"

# Extract major and minor version
MAJOR=$(echo $PYTHON_VERSION | cut -d'.' -f1)
MINOR=$(echo $PYTHON_VERSION | cut -d'.' -f2)

if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 12 ]); then
    echo "ERROR: Python $MIN_PYTHON_VERSION or higher required, found $PYTHON_VERSION"
    exit 1
fi
echo "  Python version OK"
echo

# ========================================
# Step 2: Check/Install UV
# ========================================
echo "[2/5] Checking uv package manager..."
if ! command -v uv &> /dev/null; then
    echo "UV not found. Installing UV..."
    pip3 install uv
    if [ $? -ne 0 ]; then
        echo "WARNING: Failed to install UV. Will use pip instead."
        USE_PIP=1
    else
        echo "  UV installed successfully"
    fi
else
    echo "  UV found"
fi
echo

# ========================================
# Step 3: Create virtual environment
# ========================================
echo "[3/5] Setting up virtual environment..."
if [ ! -d "$ENV_PATH" ]; then
    echo "Creating new virtual environment: $ENV_PATH"
    if [ -n "$USE_PIP" ]; then
        python3 -m venv $ENV_PATH
    else
        uv venv $ENV_PATH
    fi
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to create virtual environment"
        exit 1
    fi
    echo "  Virtual environment created"
else
    echo "  Virtual environment already exists"
fi
echo

# ========================================
# Step 4: Activate and install
# ========================================
echo "[4/5] Installing Spyoncino package..."
source $ENV_PATH/bin/activate
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to activate virtual environment"
    exit 1
fi

# Detect GPU and choose appropriate PyTorch installation
INDEX_URL=""

if [ -z "$SPYONCINO_PYTORCH" ]; then
    echo "Detecting hardware capabilities..."
    if command -v nvidia-smi &> /dev/null; then
        echo "  NVIDIA GPU detected - installing CUDA-enabled PyTorch"
        INDEX_URL="--index-url https://download.pytorch.org/whl/cu118"
    else
        echo "  No NVIDIA GPU detected - installing CPU-only PyTorch"
        INDEX_URL="--index-url https://download.pytorch.org/whl/cpu"
    fi
else
    echo "Using manual PyTorch selection: $SPYONCINO_PYTORCH"
    if [ "$SPYONCINO_PYTORCH" = "cuda" ]; then
        INDEX_URL="--index-url https://download.pytorch.org/whl/cu118"
    elif [ "$SPYONCINO_PYTORCH" = "cpu" ]; then
        INDEX_URL="--index-url https://download.pytorch.org/whl/cpu"
    fi
fi

if [ -n "$USE_PIP" ]; then
    pip install -e . $INDEX_URL
else
    uv pip install -e . $INDEX_URL
fi

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install package"
    echo "Try running manually: uv pip install -e ."
    exit 1
fi
echo "  Package installed successfully"
echo

# ========================================
# Step 5: Run the system
# ========================================
echo "[5/5] Starting Spyoncino Security System..."
echo
echo "TIP: Check config/setting.json and config/secrets.json before first run"
echo "Press Ctrl+C to stop the system"
echo "========================================"
echo

spyoncino

echo
echo "========================================"
if [ $? -ne 0 ]; then
    echo "System stopped with errors. Check recordings/security_system.log"
else
    echo "System stopped successfully"
fi
echo "========================================"


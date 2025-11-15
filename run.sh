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
PYTHON_CMD=""
NEED_UV_PYTHON=""
NEED_ONLINE_PYTHON=""

if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
    echo "Found Python $PYTHON_VERSION"

    # Extract major and minor version
    MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
    MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)

    if [ "$MAJOR" -gt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 12 ]); then
        PYTHON_CMD=$(command -v python3)
        echo "  Python version OK"
    else
        echo "  Python $PYTHON_VERSION is below required $MIN_PYTHON_VERSION"
        NEED_UV_PYTHON=1
    fi
else
    echo "Python 3 is not installed or not in PATH."
    NEED_UV_PYTHON=1
    NEED_ONLINE_PYTHON=1
fi

if [ -n "$NEED_UV_PYTHON" ]; then
    echo "  Will attempt to provision Python $MIN_PYTHON_VERSION with uv."
fi
echo

# ========================================
# Step 2: Check/Install UV
# ========================================
echo "[2/5] Checking uv package manager..."
HAVE_UV=""
if ! command -v uv &> /dev/null; then
    if [ -z "$PYTHON_CMD" ]; then
        echo "UV not found. Downloading UV installer..."
        INSTALL_SUCCESS=0
        if command -v curl &> /dev/null; then
            curl -LsSf https://astral.sh/install.sh | sh
            INSTALL_SUCCESS=$?
        elif command -v wget &> /dev/null; then
            wget -qO- https://astral.sh/install.sh | sh
            INSTALL_SUCCESS=$?
        else
            echo "ERROR: Neither curl nor wget is available to download UV."
            exit 1
        fi
        if [ $INSTALL_SUCCESS -ne 0 ]; then
            echo "ERROR: Failed to install UV from internet."
            exit 1
        fi
        # Ensure default install location is on PATH
        if [ -d "$HOME/.local/bin" ] && ! command -v uv &> /dev/null; then
            export PATH="$HOME/.local/bin:$PATH"
        fi
        if command -v uv &> /dev/null; then
            echo "  UV installed successfully"
            HAVE_UV=1
        else
            echo "ERROR: UV installation completed but executable not found."
            exit 1
        fi
    else
        echo "UV not found. Installing UV..."
        pip3 install uv
        if [ $? -ne 0 ]; then
            echo "WARNING: Failed to install UV. Will use pip instead."
            USE_PIP=1
        else
            echo "  UV installed successfully"
            HAVE_UV=1
        fi
    fi
else
    echo "  UV found"
    HAVE_UV=1
fi
echo

# ========================================
# Step 3: Create virtual environment
# ========================================
echo "[3/5] Setting up virtual environment..."

if [ -n "$NEED_UV_PYTHON" ]; then
    if [ -n "$HAVE_UV" ]; then
        if [ -n "$NEED_ONLINE_PYTHON" ]; then
            echo "  No system Python detected. Downloading Python $MIN_PYTHON_VERSION with uv..."
        else
            echo "  Provisioning Python $MIN_PYTHON_VERSION via uv..."
        fi
        uv python install $MIN_PYTHON_VERSION
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to install Python $MIN_PYTHON_VERSION with uv"
            exit 1
        fi
        PYTHON_CMD=$(uv python find $MIN_PYTHON_VERSION)
        if [ -z "$PYTHON_CMD" ]; then
            echo "ERROR: uv could not locate Python $MIN_PYTHON_VERSION after installation"
            exit 1
        fi
        echo "  Using Python interpreter: $PYTHON_CMD"
    else
        echo "ERROR: Python $MIN_PYTHON_VERSION or higher required but uv is unavailable to provision it."
        exit 1
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    PYTHON_CMD=$(command -v python3 2>/dev/null || true)
fi

if [ ! -d "$ENV_PATH" ]; then
    echo "Creating new virtual environment: $ENV_PATH"
    if [ -n "$USE_PIP" ]; then
        "$PYTHON_CMD" -m venv $ENV_PATH
    else
        uv venv --python "$PYTHON_CMD" $ENV_PATH
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

# Install PyTorch first with correct version, then install spyoncino
if [ -n "$USE_PIP" ]; then
    if [ -z "$INDEX_URL" ]; then
        pip install torch torchvision
    else
        pip install torch torchvision $INDEX_URL
    fi
    pip install -e .
else
    if [ -z "$INDEX_URL" ]; then
        uv pip install torch torchvision
    else
        uv pip install torch torchvision $INDEX_URL
    fi
    uv pip install -e .
fi

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install package"
    echo "Try running manually: uv pip install -e ."
    exit 1
fi

# Verify PyTorch installation
echo "Verifying PyTorch installation..."
python -c "import torch; cuda=torch.cuda.is_available(); print(f'  PyTorch {torch.__version__}'); print(f'  CUDA available: {cuda}')" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "  WARNING: Could not verify PyTorch installation"
else
    python -c "import torch; exit(0 if torch.cuda.is_available() or '$INDEX_URL' != '--index-url https://download.pytorch.org/whl/cu118' else 1)" 2>/dev/null
    if [ $? -ne 0 ]; then
        echo "  WARNING: GPU detected but PyTorch has no CUDA support!"
        echo "  This usually means UV cached the CPU version."
        echo "  Fixing: Reinstalling PyTorch with CUDA..."
        if [ -n "$USE_PIP" ]; then
            pip uninstall torch torchvision -y
            pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118 --force-reinstall --no-cache-dir
        else
            yes | uv pip uninstall torch torchvision
            uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118 --reinstall --refresh
        fi
    fi
fi

echo "  Package installed successfully"
echo

# ========================================
# Step 5: Run the system
# ========================================
echo "[5/5] Starting Spyoncino Security System..."
echo
echo "TIP: Edit config/config.yaml and config/secrets.yaml"
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

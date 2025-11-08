@echo off
REM Spyoncino Security System - Professional Launcher
REM Checks dependencies, creates environment, and runs the system

setlocal enabledelayedexpansion

echo ========================================
echo Spyoncino Security System Launcher
echo ========================================
echo.

REM Configuration
set "ENV_PATH=spyoncino_env"
set "MIN_PYTHON_VERSION=3.12"

REM ========================================
REM Step 1: Check Python version
REM ========================================
echo [1/5] Checking Python version...
set "PYTHON_CMD="
set "PYTHON_NEEDS_BOOTSTRAP="
set "PYTHON_VERSION="
set "MAJOR="
set "MINOR="
set "PYTHON_FROM_WEB="

python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not in PATH.
    set "PYTHON_NEEDS_BOOTSTRAP=1"
    set "PYTHON_FROM_WEB=1"
) else (
    for /f "tokens=2" %%i in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%i"
    echo Found Python !PYTHON_VERSION!
    for /f "tokens=1,2 delims=." %%a in ("!PYTHON_VERSION!") do (
        set "MAJOR=%%a"
        set "MINOR=%%b"
    )
)

if defined MAJOR (
    if !MAJOR! LSS 3 (
        echo   Python version below required %MIN_PYTHON_VERSION%.
        set "PYTHON_NEEDS_BOOTSTRAP=1"
    ) else (
        if !MAJOR! EQU 3 (
            if !MINOR! LSS 12 (
                echo   Python version below required %MIN_PYTHON_VERSION%.
                set "PYTHON_NEEDS_BOOTSTRAP=1"
            ) else (
                echo   Python version OK
            )
        ) else (
            echo   Python version OK
        )
    )
) else (
    if not defined PYTHON_NEEDS_BOOTSTRAP (
        set "PYTHON_NEEDS_BOOTSTRAP=1"
    )
)

if not defined PYTHON_NEEDS_BOOTSTRAP (
    for %%I in (python.exe) do set "PYTHON_CMD=%%~$PATH:I"
) else (
    echo   Will attempt to provision Python %MIN_PYTHON_VERSION% with uv.
)
echo.

REM ========================================
REM Step 2: Check/Install UV
REM ========================================
echo [2/5] Checking uv package manager...
set "HAVE_UV="
uv --version >nul 2>&1
if errorlevel 1 (
    if defined PYTHON_CMD (
        echo UV not found. Installing UV...
        pip install uv
        if errorlevel 1 (
            echo WARNING: Failed to install UV. Will use pip instead.
            set "USE_PIP=1"
        ) else (
            echo   UV installed successfully
            set "HAVE_UV=1"
        )
    ) else (
        echo UV not found. Downloading UV installer...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "try { iwr https://astral.sh/install.ps1 -UseBasicParsing | powershell -NoProfile - -y; exit 0 } catch { exit 1 }"
        if errorlevel 1 (
            echo ERROR: Failed to install UV from internet.
            pause
            exit /b 1
        )
        set "UV_DEFAULT=%LOCALAPPDATA%\uv\bin"
        if exist "%UV_DEFAULT%\uv.exe" (
            set "PATH=%UV_DEFAULT%;%PATH%"
        )
        uv --version >nul 2>&1
        if errorlevel 1 (
            echo ERROR: UV installation completed but executable not found.
            pause
            exit /b 1
        )
        echo   UV installed successfully
        set "HAVE_UV=1"
    )
) else (
    echo   UV found
    set "HAVE_UV=1"
)
echo.

REM ========================================
REM Step 3: Create virtual environment
REM ========================================
echo [3/5] Setting up virtual environment...

if defined PYTHON_NEEDS_BOOTSTRAP (
    if not defined HAVE_UV (
        echo ERROR: Python %MIN_PYTHON_VERSION% or higher required but uv is unavailable to provision it.
        pause
        exit /b 1
    )
    if defined PYTHON_FROM_WEB (
        echo   No system Python detected. Downloading Python %MIN_PYTHON_VERSION% with uv...
    ) else (
        echo   Provisioning Python %MIN_PYTHON_VERSION% via uv...
    )
    uv python install %MIN_PYTHON_VERSION%
    if errorlevel 1 (
        echo ERROR: Failed to install Python %MIN_PYTHON_VERSION% with uv.
        pause
        exit /b 1
    )
    for /f "delims=" %%p in ('uv python find %MIN_PYTHON_VERSION%') do set "PYTHON_CMD=%%p"
    if not defined PYTHON_CMD (
        echo ERROR: uv could not locate Python %MIN_PYTHON_VERSION% after installation.
        pause
        exit /b 1
    )
    echo   Using Python from !PYTHON_CMD!
)

if not defined PYTHON_CMD (
    for %%I in (python.exe) do if not defined PYTHON_CMD set "PYTHON_CMD=%%~$PATH:I"
)

if not exist "%ENV_PATH%\Scripts\activate.bat" (
    echo Creating new virtual environment: %ENV_PATH%
    if defined USE_PIP (
        "%PYTHON_CMD%" -m venv %ENV_PATH%
    ) else (
        uv venv --python "%PYTHON_CMD%" %ENV_PATH%
    )
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
    echo   Virtual environment created
) else (
    echo   Virtual environment already exists
)
echo.

REM ========================================
REM Step 4: Activate and install
REM ========================================
echo [4/5] Installing Spyoncino package...
call %ENV_PATH%\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    pause
    exit /b 1
)

REM Detect GPU and choose appropriate PyTorch installation
set "INDEX_URL="

if not defined SPYONCINO_PYTORCH (
    echo Detecting hardware capabilities...
    nvidia-smi >nul 2>&1
    if errorlevel 1 (
        echo   No NVIDIA GPU detected - installing CPU-only PyTorch
        set "INDEX_URL=--index-url https://download.pytorch.org/whl/cpu"
    ) else (
        echo   NVIDIA GPU detected - installing CUDA-enabled PyTorch
        set "INDEX_URL=--index-url https://download.pytorch.org/whl/cu118"
    )
) else (
    echo Using manual PyTorch selection: %SPYONCINO_PYTORCH%
    if "%SPYONCINO_PYTORCH%"=="cuda" (
        set "INDEX_URL=--index-url https://download.pytorch.org/whl/cu118"
    ) else if "%SPYONCINO_PYTORCH%"=="cpu" (
        set "INDEX_URL=--index-url https://download.pytorch.org/whl/cpu"
    )
)

REM Install PyTorch first with correct version, then install spyoncino
if defined USE_PIP (
    if "%INDEX_URL%"=="" (
        pip install torch torchvision
    ) else (
        pip install torch torchvision %INDEX_URL%
    )
    pip install -e .
) else (
    if "%INDEX_URL%"=="" (
        uv pip install torch torchvision
    ) else (
        uv pip install torch torchvision %INDEX_URL%
    )
    uv pip install -e .
)

if errorlevel 1 (
    echo ERROR: Failed to install package
    echo Try running manually: uv pip install -e .
    pause
    exit /b 1
)

REM Verify PyTorch installation
echo Verifying PyTorch installation...
python -c "import torch; cuda=torch.cuda.is_available(); print(f'  PyTorch {torch.__version__}'); print(f'  CUDA available: {cuda}')" 2>nul
if errorlevel 1 (
    echo   WARNING: Could not verify PyTorch installation
) else (
    python -c "import torch; cuda=torch.cuda.is_available(); driver='nvidia-smi' if cuda else 'none'; exit(0 if cuda or not '%INDEX_URL%'=='--index-url https://download.pytorch.org/whl/cu118' else 1)" 2>nul
    if errorlevel 1 (
        echo   WARNING: GPU detected but PyTorch has no CUDA support!
        echo   This usually means UV cached the CPU version.
        echo   Fixing: Reinstalling PyTorch with CUDA...
        if defined USE_PIP (
            pip uninstall torch torchvision -y
            pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118 --force-reinstall --no-cache-dir
        ) else (
            echo y | uv pip uninstall torch torchvision
            uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118 --reinstall --refresh
        )
    )
)

echo   Package installed successfully
echo.

REM ========================================
REM Step 5: Run the system
REM ========================================
echo [5/5] Starting Spyoncino Security System...
echo.
echo TIP: Edit config/config.yaml, config/telegram.yaml, and config/secrets.yaml
echo Press Ctrl+C to stop the system
echo ========================================
echo.

spyoncino

echo.
echo ========================================
if errorlevel 1 (
    echo System stopped with errors. Check recordings\security_system.log
) else (
    echo System stopped successfully
)
echo ========================================
echo.
pause

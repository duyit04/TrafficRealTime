@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================
echo   Traffic Monitor - One Click Run
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Khong tim thay Python trong PATH.
    echo         Hay cai Python 3.11+ va tick "Add Python to PATH".
    pause
    exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Khong tim thay npm trong PATH.
    echo         Hay cai Node.js 18+.
    pause
    exit /b 1
)

if not exist "backend\.env" (
    if exist "backend\.env.example" (
        copy /Y "backend\.env.example" "backend\.env" >nul
        echo [INFO] Da tao backend\.env tu .env.example
    )
)

set "VENV_DIR=venv-gpu"
set "VENV_PY=%~dp0%VENV_DIR%\Scripts\python.exe"
set "VENV_ACT=%~dp0%VENV_DIR%\Scripts\activate.bat"
set "DEPS_MARK=%~dp0%VENV_DIR%\.deps_installed"

if not exist "%VENV_ACT%" (
    echo [INFO] Dang tao virtual environment GPU o root: %VENV_DIR%
    py -3.11 -m venv "%~dp0%VENV_DIR%"
    if errorlevel 1 (
        echo [WARN] Khong tao duoc bang py -3.11, fallback sang python mac dinh...
        python -m venv "%~dp0%VENV_DIR%"
    )
    if errorlevel 1 (
        echo [ERROR] Tao virtual environment that bai.
        pause
        exit /b 1
    )
)

if not exist "%DEPS_MARK%" (
    echo [INFO] Dang cai dependencies cho backend...
    call "%VENV_ACT%"
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -r "backend\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Cai dependencies backend that bai.
        pause
        exit /b 1
    )
    echo [INFO] Dang cai TensorRT ^(NVIDIA index^)...
    "%VENV_PY%" -m pip install --extra-index-url https://pypi.nvidia.com tensorrt
    if errorlevel 1 (
        echo [WARN] Cai TensorRT that bai. Van co the chay torch CUDA neu da cai dung.
    )
    echo [INFO] Dam bao torch CUDA ^(cu126^)...
    "%VENV_PY%" -m pip install --index-url https://download.pytorch.org/whl/cu126 torch torchvision
    if errorlevel 1 (
        echo [ERROR] Cai dependencies backend that bai.
        pause
        exit /b 1
    )
    type nul > "%DEPS_MARK%"
)

echo [INFO] Kiem tra nhanh CUDA + TensorRT trong venv...
"%VENV_PY%" -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
if errorlevel 1 (
    echo [ERROR] Torch trong venv loi.
    pause
    exit /b 1
)
"%VENV_PY%" -c "import tensorrt; print('tensorrt', tensorrt.__version__)"
if errorlevel 1 (
    echo [WARN] TensorRT import loi. Export engine co the that bai.
)

if not exist "frontend\node_modules" (
    echo [INFO] Dang cai dependencies cho frontend...
    pushd "frontend"
    npm install
    if errorlevel 1 (
        popd
        echo [ERROR] Cai dependencies frontend that bai.
        pause
        exit /b 1
    )
    popd
)

echo [INFO] Starting backend...
start "Backend" cmd /k "cd /d ""%~dp0backend"" && call ""%VENV_ACT%"" && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

echo [INFO] Cho backend san sang o :8000 (toi da 60s)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i -lt 60;$i++){ try { $c=Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop; if($c){$ok=$true; break} } catch {}; Start-Sleep -Seconds 1 }; if($ok){ exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo [WARN] Backend chua bind cong 8000 sau 60s. Van tiep tuc mo frontend...
)

echo [INFO] Starting frontend...
start "Frontend" cmd /k "cd /d ""%~dp0frontend"" && npm run dev -- --host 0.0.0.0 --port 5173"

timeout /t 3 /nobreak >nul
start "" "http://localhost:5173"

echo.
echo [OK] Da khoi dong backend + frontend.
echo      Backend docs: http://localhost:8000/docs
echo      Frontend    : http://localhost:5173
echo.
pause

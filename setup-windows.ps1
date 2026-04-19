# 8-cut Windows setup script
# Run once: powershell -ExecutionPolicy Bypass -File setup-windows.ps1
#
# Prerequisites: Python 3.11+ must be installed and on PATH
#   https://www.python.org/downloads/

$ErrorActionPreference = "Stop"
trap { Write-Host "`n$_" -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== 8-cut Windows Setup ===" -ForegroundColor Cyan

# ── Virtual environment ───────────────────────────────────
$venvDir = Join-Path $root ".venv"
if (Test-Path (Join-Path $venvDir "Scripts\python.exe")) {
    Write-Host "`nVirtual environment already exists, activating..." -ForegroundColor Green
} else {
    Write-Host "`nCreating virtual environment..."
    python -m venv $venvDir
    Write-Host "Virtual environment created at $venvDir" -ForegroundColor Green
}
& "$venvDir\Scripts\Activate.ps1"

# ── PyTorch ───────────────────────────────────────────────
$hasTorch = python -c "import torch" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "`nPyTorch already installed, skipping." -ForegroundColor Green
} else {
    # Detect NVIDIA GPU via nvidia-smi
    $hasNvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($hasNvidia) {
        Write-Host "`nNVIDIA GPU detected — installing PyTorch with CUDA 12.8..." -ForegroundColor Green
        pip install torch torchaudio torchvision --index-url https://download.pytorch.org/whl/cu128
    } else {
        Write-Host "`nNo NVIDIA GPU detected — installing CPU-only PyTorch..." -ForegroundColor Yellow
        Write-Host "(Audio scanning will work but will be slower without GPU)" -ForegroundColor Yellow
        pip install torch torchaudio torchvision --index-url https://download.pytorch.org/whl/cpu
    }
}

# ── Python deps ───────────────────────────────────────────
Write-Host "`nInstalling project dependencies..."
pip install -r (Join-Path $root "requirements.txt")

# ── libmpv ────────────────────────────────────────────────
$mpvDll = Join-Path $root "libmpv-2.dll"
if (Test-Path $mpvDll) {
    Write-Host "`nlibmpv-2.dll already present, skipping." -ForegroundColor Green
} else {
    Write-Host "`nDownloading libmpv..."
    $release = Invoke-RestMethod "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -like "mpv-dev-x86_64-v3-*" } | Select-Object -First 1
    $tmpFile = Join-Path $root "mpv-dev.7z"
    Invoke-WebRequest $asset.browser_download_url -OutFile $tmpFile
    7z x $tmpFile -o"$root\mpv-dev" -y | Out-Null
    Copy-Item "$root\mpv-dev\libmpv-2.dll" $root
    Remove-Item $tmpFile -Force
    Remove-Item "$root\mpv-dev" -Recurse -Force
    Write-Host "libmpv-2.dll downloaded." -ForegroundColor Green
}

# ── ffmpeg ────────────────────────────────────────────────
$ffmpeg = Join-Path $root "ffmpeg.exe"
if (Test-Path $ffmpeg) {
    Write-Host "`nffmpeg.exe already present, skipping." -ForegroundColor Green
} else {
    $onPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($onPath) {
        Write-Host "`nffmpeg found on PATH: $($onPath.Source)" -ForegroundColor Green
    } else {
        Write-Host "`nDownloading ffmpeg..."
        $ffUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        $tmpZip = Join-Path $root "ffmpeg.zip"
        Invoke-WebRequest $ffUrl -OutFile $tmpZip
        Expand-Archive $tmpZip -DestinationPath "$root\ffmpeg-tmp" -Force
        $bin = Get-ChildItem -Path "$root\ffmpeg-tmp" -Recurse -Filter ffmpeg.exe | Select-Object -First 1
        Copy-Item "$($bin.DirectoryName)\ffmpeg.exe" $root
        Copy-Item "$($bin.DirectoryName)\ffprobe.exe" $root
        Remove-Item $tmpZip -Force
        Remove-Item "$root\ffmpeg-tmp" -Recurse -Force
        Write-Host "ffmpeg.exe downloaded." -ForegroundColor Green
    }
}

# ── Verify ────────────────────────────────────────────────
Write-Host "`n--- Verification ---" -ForegroundColor Cyan
python -c "import torch; print('PyTorch', torch.__version__, 'CUDA', torch.version.cuda)"
python -c "import sklearn, librosa, torchaudio; print('All imports OK')"

Write-Host "`n=== Setup complete ===" -ForegroundColor Cyan
Write-Host "Run 8-cut with: .venv\Scripts\python.exe main.py"
Write-Host "Or double-click: 8cut.bat"
Read-Host "`nPress Enter to close"

# Runs body detection, face recognition and follow mode on this laptop.
# The laptop reads the rover camera stream from the Pi and drives through the Pi.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_laptop.ps1
#
# Then open http://127.0.0.1:8090/ on the laptop.
param(
    [string]$PiUrl = "http://192.168.4.10:8080",
    [int]$Port = 8090
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$env:VISION_ENABLED = "1"
$env:ESP32_URL = $PiUrl
$env:CAMERA_STREAM_URL = "$PiUrl/camera"
$env:ROVER_HOST = "127.0.0.1"
$env:ROVER_PORT = "$Port"
$env:PYTHONUNBUFFERED = "1"

Write-Host "Vision dashboard: http://127.0.0.1:$Port/  (camera and rover via $PiUrl)"
& $python (Join-Path $root "app.py")

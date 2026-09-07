Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "  PowerMesh ESP32-S3 Simulator & Captive Portal" -ForegroundColor Yellow
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "Starting local firmware emulator and digital twin on http://127.0.0.1:8000..." -ForegroundColor Green
python server.py

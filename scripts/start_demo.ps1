# Запуск демо на локальной машине.
#
# Что делает:
#   1. проверяет виртуальное окружение и зависимости;
#   2. создаёт локальную базу и наполняет её демо-данными (если ещё не наполнена);
#   3. поднимает сервер и открывает интерфейс в браузере.
#
# Запуск:  powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1
# Остановка: Ctrl+C в этом окне.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Виртуальное окружение не найдено. Создаю..." -ForegroundColor Yellow
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Write-Host "Нужен uv (https://docs.astral.sh/uv/) или готовый .venv." -ForegroundColor Red
        Write-Host "Установка:  pip install uv   затем:  uv venv .venv"
        exit 1
    }
    $env:UV_CACHE_DIR = Join-Path $root ".uv-cache"
    uv venv --python 3.13 .venv
    uv pip install -e ".[dev]"
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:HIREBOT_DATABASE__DSN = "sqlite+aiosqlite:///./hirebot.db"
$env:HIREBOT_ENV = "local"
# Ключ доступа не задаём: интерфейс открыт локально, в шапке будет предупреждение.
# Для проверки входа по ключу раскомментируйте строку ниже.
# $env:HIREBOT_SECURITY__WEBHOOK_SECRET = "local-demo-key"
$db = Join-Path $root "hirebot.db"
if (-not (Test-Path $db)) {
    Write-Host "Наполняю демо-данными..." -ForegroundColor Cyan
    & $python -m hirebot.cli seed
}

Write-Host ""
Write-Host "Интерфейс:  http://127.0.0.1:8000/ui" -ForegroundColor Green
Write-Host "API-документация: http://127.0.0.1:8000/docs" -ForegroundColor Green
Write-Host "Метрики: http://127.0.0.1:8000/metrics (нужен заголовок X-API-Key)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Остановка: Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

Start-Process "http://127.0.0.1:8000/ui"
& $python -m hirebot.cli serve --host 127.0.0.1 --port 8000

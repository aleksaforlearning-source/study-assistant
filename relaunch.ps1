# Starts everything for local work: Docker Desktop, the database, the server,
# and opens the app in the browser once the server answers.
Set-Location $PSScriptRoot

function Wait-Until([scriptblock]$ready, [string]$what, [int]$seconds = 180) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while (-not (& $ready)) {
        if ((Get-Date) -gt $deadline) { throw "$what is not ready after $seconds s" }
        Start-Sleep -Seconds 2
    }
}

# 1. Docker Desktop: opened only when the engine does not answer
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "starting Docker Desktop..."
    Start-Process "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
    Wait-Until { docker info *> $null; $LASTEXITCODE -eq 0 } "Docker"
}

# 2. The database: the server hangs on start while Postgres is not up
docker compose up -d
Wait-Until { docker exec study-assistant-db pg_isready -q *> $null; $LASTEXITCODE -eq 0 } "the database"
Write-Host "database is up"

# 3. The browser opens from a background job; the server runs in this window
Start-Job {
    for ($i = 0; $i -lt 90; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/subjects -TimeoutSec 2 | Out-Null
            Start-Process "http://127.0.0.1:8000/app/"
            return
        } catch { Start-Sleep -Seconds 1 }
    }
} | Out-Null

.\.venv\Scripts\python.exe -m uvicorn main:app --reload

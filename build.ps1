#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Join-Path $ProjectDir 'src'
$BuildDir = Join-Path $ProjectDir 'build'
$FnpackUrl = 'https://static2.fnnas.com/fnpack/fnpack-1.2.1-windows-amd64'
$FnpackBin = Join-Path $ProjectDir 'fnpack.exe'

Write-Host '========================================='
Write-Host '  UGreenLedPilot Build (Windows)'
Write-Host '========================================='

# Step 0: Icons
Write-Host "`n[0/4] Generating icons..."
python (Join-Path $ProjectDir 'scripts\generate_icons.py')
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed' }

# Step 1: Download fnpack if missing
if (-not (Test-Path $FnpackBin)) {
    Write-Host "`n[1/4] Downloading fnpack..."
    Invoke-WebRequest -Uri $FnpackUrl -OutFile $FnpackBin -UseBasicParsing
} else {
    Write-Host "`n[1/4] fnpack already exists, skipping download."
}

# Step 2: Build LED CLI via Docker (optional)
$CliOutput = Join-Path $AppDir 'app\server\ugreen_leds_cli'
if (-not (Test-Path $CliOutput)) {
    Write-Host "`n[2/4] Building ugreen_leds_cli via Docker..."
    $dockerCmd = @'
set -e
apk add --no-cache git g++ make linux-headers > /dev/null 2>&1
git clone --depth 1 https://github.com/miskcoo/ugreen_leds_controller.git > /dev/null 2>&1
cd ugreen_leds_controller
git fetch --depth 1 origin af2b7ae84f65a8730768d4b626570bc824b196e0 > /dev/null 2>&1
git checkout af2b7ae84f65a8730768d4b626570bc824b196e0 > /dev/null 2>&1
[ "$(git rev-parse HEAD)" = "af2b7ae84f65a8730768d4b626570bc824b196e0" ]
cd cli && make > /dev/null 2>&1
cp ugreen_leds_cli /output/
'@
    docker run --platform linux/amd64 --rm `
        -v "${CliOutput}:/output/ugreen_leds_cli" `
        -w /build alpine:latest sh -c $dockerCmd
    if (Test-Path $CliOutput) { Write-Host "  Built: $CliOutput" }
    else { Write-Host '  WARNING: CLI build skipped (Docker unavailable or failed)' }
} else {
    Write-Host "`n[2/4] ugreen_leds_cli already exists."
}

# Step 3: fnpack build
Write-Host "`n[3/4] Building fpk package..."
Push-Location $AppDir
& $FnpackBin build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'fnpack build failed' }
Pop-Location

# Step 4: Collect artifacts
Write-Host "`n[4/4] Collecting artifacts..."
if (Test-Path $BuildDir) { Remove-Item $BuildDir -Recurse -Force }
New-Item -ItemType Directory -Path $BuildDir | Out-Null

$version = (Select-String -Path (Join-Path $AppDir 'manifest') -Pattern '^version\s*=' |
    ForEach-Object { $_.Line -replace '.*=\s*', '' }).Trim()
$fpk = Join-Path $AppDir 'UGreenLedPilot.fpk'
if (-not (Test-Path $fpk)) { $fpk = Join-Path $ProjectDir 'UGreenLedPilot.fpk' }
if (Test-Path $fpk) {
    $dest = Join-Path $BuildDir "UGreenLedPilot-${version}.x86_64.fpk"
    Copy-Item $fpk $dest
    Write-Host "  $dest"
} else {
    throw 'fpk file not found after build'
}

if (Test-Path $CliOutput) {
    Copy-Item $CliOutput (Join-Path $BuildDir 'ugreen_leds_cli')
    Write-Host "  $(Join-Path $BuildDir 'ugreen_leds_cli')"
}

Write-Host "`n========================================="
Write-Host '  Build complete'
Write-Host '========================================='

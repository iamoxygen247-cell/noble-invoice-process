# Install the Noble pre-commit hook into this checkout's .git/hooks.
#
# .git/hooks is not versioned, so the tracked hook at scripts/hooks/pre-commit
# is the source of truth; this copies it into place. Re-run after the hook
# changes. Run from anywhere in the repo:
#     .\.venv\Scripts\python.exe -m nothing   # (no-op; just ensure you're in the repo)
#     powershell -ExecutionPolicy Bypass -File scripts\hooks\install.ps1

$ErrorActionPreference = "Stop"

$root = (git rev-parse --show-toplevel).Trim()
if (-not $root) { throw "not inside a git repository" }

$src = Join-Path $root "scripts\hooks\pre-commit"
$dstDir = Join-Path $root ".git\hooks"
$dst = Join-Path $dstDir "pre-commit"

if (-not (Test-Path $src)) { throw "hook source not found: $src" }
if (-not (Test-Path $dstDir)) { throw ".git/hooks not found: $dstDir (is this a git checkout?)" }

Copy-Item -Path $src -Destination $dst -Force
# Git for Windows runs the hook via its bundled sh; the shebang is what matters.
# Mark it executable for good measure (harmless on NTFS, needed under WSL/git-bash).
& git update-index --chmod=+x --add -- "scripts/hooks/pre-commit" 2>$null | Out-Null

Write-Host "Installed pre-commit hook -> $dst"
Write-Host "It runs pytest on every commit, then scripts/regress.py (CU golden-corpus)."
Write-Host "Set NOBLE_SKIP_REGRESS=1 to skip only the CU part for an offline commit."

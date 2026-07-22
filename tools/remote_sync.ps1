[CmdletBinding()]
param(
    [string]$HostAlias = 'geng-agent-remote',
    [string]$RemoteRoot = '/root/geng-agent-task-driven'
)

$ErrorActionPreference = 'Stop'

if ($RemoteRoot -ne '/root/geng-agent-task-driven') {
    throw "Refusing unapproved remote root: $RemoteRoot"
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ArchiveName = "geng-agent-task-driven-$Stamp.tar"
$ApplyName = "apply-geng-agent-$Stamp.sh"
$TempRoot = [System.IO.Path]::GetTempPath()
$ArchivePath = Join-Path $TempRoot $ArchiveName
$ApplyPath = Join-Path $TempRoot $ApplyName
$RemoteCache = '/root/.cache/geng-agent-sync'
$RemoteArchive = "$RemoteCache/$ArchiveName"
$RemoteApply = "$RemoteCache/$ApplyName"

function Invoke-ExternalWithRetry {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Label,
        [int]$Attempts = 4
    )

    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
        & $Action
        $ExitCode = $LASTEXITCODE
        if ($ExitCode -eq 0) {
            return
        }
        if ($Attempt -eq $Attempts) {
            throw "$Label failed with exit code $ExitCode after $Attempts attempts"
        }
        Start-Sleep -Seconds ([Math]::Min(20, 5 * $Attempt))
    }
}

try {
    & tar -cf $ArchivePath `
        --exclude='./.pytest_cache' `
        --exclude='./.mypy_cache' `
        --exclude='./.ruff_cache' `
        --exclude='./.venv' `
        --exclude='./venv' `
        --exclude='./node_modules' `
        --exclude='*/__pycache__' `
        --exclude='*.pyc' `
        -C $ProjectRoot .
    if ($LASTEXITCODE -ne 0) {
        throw "tar failed with exit code $LASTEXITCODE"
    }

    $LocalHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $ApplyScript = @'
#!/usr/bin/env bash
set -euo pipefail

remote_root='__REMOTE_ROOT__'
remote_archive='__REMOTE_ARCHIVE__'
expected_hash='__EXPECTED_HASH__'
stage="${remote_root}.incoming"
previous="${remote_root}.previous"
success_marker='/root/.cache/geng-agent-sync/last-success.sha256'

if [[ "$remote_root" != '/root/geng-agent-task-driven' ]]; then
  echo "refusing unsafe remote root: $remote_root" >&2
  exit 2
fi

if [[ -d "$remote_root/.git" && -f "$success_marker" ]] && grep -qxF "$expected_hash" "$success_marker"; then
  printf 'REMOTE_SYNC_ALREADY_APPLIED\n'
  git -C "$remote_root" config core.autocrlf true
  git -C "$remote_root" config core.filemode false
  cd "$remote_root"
  git status --short --branch
  exit 0
fi

actual_hash="$(sha256sum "$remote_archive" | awk '{print $1}')"
if [[ "$actual_hash" != "$expected_hash" ]]; then
  echo "archive hash mismatch" >&2
  exit 3
fi

rm -rf -- "$stage"
mkdir -p -- "$stage"
tar -xf "$remote_archive" -C "$stage"
test -f "$stage/pyproject.toml"
test -f "$stage/geng_agent/pipeline.py"
test -d "$stage/.git"
if [[ -d "$stage/tools" ]]; then
  while IFS= read -r -d '' shell_script; do
    sed -i 's/\r$//' "$shell_script"
    chmod 755 "$shell_script"
  done < <(find "$stage/tools" -maxdepth 1 -type f -name '*.sh' -print0)
fi

rm -rf -- "$previous"
if [[ -e "$remote_root" ]]; then
  mv -- "$remote_root" "$previous"
fi
mv -- "$stage" "$remote_root"
git -C "$remote_root" config core.autocrlf true
git -C "$remote_root" config core.filemode false

if [[ -f /root/.config/geng-agent/env.sh ]]; then
  source /root/.config/geng-agent/env.sh
fi
frontend="$remote_root/geng_agent/web/frontend"
if command -v npm >/dev/null 2>&1 && [[ -f "$frontend/package-lock.json" ]]; then
  (cd "$frontend" && npm ci && npm run build)
fi

printf '%s\n' "$expected_hash" > "$success_marker"
rm -f -- "$remote_archive"

printf 'REMOTE_SYNC_OK\n'
cd "$remote_root"
git status --short --branch
'@
    $ApplyScript = $ApplyScript.Replace('__REMOTE_ROOT__', $RemoteRoot)
    $ApplyScript = $ApplyScript.Replace('__REMOTE_ARCHIVE__', $RemoteArchive)
    $ApplyScript = $ApplyScript.Replace('__EXPECTED_HASH__', $LocalHash)
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $ApplyPath,
        ($ApplyScript -replace "`r`n", "`n"),
        $Utf8NoBom
    )

    Invoke-ExternalWithRetry -Label 'remote cache setup' -Action {
        & ssh $HostAlias "mkdir -p $RemoteCache"
    }
    Invoke-ExternalWithRetry -Label 'archive upload' -Action {
        & scp $ArchivePath "${HostAlias}:$RemoteArchive"
    }
    Invoke-ExternalWithRetry -Label 'apply-script upload' -Action {
        & scp $ApplyPath "${HostAlias}:$RemoteApply"
    }
    Invoke-ExternalWithRetry -Label 'remote apply' -Action {
        & ssh $HostAlias "bash $RemoteApply"
    }
    & ssh $HostAlias "rm -f $RemoteApply" 2>$null

    [PSCustomObject]@{
        Host = $HostAlias
        RemoteRoot = $RemoteRoot
        ArchiveBytes = (Get-Item -LiteralPath $ArchivePath).Length
        Sha256 = $LocalHash
        PreviousMirror = "$RemoteRoot.previous"
    } | Format-List
}
finally {
    Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ApplyPath -Force -ErrorAction SilentlyContinue
}

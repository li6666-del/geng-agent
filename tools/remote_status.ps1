[CmdletBinding()]
param(
    [string]$HostAlias = 'geng-agent-remote'
)

$ErrorActionPreference = 'Stop'
$RemoteCommand = @'
set -eu
[[ -f /root/.config/geng-agent/env.sh ]] && source /root/.config/geng-agent/env.sh
printf '%s\n' '== identity =='
hostname
printf '%s\n' '== project =='
if [[ -d /root/geng-agent-task-driven/.git ]]; then
  cd /root/geng-agent-task-driven
  git status --short --branch
else
  echo 'project mirror missing'
fi
printf '%s\n' '== runtime =='
if [[ -x /root/.venvs/geng-agent/bin/python ]]; then
  /root/.venvs/geng-agent/bin/python --version
  /root/.venvs/geng-agent/bin/python -m pip --version
else
  echo 'Python environment missing'
fi
codex --version 2>/dev/null || echo 'Codex CLI missing'
codex login status 2>&1 || true
printf '%s\n' '== gpu =='
nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv,noheader 2>/dev/null || true
printf '%s\n' '== storage =='
df -h / /root/geng-agent-cases 2>/dev/null | awk 'NR==1 || !seen[$6]++'
printf '%s\n' '== active project processes =='
ps -eo pid,etimes,cmd | grep -E 'geng_agent|codex exec|uvicorn|celery' | grep -v grep || true
printf '%s\n' '== last successful sync =='
cat /root/.cache/geng-agent-sync/last-success.sha256 2>/dev/null || echo 'no sync marker'
'@

$RemoteCommand = $RemoteCommand -replace "`r`n", "`n"
$Encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($RemoteCommand))
$RemoteInvocation = "printf '%s' '$Encoded' | base64 -d | bash"

for ($Attempt = 1; $Attempt -le 4; $Attempt++) {
    $Output = & ssh -o BatchMode=yes -o ConnectTimeout=20 $HostAlias $RemoteInvocation 2>&1
    $ExitCode = $LASTEXITCODE
    $Output
    if ($ExitCode -eq 0) {
        exit 0
    }
    if ($Attempt -lt 4) {
        Start-Sleep -Seconds (2 * $Attempt)
    }
}
exit $ExitCode

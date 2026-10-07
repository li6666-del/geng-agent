"""Compile the fixed boot helper with Windows .NET; never apply system ACLs."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from geng_agent.windows_appcontainer_setup import _targets

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows boot helper")


@pytest.fixture(scope="module")
def helper_results(tmp_path_factory):
    root = tmp_path_factory.mktemp("boot")
    source = Path(__file__).resolve().parents[1] / "tools/windows/WriterVolumeQuery.cs"
    script = root / "check.ps1"
    script.write_text(r'''
$ErrorActionPreference = 'Stop'
$env:TEMP=$PSScriptRoot; $env:TMP=$PSScriptRoot
Add-Type -Path '__SOURCE__' -ReferencedAssemblies @('System.dll','System.Core.dll','System.Web.Extensions.dll')
Add-Type -TypeDefinition @'
using System;
using System.Security.AccessControl;
public static class AclBytes {
    public static string Encode(GenericAce ace) {
        byte[] bytes=new byte[ace.BinaryLength]; ace.GetBinaryForm(bytes,0);
        return Convert.ToBase64String(bytes);
    }
}
'@
$sid=[GengAgent.WriterBoundary.VolumeQuery]::Capability()
$original=[Security.AccessControl.RawSecurityDescriptor]::new('D:(D;;0x2;;;BU)(A;;GA;;;SY)(A;;GR;;;BA)')
$before=@($original.DiscretionaryAcl | ForEach-Object { [AclBytes]::Encode($_) })
$merged=[GengAgent.WriterBoundary.VolumeQuery]::WithReadGrant($original.DiscretionaryAcl,$sid,0x20003)
$after=@($merged | Select-Object -First 3 | ForEach-Object { [AclBytes]::Encode($_) })
$again=[GengAgent.WriterBoundary.VolumeQuery]::WithReadGrant($merged,$sid,0x20003)
$partial=[Security.AccessControl.RawSecurityDescriptor]::new(('D:(A;;0x20000;;;'+$sid.Value+')'))
$completed=[GengAgent.WriterBoundary.VolumeQuery]::WithReadGrant($partial.DiscretionaryAcl,$sid,0x20003)
$denied=[Security.AccessControl.RawSecurityDescriptor]::new(('D:(D;;0x1;;;'+$sid.Value+')(A;;GA;;;SY)'))
$denyRefused=$false
try { [GengAgent.WriterBoundary.VolumeQuery]::WithReadGrant($denied.DiscretionaryAcl,$sid,0x20003) | Out-Null } catch { $denyRefused=$true }
$nullRefused=$false
try { [GengAgent.WriterBoundary.VolumeQuery]::WithReadGrant($null,$sid,0x20003) | Out-Null } catch { $nullRefused=$true }
$inherited=[Security.AccessControl.RawSecurityDescriptor]::new('D:(A;ID;GR;;;BU)')
$inheritedResult=[GengAgent.WriterBoundary.VolumeQuery]::WithReadGrant($inherited.DiscretionaryAcl,$sid,0x20003)
[pscustomobject]@{
    capability=$sid.Value
    preserved=(($before -join ',') -ceq ($after -join ','))
    originalUnchanged=($original.DiscretionaryAcl.Count -eq 3)
    exactGrant=($merged.Count -eq 4 -and $merged[3].AccessMask -eq 0x20003 -and $merged[3].SecurityIdentifier.Equals($sid))
    idempotent=($again.Count -eq $merged.Count -and [AclBytes]::Encode($again[3]) -eq [AclBytes]::Encode($merged[3]))
    addsOnlyMissing=($completed.Count -eq 2 -and $completed[1].AccessMask -eq 3)
    denyRefused=$denyRefused
    nullRefused=$nullRefused
    explicitBeforeInherited=($inheritedResult[0].SecurityIdentifier.Equals($sid) -and [AclBytes]::Encode($inheritedResult[1]) -eq [AclBytes]::Encode($inherited.DiscretionaryAcl[0]))
    argumentsRejected=([GengAgent.WriterBoundary.VolumeQuery]::Main(@('arbitrary-path')) -eq 2)
    inspected=[GengAgent.WriterBoundary.VolumeQuery]::CheckOnly()
} | ConvertTo-Json -Depth 7
'''.replace("__SOURCE__", str(source).replace("'", "''")), encoding="utf-8-sig")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("case", ["preserved", "originalUnchanged", "exactGrant", "idempotent",
                                 "addsOnlyMissing", "denyRefused", "nullRefused",
                                 "explicitBeforeInherited", "argumentsRejected"])
def test_boot_helper_only_adds_required_read_permissions(helper_results, case):
    assert helper_results[case] is True


def test_boot_helper_matches_the_host_capability_and_fixed_targets(helper_results):
    from geng_agent.windows_process_boundary import _readonly_capability, _sid_string
    assert helper_results["capability"] == _sid_string(_readonly_capability())
    assert [(item["name"], item["mask"]) for item in helper_results["inspected"]] == [
        (item["name"], item["mask"]) for item in _targets(("C", "D"))]

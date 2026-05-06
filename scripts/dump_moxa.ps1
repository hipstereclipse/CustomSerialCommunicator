$paths = @(
  'HKLM:\SYSTEM\CurrentControlSet\Enum\MxUpser',
  'HKLM:\SYSTEM\CurrentControlSet\Services\MxUpser',
  'HKLM:\SYSTEM\CurrentControlSet\Enum\MXUPSER'
)
foreach ($p in $paths) {
  if (Test-Path $p) {
    Write-Host "===" $p
    Get-ChildItem -Recurse $p -ErrorAction SilentlyContinue | ForEach-Object {
      $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
      if ($props) {
        Write-Host "---" $_.PSPath
        $props | Format-List
      }
    }
  }
}

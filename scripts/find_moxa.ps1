Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Services' | Where-Object { $_.Name -match 'Mx|mx|Moxa|UPort' } | Select-Object -ExpandProperty Name

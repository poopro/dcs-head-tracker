# Creates "DCS Head Tracker" on the real Desktop (works with OneDrive-redirected desktops).
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $desktop 'DCS Head Tracker.lnk'))
$lnk.TargetPath = 'wscript.exe'
$lnk.Arguments = '"' + (Join-Path $here 'launch.vbs') + '"'
$lnk.WorkingDirectory = $here
$lnk.IconLocation = (Join-Path $here 'headtracker.ico') + ',0'
$lnk.Description = 'DCS Head Tracker'
$lnk.Save()
Write-Host "Desktop shortcut created: $($lnk.FullName)"

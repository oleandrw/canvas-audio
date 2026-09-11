# Creates a desktop shortcut "Canvas til lyd" that starts the app on double-click.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1
#
# -ExecutionPolicy Bypass applies to this one run only; no system setting changes.

$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "start.bat"
$icon   = Join-Path $root "assets\canvas-audio.ico"
if (-not (Test-Path $target)) { throw "Fant ikke $target" }

$desktop = [Environment]::GetFolderPath("Desktop")
$path    = Join-Path $desktop "Canvas til lyd.lnk"

$shell    = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath       = $target
$shortcut.WorkingDirectory = $root
$shortcut.Description      = "Start Canvas til lyd i nettleseren"
$shortcut.WindowStyle      = 1
if (Test-Path $icon) { $shortcut.IconLocation = "$icon,0" }
$shortcut.Save()

Write-Output "Snarvei laget: $path"

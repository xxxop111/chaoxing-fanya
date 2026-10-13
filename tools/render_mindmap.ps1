# Render docs/mindmap.html -> docs/mindmap.png
#
# Why PowerShell instead of Python (both points verified by experiment):
#
#   1. Under this sandbox, launching msedge from a Python process fails: the Edge
#      launcher stub cannot re-launch itself detached, so you get exit code 0, no
#      stderr, and no output file. Launching from the shell works fine.
#
#   2. --screenshot silently fails when the output path contains non-ASCII
#      characters. This repo lives under D:\03-开发\..., so we render into TEMP
#      (pure ASCII) and move the result into place afterwards.
#
#   This file is deliberately ASCII-only. Windows PowerShell 5.1 reads a
#   BOM-less .ps1 as ANSI, which corrupts non-ASCII text (and can even merge
#   lines), so keeping the script ASCII removes that whole class of problem.
#   Every tool here also strips the BOM on write, so do not add one.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools/render_mindmap.ps1

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$html = Join-Path $root 'docs\mindmap.html'
$dest = Join-Path $root 'docs\mindmap.png'

if (-not (Test-Path $html)) { throw "missing $html" }

$exe = @(
  'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
  'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
  'C:\Program Files\Google\Chrome\Application\chrome.exe',
  'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) { throw 'Edge/Chrome not found' }

# Kill leftover headless instances. If one is already running, a new invocation
# hands the request to it and exits 0 without writing anything.
Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
  Where-Object { $_.CommandLine -match '--headless' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

$work = Join-Path $env:TEMP ('mindmap-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$srcCopy = Join-Path $work 'mindmap.html'
$shot    = Join-Path $work 'mindmap.png'
Copy-Item $html $srcCopy -Force

$url = ([System.Uri]$srcCopy).AbsoluteUri
$args = "--headless=new --disable-gpu --hide-scrollbars --no-first-run " +
        "--user-data-dir=`"$work\profile`" --window-size=1560,3000 " +
        "--screenshot=`"$shot`" `"$url`""

Write-Host "browser: $exe"
Write-Host "rendering ..."
$p = Start-Process -FilePath $exe -ArgumentList $args -PassThru -WindowStyle Hidden
$null = $p.WaitForExit(180000)

for ($i = 0; $i -lt 40 -and -not (Test-Path $shot); $i++) { Start-Sleep -Milliseconds 250 }
if (-not (Test-Path $shot)) { throw 'screenshot failed: no PNG produced' }

# Trim the blank area below the content. Background is #0f1419.
#
# The crop must be written to a DIFFERENT file: GDI+ refuses to Save() over an
# Image that is still open, and dies with "A generic error occurred in GDI+".
Add-Type -AssemblyName System.Drawing

$src = [System.Drawing.Bitmap]::FromFile($shot)
$bgR = 15; $bgG = 20; $bgB = 25; $tol = 10
$last = $src.Height - 1
while ($last -gt 0) {
  $has = $false
  for ($x = 0; $x -lt $src.Width; $x += 6) {
    $c = $src.GetPixel($x, $last)
    if ([Math]::Abs($c.R-$bgR) -gt $tol -or [Math]::Abs($c.G-$bgG) -gt $tol -or [Math]::Abs($c.B-$bgB) -gt $tol) {
      $has = $true; break
    }
  }
  if ($has) { break }
  $last -= 3
}
$newH = [Math]::Min($src.Height, [Math]::Max(1, $last + 40))

$cropOut = Join-Path $work 'mindmap.cropped.png'
if ($newH -lt $src.Height) {
  $rect = New-Object System.Drawing.Rectangle 0, 0, $src.Width, $newH
  $crop = $src.Clone($rect, $src.PixelFormat)
  $crop.Save($cropOut, [System.Drawing.Imaging.ImageFormat]::Png)
  $crop.Dispose()
  Write-Host ("cropped: {0}px -> {1}px" -f $src.Height, $newH)
} else {
  $src.Save($cropOut, [System.Drawing.Imaging.ImageFormat]::Png)
}
$src.Dispose()

Move-Item $cropOut $dest -Force
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue

$fi = Get-Item $dest
Write-Host ("OK  {0}  {1:N0} KB" -f $dest, ($fi.Length / 1KB))

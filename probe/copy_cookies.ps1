$ErrorActionPreference = 'Stop'
$src = 'C:\Users\klklk\AppData\Local\Microsoft\Edge\User Data\Default\Network\Cookies'
$dstDir = 'D:\plan\HANDOFF-direct-link-tool\.private\edgecopy'
New-Item -ItemType Directory -Force -Path $dstDir | Out-Null

$share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
try {
    $fs = [System.IO.File]::Open($src, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
    $out = [System.IO.File]::Create((Join-Path $dstDir 'Cookies'))
    $fs.CopyTo($out)
    $out.Close()
    $fs.Close()
    Write-Output ("OK size=" + (Get-Item (Join-Path $dstDir 'Cookies')).Length)
} catch {
    Write-Output ("FAIL: " + $_.Exception.Message)
}

# 同时尝试 -wal / -shm
foreach ($suffix in @('-wal', '-shm')) {
    $s = $src + $suffix
    if (Test-Path $s) {
        try {
            $a = [System.IO.File]::Open($s, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
            $b = [System.IO.File]::Create((Join-Path $dstDir ('Cookies' + $suffix)))
            $a.CopyTo($b); $b.Close(); $a.Close()
            Write-Output ("OK " + $suffix + " size=" + (Get-Item (Join-Path $dstDir ('Cookies' + $suffix))).Length)
        } catch {
            Write-Output ("FAIL " + $suffix + ": " + $_.Exception.Message)
        }
    }
}

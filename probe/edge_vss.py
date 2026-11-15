"""Edge 运行中也能读 cookie 的办法：用 VSS/BackupRead 风格复制。
Windows 上 影子复制需要管理员；这里先试最简单的：robocopy /B (备份模式)。
"""
import subprocess, sys, tempfile, os, shutil
from pathlib import Path
UD = Path(os.environ['LOCALAPPDATA']) / 'Microsoft/Edge/User Data'
db = UD / 'Default/Network/Cookies'
tmpd = Path(tempfile.mkdtemp(prefix='ckb_'))
dst = tmpd / 'Cookies'
r = subprocess.run(['robocopy', str(db.parent), str(tmpd), 'Cookies', '/B', '/NFL', '/NDL', '/NJH', '/NJS'],
                   capture_output=True, text=True)
print('rc=', r.returncode)
print(r.stdout[-400:])
print('exists:', dst.exists(), dst.stat().st_size if dst.exists() else 0)
print('tmpd:', tmpd)

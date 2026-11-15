"""尝试解 Edge 的 v20 (App-Bound Encryption) cookie。

ABE 的设计目的就是**阻止外部进程解密**，所以这里大概率会失败。
失败是有价值的结论：说明这条路不可行，应改用别的方案。
"""

import base64
import json
import sqlite3

import win32crypt

UD = __import__('os').path.join(__import__('os').environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'User Data')
DB = __import__('os').path.join(UD, 'Default', 'Network', 'Cookies')
LS = __import__('os').path.join(UD, 'Local State')

state = json.load(open(LS, encoding='utf-8'))
oc = state.get('os_crypt', {})

print('== Local State ==')
for k in ('encrypted_key', 'app_bound_encrypted_key'):
    v = oc.get(k)
    print(f'  {k}: {"present len=%d" % len(v) if v else "absent"}')

# 1) 传统 DPAPI key
try:
    raw = base64.b64decode(oc['encrypted_key'])
    print(f'  encrypted_key prefix={raw[:5]!r}')
    key = win32crypt.CryptUnprotectData(raw[5:], None, None, None, 0)[1]
    print(f'  -> DPAPI 解出 key: {len(key)} bytes  OK')
except Exception as e:
    print(f'  -> DPAPI 失败: {type(e).__name__}: {e}')
    key = None

# 2) 取 BDUSS 密文
con = sqlite3.connect(DB)
row = con.execute(
    "SELECT encrypted_value FROM cookies WHERE name='BDUSS' AND host_key='.baidu.com'"
).fetchone()
con.close()
blob = row[0]
print(f'\n== BDUSS 密文: {len(blob)} bytes, prefix={blob[:3]!r} ==')

if blob[:3] == b'v20':
    print('v20 = App-Bound Encryption。Edge 新版用它把 key 绑到浏览器进程上。')
    print('按设计，外部进程无法解出明文 —— 需要绕过 ABE，属于逆向范畴。')
    print('\n尝试用传统 key 硬解（预期失败，用于确认）：')
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if key:
        try:
            nonce, payload = blob[3:15], blob[15:]
            pt = AESGCM(key).decrypt(nonce, payload, None)
            print(f'  意外成功！{len(pt)} bytes')
        except Exception as e:
            print(f'  如预期失败: {type(e).__name__}')
    else:
        print('  无 key，跳过')
elif blob[:3] == b'v10':
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce, payload = blob[3:15], blob[15:]
    pt = AESGCM(key).decrypt(nonce, payload, None)
    print('v10 解密成功，长度', len(pt))

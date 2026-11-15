import sqlite3, sys

p = r'C:\Users\klklk\AppData\Local\Microsoft\Edge\User Data\Default\Network\Cookies'
uri = 'file:' + p.replace('\\', '/') + '?mode=ro&immutable=1'
try:
    con = sqlite3.connect(uri, uri=True, timeout=2)
    rows = con.execute(
        "SELECT host_key, name, length(encrypted_value), substr(encrypted_value,1,3) "
        "FROM cookies WHERE host_key LIKE '%baidu%'"
    ).fetchall()
    con.close()
    print(f"matched {len(rows)} rows")
    for h, n, ln, pre in rows[:25]:
        try:
            pref = pre.decode('utf-8', 'replace') if isinstance(pre, bytes) else str(pre)
        except Exception:
            pref = '?'
        print(f"{h:35} {n:22} len={ln:5} prefix={pref!r}")
except Exception as e:
    print('ERR', type(e).__name__, e)

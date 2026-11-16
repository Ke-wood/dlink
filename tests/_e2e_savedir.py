"""端到端验证：更换下载目录是否真正生效并持久化。

跑法：python tests/_e2e_savedir.py
（手动脚本，不进 pytest，因为它需要起真实服务进程）
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TMP = Path(tempfile.mkdtemp(prefix="dlink_e2e_"))
SETTINGS_FILE = TMP / "settings.json"


def start_server(env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["DLINK_SETTINGS_FILE"] = str(SETTINGS_FILE)
    if env_extra:
        env.update(env_extra)
    p = subprocess.Popen(
        [sys.executable, str(ROOT / "src" / "dlink" / "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    # 等端口就绪
    for _ in range(100):
        time.sleep(0.1)
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=0.4)
            return p
        except Exception:
            continue
    raise RuntimeError("server 未就绪")


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()


def get(path):
    return json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:8765{path}", timeout=5).read())


def post(path, data):
    req = urllib.request.Request(
        f"http://127.0.0.1:8765{path}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def main():
    ok = True
    print("=" * 70)
    print("端到端：更换下载目录")
    print("=" * 70)
    print(f"临时设置文件: {SETTINGS_FILE}\n")

    # ---------- 第一次启动：默认目录 ----------
    print("[1] 首次启动（无配置文件）")
    srv = start_server()
    try:
        before = get("/api/settings")["settings"]
        print(f"    默认 save_dir = {before['save_dir']}")
        assert "DLink" in before["save_dir"], "默认值异常"

        # ---------- 改成新目录 ----------
        new_dir = str(TMP / "我的新下载目录")
        print(f"\n[2] 通过 API 改成: {new_dir}")
        r = post("/api/settings", {"save_dir": new_dir, "max_concurrent": 5})
        print(f"    响应: ok={r.get('ok')} saved_to={r.get('saved_to')}")
        if r.get("warning"):
            print(f"    ⚠ warning: {r['warning']}")
        assert r["settings"]["save_dir"] == new_dir, "内存里没变"
        print(f"    ✓ 内存已更新")

        assert SETTINGS_FILE.exists(), "设置文件没写出来"
        print(f"    ✓ 磁盘文件已创建")
    finally:
        stop(srv)

    # ---------- 模拟重启 ----------
    print("\n[3] 杀掉进程，重新启动（模拟重启 exe）")
    srv2 = start_server()
    try:
        after = get("/api/settings")["settings"]
        print(f"    重启后 save_dir = {after['save_dir']}")
        print(f"    重启后 max_concurrent = {after['max_concurrent']}")

        if after["save_dir"] != new_dir:
            print("    ✗ 失败：重启后还原了！")
            ok = False
        else:
            print("    ✓ 新目录被保留")

        if after["max_concurrent"] != 5:
            print("    ✗ 失败：并发数没保留")
            ok = False
        else:
            print("    ✓ 并发数被保留")

        # ---------- 验证新任务真的落到新目录 ----------
        print("\n[4] 提交任务，验证文件落到新目录")
        post("/api/task", {
            "version": 1, "platform": "direct",
            "items": [{
                "url": "https://registry.npmmirror.com/-/binary/node/v20.11.0/node-v20.11.0-win-x64.zip",
                "filename": "__e2e_test.zip",
            }],
        })
        landed = None
        for _ in range(30):
            time.sleep(0.5)
            tasks = get("/api/tasks")["tasks"]
            t = next((x for x in tasks if x["name"] == "__e2e_test.zip"), None)
            if t and t.get("path"):
                landed = t["path"]
                if t["status"] in ("done", "failed"):
                    break
        print(f"    任务落盘路径: {landed}")
        if landed and Path(landed).parent == Path(new_dir):
            print("    ✓ 确实下到了新目录")
        elif landed:
            print(f"    ✗ 下到了错误位置（期望 {new_dir}）")
            ok = False
        else:
            print("    ⚠ 未拿到路径（任务可能还在跑）")
    finally:
        stop(srv2)

    print("\n" + "=" * 70)
    print("结论:", "全部通过 ✓" if ok else "有失败 ✗")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

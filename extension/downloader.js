/**
 * 与本地 DLink 下载器通信（127.0.0.1:8765 起的端口）。
 *
 * 单独抽一个模块，是因为 background.js（提取直链/续链）和
 * intercept.js（接管浏览器下载）都要用它，而两者互相 import 会成环。
 *
 * 契约见 docs/HANDOFF-INTERCEPT.md §0.4：
 *   GET  /api/health   → {app:"dlink", ok, version, queue, extension}
 *   GET  /api/settings → {settings:{save_dir,...}}
 *   POST /api/task     ← {version, platform, title, items:[...]}
 */

export const DOWNLOADER_PORTS = [8765, 8766, 8767, 8768, 8769];
let downloaderPort = null;

export async function ping(port) {
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/health`, {
      signal: AbortSignal.timeout(800),
    });
    if (!r.ok) return false;
    const j = await r.json();
    return j && j.app === 'dlink';
  } catch {
    return false;
  }
}

/** 找到在线端口；找不到返回 null（调用方必须按「放行」处理） */
export async function findDownloader() {
  if (downloaderPort) {
    if (await ping(downloaderPort)) return downloaderPort;
    downloaderPort = null;
  }
  for (const p of DOWNLOADER_PORTS) {
    if (await ping(p)) {
      downloaderPort = p;
      return p;
    }
  }
  return null;
}

export async function sendToDownloader(task) {
  const port = await findDownloader();
  if (!port) {
    throw new Error(
      '下载器没有运行。请先启动 DLink 下载器（它会监听 127.0.0.1:8765 起的端口）。');
  }
  const r = await fetch(`http://127.0.0.1:${port}/api/task`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(task),
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`下载器返回 ${r.status}: ${t.slice(0, 200)}`);
  }
  return r.json();
}

/** 健康信息（含队列统计）。离线返回 null，用于 UI 展示而不抛错。 */
export async function getHealth() {
  const port = await findDownloader();
  if (!port) return null;
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/health`, {
      signal: AbortSignal.timeout(2000),
    });
    if (!r.ok) return null;
    const j = await r.json();
    return { port, ...j };
  } catch {
    return null;
  }
}

/** 下载器侧设置（目前只取 save_dir 展示） */
export async function getRemoteSettings() {
  const port = await findDownloader();
  if (!port) return null;
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/settings`, {
      signal: AbortSignal.timeout(2000),
    });
    if (!r.ok) return null;
    const j = await r.json();
    return j.settings || null;
  } catch {
    return null;
  }
}

/**
 * Service worker：编排解析流程 + 与本地下载器通信。
 *
 * 职责边界（对应 docs/ARCHITECTURE.md）：
 *   1. 决定用哪个平台
 *   2. 把 injected.js 注入页面上下文执行（拿登录态）
 *   3. 用 chrome.cookies 补齐 HttpOnly cookie（页面 JS 读不到的）
 *   4. POST 到本地下载器 127.0.0.1:<port>
 *
 * 不负责：下载、写盘（那是下载器的事）
 */

import { detectPlatform, parseShareId } from './platforms.js';
import { findDownloader, sendToDownloader } from './downloader.js';
import { initIntercept, handleInterceptMessage } from './intercept.js';

// 与下载器的通信见 downloader.js（findDownloader / sendToDownloader）

// ---------------- 注入解析 ----------------

/** 解析前先拿一些「页面 JS 读不到」的辅助参数。
 *
 * 目前只有百度：分享直下需要 sekey（提取码验证通过后的凭证），而它可能
 * 只存在于 HttpOnly 的 BDCLND cookie 里。页面侧读不到时，就由这里补一个
 * 候选交给解析器 —— 解析器遇到 errno=-20 会换来源重试。
 */
async function resolveOptions(platformId) {
  const opts = {};
  if (platformId === 'baidu') {
    try {
      const c = await chrome.cookies.get({
        url: 'https://pan.baidu.com', name: 'BDCLND',
      });
      if (c && c.value) opts.sekey = c.value;
    } catch { /* 没权限就算了，页面侧还有别的来源 */ }
  }
  return opts;
}

async function injectAndResolve(tabId, platformId, shareUrl) {
  // 先注入脚本（world: MAIN 才能在页面上下文执行）
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ['injected.js'],
    world: 'MAIN',
  });

  // 再调用
  const opts = await resolveOptions(platformId);
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    args: [platformId, shareUrl, opts],
    func: (pid, url, o) => window.__DLINK__ && window.__DLINK__.resolve(pid, url, undefined, o),
  });
  const res = results && results[0] && results[0].result;
  if (!res) throw new Error('页面没有返回结果（脚本可能被 CSP 拦截）');
  return res;
}

/** 用 chrome.cookies 补 HttpOnly 的关键 cookie */
async function collectHttpOnlyCookies(platformId) {
  const domains = {
    baidu: ['https://pan.baidu.com', 'https://passport.baidu.com'],
    quark: ['https://pan.quark.cn', 'https://drive-pc.quark.cn'],
    uc: ['https://drive.uc.cn', 'https://pc-api.uc.cn'],
    weiyun: ['https://share.weiyun.com'],
  }[platformId] || [];

  const out = {};
  for (const d of domains) {
    try {
      const list = await chrome.cookies.getAll({ url: d });
      for (const c of list) out[c.name] = c.value;
    } catch { /* 权限不足就跳过 */ }
  }
  return out;
}

function mergeCookies(fromPage, fromBrowser) {
  // 浏览器侧（含 HttpOnly）优先，但页面侧的补全
  return { ...fromPage, ...fromBrowser };
}

// ---------------- 主流程 ----------------

async function handleResolve(tab) {
  const platform = detectPlatform(tab.url);
  if (!platform) {
    throw new Error(`不支持的页面：${tab.url}\n目前支持：百度网盘 / 夸克 / UC / 腾讯微云`);
  }
  const shareId = parseShareId(tab.url, platform);
  if (!shareId) throw new Error('这个页面不是分享页');

  const res = await injectAndResolve(tab.id, platform.id, tab.url);
  if (!res.ok) {
    const err = new Error(res.message || '解析失败');
    err.hint = res.hint || '';
    err.code = res.code || '';
    throw err;
  }

  // 补 HttpOnly cookie（BDUSS/STOKEN 等 JS 读不到的）
  const httpOnly = await collectHttpOnlyCookies(platform.id);

  const items = [];
  for (const f of res.files) {
    if (f.error) {
      items.push({
        platform: platform.id,
        status: 'failed',
        name: f.name,
        reason: f.error,
      });
      continue;
    }
    items.push({
      platform: platform.id,
      status: 'ok',
      url: f.url,
      name: f.name,
      size: f.size || 0,
      md5: f.md5 || '',
      sha1: f.sha1 || '',
      cookies: mergeCookies(f.cookies || {}, httpOnly),
      headers: f.headers || {},
      refresh: f.refresh || null,
    });
  }
  return { platform: platform.id, platformName: platform.name, items };
}

// ---------------- 消息接口 ----------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg.type === 'resolve') {
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        const tab = tabs[0];
        if (!tab) throw new Error('找不到当前标签页');
        const result = await handleResolve(tab);
        sendResponse({ ok: true, data: result });
      } else if (msg.type === 'send') {
        const result = await sendToDownloader(msg.task);
        sendResponse({ ok: true, data: result });
      } else if (msg.type === 'ping-downloader') {
        const port = await findDownloader();
        sendResponse({ ok: true, data: { port } });
      } else {
        const handled = await handleInterceptMessage(msg);
        if (handled) sendResponse(handled);
        else sendResponse({ ok: false, error: `未知消息类型: ${msg.type}` });
      }
    } catch (e) {
      sendResponse({
        ok: false,
        error: String(e && e.message ? e.message : e),
        hint: e && e.hint ? e.hint : '',
        code: e && e.code ? e.code : '',
      });
    }
  })();
  return true; // 异步
});

// ---------------- 直链续期（自动续链）----------------
//
// 为什么由扩展发起轮询：
//   下载器不知道扩展在哪（没有固定地址），而扩展随时可以 POST 到
//   127.0.0.1:8765。所以由扩展主动轮询「有没有任务需要新直链」。

const REFRESH_INTERVAL_SEC = 20;
let refreshTimer = null;

async function pollPendingRefresh() {
  const port = await findDownloader();
  if (!port) return;

  let data;
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/pending-refresh`, {
      signal: AbortSignal.timeout(5000),
    });
    if (!r.ok) return;
    data = await r.json();
  } catch {
    return;
  }

  const pending = (data && data.pending) || [];
  for (const p of pending) {
    if (p.timed_out) continue;
    try {
      await refreshOne(port, p);
    } catch (e) {
      console.warn('[DLink] 续链失败', p, e);
    }
  }
}

async function refreshOne(port, req) {
  const { task_id, platform, share_url, fid } = req;
  if (!share_url || !platform) return;

  // 打开（或复用）分享页，在其上下文里重新执行解析
  const tabs = await chrome.tabs.query({});
  let tab = tabs.find((t) => t.url && t.url.split('#')[0] === share_url.split('#')[0]);
  let created = false;
  if (!tab) {
    // 需要后台打开一次，避免打断用户
    tab = await chrome.tabs.create({ url: share_url, active: false });
    created = true;
    await new Promise((res) => setTimeout(res, 3000));
  }

  let res;
  try {
    res = await injectAndResolve(tab.id, platform, share_url);
  } finally {
    if (created && tab.id) {
      try { await chrome.tabs.remove(tab.id); } catch { /* ignore */ }
    }
  }
  if (!res || !res.ok) return;

  // 找到对应 fid 的新直链
  const hit = res.files.find(
    (f) => !f.error && String(f.fid || '') === String(fid || '')
  ) || res.files.find((f) => !f.error);

  if (!hit) return;

  const httpOnly = await collectHttpOnlyCookies(platform);
  const cookies = mergeCookies(hit.cookies || {}, httpOnly);

  await fetch(`http://127.0.0.1:${port}/api/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_id,
      url: hit.url,
      cookies,
      headers: hit.headers || {},
      expires_at: hit.expires_at || null,
    }),
  });
  console.log('[DLink] 已为任务', task_id, '续链');
}

// ⚠️ 不能用 setInterval：MV3 的 service worker 会被回收，定时器随之消失。
//    chrome.alarms 由浏览器调度，worker 被唤醒后会触发，才是正确做法。
async function startRefreshPolling() {
  try {
    await chrome.alarms.create('dlink-refresh', {
      periodInMinutes: Math.max(0.5, REFRESH_INTERVAL_SEC / 60),
    });
  } catch (e) {
    console.warn('[DLink] 创建轮询 alarm 失败', e);
  }
  pollPendingRefresh().catch(() => {});
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'dlink-refresh') {
    pollPendingRefresh().catch(() => {});
  }
});
chrome.runtime.onStartup.addListener(startRefreshPolling);
chrome.runtime.onInstalled.addListener(startRefreshPolling);
startRefreshPolling();
initIntercept();

// 首次安装提示
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === 'install') {
    console.log('[DLink] 扩展已安装。打开网盘分享页，点扩展图标即可提取直链。');
  }
});

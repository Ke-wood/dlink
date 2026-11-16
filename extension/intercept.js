/**
 * 浏览器下载接管（chrome.downloads.onCreated）。
 *
 * 设计要点（见 docs/HANDOFF-INTERCEPT.md §2.3）：
 *   1. **首次**：pause → 弹窗询问 → 接受则 cancel+转交 / 拒绝则 resume。
 *      用 pause 而不是直接 cancel，是因为 cancel 后只能重新 download(url)，
 *      会丢掉 POST 表单体、自定义请求头、一次性签名。
 *   2. **已开启**：cancel + 清临时文件 + 转交；转交失败回退浏览器下载。
 *   3. **DLink 不在线一律放行** —— 绝不「拦下来又发不出去」。
 *   4. **本扩展发起的下载必须放行**，否则回退用的 chrome.downloads.download()
 *      会再次触发 onCreated，形成死循环。
 *   5. 平台由 CDN 域名反推（inferPlatform），否则百度会被 8 路并发跑成 9 KB/s。
 *
 * 不支持（检测到直接放行，不报错）：
 *   blob: / data: / filesystem:（内容只在页面内存里，DLink 拿不到）
 *   .m3u8 / .mpd（分片清单，不是文件）
 *   POST 表单下载（暂停能恢复，但取消后重新拉不到）
 */

import { inferPlatform, isNetdiskUrl, hostOf } from './platforms.js';
import { getSettings, setSettings } from './settings.js';
import { findDownloader, sendToDownloader } from './downloader.js';

const PENDING_KEY = 'intercept.pending';
const PROMPT_FILE = 'prompt.html';
const DEDUPE_MS = 3000;
const BADGE_MS = 4000;
const MAX_PENDING = 20;

let promptWindowId = null;
let memPending = [];
const recentUrls = new Map();
let badgeTimer = null;

// ---------------- 小工具 ----------------

function log(reason, item) {
  // 只在下述几种情况下打日志，避免每次下载都刷屏
  if (reason && item) console.debug(`[DLink] 放行(${reason}) ${item.url || ''}`);
}

function baseName(p) {
  const s = String(p || '');
  const name = s.split(/[\\/]/).pop() || '';
  // eslint-disable-next-line no-control-regex
  return name.replace(/[\u0000-\u001f<>:"|?*]/g, '_').trim().slice(0, 200);
}

function nameFromUrl(url) {
  try {
    const u = new URL(url);
    const last = decodeURIComponent(u.pathname.split('/').pop() || '');
    return baseName(last) || 'download.bin';
  } catch {
    return 'download.bin';
  }
}

function flashBadge(text) {
  try {
    chrome.action.setBadgeBackgroundColor({ color: '#000000' });
    chrome.action.setBadgeText({ text });
    if (badgeTimer) clearTimeout(badgeTimer);
    badgeTimer = setTimeout(() => {
      try { chrome.action.setBadgeText({ text: '' }); } catch { /* ignore */ }
    }, BADGE_MS);
  } catch { /* ignore */ }
}

function notify(title, message) {
  try {
    if (!chrome.notifications) return;
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon128.png',
      title,
      message: String(message || '').slice(0, 200),
    });
  } catch { /* ignore */ }
}

// ---------------- 待决下载（跨 service worker 重启） ----------------

async function readPending() {
  if (!chrome.storage.session) return memPending.slice();
  try {
    const r = await chrome.storage.session.get(PENDING_KEY);
    memPending = r[PENDING_KEY] || [];
    return memPending.slice();
  } catch {
    return memPending.slice();
  }
}

async function writePending(list) {
  memPending = list || [];
  if (!chrome.storage.session) return;
  try {
    await chrome.storage.session.set({ [PENDING_KEY]: memPending });
  } catch { /* ignore */ }
}

// ---------------- 过滤 ----------------

/** 结构性拒绝：和用户设置无关，任何情况都放行 */
function basicReason(item) {
  if (!item || !item.url) return 'no-url';
  if (item.byExtensionId) return 'by-extension'; // 含本扩展（回退下载）
  if (item.incognito) return 'incognito';
  if (item.state === 'complete') return 'complete';
  const u = String(item.url);
  if (!/^https?:/i.test(u)) return 'non-http'; // blob: / data: / filesystem:
  if (/\.(m3u8|mpd)([?#]|$)/i.test(u)) return 'hls';
  const host = hostOf(u);
  if (!host) return 'no-host';
  if (/^(127\.0\.0\.1|localhost|::1|\[::1\])$/i.test(host) || host.endsWith('.localhost')) {
    return 'local';
  }
  return null;
}

/** 策略性拒绝：跟随设置（接管模式 / 放行名单 / 最小大小） */
function policyReason(item, st) {
  const host = hostOf(item.url);
  if (st['intercept.mode'] === 'netdisk' && !isNetdiskUrl(item.url)) return 'not-netdisk';
  for (const d of st['intercept.domainsAllow'] || []) {
    if (d && (host === d || host.endsWith('.' + d))) return 'allowlisted';
  }
  const min = Number(st['intercept.minSize']) || 0;
  const size = Number(item.totalBytes) > 0
    ? Number(item.totalBytes)
    : Number(item.fileSize) || 0;
  if (min > 0 && size > 0 && size < min) return 'too-small';
  return null;
}

/** 3 秒内同 URL 去重（浏览器有时会连发两次 onCreated） */
function isDuplicate(url) {
  const now = Date.now();
  for (const [k, t] of recentUrls) if (now - t > DEDUPE_MS) recentUrls.delete(k);
  const last = recentUrls.get(url);
  if (last && now - last < DEDUPE_MS) return true;
  recentUrls.set(url, now);
  return false;
}

// ---------------- 记录 ----------------

function recordOf(item) {
  const url = item.finalUrl || item.url || '';
  const filename = baseName(item.filename) || nameFromUrl(url);
  const size = Number(item.totalBytes) > 0 ? Number(item.totalBytes) : 0;
  return {
    id: item.id,
    url: item.url || '',
    finalUrl: item.finalUrl || '',
    filename,
    size,
    mime: item.mime || '',
    referrer: item.referrer || '',
    platform: inferPlatform(url, item.referrer || ''),
    ts: Date.now(),
  };
}

async function cookiesFor(url) {
  try {
    const list = await chrome.cookies.getAll({ url });
    const out = {};
    for (const c of list) out[c.name] = c.value;
    return out;
  } catch {
    // 没有该域名的 host 权限时 Chrome 会抛错 —— 普通 CDN 一般不需要 cookie
    return {};
  }
}

// ---------------- 转交 ----------------

async function transfer(rec, { fallback = true } = {}) {
  const url = rec.finalUrl || rec.url;
  const platform = rec.platform || inferPlatform(url, rec.referrer);
  const filename = rec.filename || nameFromUrl(url);
  const headers = {};
  if (rec.referrer) headers['Referer'] = rec.referrer;
  const cookies = await cookiesFor(url);

  const task = {
    version: 1,
    platform,
    title: filename,
    items: [{
      url,
      filename,
      size: rec.size || 0,
      md5: '',
      sha1: '',
      cookies,
      headers,
      referrer: rec.referrer || '',
    }],
  };

  try {
    await sendToDownloader(task);
    return true;
  } catch (e) {
    console.warn('[DLink] 转交下载器失败', e);
    if (fallback) {
      try {
        await chrome.downloads.download({
          url,
          filename: filename || undefined,
        });
        notify('已回退到浏览器下载', 'DLink 没能接手，文件改由浏览器下载。');
      } catch (e2) {
        notify('接管失败', `转交 DLink 失败，且回退浏览器下载也失败了：${e2}`);
      }
    }
    return false;
  }
}

async function cancelDownload(id) {
  try { await chrome.downloads.cancel(id); } catch { /* 已完成/已取消 */ }
  try { await chrome.downloads.removeFile(id); } catch { /* 无临时文件 */ }
  try { await chrome.downloads.erase({ id }); } catch { /* ignore */ }
}

// ---------------- 首次询问 ----------------

async function openPrompt() {
  if (promptWindowId != null) {
    try {
      await chrome.windows.update(promptWindowId, { focused: true });
      return true;
    } catch {
      promptWindowId = null;
    }
  }
  try {
    const w = await chrome.windows.create({
      url: chrome.runtime.getURL(PROMPT_FILE),
      type: 'popup',
      width: 520,
      height: 640,
      focused: true,
    });
    promptWindowId = w.id;
    return true;
  } catch (e) {
    console.warn('[DLink] 打不开提示窗口', e);
    return false;
  }
}

async function askFirstTime(item) {
  try {
    await chrome.downloads.pause(item.id);
  } catch (e) {
    // 已经下完 / 无法暂停 —— 不打扰用户，直接放行
    console.warn('[DLink] 无法暂停下载，跳过询问', e);
    return;
  }
  const list = await readPending();
  list.push(recordOf(item));
  if (list.length > MAX_PENDING) list.splice(0, list.length - MAX_PENDING);
  await writePending(list);

  if (!(await openPrompt())) {
    // 弹窗起不来 → 立刻恢复，绝不把下载卡死
    await resumeAll();
  }
}

async function resumeAll() {
  const list = await readPending();
  await writePending([]);
  for (const p of list) {
    try { await chrome.downloads.resume(p.id); } catch { /* ignore */ }
  }
  return list.length;
}

// ---------------- 接管 ----------------

async function takeover(item, st) {
  const port = await findDownloader();
  if (!port) {
    log('downloader-offline', item);
    return;
  }
  const rec = recordOf(item);
  await cancelDownload(item.id);
  if (await transfer(rec, { fallback: true })) {
    flashBadge('↓');
    if (!st['intercept.silent']) {
      notify('已转交 DLink', `${rec.filename} 正在由 DLink 下载。`);
    }
  }
}

// ---------------- 事件入口 ----------------

async function handleCreated(item) {
  const st = await getSettings();

  const basic = basicReason(item);
  if (basic) return log(basic, item);

  // 首次：只在网盘下载时问，避免下个普通文件就弹窗
  if (!st['intercept.asked']) {
    if (!isNetdiskUrl(item.url)) return;
    if (isDuplicate(item.url)) return;
    await askFirstTime(item);
    return;
  }

  if (!st['intercept.enabled']) return;

  // 「本次放行」在下一次 http 下载上生效（不管是不是网盘）
  if (st['intercept.skipOnce']) {
    await setSettings({ 'intercept.skipOnce': false });
    log('skip-once', item);
    return;
  }

  const policy = policyReason(item, st);
  if (policy) return log(policy, item);

  if (isDuplicate(item.url)) return;
  await takeover(item, st);
}

/** 提示框回传决策 */
export async function applyDecision(msg) {
  const pending = await readPending();
  await writePending([]);

  const accept = !!msg.accept;
  const mode = msg.mode === 'all' ? 'all' : 'netdisk';
  await setSettings({
    'intercept.asked': true,
    'intercept.enabled': accept,
    'intercept.mode': mode,
  });

  let transferred = 0;
  let resumed = 0;
  for (const p of pending) {
    if (accept) {
      await cancelDownload(p.id);
      if (await transfer(p, { fallback: true })) transferred++;
    } else {
      try {
        await chrome.downloads.resume(p.id);
        resumed++;
      } catch (e) {
        console.warn('[DLink] 恢复下载失败（可能已自行完成）', p.id, e);
      }
    }
  }
  return { asked: pending.length, transferred, resumed, mode };
}

/** 供 background.js 分发的消息；返回 null 表示不归本模块管 */
export async function handleInterceptMessage(msg) {
  if (!msg || typeof msg.type !== 'string') return null;
  if (msg.type === 'intercept-decision') {
    return { ok: true, data: await applyDecision(msg) };
  }
  if (msg.type === 'intercept-status') {
    const st = await getSettings();
    return { ok: true, data: { settings: st, pending: (await readPending()).length } };
  }
  if (msg.type === 'intercept-pending') {
    return { ok: true, data: await readPending() };
  }
  return null;
}

export function initIntercept() {
  if (!chrome.downloads || !chrome.downloads.onCreated) {
    console.warn('[DLink] 缺少 downloads 权限，接管功能不可用');
    return;
  }

  chrome.downloads.onCreated.addListener((item) => {
    handleCreated(item).catch((e) => console.warn('[DLink] 接管流程出错', e));
  });

  // 用户自己取消/失败时，把待决记录清掉
  chrome.downloads.onChanged.addListener(async (delta) => {
    if (!delta || !delta.id) return;
    const list = await readPending();
    if (!list.some((p) => p.id === delta.id)) return;
    if (delta.state && (delta.state.current === 'interrupted'
      || delta.state.current === 'complete')) {
      await writePending(list.filter((p) => p.id !== delta.id));
    }
  });

  // 直接关掉提示窗口 = 保持浏览器下载（不丢文件）
  chrome.windows.onRemoved.addListener(async (winId) => {
    if (winId !== promptWindowId) return;
    promptWindowId = null;
    const list = await readPending();
    if (!list.length) return;
    await writePending([]);
    await setSettings({ 'intercept.asked': true, 'intercept.enabled': false });
    for (const p of list) {
      try { await chrome.downloads.resume(p.id); } catch { /* ignore */ }
    }
  });
}

// 测试用（node 里 import 时不报错）
export const __test__ = {
  basicReason, policyReason, baseName, nameFromUrl, recordOf,
};

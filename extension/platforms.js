/**
 * 平台解析器集合。
 *
 * 核心原则：**所有平台 API 调用都在目标页面的上下文里发起**。
 * 原因（实测得出）：
 *   1. 百度/夸克/微云的登录态是 HttpOnly 或与 TLS 指纹/OA 绑定，
 *      service worker 里 fetch 拿不到、也过不了风控。
 *   2. 平台自己的页面已经带着合法会话，在页面里 `fetch(..., {credentials:'include'})`
 *      同源、免 CORS、天然带 cookie。
 *
 * 所以每个平台都通过 `chrome.scripting.executeScript` 注入执行，
 * 而不是在 background 里直接请求。
 *
 * 这个文件同时负责「域名 → 平台」的映射，供两处使用：
 *   · 分享页 URL（detectPlatform）—— popup 提取直链时
 *   · CDN 直链 URL（platformForDownloadUrl）—— 接管浏览器下载时
 *
 * 后者是**必须**的：接管到的是 `d.pcs.baidu.com/...` 这类 CDN 地址，
 * 如果一律按 direct（8 路并发）跑，百度会触发账号级限速掉到 9 KB/s
 * （见 docs/PLATFORMS.md 实测数据）。
 */

export const PLATFORMS = {
  baidu: {
    id: 'baidu',
    name: '百度网盘',
    host: /^pan\.baidu\.com$|^yun\.baidu\.com$/,
    shareUrl: /\/s\/([A-Za-z0-9_-]+)/,
  },
  quark: {
    id: 'quark',
    name: '夸克网盘',
    host: /^pan\.quark\.cn$/,
    shareUrl: /\/s\/([A-Za-z0-9]+)/,
  },
  uc: {
    id: 'uc',
    name: 'UC 网盘',
    host: /^drive\.uc\.cn$|^fast\.uc\.cn$/,
    shareUrl: /\/s\/([A-Za-z0-9]+)/,
  },
  weiyun: {
    id: 'weiyun',
    name: '腾讯微云',
    host: /^share\.weiyun\.com$/,
    shareUrl: /\/([A-Za-z0-9]{8,})/,
  },
  cloudreve: {
    id: 'cloudreve',
    // 自建站，域名不固定 —— host 写成永不匹配，靠 URL 里的 cloudreve:// 识别
    host: /$^/,
    shareUrl: /cloudreve:\/\/([A-Za-z0-9]+)@|\/s\/([A-Za-z0-9]+)/,
  },
};

/** 平台 id → 中文名（含 direct 兜底），给 UI 用 */
export const PLATFORM_NAMES = {
  baidu: '百度网盘',
  quark: '夸克网盘',
  uc: 'UC 网盘',
  weiyun: '腾讯微云',
  cloudreve: 'Cloudreve',
  direct: '普通链接',
};

/**
 * CDN 直链域名 → 平台。
 *
 * ⚠️ CDN 域名会变，且前缀随机（实测夸克是 `dl-pc-zb-j.drive.quark.cn`），
 *    所以正则一律写成 `*.drive.quark.cn` 这种宽松形式，兜底 `direct`。
 *    新增平台时，这里必须加一行（见 docs/PLATFORMS.md 接入清单）。
 */
const DOWNLOAD_HOST_MAP = [
  { re: /(^|\.)pcs\.baidu\.com$/i, platform: 'baidu' },
  { re: /(^|\.)baidupcs\.com$/i, platform: 'baidu' },
  { re: /(^|\.)drive\.quark\.cn$/i, platform: 'quark' },
  { re: /(^|\.)quark\.cn$/i, platform: 'quark' },
  { re: /(^|\.)drive\.uc\.cn$/i, platform: 'uc' },
  { re: /(^|\.)uc\.cn$/i, platform: 'uc' },
  { re: /(^|\.)weiyun\.com$/i, platform: 'weiyun' },
  // Cloudreve 站点常用 Cloudflare R2 存储；直链是预签名 URL，可安全多线程
  { re: /(^|\.)r2\.cloudflarestorage\.com$/i, platform: 'cloudreve' },
];

function safeUrl(url) {
  try {
    return new URL(url);
  } catch {
    return null;
  }
}

/** 取 hostname（解析失败返回空串，不抛异常） */
export function hostOf(url) {
  const u = safeUrl(url);
  return u ? u.hostname.toLowerCase() : '';
}

/** 这个 host 是不是已知网盘（含 CDN）？ —— 决定「只接管网盘」模式下是否动手 */
export function isNetdiskHost(host) {
  const h = String(host || '').toLowerCase();
  if (!h) return false;
  for (const p of Object.values(PLATFORMS)) if (p.host.test(h)) return true;
  for (const m of DOWNLOAD_HOST_MAP) if (m.re.test(h)) return true;
  return false;
}

/** 这个 URL 是不是已知网盘（含 CDN）？ */
export function isNetdiskUrl(url) {
  if (looksLikeCloudreve(url)) return true;
  return isNetdiskHost(hostOf(url));
}

export function detectPlatform(url) {
  const u = safeUrl(url);
  if (!u) return null;
  // Cloudreve 是自建站，域名任意；靠 URL 里的 cloudreve://<shareId>@share 识别。
  // 注意这个串可能被编码成 cloudreve%3A%2F%2F（实测分享链接就是这种形态）。
  if (looksLikeCloudreve(url)) return PLATFORMS.cloudreve;
  for (const p of Object.values(PLATFORMS)) {
    // host 为 /$^/ 的平台（cloudreve）靠上面显式判断，这里跳过
    if (p.host && p.host.source !== '$^' && p.host.test(u.hostname)) return p;
  }
  return null;
}

/** 这个 URL 是不是 Cloudreve 分享页（编码/未编码都认） */
export function looksLikeCloudreve(url) {
  const raw = String(url || '');
  if (!raw) return false;
  let decoded = raw;
  try { decoded = decodeURIComponent(raw); } catch { /* 保持原样 */ }
  return /cloudreve:\/\/[A-Za-z0-9]+@/i.test(decoded)
    || /cloudreve%3A%2F%2F/i.test(raw);
}

/**
 * 由**下载地址**推断平台（接管场景的主力函数）。
 * 拿不准就返回 'direct' —— 宁可少并发，也不要给百度上 8 路。
 */
export function platformForDownloadUrl(url) {
  const h = hostOf(url);
  if (!h) return 'direct';
  for (const m of DOWNLOAD_HOST_MAP) if (m.re.test(h)) return m.platform;
  return 'direct';
}

/**
 * 综合推断：先看下载地址本身，再看 Referer（分享页域名）。
 * CDN 域名没被覆盖时，Referer 往往还留在分享页，能救回来。
 */
export function inferPlatform(url, referrer) {
  const byUrl = platformForDownloadUrl(url);
  if (byUrl !== 'direct') return byUrl;
  const p = detectPlatform(referrer || '');
  if (p) return p.id;
  return 'direct';
}

/** 解析分享 ID */
export function parseShareId(url, platform) {
  const u = safeUrl(url);
  if (!u) return null;
  // Cloudreve 的 shareId 在**查询串**里（?path=cloudreve://<id>@share），
  // 不在 pathname 上，所以单独处理。
  if (platform && platform.id === 'cloudreve') {
    const raw = String(url || '');
    let dec = raw;
    try { dec = decodeURIComponent(raw); } catch { /* 保持原样 */ }
    const m = dec.match(/cloudreve:\/\/([A-Za-z0-9]+)@/)
      || u.pathname.match(/\/s\/([A-Za-z0-9]+)/);
    return m ? m[1] : null;
  }
  const m = u.pathname.match(platform.shareUrl);
  return m ? m[1] : null;
}

/** 解析提取码（?pwd= 或 #pwd=） */
export function parsePwd(url) {
  const u = safeUrl(url);
  if (!u) return null;
  const q = u.searchParams.get('pwd') || u.searchParams.get('passcode');
  if (q) return q;
  const frag = u.hash || '';
  const m = frag.match(/pwd=([A-Za-z0-9]{4})/);
  return m ? m[1] : null;
}

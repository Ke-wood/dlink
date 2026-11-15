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
};

export function detectPlatform(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  for (const p of Object.values(PLATFORMS)) {
    if (p.host.test(u.hostname)) return p;
  }
  return null;
}

/** 解析分享 ID */
export function parseShareId(url, platform) {
  const u = new URL(url);
  const m = u.pathname.match(platform.shareUrl);
  return m ? m[1] : null;
}

/** 解析提取码（?pwd= 或 #pwd=） */
export function parsePwd(url) {
  const u = new URL(url);
  const q = u.searchParams.get('pwd') || u.searchParams.get('passcode');
  if (q) return q;
  const frag = u.hash || '';
  const m = frag.match(/pwd=([A-Za-z0-9]{4})/);
  return m ? m[1] : null;
}

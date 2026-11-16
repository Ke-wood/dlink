/**
 * 扩展设置读写。
 *
 * 存 `chrome.storage.sync`（跟随账号同步），失败回退 `chrome.storage.local`
 * （某些企业策略/未登录同步时 sync 不可用）。
 *
 * 键名带 `intercept.` 前缀，避免以后加别的功能时撞名。
 * 新增键必须同时改 DEFAULT_SETTINGS 和 coerceSettings，否则读出来是
 * undefined，过滤逻辑会静默失效。
 */

export const DEFAULT_SETTINGS = Object.freeze({
  'intercept.enabled': false, // 总开关：是否接管浏览器下载
  'intercept.asked': false, // 是否问过（首次只弹一次）
  'intercept.mode': 'netdisk', // 'netdisk' 只接管网盘 | 'all' 全部接管
  'intercept.domainsAllow': [], // 永不接管的域名（放行名单）
  'intercept.minSize': 0, // 小于此字节数不接管（0 = 不限）
  'intercept.silent': false, // 接管时不弹通知
  'intercept.skipOnce': false, // 一次性放行下一个下载
});

const VALUES = {
  'intercept.mode': ['netdisk', 'all'],
};

function area() {
  // 同步存储优先；不可用则退到本地，保证功能不因同步失败而挂掉
  try {
    if (chrome.storage.sync) return chrome.storage.sync;
  } catch { /* ignore */ }
  return chrome.storage.local;
}

/** 把任意输入洗成合法值，防止 UI 传入字符串 "false" / NaN 之类 */
export function coerceSettings(patch) {
  const out = {};
  for (const [k, v] of Object.entries(patch || {})) {
    if (!(k in DEFAULT_SETTINGS)) continue;
    const def = DEFAULT_SETTINGS[k];
    if (Array.isArray(def)) {
      out[k] = Array.isArray(v)
        ? v.filter((x) => x != null)
          .map((x) => String(x).trim().toLowerCase()).filter(Boolean)
        : String(v || '').split(/[\s,]+/).map((x) => x.trim().toLowerCase()).filter(Boolean);
    } else if (typeof def === 'boolean') {
      out[k] = v === true || v === 'true' || v === 1 || v === '1';
    } else if (typeof def === 'number') {
      const n = Number(v);
      out[k] = Number.isFinite(n) && n >= 0 ? Math.floor(n) : def;
    } else if (VALUES[k]) {
      out[k] = VALUES[k].includes(v) ? v : def;
    } else {
      out[k] = String(v ?? def);
    }
  }
  return out;
}

/** 读全部设置（缺失的键自动补默认值） */
export async function getSettings() {
  let store = {};
  try {
    store = await area().get(DEFAULT_SETTINGS);
  } catch (e) {
    console.warn('[DLink] 读设置失败，用默认值', e);
  }
  return { ...DEFAULT_SETTINGS, ...coerceSettings(store) };
}

/** 写设置（只写传入的键） */
export async function setSettings(patch) {
  const clean = coerceSettings(patch);
  if (!Object.keys(clean).length) return {};
  try {
    await area().set(clean);
  } catch (e) {
    console.warn('[DLink] 写设置失败', e);
    // sync 失败再试 local 一次
    try {
      await chrome.storage.local.set(clean);
    } catch { /* 放弃 */ }
  }
  return clean;
}

/** 恢复首次询问（设置页按钮） */
export async function resetFirstRun() {
  return setSettings({ 'intercept.asked': false });
}

/** 监听设置变化：cb(改变后的完整设置, 变化的键) */
export function onSettingsChanged(cb) {
  chrome.storage.onChanged.addListener(async (changes, areaName) => {
    if (areaName !== 'sync' && areaName !== 'local') return;
    const keys = Object.keys(changes);
    if (!keys.some((k) => k in DEFAULT_SETTINGS)) return;
    cb(await getSettings(), keys);
  });
}

/** 选项页：读写 chrome.storage.sync，并展示 DLink 下载器状态 */

import { getSettings, setSettings, resetFirstRun } from './settings.js';
import { getHealth, getRemoteSettings } from './downloader.js';

const $ = (id) => document.getElementById(id);
const MB = 1024 * 1024;

let toastTimer = null;
function toast(text) {
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 1600);
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v <= 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return x.toFixed(x >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
}

function render(st) {
  $('enabled').checked = !!st['intercept.enabled'];
  $('silent').checked = !!st['intercept.silent'];
  const mode = st['intercept.mode'] === 'all' ? 'all' : 'netdisk';
  for (const el of document.querySelectorAll('input[name=mode]')) {
    el.checked = el.value === mode;
  }
  $('domains').value = (st['intercept.domainsAllow'] || []).join('\n');
  const min = Number(st['intercept.minSize']) || 0;
  $('minSize').value = min > 0 ? String(Math.round(min / MB)) : '0';
}

async function load() {
  $('ver').textContent = 'v' + chrome.runtime.getManifest().version;
  render(await getSettings());
}

// ---------------- 变更保存 ----------------

$('enabled').addEventListener('change', async (e) => {
  const on = e.target.checked;
  await setSettings({ 'intercept.enabled': on, 'intercept.asked': true });
  toast(on ? '已开启接管' : '已关闭接管');
});

for (const el of document.querySelectorAll('input[name=mode]')) {
  el.addEventListener('change', async (e) => {
    if (!e.target.checked) return;
    await setSettings({ 'intercept.mode': e.target.value });
    toast(e.target.value === 'all' ? '已改为接管所有下载' : '已改为只接管网盘');
  });
}

$('silent').addEventListener('change', async (e) => {
  await setSettings({ 'intercept.silent': e.target.checked });
  toast(e.target.checked ? '已静默接管（不弹通知）' : '已开启接管通知');
});

$('domains').addEventListener('change', async (e) => {
  const list = e.target.value.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);
  await setSettings({ 'intercept.domainsAllow': list });
  toast(list.length ? `已保存 ${list.length} 条放行域名` : '放行域名已清空');
});

$('minSize').addEventListener('change', async (e) => {
  const mb = Math.max(0, Number(e.target.value) || 0);
  e.target.value = String(Math.round(mb));
  await setSettings({ 'intercept.minSize': Math.round(mb * MB) });
  toast(mb > 0 ? `小于 ${mb} MB 不接管` : '不限最小大小');
});

// ---------------- 按钮 ----------------

$('btn-skip').addEventListener('click', async () => {
  await setSettings({ 'intercept.skipOnce': true });
  toast('下一个下载将放行给浏览器');
});

$('btn-reset').addEventListener('click', async () => {
  await resetFirstRun();
  toast('下次下载网盘文件时会重新询问');
});

function renderPerm(granted) {
  const btn = $('btn-perm');
  btn.textContent = granted ? '已授权读取所有站点 Cookie（点击撤销）' : '允许读取所有站点 Cookie（可选）';
}

$('btn-perm').addEventListener('click', async () => {
  try {
    const granted = await chrome.permissions.contains({ origins: ['<all_urls>'] });
    if (granted) {
      const ok = await chrome.permissions.remove({ origins: ['<all_urls>'] });
      renderPerm(!ok);
      toast(ok ? '已撤销该权限' : '撤销失败');
      return;
    }
    const ok = await chrome.permissions.request({ origins: ['<all_urls>'] });
    renderPerm(!!ok);
    toast(ok ? '已授权' : '未授权（不影响网盘下载）');
  } catch (e) {
    toast('操作失败：' + (e.message || e));
  }
});

// ---------------- 下载器状态 ----------------

let polling = null;

async function refreshStatus() {
  const health = await getHealth();
  if (!health) {
    $('dot').className = 'dot off';
    $('dlstate').textContent = '未运行';
    $('dlport').textContent = '—';
    $('dlqueue').textContent = '—';
    $('savedir').textContent = '—';
    return;
  }
  $('dot').className = 'dot on';
  $('dlstate').textContent = `在线（v${health.version || '?'}）`;
  $('dlport').textContent = String(health.port);
  const q = health.queue || {};
  $('dlqueue').textContent = [
    q.running ? `${q.running} 个下载中` : '',
    q.queued ? `${q.queued} 个排队` : '',
  ].filter(Boolean).join('，') || '空闲';
  const rs = await getRemoteSettings();
  $('savedir').textContent = (rs && rs.save_dir) || '—';
}

chrome.permissions.contains({ origins: ['<all_urls>'] })
  .then(renderPerm)
  .catch(() => renderPerm(false));

load();
refreshStatus();
polling = setInterval(refreshStatus, 4000);
window.addEventListener('unload', () => { if (polling) clearInterval(polling); });

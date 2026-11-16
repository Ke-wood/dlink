/** popup 交互逻辑 */

import { getSettings, setSettings } from './settings.js';

const $ = (id) => document.getElementById(id);
let currentResult = null;

function setStatus(text, kind) {
  const el = $('status');
  el.textContent = text;
  el.className = kind || '';
}

function setHint(text) {
  const el = $('hint');
  if (text) {
    el.textContent = '→ ' + text;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function fmtSize(n) {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
}

function renderList(items) {
  const box = $('list');
  box.innerHTML = '';
  for (const it of items) {
    const d = document.createElement('div');
    d.className = 'file ' + (it.status === 'ok' ? 'ok' : 'failed');
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = it.name || '(未命名)';
    d.appendChild(name);

    const meta = document.createElement('div');
    meta.className = 'meta';
    if (it.status === 'ok') {
      const bits = [fmtSize(it.size)];
      if (it.md5) bits.push('md5 ' + it.md5.slice(0, 8));
      if (it.transferred) bits.push('已转存到网盘根目录，请自行删除');
      meta.textContent = bits.filter(Boolean).join(' · ');
    } else {
      meta.textContent = '提取失败';
    }
    d.appendChild(meta);

    if (it.status !== 'ok' && it.reason) {
      const err = document.createElement('div');
      err.className = 'err';
      err.textContent = it.reason;
      d.appendChild(err);
    }
    box.appendChild(d);
  }
}

async function checkDownloader() {
  const el = $('dl-status');
  try {
    const r = await chrome.runtime.sendMessage({ type: 'ping-downloader' });
    if (r && r.ok && r.data && r.data.port) {
      el.textContent = '下载器在线 :' + r.data.port;
      el.className = 'badge';
      return true;
    }
  } catch { /* fallthrough */ }
  el.textContent = '下载器未运行';
  el.className = 'badge off';
  return false;
}

$('btn-resolve').addEventListener('click', async () => {
  const btn = $('btn-resolve');
  btn.disabled = true;
  setHint('');
  setStatus('正在解析…');
  $('list').innerHTML = '';
  try {
    const r = await chrome.runtime.sendMessage({ type: 'resolve' });
    if (!r.ok) {
      setStatus('解析失败：' + r.error, 'err');
      if (r.hint) setHint(r.hint);
      return;
    }
    currentResult = r.data;
    const okCount = r.data.items.filter((x) => x.status === 'ok').length;
    setStatus(
      `${r.data.platformName}：${okCount}/${r.data.items.length} 个文件就绪`,
      okCount > 0 ? 'ok' : 'err');
    renderList(r.data.items);
    $('btn-send').disabled = okCount === 0;
  } catch (e) {
    setStatus('出错：' + (e.message || e), 'err');
  } finally {
    btn.disabled = false;
  }
});

$('btn-clear').addEventListener('click', () => {
  currentResult = null;
  $('list').innerHTML = '';
  setStatus('已清空');
  setHint('');
  $('btn-send').disabled = true;
});

$('btn-send').addEventListener('click', async () => {
  if (!currentResult) return;
  const okItems = currentResult.items.filter((x) => x.status === 'ok');
  if (!okItems.length) return;

  const btn = $('btn-send');
  btn.disabled = true;
  setStatus('发送中…');

  const task = {
    version: 1,
    platform: currentResult.platform,
    title: okItems.length === 1 ? okItems[0].name : `${okItems.length} 个文件`,
    items: okItems.map((it) => ({
      url: it.url,
      filename: it.name,
      size: it.size,
      md5: it.md5,
      sha1: it.sha1,
      cookies: it.cookies,
      headers: it.headers,
      refresh: it.refresh,
    })),
  };

  try {
    const r = await chrome.runtime.sendMessage({ type: 'send', task });
    if (r.ok) {
      setStatus(`已发送 ${okItems.length} 个任务到下载器`, 'ok');
      setHint('');
    } else {
      setStatus('发送失败：' + r.error, 'err');
      if (r.hint) setHint(r.hint);
      btn.disabled = false;
    }
  } catch (e) {
    setStatus('发送出错：' + (e.message || e), 'err');
    btn.disabled = false;
  }
});

// ---------------- 顶部接管开关 ----------------

function renderModeSub(st) {
  const on = $('toggle-intercept').checked;
  $('mode-sub').textContent = on
    ? (st['intercept.mode'] === 'all' ? '已开启：接管所有下载' : '已开启：只接管网盘链接')
    : '关闭时浏览器下载不受影响';
}

async function initToggle() {
  try {
    const st = await getSettings();
    $('toggle-intercept').checked = !!st['intercept.enabled'];
    renderModeSub(st);
  } catch (e) {
    console.warn('[DLink] 读取接管开关失败', e);
  }
}

$('toggle-intercept').addEventListener('change', async (e) => {
  const on = e.target.checked;
  const patch = { 'intercept.enabled': on };
  // 用户主动开关过，就不再弹首次询问窗口
  if (on) patch['intercept.asked'] = true;
  await setSettings(patch);
  renderModeSub(await getSettings());
  setStatus(on ? '已开启接管' : '已关闭接管', on ? 'ok' : '');
});

$('btn-options').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
});

// 打开时检测下载器
initToggle();
checkDownloader();
setInterval(checkDownloader, 3000);

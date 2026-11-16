/**
 * 首次接管提示窗口。
 *
 * 只做三件事：展示当前下载、把「接管 / 不接管」告诉 background、
 * 展示「以后在哪里改」。真正的暂停/取消/转交都在 intercept.js。
 *
 * 关掉窗口 = 保持浏览器下载（background 监听 windows.onRemoved 后 resume）。
 *
 * 采用独立小窗而不是 chrome.notifications：通知的文字长度和按钮数
 * 都不够放「①说明 ②选择 ③以后在哪改」这三块（见交接文档 §2.7）。
 */

import { PLATFORM_NAMES } from './platforms.js';

const $ = (id) => document.getElementById(id);

function fmtSize(n) {
  const v = Number(n) || 0;
  if (v <= 0) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return x.toFixed(x >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
}

function showError(text) {
  const el = $('err');
  el.textContent = text;
  el.style.display = 'block';
}

function setBusy(busy) {
  $('btn-accept').disabled = busy;
  $('btn-decline').disabled = busy;
}

async function loadPending() {
  try {
    const r = await chrome.runtime.sendMessage({ type: 'intercept-pending' });
    const list = (r && r.ok && r.data) || [];
    if (!list.length) {
      $('fname').textContent = '（没有待处理的下载）';
      return;
    }
    const first = list[0] || {};
    $('fname').textContent = first.filename || first.url || '(未命名)';
    const bits = [];
    const size = fmtSize(first.size);
    if (size) bits.push(size);
    if (first.platform) bits.push(PLATFORM_NAMES[first.platform] || '普通链接');
    if (list.length > 1) bits.push(`等 ${list.length} 个文件`);
    $('fmeta').textContent = bits.join(' · ');
  } catch (e) {
    $('fname').textContent = '（读取失败，仍可正常选择）';
    console.warn('[DLink] 读取待决下载失败', e);
  }
}

async function decide(accept) {
  setBusy(true);
  const modeEl = document.querySelector('input[name=mode]:checked');
  const mode = modeEl ? modeEl.value : 'netdisk';
  try {
    const r = await chrome.runtime.sendMessage({
      type: 'intercept-decision',
      accept,
      mode,
    });
    if (!r || !r.ok) throw new Error((r && r.error) || '后台没有响应');
  } catch (e) {
    showError('设置没有保存成功：' + (e.message || e));
    setBusy(false);
    return;
  }
  window.close();
}

$('btn-accept').addEventListener('click', () => decide(true));
$('btn-decline').addEventListener('click', () => decide(false));
$('open-options').addEventListener('click', (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

loadPending();

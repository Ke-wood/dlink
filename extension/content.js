/**
 * Content script：在分享页注入一个悬浮按钮。
 *
 * 只做入口，不做解析——解析交给 background + injected.js（页面上下文）。
 * 不劫持任何原生下载行为。
 */

(() => {
  if (window.__DLINK_BTN__) return;
  window.__DLINK_BTN__ = true;

  const btn = document.createElement('div');
  btn.id = '__dlink_fab__';
  btn.textContent = '↓ 提取直链';
  btn.style.cssText = `
    position: fixed; right: 20px; bottom: 20px; z-index: 2147483646;
    padding: 10px 16px; border-radius: 22px;
    background: #2f6fdb; color: #fff; font: 500 13px/1 system-ui, sans-serif;
    cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,.35);
    user-select: none; transition: transform .15s, background .15s;
  `;
  btn.onmouseenter = () => { btn.style.background = '#3b7eeb'; };
  btn.onmouseleave = () => { btn.style.background = '#2f6fdb'; };
  btn.onclick = async () => {
    const old = btn.textContent;
    btn.textContent = '解析中…';
    btn.style.background = '#555';
    try {
      const r = await chrome.runtime.sendMessage({ type: 'resolve' });
      if (r.ok) {
        const ok = r.data.items.filter((x) => x.status === 'ok').length;
        btn.textContent = `✓ ${ok} 个就绪，点扩展图标`;
        setTimeout(() => { btn.textContent = old; btn.style.background = '#2f6fdb'; }, 2600);
      } else {
        btn.textContent = '✗ ' + (r.error || '失败').slice(0, 20);
        setTimeout(() => { btn.textContent = old; btn.style.background = '#2f6fdb'; }, 3200);
      }
    } catch (e) {
      btn.textContent = '✗ ' + String(e).slice(0, 20);
      setTimeout(() => { btn.textContent = old; btn.style.background = '#2f6fdb'; }, 3200);
    }
  };

  const attach = () => {
    if (!document.body) return setTimeout(attach, 300);
    document.body.appendChild(btn);
  };
  attach();
})();

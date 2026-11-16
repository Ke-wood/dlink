/**
 * 接管流程集成测试：用假的 chrome API 跑完整链路。
 *
 *   node --test tests/js/extension.test.mjs tests/js/intercept-flow.test.mjs
 *
 * 这里验证的是「流程对不对」，不是「语法过不过」：
 *   首次询问 → 暂停 → 拒绝则 resume / 接受则 cancel+转交
 *   已开启 → cancel + POST /api/task（平台、文件名、Referer 都要对）
 *   DLink 离线 → 放行（绝不拦下来又发不出去）
 *   转交失败 → 回退 chrome.downloads.download
 *   关掉提示窗口 → resume（不丢文件）
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// ---------------- 假 chrome ----------------

const REF = { chrome: null };

function makeChrome() {
  const box = { sync: {}, session: {} };
  const calls = {
    pause: [], resume: [], cancel: [], erase: [], removeFile: [],
    download: [], windows: [], badge: [], notify: [],
  };
  const listeners = { created: [], changed: [], removed: [] };
  const openWindows = new Set();
  let nextWinId = 100;

  const area = (store) => ({
    async get(keys) {
      if (keys == null) return { ...store };
      // chrome.storage.get 也接受单个字符串键
      if (typeof keys === 'string') keys = [keys];
      if (Array.isArray(keys)) {
        const out = {};
        for (const k of keys) if (k in store) out[k] = store[k];
        return out;
      }
      const out = {};
      for (const k of Object.keys(keys)) out[k] = (k in store) ? store[k] : keys[k];
      return out;
    },
    async set(patch) { Object.assign(store, patch); },
  });

  const chrome = {
    runtime: {
      id: 'self-id',
      getURL: (p) => 'chrome-extension://self-id/' + p,
      onMessage: { addListener() {} },
    },
    storage: { sync: area(box.sync), session: area(box.session), local: area({}) },
    downloads: {
      onCreated: { addListener: (f) => listeners.created.push(f) },
      onChanged: { addListener: (f) => listeners.changed.push(f) },
      async pause(id) { calls.pause.push(id); },
      async resume(id) { calls.resume.push(id); },
      async cancel(id) { calls.cancel.push(id); },
      async erase(q) { calls.erase.push(q.id); },
      async removeFile(id) { calls.removeFile.push(id); },
      async download(opt) { calls.download.push(opt); return 1; },
    },
    windows: {
      async create(opt) {
        calls.windows.push(opt);
        const id = nextWinId++;
        openWindows.add(id);
        return { id };
      },
      async update(id) {
        // 真实 Chrome 对已关闭的窗口会报错，这里保持一致
        if (!openWindows.has(id)) throw new Error('No window with id: ' + id);
        return {};
      },
      onRemoved: { addListener: (f) => listeners.removed.push(f) },
    },
    cookies: {
      async getAll() { return [{ name: 'BDUSS', value: 'ck' }]; },
    },
    action: {
      setBadgeText(o) { calls.badge.push(o.text); },
      setBadgeBackgroundColor() {},
    },
    notifications: {
      create(o) { calls.notify.push(o); },
    },
  };

  return { chrome, calls, listeners, box, openWindows };
}

const state = makeChrome();
globalThis.chrome = state.chrome;

// ---------------- 假网络 ----------------

const net = { mode: 'online', tasks: [] };

globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (u.includes('/api/health')) {
    if (net.mode === 'offline') throw new Error('ECONNREFUSED');
    return { ok: true, json: async () => ({ app: 'dlink', version: '0.2.0', queue: {} }) };
  }
  if (u.includes('/api/task')) {
    net.tasks.push(JSON.parse(opts.body));
    if (net.mode === 'reject') return { ok: false, status: 500, text: async () => 'boom' };
    return { ok: true, json: async () => ({ ok: true, accepted: 1, task_ids: ['t'] }) };
  }
  if (u.includes('/api/settings')) {
    return { ok: true, json: async () => ({ settings: { save_dir: 'D:/dl' } }) };
  }
  throw new Error('unexpected fetch ' + u);
};

// 动态 import：必须等 chrome 挂到 globalThis 之后
const { initIntercept, handleInterceptMessage } = await import('../../extension/intercept.js');
initIntercept();

const tick = (ms = 40) => new Promise((r) => setTimeout(r, ms));

let nextId = 1;
function item(over = {}) {
  const id = nextId++;
  return {
    id,
    url: 'https://d.pcs.baidu.com/file/abc?fid=' + id,
    finalUrl: '',
    filename: 'C:\\Users\\x\\Downloads\\a' + id + '.zip',
    totalBytes: 2048,
    mime: 'application/zip',
    referrer: 'https://pan.baidu.com/s/1abc',
    state: 'in_progress',
    ...over,
  };
}

async function fireCreated(it) {
  for (const f of state.listeners.created) f(it);
  await tick();
}

function resetCalls() {
  for (const k of Object.keys(state.calls)) state.calls[k].length = 0;
  net.tasks.length = 0;
}

async function resetAll(extra = {}) {
  resetCalls();
  net.mode = 'online';
  // ⚠️ 必须原地清空/赋值：area(store) 闭包持有的是同一个对象引用
  for (const k of Object.keys(state.box.sync)) delete state.box.sync[k];
  Object.assign(state.box.sync, {
    'intercept.enabled': false,
    'intercept.asked': false,
    'intercept.mode': 'netdisk',
    'intercept.domainsAllow': [],
    'intercept.minSize': 0,
    'intercept.silent': false,
    'intercept.skipOnce': false,
    ...extra,
  });
  for (const k of Object.keys(state.box.session)) delete state.box.session[k];
  state.openWindows.clear();
}

const settings = async () => (await state.chrome.storage.sync.get(null));

// ---------------- 用例 ----------------

test('首次：网盘下载被暂停并弹提示窗口', async () => {
  await resetAll();
  const it = item();
  await fireCreated(it);

  assert.deepEqual(state.calls.pause, [it.id], '应当 pause 而不是 cancel');
  assert.equal(state.calls.cancel.length, 0, '询问阶段不能 cancel');
  assert.equal(state.calls.windows.length, 1);
  assert.match(state.calls.windows[0].url, /prompt\.html$/);
  assert.equal(state.calls.windows[0].type, 'popup');
});

test('首次：非网盘下载不打扰（不弹窗）', async () => {
  await resetAll();
  await fireCreated(item({ url: 'https://example.com/a.zip', referrer: '' }));
  assert.equal(state.calls.pause.length, 0);
  assert.equal(state.calls.windows.length, 0);
});

test('首次选「保持浏览器下载」：resume 且从此不接管', async () => {
  await resetAll();
  const it = item();
  await fireCreated(it);
  const r = await handleInterceptMessage({
    type: 'intercept-decision', accept: false, mode: 'netdisk',
  });
  assert.equal(r.ok, true);
  assert.equal(r.data.resumed, 1);
  assert.deepEqual(state.calls.resume, [it.id]);
  assert.equal(state.calls.cancel.length, 0);

  const st = await settings();
  assert.equal(st['intercept.asked'], true);
  assert.equal(st['intercept.enabled'], false);

  // 之后再下载：不暂停、不弹窗
  resetCalls();
  await fireCreated(item());
  assert.equal(state.calls.pause.length, 0);
  assert.equal(state.calls.windows.length, 0);
});

test('首次选「接管」：cancel + 转交，平台与文件名正确', async () => {
  await resetAll();
  const it = item();
  await fireCreated(it);
  const r = await handleInterceptMessage({
    type: 'intercept-decision', accept: true, mode: 'netdisk',
  });
  assert.equal(r.data.transferred, 1);
  assert.deepEqual(state.calls.cancel, [it.id]);
  assert.equal(net.tasks.length, 1);

  const task = net.tasks[0];
  assert.equal(task.platform, 'baidu', '必须按 CDN 域名推断成 baidu（否则 8 路会被限速）');
  assert.equal(task.items[0].filename, `a${it.id}.zip`, '必须剥掉浏览器给的完整路径');
  assert.equal(task.items[0].headers.Referer, 'https://pan.baidu.com/s/1abc');
  assert.equal(task.items[0].cookies.BDUSS, 'ck');
  assert.equal(task.items[0].size, 2048);

  const st = await settings();
  assert.equal(st['intercept.enabled'], true);
  assert.equal(st['intercept.asked'], true);
});

test('已开启：直接接管，不再弹窗', async () => {
  await resetAll({ 'intercept.enabled': true, 'intercept.asked': true });
  const it = item();
  await fireCreated(it);

  assert.equal(state.calls.windows.length, 0);
  assert.deepEqual(state.calls.cancel, [it.id]);
  assert.equal(net.tasks.length, 1);
  assert.equal(state.calls.badge.at(-1), '↓');
});

test('已开启 + 只接管网盘：普通站点放行', async () => {
  await resetAll({ 'intercept.enabled': true, 'intercept.asked': true });
  await fireCreated(item({ url: 'https://example.com/a.zip', referrer: '' }));
  assert.equal(state.calls.cancel.length, 0);
  assert.equal(net.tasks.length, 0);
});

test('已开启 + 全部接管：普通站点也转交（平台 direct）', async () => {
  await resetAll({
    'intercept.enabled': true, 'intercept.asked': true, 'intercept.mode': 'all',
  });
  await fireCreated(item({ url: 'https://example.com/a.zip', referrer: '' }));
  assert.equal(state.calls.cancel.length, 1);
  assert.equal(net.tasks[0].platform, 'direct');
});

test('DLink 离线：一律放行，不暂停不取消', async () => {
  await resetAll({ 'intercept.enabled': true, 'intercept.asked': true });
  net.mode = 'offline';
  await fireCreated(item());
  assert.equal(state.calls.pause.length, 0);
  assert.equal(state.calls.cancel.length, 0);
  assert.equal(net.tasks.length, 0);
});

test('转交失败：回退到浏览器下载', async () => {
  await resetAll({ 'intercept.enabled': true, 'intercept.asked': true });
  net.mode = 'reject';
  const it = item();
  await fireCreated(it);
  assert.equal(state.calls.download.length, 1, '必须回退，不能把文件弄丢');
  assert.match(state.calls.download[0].url, /pcs\.baidu\.com/);
  assert.ok(state.calls.notify.some((n) => /回退/.test(n.title)));
});

test('本次放行：跳过下一个下载后自动复位', async () => {
  await resetAll({
    'intercept.enabled': true, 'intercept.asked': true, 'intercept.skipOnce': true,
  });
  await fireCreated(item());
  assert.equal(net.tasks.length, 0, '本次不接管');
  assert.equal((await settings())['intercept.skipOnce'], false, '标记要复位');

  await fireCreated(item());
  assert.equal(net.tasks.length, 1, '下一次恢复接管');
});

test('blob: / m3u8 / 本扩展发起：一律放行', async () => {
  await resetAll({ 'intercept.enabled': true, 'intercept.asked': true, 'intercept.mode': 'all' });
  await fireCreated(item({ url: 'blob:https://a.com/uuid' }));
  await fireCreated(item({ url: 'https://a.com/v.m3u8' }));
  await fireCreated(item({ byExtensionId: 'self-id' }));
  assert.equal(net.tasks.length, 0);
  assert.equal(state.calls.cancel.length, 0);
});

test('3 秒内重复 URL 只接管一次', async () => {
  await resetAll({ 'intercept.enabled': true, 'intercept.asked': true, 'intercept.mode': 'all' });
  const url = 'https://cdn.example.com/same.zip?t=1';
  await fireCreated(item({ url, referrer: '' }));
  await fireCreated(item({ url, referrer: '' }));
  assert.equal(net.tasks.length, 1, '连点两次只能产生一个任务');
});

test('关掉提示窗口 = 保持浏览器下载（resume，不丢文件）', async () => {
  await resetAll();
  const it = item();
  await fireCreated(it);
  assert.equal(state.calls.pause.length, 1);

  const winId = [...state.openWindows][0];
  assert.ok(winId, '应当创建过提示窗口');
  state.openWindows.delete(winId);
  for (const f of state.listeners.removed) f(winId);
  await tick();

  assert.deepEqual(state.calls.resume, [it.id]);
  const st = await settings();
  assert.equal(st['intercept.enabled'], false);
  assert.equal(st['intercept.asked'], true);
});

test('设置里的放行域名 / 最小大小生效', async () => {
  await resetAll({
    'intercept.enabled': true,
    'intercept.asked': true,
    'intercept.mode': 'all',
    'intercept.domainsAllow': ['example.com'],
    'intercept.minSize': 1024 * 1024,
  });
  await fireCreated(item({ url: 'https://cdn.example.com/a.zip', referrer: '' }));
  await fireCreated(item({ url: 'https://other.com/b.zip', referrer: '', totalBytes: 100 }));
  assert.equal(net.tasks.length, 0);

  await fireCreated(item({ url: 'https://other.com/c.zip', referrer: '', totalBytes: 5 * 1024 * 1024 }));
  assert.equal(net.tasks.length, 1);
});

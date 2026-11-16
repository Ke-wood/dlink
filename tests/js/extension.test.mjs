/**
 * 扩展纯函数单测（不依赖浏览器，直接跑 node）。
 *
 *   node --test tests/js/
 *
 * 覆盖三块最容易悄悄坏掉、坏了又很难在浏览器里发现的逻辑：
 *   1. CDN 域名 → 平台的推断（错了会让百度被 8 路并发跑成 9 KB/s）
 *   2. 接管过滤（错了会拦下不该拦的下载，或漏拦）
 *   3. 设置值清洗（错了会让 UI 传上来的 "false" 变成 true）
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  platformForDownloadUrl,
  inferPlatform,
  isNetdiskUrl,
  isNetdiskHost,
  detectPlatform,
  PLATFORM_NAMES,
} from '../../extension/platforms.js';

import { DEFAULT_SETTINGS, coerceSettings } from '../../extension/settings.js';

import { __test__ as it } from '../../extension/intercept.js';

// ---------------- 平台推断 ----------------

test('CDN 域名 → 平台', () => {
  const cases = [
    ['https://d.pcs.baidu.com/file/abc?fid=1', 'baidu'],
    ['https://nj02all01.baidupcs.com/file/x', 'baidu'],
    ['https://dl-pc-zb-j.drive.quark.cn/xxx', 'quark'],
    ['https://pan.quark.cn/api/x', 'quark'],
    ['https://pc-api.uc.cn/1/clouddrive/x', 'uc'],
    ['https://abc.weiyun.com/xxx', 'weiyun'],
    ['https://cdn.example.com/a.bin', 'direct'],
    ['https://storage.googleapis.com/a.bin', 'direct'],
  ];
  for (const [url, want] of cases) {
    assert.equal(platformForDownloadUrl(url), want, url);
  }
});

test('非法 URL 兜底 direct，不抛异常', () => {
  for (const bad of ['', 'not-a-url', 'blob:https://a/b', 'javascript:void 0', null]) {
    assert.equal(platformForDownloadUrl(bad), 'direct');
  }
});

test('CDN 认不出时用 Referer 兜底', () => {
  assert.equal(
    inferPlatform('https://cdn.example.com/a.zip', 'https://pan.baidu.com/s/1abc'),
    'baidu');
  assert.equal(
    inferPlatform('https://cdn.example.com/a.zip', ''),
    'direct');
  // 下载地址本身能认出平台时，Referer 不参与
  assert.equal(
    inferPlatform('https://d.pcs.baidu.com/x', 'https://pan.quark.cn/s/1'),
    'baidu');
});

test('网盘域名识别（只接管网盘模式的判断依据）', () => {
  assert.equal(isNetdiskUrl('https://pan.baidu.com/s/1abc'), true);
  assert.equal(isNetdiskUrl('https://d.pcs.baidu.com/file/x'), true);
  assert.equal(isNetdiskUrl('https://example.com/a.zip'), false);
  assert.equal(isNetdiskHost('share.weiyun.com'), true);
  assert.equal(isNetdiskHost(''), false);
  assert.equal(detectPlatform('https://pan.baidu.com/s/1abc').id, 'baidu');
  assert.equal(detectPlatform('https://example.com/'), null);
});

test('每个平台都有中文名（UI 会直接显示）', () => {
  for (const id of ['baidu', 'quark', 'uc', 'weiyun', 'direct']) {
    assert.ok(PLATFORM_NAMES[id], id);
  }
});

// ---------------- 接管过滤 ----------------

const baseItem = (over = {}) => ({
  id: 1,
  url: 'https://d.pcs.baidu.com/file/abc?fid=1',
  finalUrl: '',
  filename: 'C:\\Users\\x\\Downloads\\a.zip',
  totalBytes: 1024,
  mime: 'application/zip',
  referrer: 'https://pan.baidu.com/s/1abc',
  state: 'in_progress',
  ...over,
});

const defaultSettings = { ...DEFAULT_SETTINGS, 'intercept.enabled': true };

test('结构性拒绝项', () => {
  const r = it.basicReason;
  assert.equal(r(baseItem()), null);
  assert.equal(r(baseItem({ url: '' })), 'no-url');
  assert.equal(r(baseItem({ byExtensionId: 'abc' })), 'by-extension');
  assert.equal(r(baseItem({ url: 'blob:https://a/b' })), 'non-http');
  assert.equal(r(baseItem({ url: 'data:text/plain,hi' })), 'non-http');
  assert.equal(r(baseItem({ url: 'https://a.com/v.m3u8' })), 'hls');
  assert.equal(r(baseItem({ url: 'https://a.com/v.mpd?x=1' })), 'hls');
  assert.equal(r(baseItem({ url: 'http://127.0.0.1:8765/x' })), 'local');
  assert.equal(r(baseItem({ url: 'http://localhost/x.zip' })), 'local');
  assert.equal(r(baseItem({ state: 'complete' })), 'complete');
  assert.equal(r(baseItem({ incognito: true })), 'incognito');
});

test('本扩展发起的下载必须放行（否则回退会死循环）', () => {
  // 回退时用 chrome.downloads.download()，byExtensionId 就是自己
  assert.equal(it.basicReason(baseItem({ byExtensionId: 'self-id' })), 'by-extension');
});

test('策略过滤：接管模式 / 放行名单 / 最小大小', () => {
  const p = it.policyReason;
  const st = { ...defaultSettings };

  // 只接管网盘：普通站点放行
  assert.equal(p(baseItem({ url: 'https://example.com/a.zip' }), st), 'not-netdisk');
  // 网盘下载放行
  assert.equal(p(baseItem(), st), null);

  // 全部接管模式
  const all = { ...st, 'intercept.mode': 'all' };
  assert.equal(p(baseItem({ url: 'https://example.com/a.zip' }), all), null);

  // 放行名单（含子域名）
  const allow = { ...all, 'intercept.domainsAllow': ['example.com'] };
  assert.equal(p(baseItem({ url: 'https://example.com/a.zip' }), allow), 'allowlisted');
  assert.equal(p(baseItem({ url: 'https://cdn.example.com/a.zip' }), allow), 'allowlisted');
  assert.equal(p(baseItem({ url: 'https://notexample.com/a.zip' }), allow), null);

  // 最小大小：只对「已知大小」生效
  const min = { ...all, 'intercept.minSize': 1024 * 1024 };
  assert.equal(p(baseItem({ url: 'https://e.com/a.zip', totalBytes: 1000 }), min), 'too-small');
  assert.equal(p(baseItem({ url: 'https://e.com/a.zip', totalBytes: -1 }), min), null);
  assert.equal(p(baseItem({ url: 'https://e.com/a.zip', totalBytes: 0 }), min), null);
});

test('文件名收敛：浏览器给的是完整路径', () => {
  assert.equal(it.baseName('C:\\Users\\x\\Downloads\\a.zip'), 'a.zip');
  assert.equal(it.baseName('/home/x/a.zip'), 'a.zip');
  assert.equal(it.baseName('a<b>c.zip'), 'a_b_c.zip');
  assert.equal(it.baseName(''), '');
  assert.equal(it.nameFromUrl('https://d.pcs.baidu.com/pt/download/%E4%B8%AD%E6%96%87.zip'),
    '中文.zip');
  assert.equal(it.nameFromUrl('https://x.com/'), 'download.bin');
});

test('recordOf 产出可用的任务字段', () => {
  const rec = it.recordOf(baseItem({ finalUrl: 'https://d.pcs.baidu.com/final' }));
  assert.equal(rec.url, 'https://d.pcs.baidu.com/file/abc?fid=1');
  assert.equal(rec.finalUrl, 'https://d.pcs.baidu.com/final');
  assert.equal(rec.filename, 'a.zip');
  assert.equal(rec.platform, 'baidu');
  assert.equal(rec.size, 1024);
  assert.equal(rec.referrer, 'https://pan.baidu.com/s/1abc');
});

// ---------------- 设置清洗 ----------------

test('设置清洗：非法值不能悄悄变成「开启」', () => {
  assert.equal(coerceSettings({ 'intercept.enabled': 'false' })['intercept.enabled'], false);
  assert.equal(coerceSettings({ 'intercept.enabled': 'true' })['intercept.enabled'], true);
  assert.equal(coerceSettings({ 'intercept.enabled': 1 })['intercept.enabled'], true);
  assert.equal(coerceSettings({ 'intercept.enabled': null })['intercept.enabled'], false);
});

test('设置清洗：数字 / 枚举 / 数组', () => {
  assert.equal(coerceSettings({ 'intercept.minSize': '1048576' })['intercept.minSize'], 1048576);
  assert.equal(coerceSettings({ 'intercept.minSize': 'abc' })['intercept.minSize'], 0);
  assert.equal(coerceSettings({ 'intercept.minSize': -5 })['intercept.minSize'], 0);
  assert.equal(coerceSettings({ 'intercept.mode': 'all' })['intercept.mode'], 'all');
  assert.equal(coerceSettings({ 'intercept.mode': 'bogus' })['intercept.mode'], 'netdisk');
  assert.deepEqual(
    coerceSettings({ 'intercept.domainsAllow': 'A.com\nb.com' })['intercept.domainsAllow'],
    ['a.com', 'b.com']);
  assert.deepEqual(
    coerceSettings({ 'intercept.domainsAllow': ['a.com', ' ', null] })['intercept.domainsAllow'],
    ['a.com']);
});

test('设置清洗：不认识的键直接丢弃', () => {
  assert.deepEqual(coerceSettings({ 'intercept.nope': 1 }), {});
});

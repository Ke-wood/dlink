/**
 * Cloudreve 平台识别单测。
 *
 * 为什么要单独测：
 *   Cloudreve 是**自建站**，域名任意（这里实测的是 base-cloud.acgmomo.com），
 *   没法像百度/夸克那样用固定 host 判断。识别一旦出错：
 *     · 识别不到 → 分享页没有「提取直链」入口
 *     · 识别错   → 把普通站当成网盘，接管下载会误伤
 *
 *   识别依据是 URL 里的 `cloudreve://<shareId>@share`（会被编码成
 *   cloudreve%3A%2F%2F...），或页面标题含 Cloudreve。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  detectPlatform,
  platformForDownloadUrl,
  inferPlatform,
  isNetdiskUrl,
  PLATFORM_NAMES,
} from '../../extension/platforms.js';

const SHARE = 'https://base-cloud.acgmomo.com/home?path=cloudreve%3A%2F%2FGB7CY%40share';
const R2 = 'https://acgmomo-private.b86840c9a51d08caa50af32d4d7c99b2.r2.cloudflarestorage.com/uploads/2/repacked/x.7z?X-Amz-Signature=abc';

test('分享页 URL 识别为 cloudreve', () => {
  const p = detectPlatform(SHARE);
  assert.ok(p, '应该识别出平台');
  assert.equal(p.id, 'cloudreve');
});

test('未编码的 cloudreve:// 也认', () => {
  const p = detectPlatform('https://x.test/home?path=cloudreve://ABC@share');
  assert.equal(p?.id, 'cloudreve');
});

test('/s/<id> 且标题含 Cloudreve 才算', () => {
  // node 里没有 document，标题为空 → 不该误判
  assert.equal(detectPlatform('https://x.test/s/GB7CY'), null);
});

test('R2 预签名直链 → cloudreve（决定并发策略）', () => {
  assert.equal(platformForDownloadUrl(R2), 'cloudreve');
  assert.equal(platformForDownloadUrl('https://a.r2.cloudflarestorage.com/x'), 'cloudreve');
});

test('其他 CDN 不受影响', () => {
  assert.equal(platformForDownloadUrl('https://cdn.example.com/a.bin'), 'direct');
  assert.equal(platformForDownloadUrl('https://d.pcs.baidu.com/file/1'), 'baidu');
});

test('R2 直链 + Cloudreve Referer 也能推断出来', () => {
  assert.equal(inferPlatform(R2, SHARE), 'cloudreve');
  // 裸 R2 地址（无 Referer）同样识别
  assert.equal(inferPlatform(R2, ''), 'cloudreve');
});

test('Cloudreve 算网盘（只接管网盘模式下要动手）', () => {
  assert.equal(isNetdiskUrl(SHARE), true);
  assert.equal(isNetdiskUrl(R2), true);
  assert.equal(isNetdiskUrl('https://cdn.example.com/a.bin'), false);
});

test('有中文名', () => {
  assert.equal(PLATFORM_NAMES.cloudreve, 'Cloudreve');
});

// ---------------- shareId 解析 ----------------

import { parseShareId } from '../../extension/platforms.js';
import { PLATFORMS } from '../../extension/platforms.js';

test('从查询串里取 shareId', () => {
  assert.equal(parseShareId(SHARE, PLATFORMS.cloudreve), 'GB7CY');
});

test('/s/<id> 形态也能取', () => {
  assert.equal(parseShareId('https://x.test/s/AbC123', PLATFORMS.cloudreve), 'AbC123');
});

test('不是分享页时返回 null', () => {
  assert.equal(parseShareId('https://x.test/home', PLATFORMS.cloudreve), null);
});

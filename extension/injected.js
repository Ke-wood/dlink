/**
 * 注入到目标页面执行的解析脚本。
 *
 * 运行环境：**页面上下文**（不是扩展的 isolated world）。
 * 这样 `credentials:'include'` 才带得上平台自己的登录态。
 *
 * 对外只暴露 `window.__DLINK__`，由 background 通过
 * chrome.scripting.executeScript + world:'MAIN' 调用。
 *
 * 约定：每个 resolve* 函数返回
 *   { ok:true, files:[{url,name,size,md5,cookies,headers,refresh}] }
 * 或 { ok:false, code, message, hint }
 */

(() => {
  // ★ 版本守卫：只跳过**同版本**的重复注入。
  //   以前写的是 `if (window.__DLINK__) return;` —— 扩展更新后重新注入时，
  //   页面里的旧对象还活着，新代码直接被挡掉，用户必须手动 F5 才能生效，
  //   表现就是「明明更新了扩展，功能还是旧的」。
  const BUILD = '0.3.2';
  if (window.__DLINK__ && window.__DLINK__.build === BUILD) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 各平台直链有效期（小时）。取链时刻 + TTL = expires_at。
  // 依据：百度响应里 expires=8h；夸克 auth_key 时间戳差约 6h；微云签名更短。
  // 这些是"保守估计"，真实值以服务端为准（过期会 403，靠续链兜底）。
  const LINK_TTL_HOURS = {
    baidu: 8,
    quark: 6,
    uc: 6,
    weiyun: 2,
    direct: 0,   // 未知，不设过期
  };

  function expiresAtFor(platformId) {
    const h = LINK_TTL_HOURS[platformId] || 0;
    return h > 0 ? Math.floor(Date.now() / 1000) + h * 3600 : null;
  }


  // ============ 百度 ============

  async function baiduResolve(shareUrl, maxLinks, opts) {
    const u = new URL(shareUrl);
    const surl = (u.pathname.match(/\/s\/([A-Za-z0-9_-]+)/) || [])[1];
    if (!surl) return { ok: false, code: 'BAD_URL', message: '无法解析分享 ID' };

    // --- 1. 分享上下文 ---
    // 首选 share/wxlist：它返回**服务端认可的 seckey**。页面变量可能缺失或过期，
    // 那正是 errno=-20 的根因（见 baiduShareContextFromApi 注释）。
    const pwd = (new URL(shareUrl)).searchParams.get('pwd') || '';
    let ctx = await baiduShareContextFromApi(surl, pwd);
    if (!ctx.ok) ctx = await baiduShareContext(surl);   // 兜底：页面里的 yunData
    if (!ctx.ok) return ctx;
    const { shareid, uk } = ctx;
    const seckey = ctx.seckey || '';

    // --- 2. 列文件（递归） ---
    const files = [];
    const seen = new Set();
    const dirQueue = [];
    const collected = [];   // 收集所有条目，最后统一分类

    // ★ 必须记住每个文件所在的父目录：分享是「文件夹」时，取直链要把它
    //   放进 path_list。少了这个参数，百度会把请求判成 -20「验证码输入错误」
    //   （它把「参数对不上」也报成验证码问题，非常误导）。
    const walkItems = (items, parent) => {
      for (const it of items || []) collected.push({ it, parent: parent || '' });
    };

    walkItems(ctx.rootFiles, '');
    for (const it of ctx.rootFiles || []) {
      if (Number(it.isdir) === 1 && it.path) dirQueue.push(it.path);
    }

    let guard = 0;
    while (dirQueue.length && guard++ < 200) {
      const dir = dirQueue.shift();
      if (seen.has(dir)) continue;
      seen.add(dir);
      const items = await baiduList(surl, shareid, uk, dir);
      walkItems(items, dir);          // 这一层的父目录就是 dir
      for (const it of items) {
        if (Number(it.isdir) === 1 && it.path && !seen.has(it.path)) dirQueue.push(it.path);
      }
    }

    for (const { it, parent } of collected) {
      if (Number(it.isdir) === 1) continue;
      const name = it.server_filename;
      files.push({
        fid: String(it.fs_id),
        name,
        size: Number(it.size || 0),
        md5: (it.md5 || '').toLowerCase(),
        path: it.path || `${parent}/${name}`,
        parent,                        // '' = 根目录
        shareid,
        uk,
      });
    }
    if (!files.length) {
      return { ok: false, code: 'EMPTY', message: '分享里没有文件（或需要提取码）' };
    }

    // --- 3. 逐文件取直链（数量受 maxLinks 限制，避免大目录超时）---
    const limit = (typeof maxLinks === 'number' && maxLinks > 0)
      ? maxLinks : files.length;
    const out = [];
    for (const f of files.slice(0, limit)) {
      const link = await baiduDlink(surl, f, seckey || (opts && opts.sekey));
      if (link.ok) out.push(link.item);
      else out.push({ ...f, error: link.message });
    }
    for (const f of files.slice(limit)) {
      out.push({ name: f.name, size: f.size, fid: f.fid,
        error: '未取直链（数量超出单次上限）', skipped: true });
    }
    return { ok: true, files: out, total: files.length };
  }

  async function baiduShareContext(surl) {
    // ★ 首选：直接读页面注入的 yunData —— 这是唯一可靠来源。
    //   实测：/api/shorturlinfo 在带提取码的分享上会返回 errno:0 但 file_list 为空，
    //   而页面自己已经把完整 file_list 放进 window.yunData 了。
    const y = window.yunData;
    if (y && y.shareid && (y.share_uk || y.uk)) {
      return {
        ok: true,
        shareid: y.shareid,
        uk: y.share_uk || y.uk,
        rootFiles: y.file_list || [],
        source: 'yunData',
      };
    }

    // 兜底：调接口（无提取码的分享可用）
    const params = new URLSearchParams({
      shorturl: surl, root: '1', channel: 'chunlei', web: '1',
      app_id: '250528', clienttype: '0',
    });
    const r = await fetch(
      `https://pan.baidu.com/api/shorturlinfo?${params}`,
      { credentials: 'include' });
    const j = await r.json();
    if (j.errno !== 0) {
      return {
        ok: false, code: `ERRNO_${j.errno}`,
        message: `shorturlinfo 失败 errno=${j.errno}`,
        hint: j.errno === -9 ? '提取码问题：先在页面里通过提取码再试'
          : 'cookie 可能不完整（需要 BDUSS + STOKEN）',
      };
    }
    return { ok: true, shareid: j.shareid, uk: j.uk,
             rootFiles: j.file_list || [], source: 'api' };
  }

  async function baiduList(surl, shareid, uk, dir) {
    const params = new URLSearchParams({
      uk: String(uk), shareid: String(shareid), order: 'other', desc: '1',
      showempty: '0', web: '1', page: '1', num: '100', dir,
      t: `0.${Math.random()}`, channel: 'chunlei', app_id: '250528',
    });
    const r = await fetch(`https://pan.baidu.com/share/list?${params}`,
      { credentials: 'include' });
    const j = await r.json();
    if (j.errno !== 0) return [];
    return j.list || [];
  }

  /** sekey 候选列表（＝分享提取码验证通过后的凭证）。
   *
   * 实测（2026-11-16）：只读 `window.currentSekey` 不够 ——
   *   · 部分页面版本不把它挂到 window 上；
   *   · 同一标签页里 SPA 路由切过别的分享时，它留着**上一个分享**的值。
   * 两种情况下 `/api/sharedownload` 都返回 errno=-20「验证码输入错误，请重试」，
   * 而这句提示会让人误以为是提取码打错了。
   * 所以收集多个来源，遇到 -20 就换下一个来源重试。
   */
  function baiduSekeyCandidates(sekeyHint) {
    const out = [];
    const push = (v) => {
      if (!v) return;
      const s = String(v).trim();
      if (s && !out.includes(s)) out.push(s);
    };
    try {
      push(sekeyHint);                                 // share/wxlist 下发，或 BDCLND：最可信
      push(window.currentSekey);                       // 页面变量：可能是旧分享的残留
      push(window.yunData && window.yunData.sekey);
      const m = String(document.cookie || '').match(/(?:^|;\s*)BDCLND=([^;]+)/);
      if (m && m[1]) push(decodeURIComponent(m[1]));   // 页面能读到的 BDCLND
    } catch (e) { /* 读不到就少一个候选 */ }
    return out;
  }

  /** 发一次 sharedownload；网络异常也归成业务错误，交给上层换 sekey 重试 */
  /** 取账号的 bdstoken（网页端接口用它做会话绑定）。
   *
   * 网页自己发 sharedownload 时是带 bdstoken 的；AList 那套写法不带
   * （它用 clienttype=12 走客户端通道）。两种形状百度都接受，但**同一账号
   * 只认其中一种** —— 实测：登录态正常却返回 errno=9019 needverify，
   * 说明当前形状和这个账号不匹配，所以要挨个试。
   */
  async function baiduBdstoken() {
    try {
      const r = await fetch(
        'https://pan.baidu.com/api/gettemplatevariable?fields='
        + encodeURIComponent('["bdstoken"]')
        + '&channel=chunlei&web=1&app_id=250528',
        { credentials: 'include' });
      const j = await r.json();
      return (j.result && j.result.bdstoken) || '';
    } catch (e) {
      return '';
    }
  }

  /** 一次 sharedownload。shape 决定「会话如何被证明」：
   *   A 网页形状：clienttype=0 + 真 bdstoken + 页面 jsToken
   *   B 旧形状  ：clienttype=0 + 不带 bdstoken + 页面 jsToken（历史实测跑通过）
   *   C 客户端形状：clienttype=12 + 都不带（AList 做法）
   */
  async function sharedownloadOnce(surl, f, sekey, shape, sign, timestamp, pathList) {
    const body = new URLSearchParams({
      encrypt: '0', product: 'share',
      uk: String(f.uk), primaryid: String(f.shareid),
      fid_list: JSON.stringify([f.fid]),
      extra: JSON.stringify({ sekey }),
      type: 'nolimit',
    });
    if (pathList) body.set('path_list', pathList);   // 子目录文件才需要
    if (shape.jsToken) body.set('jsToken', shape.jsToken);

    const q = new URLSearchParams({
      sign: String(sign), timestamp: String(timestamp), channel: 'chunlei',
      web: '1', app_id: '250528', clienttype: String(shape.ct),
    });
    if (shape.bdstoken) q.set('bdstoken', shape.bdstoken);
    try {
      const r = await fetch('https://pan.baidu.com/api/sharedownload?' + q, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
      });
      return await r.json();
    } catch (e) {
      return { errno: -1, errmsg: `请求失败：${e && e.message ? e.message : e}` };
    }
  }

  /** 按顺序试几种请求形状，成功即止 */
  async function baiduSharedownload(surl, f, sekey, sign, timestamp, pathList) {
    const jsToken = window.jsToken || '';
    const bdstoken = await baiduBdstoken();
    const shapes = [
      { name: 'web', ct: 0, bdstoken, jsToken },
      { name: 'legacy', ct: 0, bdstoken: '', jsToken },
      { name: 'client', ct: 12, bdstoken: '', jsToken: '' },
    ];
    const tried = [];
    let last = null;
    for (const shape of shapes) {
      // 没有 bdstoken 时 web 形状和 legacy 完全一样，跳过去省一次请求
      if (shape.name === 'web' && !shape.bdstoken) continue;
      const dl = await sharedownloadOnce(surl, f, sekey, shape, sign, timestamp, pathList);
      tried.push({ shape: shape.name, errno: dl && dl.errno });
      last = dl;
      if (dl && dl.errno === 0 && dl.list && dl.list[0] && dl.list[0].dlink) {
        return { dl, shape: shape.name, tried };
      }
      if (![9019, -6, -20, -9, 105].includes(Number(dl && dl.errno))) break;
    }
    return { dl: last, shape: null, tried };
  }

  /** 从分享接口拿「服务端认可的」上下文：seckey / shareid / uk / 根路径。
   *
   * 为什么不用页面变量（实测 2026-11-16，PC.7z 那个分享）：
   *   · 页面里可能根本没有 `window.currentSekey`；
   *   · 同标签页 SPA 切过别的分享时它还留着旧值。
   * 两种情况下 `/api/sharedownload` 一律返回 errno=-20「验证码输入错误」。
   *
   * `share/wxlist` 是微信端在用的接口：带 pwd 就能拿到**当前有效的 seckey**
   * （匿名可列目录；取直链仍需要登录，匿名会返回 errno=9019 need verify）。
   * 这个来源是 AList 百度分享驱动的做法，属于事实上的标准。
   */
  async function baiduShareContextFromApi(surl, pwd) {
    const body = new URLSearchParams({ shorturl: surl, root: '1' });
    if (pwd) body.set('pwd', pwd);
    try {
      const r = await fetch(
        'https://pan.baidu.com/share/wxlist?channel=weixin&version=2.2.2&clienttype=25&web=1',
        {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: body.toString(),
        });
      const j = await r.json();
      if (j.errno !== 0 || !j.data) {
        return { ok: false, errno: j.errno, message: `wxlist errno=${j.errno}` };
      }
      const d = j.data;
      const first = (d.list || [])[0] || {};
      return {
        ok: true,
        seckey: d.seckey || '',
        shareid: d.shareid,
        uk: d.uk,
        rootFiles: d.list || [],
        // 分享根的绝对路径（/Game 这类），子目录列目录时会用到
        rootPath: first.path ? String(first.path).replace(/\/[^/]+$/, '') : '',
        source: 'wxlist',
      };
    } catch (e) {
      return { ok: false, message: `wxlist 请求失败：${e && e.message ? e.message : e}` };
    }
  }

  /** 兜底：把分享文件**转存到用户自己网盘**，再从自己网盘取直链。
   *
   * 为什么需要：实测（2026-11-16）这个账号在 `sharedownload` 上会被要求
   * 「验证码/身份验证」（errno 9019 needverify），但返回 errno=0 时 list 里
   * 也不给 dlink。而「自己网盘的文件」取直链走的是另一套接口，不需要分享验证。
   *
   * ⚠️ 副作用：会在用户网盘**根目录**留下一个副本，占空间，需要用户自己删。
   *    所以调用方必须把这件事告诉用户。
   */
  /** 在自己网盘的某个目录里按文件名找 fs_id。
   *
   * 用途：转存返回 errno=4「文件已转存」时说明文件已在网盘里（可能是上次留下的，
   * 也可能是同名不同文件 —— 实测就撞过 29MB 的同名 PC.7z），
   * 这时按「名字 + 大小」双重匹配，避免拿错文件。
   */
  async function baiduFindInDir(dir, name, size, bdstoken) {
    if (!name) return '';
    const q = new URLSearchParams({
      dir: dir || '/', order: 'name', desc: '0', num: '1000', page: '1',
      channel: 'chunlei', web: '1', app_id: '250528',
      clienttype: '0', bdstoken,
    });
    try {
      const j = await (await fetch('https://pan.baidu.com/api/list?' + q,
        { credentials: 'include', headers: { 'X-Requested-With': 'XMLHttpRequest' } })).json();
      if (j.errno !== 0) return '';
      const list = j.list || [];
      // 优先「同名 + 同大小」，避免撞上同名但内容不同的文件
      const exact = list.find((x) => x.server_filename === name
        && (!size || Number(x.size) === Number(size)));
      if (exact) return String(exact.fs_id);
      const loose = list.find((x) => x.server_filename === name);
      return loose ? String(loose.fs_id) : '';
    } catch (e) {
      return '';
    }
  }

  /** 从「自己的网盘」取直链。
   *
   * ⚠️ 参数名是 `fsids`，不是 `target`。
   *   实测（2026-11-16）：用 `target=` 调 /api/filemetas，无论文件是否存在
   *   都会返回 errno=12、info:[{errno:31021}]；换成 `fsids=` 立刻返回
   *   errno=0 + dlink。`target` 是**分享**接口的参数，张冠李戴了。
   */
  async function baiduOwnDlink(fid, bdstoken) {
    const base = {
      dlink: '1', channel: 'chunlei', web: '1', app_id: '250528',
      clienttype: '0', bdstoken,
    };
    const variants = [
      { fsids: JSON.stringify([Number(fid)]) },
      { fsids: JSON.stringify([String(fid)]) },
    ];
    let lastErrno = null;
    for (const extra of variants) {
      const q = new URLSearchParams({ ...base, ...extra });
      try {
        const j = await (await fetch(`https://pan.baidu.com/api/filemetas?${q}`,
          { credentials: 'include', headers: { 'X-Requested-With': 'XMLHttpRequest' } })).json();
        const info = (j.info || [])[0] || {};
        if (j.errno === 0 && info.dlink) {
          return {
            ok: true,
            url: info.dlink,
            size: Number(info.size || 0),
            md5: (info.md5 || '').toLowerCase(),
            name: info.server_filename || '',
          };
        }
        lastErrno = j.errno;
      } catch (e) {
        lastErrno = 'exception';
      }
    }
    return { ok: false, message: `取自己网盘直链失败（errno=${lastErrno}）` };
  }

  /** 转存目标目录：独立一个目录，既避免根目录同名冲突，也方便用户清理。 */
  const BAIDU_TRANSFER_DIR = '/DLink';

  async function baiduMkdir(path, bdstoken) {
    const q = '?a=commit&channel=chunlei&web=1&app_id=250528&clienttype=0&bdstoken='
      + encodeURIComponent(bdstoken);
    try {
      const j = await (await fetch('https://pan.baidu.com/api/create' + q, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          path, isdir: '1', block_list: '[]', size: '0',
        }).toString(),
      })).json();
      return j.errno === 0 || j.errno === -8;   // -8 = 已存在
    } catch (e) {
      return false;
    }
  }

  async function baiduTransferTo(dir, f, seckey, bdstoken) {
    const q = '?' + new URLSearchParams({
      shareid: String(f.shareid), from: String(f.uk), sekey: seckey || '',
      channel: 'chunlei', web: '1', app_id: '250528', bdstoken, clienttype: '0',
    });
    try {
      return await (await fetch('https://pan.baidu.com/share/transfer' + q, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          fsidlist: JSON.stringify([f.fid]), path: dir,
        }).toString(),
      })).json();
    } catch (e) {
      return { errno: -1, errmsg: String(e) };
    }
  }

  /** 兜底：转存到自己的网盘，再取自己网盘的直链。
   *
   * 为什么需要：这个分享（文件夹分享 + 原文件 1.56GB）用 /api/sharedownload
   * 无论怎么调都是 118「违规操作」/ -20 / 9019 —— 变化无常，明显是风控。
   * 走「转存 → 从自己网盘取直链」就绕开了分享下载接口，实测可通。
   *
   * ⚠️ 副作用：会在用户网盘里留一份副本（默认 /DLink），占空间，要用户自己删。
   */
  async function baiduTransferThenLink(surl, f, seckey, bdstoken) {
    if (!bdstoken) return { ok: false, message: '没有 bdstoken（登录态异常）' };

    await baiduMkdir(BAIDU_TRANSFER_DIR, bdstoken);
    let dir = BAIDU_TRANSFER_DIR;
    let tj = await baiduTransferTo(dir, f, seckey, bdstoken);
    if (tj.errno === 2) {          // 目录不可用 → 退回根目录
      dir = '/';
      tj = await baiduTransferTo('/', f, seckey, bdstoken);
    }

    let fid = '';
    if (tj.errno === 0) {
      const moved = (tj.extra && tj.extra.list) || [];
      fid = moved[0] ? String(moved[0].to_fs_id || '') : '';
    } else if (tj.errno === 4) {
      // 已转存过：去目标目录里按「名字 + 大小」找回来
      fid = await baiduFindInDir(dir, f.name, f.size, bdstoken);
      if (!fid) {
        return {
          ok: false,
          message: `网盘提示「已转存」，但${dir}里没找到同名且同大小的文件`
            + '（可能被改名/移动，或同名文件内容不同）',
        };
      }
    } else {
      return {
        ok: false,
        message: `转存失败 errno=${tj.errno} ${tj.errmsg || tj.show_msg || ''}`.trim(),
      };
    }
    if (!fid) fid = await baiduFindInDir(dir, f.name, f.size, bdstoken);
    if (!fid) return { ok: false, message: '转存后没找到目标文件' };

    const own = await baiduOwnDlink(fid, bdstoken);
    if (!own.ok) return own;
    return {
      ok: true,
      url: own.url,
      size: own.size || f.size || 0,
      md5: own.md5 || '',
      name: own.name || f.name,
      transferredTo: dir,
    };
  }

  async function baiduDlink(surl, f, sekeyHint) {
    // 百度取直链需要 sign + timestamp
    const cfgR = await fetch(
      'https://pan.baidu.com/share/tplconfig?' + new URLSearchParams({
        surl, fields: 'sign,timestamp', view_mode: '1',
        channel: 'chunlei', web: '1', app_id: '250528', bdstoken: '',
      }), { credentials: 'include' });
    const cfg = await cfgR.json();
    if (cfg.errno !== 0) {
      return { ok: false, message: `tplconfig errno=${cfg.errno}` };
    }
    const { sign, timestamp } = cfg.data || {};
    const jsToken = window.jsToken || '';

    // 三个维度都要试，任一成功即止（总请求数封顶，避免触发限流）：
    //   sekey     —— 页面变量可能缺失，或残留上一个分享的值
    //   path_list —— 子目录里的文件不带父目录会被判成 -20
    //   请求形状  —— bdstoken/jsToken/clienttype 组合，见 baiduSharedownload
    const candidates = baiduSekeyCandidates(sekeyHint);
    const sekList = candidates.length ? candidates : [''];
    const dir = f.parent || '';
    const pathVariants = dir ? [JSON.stringify([dir]), ''] : [''];

    const RETRY_ERRNOS = [9019, -6, -20, -9, 105];
    const jobs = [];
    sekList.forEach((sk, i) => {
      // 只有最可信的那个 sekey 才试两种 path_list，其余用根目录写法
      for (const pl of (i === 0 ? pathVariants : [''])) jobs.push({ sk, pl });
    });

    let dl = null;
    let attempts = 0;
    let shapeTried = [];
    let emptyList = null;      // errno=0 但 list 里没有 dlink 时的原始条目
    for (const job of jobs) {
      if (attempts >= 6) break;
      const res = await baiduSharedownload(surl, f, job.sk, sign, timestamp, job.pl);
      attempts += res.tried.length;
      shapeTried = res.tried;
      dl = res.dl;
      if (res.shape) {
        const it = dl.list[0];
        return {
          ok: true,
          item: {
            url: it.dlink,
            name: f.name,
            size: Number(it.size || f.size || 0),
            md5: (it.md5 || f.md5 || '').toLowerCase(),
            // 直链必须带 BDUSS 系 cookie，否则 403 error_code:31045
            cookies: await pickCookies('baidu'),
            // ★ UA 必须是客户端 UA：百度 CDN 对浏览器 UA 会返回
            //   error_code 31326「user is not authorized」（AList 文档亦如此）
            headers: {
              Referer: 'https://pan.baidu.com/',
              'User-Agent': 'netdisk',
            },
            expires_at: expiresAtFor('baidu'),
            refresh: { platform: 'baidu', shareUrl: surl, fid: f.fid },
          },
        };
      }
      if (Number(dl && dl.errno) === 0) {
        // errno=0 说明接口通了，但 list[0] 里没有 dlink —— 记下来看它给了什么
        emptyList = (dl.list && dl.list[0]) || {};
      }
      if (!RETRY_ERRNOS.includes(Number(dl && dl.errno))) break;
    }

    const errno = Number(dl && dl.errno);

    // 只在「接口通了但没给直链」或「被要求验证」时才转存，
    // 避免因为一次网络抖动就在用户网盘里凭空多出文件。
    const needTransfer = !!emptyList || errno === 9019 || errno === -6;
    const bdstoken = needTransfer ? await baiduBdstoken() : '';
    const tr = needTransfer
      ? await baiduTransferThenLink(surl, f, sekList[0] || '', bdstoken)
      : { ok: false, message: '未尝试（错误类型不需要转存）' };
    if (tr.ok) {
      return {
        ok: true,
        item: {
          url: tr.url,
          name: tr.name || f.name,
          size: tr.size || f.size || 0,
          md5: tr.md5 || f.md5 || '',
          cookies: await pickCookies('baidu'),
          headers: {
            Referer: 'https://pan.baidu.com/',
            'User-Agent': 'netdisk',
          },
          expires_at: expiresAtFor('baidu'),
          // 转存下载拿不到分享页信息，过期后没法自动续链
          refresh: null,
          transferred: true,
          transferredTo: tr.transferredTo || '',
        },
      };
    }

    const detail = (dl && (dl.show_msg || dl.errmsg)) || '';
    const shapeInfo = shapeTried.map((x) => `${x.shape}:${x.errno}`).join('/');
    let msg = `sharedownload errno=${dl && dl.errno} ${detail}`.trim();
    msg += `｜试了 ${attempts} 次（${sekList.length} 个 sekey${shapeInfo ? '，形状 ' + shapeInfo : ''}）`;
    if (errno === -20) {
      // 百度把「验证状态对不上」和「参数对不上」都报成「验证码输入错误」
      msg += dir ? `；文件在子目录「${dir}」里` : '';
      msg += '。可试：分享页重输提取码/过掉图形验证码后 F5 再点；'
        + '仍失败则这个分享可能只允许转存或客户端下载';
    } else if (errno === -9 || errno === 105) {
      msg += '｜提取码错误或未通过：确认链接里的 ?pwd= 是否正确';
    } else if (errno === 9019) {
      msg += '｜百度判定「需要验证」：网页形状和客户端形状都被拒。'
        + '这通常意味着该账号的下载接口要求二次验证（网页上会弹验证码）';
    }
    if (emptyList) {
      msg += `；list[0] 里没有 dlink，字段=${Object.keys(emptyList).join(',')}`
        + `${emptyList.error_code ? ` error_code=${emptyList.error_code}` : ''}`
        + `${emptyList.error_msg ? ` ${emptyList.error_msg}` : ''}`;
    }
    msg += `；转存兜底也失败：${tr.message}`;
    return { ok: false, message: msg };
  }


  // ============ 夸克 / UC ============

  async function quarkResolve(shareUrl, platformId, maxLinks) {
    const isUC = platformId === 'uc';
    const API = isUC
      ? 'https://pc-api.uc.cn/1/clouddrive'
      : 'https://drive-pc.quark.cn/1/clouddrive';
    const PR = isUC ? 'UCBrowser' : 'ucpro';
    const Q = `?pr=${PR}&fr=pc&uc_param_str=`;

    const pwdId = (new URL(shareUrl).pathname.match(/\/s\/([A-Za-z0-9]+)/) || [])[1];
    if (!pwdId) return { ok: false, code: 'BAD_URL', message: '无法解析分享 ID' };
    const passcode = (new URL(shareUrl).searchParams.get('pwd') ||
      new URL(shareUrl).searchParams.get('passcode') || '');

    // 1. token
    const tokR = await fetch(API + '/share/sharepage/token' + Q, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pwd_id: pwdId, passcode }),
    });
    const tok = await tokR.json();
    if (tok.code !== 0) {
      return {
        ok: false, code: `CODE_${tok.code}`,
        message: `取 stoken 失败：${tok.message || tok.code}`,
        hint: [41008, 41009, 41010].includes(tok.code) ? '提取码问题' : '可能需要登录',
      };
    }
    const stoken = tok.data.stoken;

    // 2. 递归列文件
    const files = [];
    const queue = ['0'];
    const seen = new Set();
    let guard = 0;
    while (queue.length && guard++ < 300) {
      const pdir = queue.shift();
      if (seen.has(pdir)) continue;
      seen.add(pdir);
      const params = new URLSearchParams({
        pwd_id: pwdId, stoken, pdir_fid: pdir, force: '0',
        _page: '1', _size: '200', _fetch_banner: '1', _fetch_share: '1',
        _fetch_total: '1', _sort: 'file_type:asc,updated_at:desc',
      });
      const r = await fetch(API + '/share/sharepage/detail' + Q + '&' + params, {
        credentials: 'include' });
      const j = await r.json();
      if (j.code !== 0) break;
      for (const it of (j.data && j.data.list) || []) {
        if (it.dir) {
          queue.push(it.fid);
        } else {
          files.push({
            fid: it.fid,
            name: it.file_name,
            size: Number(it.size || 0),
            pwdId, stoken,
            // 转存时需要，必须保留完整 32 位
            shareFidToken: it.share_fid_token || '',
          });
        }
      }
    }
    if (!files.length) {
      return { ok: false, code: 'EMPTY', message: '分享里没有文件' };
    }

    // 3. 取直链
    //    ⚠️ 默认只取"有限个"直链：目录里有 178 个文件时逐个取会超时。
    //    maxLinks 由调用方给，不传则取全部（小目录场景）。
    const limit = (typeof maxLinks === 'number' && maxLinks > 0)
      ? maxLinks : files.length;
    const targets = files.slice(0, limit);
    const out = [];
    for (const f of targets) {
      const link = await quarkDlink(API, Q, f, platformId);
      if (link.ok) out.push(link.item);
      else out.push({ ...f, error: link.message });
    }
    // 超出 limit 的只回元数据，不取直链
    for (const f of files.slice(limit)) {
      out.push({
        name: f.name, size: f.size, fid: f.fid,
        error: '未取直链（数量超出单次上限）',
        skipped: true,
      });
    }
    return { ok: true, files: out, total: files.length };
  }

  async function quarkDlink(API, Q, f, platformId) {
    // 先试直接取（自己的文件）
    let r = await fetch(API + '/file/download' + Q, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fids: [f.fid] }),
    });
    let j = await r.json();

    // 21001 / 31001 → 需要转存
    let transfer = null;
    if (j.code !== 0 && [21001, 31001].includes(j.code)) {
      const saved = await quarkTransfer(API, Q, f);
      if (!saved.ok) return saved;
      // 记录转存信息：下载器完成后可据此删除（用户可在设置里开/关）
      transfer = {
        platform: platformId,
        apiBase: API,
        pdirFid: saved.pdir || '0',
        fid: saved.fid,
        name: saved.name || f.name,
      };
      r = await fetch(API + '/file/download' + Q, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fids: [saved.fid] }),
      });
      j = await r.json();
    }

    if (j.code !== 0) {
      return { ok: false, message: `file/download code=${j.code} ${j.message || ''}` };
    }
    const d = j.data && j.data[0];
    if (!d || !d.download_url) return { ok: false, message: '响应里没有 download_url' };

    return {
      ok: true,
      item: {
        url: d.download_url,
        name: f.name,
        size: Number(d.size || f.size || 0),
        md5: (d.md5 || '').toLowerCase(),
        // 夸克直链与取链时的 cookie 强绑定，必须快照
        cookies: await pickCookies('quark-uc'),
        headers: {
          Referer: 'https://pan.quark.cn/',
          // 必须伪装 PC 客户端，否则 412
          'User-Agent': navigator.userAgent.includes('quark-cloud-drive')
            ? navigator.userAgent
            : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              + '(KHTML, like Gecko) quark-cloud-drive/3.20.0 Chrome/112.0.5615.165 '
              + 'Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch',
        },
        expires_at: expiresAtFor(platformId),
        transfer,
        refresh: { platform: platformId, shareUrl: location.href, fid: f.fid },
      },
    };
  }

  async function quarkTransfer(API, Q, f) {
    const body = {
      fid_list: [f.fid],
      fid_token_list: [f.shareFidToken],
      to_pdir_fid: '0',
      pwd_id: f.pwdId,
      stoken: f.stoken,
      pdir_fid: '0',
      pdir_save_all: false,
      exclude_fids: [],
      scene: 'link',
    };
    const r = await fetch(API + '/share/sharepage/save' + Q, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (j.code !== 0) {
      return { ok: false, message: `转存失败 code=${j.code} ${j.message || ''}` };
    }
    const taskId = j.data && j.data.task_id;

    // 等任务完成
    for (let i = 0; i < 20; i++) {
      const tR = await fetch(API + '/task' + Q + `&task_id=${taskId}&retry_index=${i}`,
        { credentials: 'include' });
      const tj = await tR.json();
      if (tj.data && tj.data.status === 2) break;
      await sleep(500);
    }
    // 在根目录找同名文件
    const lsR = await fetch(
      API + '/file/sort' + Q + '&pdir_fid=0&_page=1&_size=100&_fetch_total=1'
      + '&_sort=file_type:asc,updated_at:desc',
      { credentials: 'include' });
    const ls = await lsR.json();
    const list = (ls.data && ls.data.list) || [];
    const hit = list.find((x) => x.file_name === f.name);
    if (!hit) return { ok: false, message: '转存后找不到文件' };
    return { ok: true, fid: hit.fid, name: hit.file_name, pdir: hit.pdir_fid || '0' };
  }

  // ============ 微云 ============

  /** 从页面自己发过的请求里捞 g_tk。
   *
   * 为什么需要：微信登录微云时**没有 skey cookie**（只有 weiyun_wx_openid /
   * wyctoken 等），hash33 算不出 g_tk，只能发 0 → 接口 403 空 body。
   * 但页面自身渲染时必发过 `share.weiyun.com/webapp/json/...?g_tk=NNN`，
   * 这些 URL 在 Resource Timing 里可读 —— 直接借用那个值。
   * （实测：能取到 732187767 / 885207716 这类真实值。）
   */
  function weiyunGtkFromPerf() {
    try {
      const urls = performance.getEntriesByType('resource').map((e) => e.name);
      for (let i = urls.length - 1; i >= 0; i--) {
        const u = urls[i];
        if (!u || !u.includes('/webapp/json/')) continue;
        const m = u.match(/g_tk=(\d+)/);
        if (m && m[1] !== '0') return Number(m[1]);
      }
    } catch (e) { /* 拿不到就算了 */ }
    return 0;
  }

  /** QQ 系 g_tk：hash33(skey)。空 skey 返回 0（游客）。 */
  function weiyunCalcGtk(skey) {
    if (!skey) return 0;
    let h = 5381;
    for (const ch of String(skey)) h = (h * 33 + ch.charCodeAt(0)) & 0x7fffffff;
    return h;
  }

  async function weiyunResolve(shareUrl, opts) {
    const key = shareUrl.split('/').pop().split('?')[0];
    const app = document.getElementById('app');
    const store = app && app.__vue__ && app.__vue__.$store;
    if (!store) {
      return { ok: false, code: 'NO_STORE', message: '页面还没加载完，或结构变了' };
    }
    const st = store.state;
    const si = st.shareInfo || {};
    const inst = st.sharefile && st.sharefile.shareFile && st.sharefile.shareFile.shareFile;
    const files = [];

    function walk(node) {
      if (!node) return;
      const kids = node._kid_files || [];
      for (const k of kids) {
        files.push({
          fid: k._id, pdir: k._pdir_key, name: k._name,
          size: k._file_size, sha: k._file_sha,
        });
      }
      const dirs = node._kid_dirs || [];
      for (const d of dirs) walk(d);
    }
    if (inst && inst.pathStack) walk(inst.pathStack[inst.pathStack.length - 1]);

    if (!files.length) {
      return { ok: false, code: 'EMPTY',
        message: '没找到文件：分享根如果是文件夹，需要先在页面里点进去再取直链' };
    }

    // 取 g_tk：优先用 background 从 chrome.cookies 读到的 skey（含 HttpOnly），
    // 页面 document.cookie 里的 skey 大多读不到，只能是兜底。
    const pageSkey = (String(document.cookie || '').match(/(?:^|;\s*)skey=([^;]+)/) || [])[1] || '';
    // 优先级：扩展给的 skey → 页面 cookie 的 skey → 页面请求 URL 里的 g_tk
    const gtk = weiyunCalcGtk((opts && opts.skey) || pageSkey) || weiyunGtkFromPerf();
    const out = [];
    for (const f of files) {
      const link = await weiyunDlink(key, gtk, f, opts);
      if (link.ok) out.push(link.item);
      else out.push({ ...f, error: link.message });
    }
    return { ok: true, files: out };
  }

  async function weiyunDlink(shareKey, gtk, f, opts) {
    const header = {
      seq: Math.floor(Math.random() * 2 ** 31),
      type: 1, cmd: 12024, appid: 30151,
      version: 3, major_version: 3, minor_version: 3, fix_version: 3,
      wx_openid: '', user_flag: 1, env_id: '',
      device_info: JSON.stringify({ browser: 'chrome' }),
    };
    const body = {
      ReqMsg_body: {
        ext_req_head: {
          token_info: { token_type: 3, login_key_type: 1540 },
          language_info: { language_type: 2052 },
        },
        '.weiyun.WeiyunShareBatchDownloadMsgReq_body': {
          share_key: shareKey, pwd: null, file_owner: null, download_type: 0,
          file_list: [{
            pdir_key: f.pdir, file_id: f.fid,
            filename: f.name, file_size: f.size,
          }],
        },
      },
    };
    const r = await fetch(
      `/webapp/json/weiyunShare/WeiyunShareBatchDownload?refer=chrome_windows`
      + `&g_tk=${gtk}&r=${Math.random()}`, {
        method: 'POST', credentials: 'include',
        headers: {
          Accept: 'application/json, text/plain, */*',
          'Content-Type': 'application/json;charset=utf-8',
        },
        body: JSON.stringify({
          req_header: JSON.stringify(header),
          req_body: JSON.stringify(body),
        }),
      });
    // 空 body（403）说明 g_tk/Accept 有问题，不能让 r.json() 抛
    // SyntaxError 把整个解析过程带崩
    const raw = await r.text();
    let j = null;
    try { j = JSON.parse(raw); } catch (e) { j = null; }
    if (!j) {
      return {
        ok: false,
        message: `微云接口返回非 JSON（HTTP ${r.status}`
          + `${raw ? '' : '，空响应'}）——多半是 g_tk 与 skey 不匹配，或缺 Accept 头`
          + `${(opts && opts.cookieNames) ? `；已读到 ${opts.cookieNames} 个 cookie` : '；没读到任何 cookie'}`
          + `；g_tk=${gtk}`,
      };
    }
    const rc = j.data && j.data.rsp_header && j.data.rsp_header.retcode;
    if (rc !== 0) {
      return {
        ok: false,
        message: `retcode=${rc} ${(j.data && j.data.rsp_header.retmsg) || ''}`,
        hint: rc === 190011 ? '需要登录微云' : '',
      };
    }
    const it = (j.data.rsp_body.RspMsg_body.file_list || [])[0];
    return {
      ok: true,
      item: {
        url: it.https_download_url || it.download_url,
        name: f.name,
        size: Number(it.file_size || f.size || 0),
        sha1: (it.file_sha || f.sha || '').toLowerCase(),
        cookies: { [it.cookie_name || 'FTN5K']: it.cookie_value || '' },
        headers: { Referer: 'https://share.weiyun.com/' },
        expires_at: expiresAtFor('weiyun'),
        refresh: { platform: 'weiyun', shareUrl: location.href, fid: f.fid },
      },
    };
  }

  // ============ 工具 ============

  /** 从页面 cookie 里挑出下载必需的字段（HttpOnly 的由 background 补齐） */
  async function pickCookies(kind) {
    const raw = document.cookie || '';
    const out = {};
    for (const kv of raw.split(';')) {
      const i = kv.indexOf('=');
      if (i < 0) continue;
      const k = kv.slice(0, i).trim();
      const v = kv.slice(i + 1).trim();
      if (!k) continue;
      // 空值也要保留：__chkey= 这类空 cookie 参与夸克的签名判定
      if (kind === 'baidu' && (k.startsWith('BDUSS') || k === 'STOKEN' || k === 'BAIDUID' || k === 'BIDUPSID')) {
        out[k] = v;
      }
      if (kind === 'quark-uc' && (k.startsWith('__') || k === 'ctoken' || k === 'tfstk'
        || k === 'b-user-id' || k === 'xlly_s' || k === 'isg'
        || k.startsWith('_c_') || k.startsWith('_nb_'))) {
        out[k] = v;
      }
    }
    return out;
  }

  async function pickGtk(kind) {
    const raw = document.cookie || '';
    const m = raw.match(/skey=([^;]+)/);
    if (!m) return 0;
    let h = 5381;
    for (const ch of m[1]) h = (h * 33 + ch.charCodeAt(0)) & 0x7fffffff;
    return h;
  }

  // ============ 对外 ============

  window.__DLINK__ = {
    build: BUILD,
    resolve: async (platformId, shareUrl, maxLinks, opts) => {
      try {
        if (platformId === 'baidu') return await baiduResolve(shareUrl, maxLinks, opts);
        if (platformId === 'quark' || platformId === 'uc') {
          return await quarkResolve(shareUrl, platformId, maxLinks);
        }
        if (platformId === 'weiyun') return await weiyunResolve(shareUrl, opts);
        return { ok: false, code: 'NO_PLATFORM', message: `不支持的平台: ${platformId}` };
      } catch (e) {
        return { ok: false, code: 'EXCEPTION', message: String(e) };
      }
    },
    // 诊断用
    /** 一步到位的取链诊断：在分享页 Console 跑
     *    await __DLINK__.debugBaidu()
     *  它会把 wxlist 上下文、目标文件、sharedownload 的原始返回都打出来，
     *  但不含任何 cookie 值，可以安全贴出来。
     */
    debugBaidu: async (shareUrl) => {
      const u = new URL(shareUrl || location.href);
      const surl = (u.pathname.match(/\/s\/([A-Za-z0-9_-]+)/) || [])[1];
      const pwd = u.searchParams.get('pwd') || '';
      if (!surl) return { ok: false, message: '无法解析分享 ID' };
      const ctx = await baiduShareContextFromApi(surl, pwd);
      if (!ctx.ok) return { step: 'wxlist', ...ctx };

      const first = (ctx.rootFiles || [])[0] || {};
      let file = {
        fid: String(first.fs_id), name: first.server_filename,
        uk: ctx.uk, shareid: ctx.shareid, parent: '',
      };
      let listed = 1;
      if (Number(first.isdir) === 1) {
        const items = await baiduList(surl, ctx.shareid, ctx.uk, first.path);
        listed = items.length;
        const f2 = items.find((x) => Number(x.isdir) !== 1);
        if (!f2) return { step: 'list', ok: false, message: '子目录里没有文件', items: listed };
        file = {
          fid: String(f2.fs_id), name: f2.server_filename,
          uk: ctx.uk, shareid: ctx.shareid, parent: first.path,
        };
      }

      const cfg = await (await fetch(
        'https://pan.baidu.com/share/tplconfig?' + new URLSearchParams({
          surl, fields: 'sign,timestamp', view_mode: '1', channel: 'chunlei',
          web: '1', app_id: '250528', bdstoken: '',
        }), { credentials: 'include' })).json();
      if (cfg.errno !== 0) return { step: 'tplconfig', errno: cfg.errno };
      const sign = (cfg.data || {}).sign;
      const ts = (cfg.data || {}).timestamp;
      const res = await baiduSharedownload(surl, file, ctx.seckey, sign, ts, '');
      const dl = res.dl;
      return {
        step: 'sharedownload',
        build: BUILD,
        file: file.name,
        listed,
        seckeyLen: (ctx.seckey || '').length,
        okShape: res.shape || null,
        shapes: res.tried,
        errno: dl && dl.errno,
        errmsg: (dl && (dl.errmsg || dl.show_msg)) || '',
        keys: dl ? Object.keys(dl) : [],
        list0: dl && dl.list && dl.list[0]
          ? Object.fromEntries(Object.entries(dl.list[0]).map(([k, v]) => [
            k, k === 'dlink' ? `<len ${String(v).length}>` : v]))
          : null,
      };
    },
    // 诊断用：分享页 Console 里跑 __DLINK__.probe()，看 sekey 各来源有没有
    probe: () => ({
      build: BUILD,
      url: location.href,
      hasYunData: !!window.yunData,
      hasJsToken: !!window.jsToken,
      hasCurrentSekey: !!window.currentSekey,
      hasYunDataSekey: !!(window.yunData && window.yunData.sekey),
      hasBdclndCookie: /(?:^|;\s*)BDCLND=/.test(String(document.cookie || '')),
      sekeyCandidates: (typeof baiduSekeyCandidates === 'function')
        ? baiduSekeyCandidates().length : -1,
      cookieNames: (document.cookie || '').split(';').map((x) => x.split('=')[0].trim()),
    }),
  };
})();

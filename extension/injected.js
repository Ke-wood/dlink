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
  if (window.__DLINK__) return; // 幂等

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
    const ctx = await baiduShareContext(surl);
    if (!ctx.ok) return ctx;
    const { shareid, uk } = ctx;

    // --- 2. 列文件（递归） ---
    const files = [];
    const seen = new Set();
    const dirQueue = [];
    const collected = [];   // 收集所有条目，最后统一分类

    const walkItems = (items) => {
      for (const it of items || []) collected.push(it);
    };

    walkItems(ctx.rootFiles);
    for (const it of ctx.rootFiles || []) {
      if (it.isdir && it.path) dirQueue.push(it.path);
    }

    let guard = 0;
    while (dirQueue.length && guard++ < 200) {
      const dir = dirQueue.shift();
      if (seen.has(dir)) continue;
      seen.add(dir);
      const items = await baiduList(surl, shareid, uk, dir);
      walkItems(items);
      for (const it of items) {
        if (it.isdir && it.path && !seen.has(it.path)) dirQueue.push(it.path);
      }
    }

    for (const it of collected) {
      if (it.isdir) continue;
      files.push({
        fid: String(it.fs_id),
        name: it.server_filename,
        size: Number(it.size || 0),
        md5: (it.md5 || '').toLowerCase(),
        path: it.path,
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
      const link = await baiduDlink(surl, f, opts && opts.sekey);
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
      push(window.currentSekey);                       // 页面变量：最准，但可能过期
      push(sekeyHint);                                 // background 读的 BDCLND（含 HttpOnly）
      push(window.yunData && window.yunData.sekey);
      const m = String(document.cookie || '').match(/(?:^|;\s*)BDCLND=([^;]+)/);
      if (m && m[1]) push(decodeURIComponent(m[1]));   // 页面能读到的 BDCLND
    } catch (e) { /* 读不到就少一个候选 */ }
    return out;
  }

  /** 发一次 sharedownload；网络异常也归成业务错误，交给上层换 sekey 重试 */
  async function sharedownloadOnce(surl, f, sekey, jsToken, sign, timestamp) {
    const body = new URLSearchParams({
      encrypt: '0', product: 'share',
      uk: String(f.uk), primaryid: String(f.shareid),
      fid_list: JSON.stringify([f.fid]), path_list: '',
      extra: JSON.stringify({ sekey }),
    });
    if (jsToken) body.set('jsToken', jsToken);
    try {
      const r = await fetch(
        'https://pan.baidu.com/api/sharedownload?' + new URLSearchParams({
          sign, timestamp: String(timestamp), channel: 'chunlei',
          web: '1', app_id: '250528', clienttype: '0', bdstoken: '',
        }),
        {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: body.toString(),
        });
      return await r.json();
    } catch (e) {
      return { errno: -1, show_msg: `请求失败：${e && e.message ? e.message : e}` };
    }
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

    // 逐个候选试；-20 才换源（其它错误换也没用，白白多打一次接口）
    const candidates = baiduSekeyCandidates(sekeyHint);
    const tried = candidates.length ? candidates : [''];
    let dl = null;
    for (const sekey of tried) {
      dl = await sharedownloadOnce(surl, f, sekey, jsToken, sign, timestamp);
      if (dl.errno === 0 && dl.list && dl.list[0] && dl.list[0].dlink) {
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
            headers: { Referer: 'https://pan.baidu.com/' },
            expires_at: expiresAtFor('baidu'),
            refresh: { platform: 'baidu', shareUrl: surl, fid: f.fid },
          },
        };
      }
      if (Number(dl.errno) !== -20) break;
    }

    const errno = Number(dl && dl.errno);
    let msg = `sharedownload errno=${dl && dl.errno} ${(dl && dl.show_msg) || ''}`.trim();
    if (errno === -20) {
      // 百度把「验证状态对不上」也报成「验证码输入错误」，必须给出可操作的下一步
      msg += candidates.length
        ? `｜试过 ${tried.length} 个 sekey 来源都被拒：在分享页重新输入提取码（或过掉图形验证码），按 F5 刷新页面后再点「提取直链」`
        : '｜页面里没拿到 sekey（提取码还没通过）：先在分享页输入提取码、看到文件列表后按 F5 刷新再点「提取直链」';
    } else if (errno === -9 || errno === 105) {
      msg += '｜提取码错误或未通过：确认链接里的 ?pwd= 是否正确';
    }
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

  async function weiyunResolve(shareUrl) {
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
      return { ok: false, code: 'EMPTY', message: '没找到文件（可能还在加载）' };
    }

    // 取 g_tk：从 cookie skey 算
    const gtk = await pickGtk('weiyun');
    const out = [];
    for (const f of files) {
      const link = await weiyunDlink(key, gtk, f);
      if (link.ok) out.push(link.item);
      else out.push({ ...f, error: link.message });
    }
    return { ok: true, files: out };
  }

  async function weiyunDlink(shareKey, gtk, f) {
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
    const j = await r.json();
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
    resolve: async (platformId, shareUrl, maxLinks, opts) => {
      try {
        if (platformId === 'baidu') return await baiduResolve(shareUrl, maxLinks, opts);
        if (platformId === 'quark' || platformId === 'uc') {
          return await quarkResolve(shareUrl, platformId, maxLinks);
        }
        if (platformId === 'weiyun') return await weiyunResolve(shareUrl);
        return { ok: false, code: 'NO_PLATFORM', message: `不支持的平台: ${platformId}` };
      } catch (e) {
        return { ok: false, code: 'EXCEPTION', message: String(e) };
      }
    },
    // 诊断用
    // 诊断用：分享页 Console 里跑 __DLINK__.probe()，看 sekey 各来源有没有
    probe: () => ({
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

# 03 · 网络层绕过百度限速：可行性与实验设计

> 调研日期：2026-11-15 ｜ 账号：普通用户（非会员）
> 测试对象：`VRQ1/PC.7z`，29,620,737 字节，直链经 `/api/filemetas` 获取
> 方法：本机实测 + 协议探测。**标注规则：✅ 本次实测 ｜ ⚠️ 资料/推断 ｜ ❓ 未验证**

---

## 1. 一句话结论

> **网络层没有任何"无损提速"的确定性手段**——百度 CDN **不支持 HTTP/2**（ALPN 只给 `http/1.1`），**没有 Range 之外的下载分块接口**，`chunk`/`part` 类参数被忽略。
> **但"换 IP"这一条有一个决定性的新证据支持它值得测**：下载响应头里带有 `x-bs-client-ip`，服务端**把客户端 IP 显式记入每个下载响应的元数据**——IP 是限速判定链路上的实参，不是旁观者。
> **判断：A（换 IP）值得做一次 20 分钟的证伪实验；B（IPv6）值得做，但前提是本机先接通 IPv6；C/D/E 可以判定为无效，不必再测。**

---

## 2. 本次新测得的硬事实

这些是写报告前当场跑出来的，不是文献推断。

| # | 事实 | 证据 | 标记 |
|---|---|---|---|
| F1 | `d.pcs.baidu.com` **有 AAAA 记录**：`2409:8c00:6c21:103f:0:ff:b05e:456c`、`2409:8c00:6c21:1040:0:ff:b045:23ef`（中国移动 CDN 段 `2409:`） | `getaddrinfo` 实测 | ✅ |
| F2 | **本机当前无 IPv6 出口**：`ipconfig` 无 IPv6 地址；ping/connect `2400:3200::1`、`2409:8c00:...` 全部 `TimeoutError` | 3 个目标全超时 | ✅ |
| F3 | 系统 DNS 服务器是 **`240c::6666`（IPv6 DNS，电信）**，但无 IPv6 出口 → 系统正靠 IPv4 回退解析 | `ipconfig /all` | ✅ |
| F4 | **百度全部 PCS 域不支持 HTTP/2**：`d.pcs`、`pcs`、`pan`、`issuecdn.baidupcs` 的 ALPN 协商结果**一律 `http/1.1`**（客户端主动优先 `h2` 仍被降级） | `ctx.set_alpn_protocols(['h2','http/1.1'])` 实测 | ✅ |
| F5 | 下载响应头含 **`x-bs-client-ip` = base64 的客户端出口 IP**（解码得 `120.229.205.229`）；`x-bs-request-id` 内含后端节点 `10.80.116.151:3338` | 响应头实测+解码 | ✅ |
| F6 | 本机出口 IP 为 `120.229.205.229`（广东·移动），CDN 解析到 `111.63.96.7/.140`（同移动网） | `myip.ipip.net` + DNS | ✅ |
| F7 | **单连接速率会随时间衰减并停在低位**：首轮 75.3 KB/s → 连续测试后稳定 **38.9/39.1/39.5 KB/s** | 3×8s 间隔 3s | ✅ |
| F8 | **多连接聚合确有 1.5–1.8× 提升**，但**总有一路恰好满速 73–75 KB/s**，其余被压到 26–38 KB/s | 2 路 108.8、3 路 131.8 KB/s | ✅ |
| F9 | 去掉 `BDUSS`/`STOKEN` → **403 `31045 user not exists`**，无法做匿名同链测试 | 实测 | ✅ |
| F10 | 改 `dstime` → **403 `31360 expire time out error`**（服务端校验签名与时间戳绑定） | 实测 | ✅ |
| F11 | `chunk=1` 参数被**忽略**（仍 206，正常返回数据）；`/rest/2.0/pcs/superfile2?method=download` → **500 `31238 upstream parse error`**（不是下载接口）；`m3u8=1` → **400 `31023 param error , sign`** | 实测 | ✅ |
| F12 | 代理端口 `127.0.0.1:7897` **当前未监听**（ConnectionRefused）；注册表 `ProxyServer=127.0.0.1:7897` 但 `ProxyEnable=0x0`；无 Clash 类进程运行 | 端口扫描 + 注册表 + tasklist | ✅ |

### 2.1 F8 的意义（比"聚合封顶"更精确）

原始结论写的是"账号级总带宽封顶 100–130 KB/s"。本次数据给出了更细的结构：

```
1 路  →  75.2 KB/s
2 路  →  agg 108.8  (per: 74.2 / 37.7)
3 路  →  agg 131.8  (per: 73.2 / 37.6 / 26.7)
```

这不是纯令牌桶（纯令牌桶应**均分**，如 3 路各 ~40）。这更像**双层限制**：

1. **单连接硬上限** ≈ 75 KB/s（先到先得，抢到的那路满速）
2. **账号/出口总量上限** ≈ 100–135 KB/s（其余连接分剩余额度）

⚠️ 该结构是**推断**，F8 只证明了现象。但它有个重要的可检验推论：**总量上限若绑定在 IP 上，换 IP 就能拿到第二份额度**——这正是实验 A 要证伪的东西。

### 2.2 F5 的意义（本报告最关键的单点事实）

`x-bs-client-ip` 说明响应链路知晓并传递客户端 IP。这**不能证明**限速按 IP 计费（IP 也可能只是审计/风控日志），但它**大幅提高了"IP 是限速维度之一"的先验**，把"换 IP 实验"从"碰运气"提升到"有明确机理假设的证伪实验"。

---

## 3. A–E 逐条评估

### A. 换 IP / 换出口 — **⚠️ 值得测（最高优先级）**

| 项 | 内容 |
|---|---|
| 机理假设 | 总量上限 ≈135 KB/s 绑定在**出口 IP**（而非纯账号），同一账号换 IP 可获第二份额度 |
| 支持证据 | F5（`x-bs-client-ip`）、F8（双层限制结构） |
| 反证先例 | 原报告 §2.2：换直链签名不重置限速 → 说明限速**不**绑定在直链/签名上；但这与"绑 IP"**不矛盾**（同一 IP 不变） |
| 判定 | **⚠️ 资料+推断，❓ 未验证**（代理当前不可用，实验未执行） |
| 预期 | 若限速纯账号级 → 代理后速度**不变**（≈75/39）；若含 IP 分量 → 代理后出现**第二份额度**（单连接回到 ~75 而基线已衰减到 39，或两路聚合突破 135） |

> ⚠️ **反面案例必须先排除**：Clash 走海外节点时，百度 CDN 可能把海外 IP 判定为"跨洋/风控"，**结果更慢甚至 403**。因此实验必须区分"换 IP 后变快"与"换 IP 后变慢"，且**必须用国内节点**做对照。

### B. IPv6 — **⚠️ 值得测（第二优先级，有前置条件）**

| 项 | 内容 |
|---|---|
| 机理 | IPv6 走移动 CDN 的另一套入口（`2409:8c00::/...`），若限速在 IPv6 侧实现不同/未生效，则免费提速 |
| 支持证据 | F1：百度**确实提供 IPv6 下载入口**（AAAA 存在）；F3：本机 DNS 已是 IPv6 DNS，说明运营商侧已部署 |
| 阻碍 | F2：**本机当前无 IPv6 出口**，实验无法就地做 |
| 判定 | **✅ 已验证"百度支持 IPv6"**（AAAA 存在）；**❓ 未验证"IPv6 通道限速策略是否不同"** |
| 可行性 | 国内家庭宽带（移动/电信/联通）普遍已给 IPv6，但**常需在路由器**开启 IPv6 或关闭光猫的 IPv6 过滤。用户机器在广东移动，属高概率可用 |

### C. 多连接不叠加的深层原因 — **⚠️ 部分证伪**

| 子假设 | 结论 | 依据 |
|---|---|---|
| 服务端令牌桶，按账号或 IP 计 | ⚠️ 与 F8 吻合（有总量上限），但**不是纯令牌桶**（未均分，有单连接硬上限） | F8 |
| 改 `dp-logid` 被视为不同会话 → 不共享桶 | **❌ 基本证伪**。单连接改 `dp-logid` 速度没变（39.4 vs 74.9，差异来自衰减而非标识）；并发时改 `dp-logid`+随机参数，聚合仍被压在 131.8 | F7/F8/F10 |
| 改 `dstime` 视为新会话 | **❌ 直接证伪**：服务端返回 `31360 expire time out error`，`dstime` 与签名强绑定，改了就直接拒 | F10 |
| 改 `dp-callid` / `r` | **❌ 无效**：`r` 改大数后 55.7 KB/s（低于基线，属衰减噪声）；`dp-callid=9` 38.9 | 实测 |
| 不同 cookie 组合算不同会话 | **❓ 未验证，且大概率无效**。F9 证明去鉴权即 403，无法构造"同一 BDUSS + 不同无关 cookie"的干净对照；基于 F8 的机制判断，服务端按账号 id 取额度，加无关 cookie 不改变账号 id | — |

**C 的净结论**：`dp-logid`/`dstime`/`r` 这类**请求级流水号不是限速分桶键**。它们是防重放/审计字段，不是配额键。**不要再在这个方向浪费时间。**

### D. HTTP/2 与多路复用 — **✅ 已证伪，不必测**

| 项 | 结论 |
|---|---|
| 百度 PCS 是否支持 HTTP/2 | **❌ 不支持**。4 个域名 ALPN 全部返回 `http/1.1`（F4） |
| 多 stream 绕过按连接计数 | **不适用**——根本没有 h2 通道，无法建立多 stream |
| 附带收益 | 既然只有 HTTP/1.1，**连接级 keep-alive 复用也无法突破单连接 75 KB/s 硬上限** |

> 这是一条**省时间**的结论：原报告建议"值得确认"，现在确认了——**不需要再测**。

### E. 分块下载接口 — **❌ 未发现，判定无效**

| 探测 | 结果 | 标记 |
|---|---|---|
| `chunk=1` / `part=N` 附加参数 | 被忽略，仍按 Range 返回 | ✅ |
| `/rest/2.0/pcs/superfile2?method=download` | `500 31238 upstream parse error` → **该接口只用于分片上传（`method=upload`），无下载能力** | ✅ |
| `m3u8=1`（HLS 切片） | `400 31023 param error , sign` → 需配套签名，且未见百度用于网盘下载 | ✅ |
| 老 PCS `/rest/2.0/pcs/file?method=download` | 可用（206），但**速率同样受限**（无独立额度） | ✅ |

**结论**：Range 就是唯一的分块原语。所谓"分块下载接口"在百度 PCS 上**不存在**（superfile2 是上传专用，名字误导）。

---

## 4. 【最重要】A 项精确实验方案

### 4.0 前置：当前环境不满足实验条件（✅ 已核实）

| 检查项 | 现状 | 需要 |
|---|---|---|
| `127.0.0.1:7897` | **未监听**（ConnectionRefused） | 启动 Clash 并开启"局域网连接"+系统代理 或 混合端口 7897 |
| 注册表 `ProxyEnable` | `0x0`（关闭） | 启动后应为 `0x1` |
| Clash 进程 | 无 | 启动 |
| 出口 IP | `120.229.205.229`（广东移动） | 记录基线 |

> **不满足就先别测**：没有代理时做"换 IP"实验等于空转。

### 4.1 实验设计原则（三条铁律）

1. **一次只改 IP 一个变量** —— 直链、账号、Range、并发数、UA 全部锁死。
2. **每次测量必须带同时段基线对照** —— 因为 F7 证明速率随时间衰减，跨时段对比会把"衰减"误读成"变快/变慢"。
3. **判据用"结构"而非"绝对值"** —— 看**单连接是否回到 75 档**、**两路聚合是否突破 135 档**，而不是看某次跑了多少。

### 4.2 精确步骤

#### 阶段 0：冷却（关键，不可跳过）

账号在短期内被反复测速后会进入衰减态（F7：75 → 39）。

```bash
# 停止所有对百度的下载请求，静置 ≥ 15 分钟
```

> ⚠️ 原报告的"单连接稳定 80.0 KB/s"和本报告的"衰减到 39"是**同一账号的不同冷却状态**。**冷却不充分，实验全部作废。**

#### 阶段 1：建立同冷却态基线（直连）

```bash
cd D:/plan/HANDOFF-direct-link-tool
# 每次实验前重新取直链（expires=8h，且旧链可能已衰减）
# 用现成的 .private/dlink.txt，若过期则重跑取链脚本
python -c "
import json,time,requests
ck=json.load(open('.private/baidu_cookies.json',encoding='utf-8'))
d=open('.private/dlink.txt',encoding='utf-8').read().strip()
H={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0','Cookie':'; '.join(f'{k}={v}' for k,v in ck.items()),'Referer':'https://pan.baidu.com/'}
for i in range(2):
    t0=time.perf_counter();n=0
    s=requests.Session();r=s.get(d,headers={**H,'Range':'bytes=0-67108863'},stream=True,timeout=(10,30))
    for c in r.iter_content(65536):
        n+=len(c)
        if time.perf_counter()-t0>15:break
    s.close();dt=time.perf_counter()-t0
    print(f'DIRECT #{i+1} {n/dt/1024:.1f} KB/s')
    time.sleep(5)
"
```

**记录**：`DIRECT_FIRST`（第一次）与 `DIRECT_STABLE`（第二次，视为稳态）。

#### 阶段 2：记录出口 IP（每一轮都要记）

```bash
python -c "import requests;print(requests.get('https://myip.ipip.net/',timeout=10).text)"
```

> 必须确认 IP **真的变了**。Clash 若未接管或走直连，IP 不变 → 本轮无效。

#### 阶段 3：代理环境下的同链测速

```bash
python - <<'EOF'
import json, time, requests
ck = json.load(open('.private/baidu_cookies.json', encoding='utf-8'))
d  = open('.private/dlink.txt', encoding='utf-8').read().strip()
H = {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0',
     'Cookie':'; '.join(f'{k}={v}' for k,v in ck.items()), 'Referer':'https://pan.baidu.com/'}
P = {'http':'http://127.0.0.1:7897','https':'http://127.0.0.1:7897'}

print('proxy出口IP:', requests.get('https://myip.ipip.net/', proxies=P, timeout=15).text[:80])
for i in range(2):
    t0=time.perf_counter(); n=0; st=0
    s=requests.Session()
    r=s.get(d, headers={**H,'Range':'bytes=0-67108863'}, proxies=P, stream=True, timeout=(15,40))
    st=r.status_code
    if st not in (200,206):
        print('  403/ERR body:', r.text[:120]); break
    for c in r.iter_content(65536):
        n+=len(c)
        if time.perf_counter()-t0>15: break
    s.close(); dt=time.perf_counter()-t0
    print(f'PROXY  #{i+1} HTTP {st}  {n/dt/1024:.1f} KB/s')
    time.sleep(5)
EOF
```

#### 阶段 4：**双出口并发实验（本方案的核心创新点）**

单连接对比只说"快/慢"，容易被衰减噪声污染。**真正的判据是并发**：如果限速含 IP 分量，那么"1 路直连 + 1 路代理"两个不同出口并行，聚合应**明显突破 135 KB/s 的账号上限**。

```bash
python - <<'EOF'
import json, time, requests, threading
ck = json.load(open('.private/baidu_cookies.json', encoding='utf-8'))
d  = open('.private/dlink.txt', encoding='utf-8').read().strip()
H  = {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0',
      'Cookie':'; '.join(f'{k}={v}' for k,v in ck.items()), 'Referer':'https://pan.baidu.com/'}
P  = {'http':'http://127.0.0.1:7897','https':'http://127.0.0.1:7897'}
out = {}

def one(key, rng, proxies, sec=20):
    n=0; t0=time.perf_counter(); st=0
    s=requests.Session()
    try:
        r=s.get(d, headers={**H,'Range':rng}, proxies=proxies, stream=True, timeout=(15,40))
        st=r.status_code
        if st not in (200,206): out[key]=(st,0); return
        for c in r.iter_content(65536):
            n+=len(c)
            if time.perf_counter()-t0>sec: break
    except Exception as e:
        out[key]=('ERR '+type(e).__name__,0); return
    finally: s.close()
    out[key]=(st, n/(time.perf_counter()-t0)/1024)

for label, pans in [('双路均直连(对照)', [None, None]),
                    ('直连+代理(核心)', [None, P])]:
    out.clear(); t0=time.perf_counter()
    ts=[threading.Thread(target=one, args=('a','bytes=0-33554431', pans[0])),
        threading.Thread(target=one, args=('b','bytes=33554432-67108863', pans[1]))]
    for t in ts: t.start()
    for t in ts: t.join()
    agg = sum(v[1] for v in out.values())
    print(f'{label:20} agg={agg:7.1f} KB/s  per={ {k:(v[0],round(v[1],1)) for k,v in out.items()} }')
    time.sleep(10)
EOF
```

#### 阶段 5（可选，最强）：手机热点作为**真正独立的第三方出口**

热点是**完全不同的运营商链路**，比代理更干净（代理可能被百度识别为数据中心 IP 而触发风控）。

1. 手机开热点，PC 连热点（**关闭 Clash，系统代理 ProxyEnable=0**）
2. 记录新 IP：`python -c "import requests;print(requests.get('https://myip.ipip.net/',timeout=10).text)"`
3. 跑阶段 1 同样的脚本，记 `HOTSPOT` 值
4. 若热点是**电信/联通**而基线是移动 → 额外得到"跨运营商是否分桶"的信息

### 4.3 判读标准（照表判，不要自由心证）

设 `DIRECT_STABLE` 为冷却后单连接稳态（本次实测参考值 **~75 KB/s**；若冷却不足会是 ~39）。

| 观察 | 判读 | 结论 |
|---|---|---|
| 代理测速 **≈ DIRECT_STABLE ±15%** | 换 IP 无效果 | **限速 = 账号级**（或 IP 与账号同一桶）。**A 方向终止** |
| 代理单连接 **明显 > DIRECT_STABLE（≥1.5×）** | 换 IP 有效 | **限速含 IP 分量**。但先排除"代理线路绕开限制"的其他解释（见 §4.4） |
| 代理**更慢**（常见） | 海外/机房 IP 被区别对待 | **不能判定限速类型**。换**国内节点**重测，或用手机热点 |
| 代理 403 / `31045` / `31360` | 百度风控拦截该 IP 或压测过密 | 本轮无效，冷却后重试 |
| **直连+代理聚合 > 135 KB/s 且两路都 > 60** | **最强证据：两个出口各拿一份额度** | **限速含 IP 分量，A 方向成立** |
| 双路聚合 ≤ 135（≈与双路直连的 108–131 同） | 两出口共享同一桶 | **限速 = 账号级，换 IP 无效** |

> **优先采信"双出口并发"判据**：单连接对比受 F7 衰减影响大，并发判据是结构性的，抗噪。

### 4.4 必须排除的混淆因素

| 混淆 | 为什么危险 | 怎么排除 |
|---|---|---|
| **时间衰减**（F7） | 会把"后测的"误判成"更慢" | 每轮都夹一次直连基线，做配对比较 |
| **代理线路本身限速** | 代理带宽小 → 测出 39 以为是百度限 | 先用代理测速其他大文件（如 `speedtest` 节点）确认代理本身 ≥ 5 MB/s |
| **代理节点在境外** | 百度对境外 IP 可能更严 | **务必用国内节点**（香港也多有区别对待） |
| **Clash 分流规则** | `baidu.com` 可能走直连规则 → IP 根本没变 | 每轮打印出口 IP 确认（阶段 2） |
| **同一账号并发风控** | 2 路以上开始 403 | 并发实验**只开 2 路**，不开 3+ |

---

## 5. IPv6 探测方案

### 5.1 为什么值得测（两条实测支撑）

- **F1**：百度 CDN **确有 AAAA 记录**（`2409:8c00:...`），不是"没做 IPv6"。
- **F3 + F6**：用户是**广东移动**、DNS 已是 `240c::6666`（IPv6 DNS），运营商侧 IPv6 已部署，只是本机/路由器没通。

> 移动网络的 IPv6 覆盖率在国内运营商里最高。**这条路的成功率不低。**

### 5.2 前置：先解决"本机无 IPv6"（F2）

按顺序排查，每步都跑下面的一行探测：

```bash
python -c "
import socket
s=socket.socket(socket.AF_INET6,socket.SOCK_STREAM);s.settimeout(5)
try:
    s.connect(('2409:8c00:6c21:103f:0:ff:b05e:456c',443));print('IPv6 出口 OK')
except Exception as e:print('IPv6 出口 FAIL',type(e).__name__)
"
```

| 步骤 | 操作 | 判据 |
|---|---|---|
| 1 | 光猫后台看宽带连接是否勾选 **IPv6**（移动光猫常在"网络→宽带设置→IP模式"选 IPv4&IPv6） | 改后本机出现 `2409:` 开头地址 |
| 2 | 路由器开启 **IPv6 / DHCPv6-PD**（华硕/小米/TP 均在"IPv6"页选 Native/DHCPv6） | 同上 |
| 3 | 关掉路由器 **IPv6 防火墙对入站的严格拦截**（出站一般不受限，先不动） | 同上 |
| 4 | Windows 确认 `netsh interface ipv6 show global` 未被禁用 | — |
| 5 | 若路由器是运营商定制的"IPv6 阉割固件" → 光猫桥接 + 换路由 | 最后手段 |

### 5.3 探测矩阵（拿到 IPv6 后执行）

```bash
python - <<'EOF'
import json, socket, ssl, time, requests
ck = json.load(open('.private/baidu_cookies.json', encoding='utf-8'))
d  = open('.private/dlink.txt', encoding='utf-8').read().strip()
H  = {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0',
      'Cookie':'; '.join(f'{k}={v}' for k,v in ck.items()), 'Referer':'https://pan.baidu.com/'}

# 用 IPv6 直连百度 CDN 并完成 HTTP/1.1 请求（绕开 DNS 的 v4 优先）
import http.client
host = 'd.pcs.baidu.com'
addr6 = socket.getaddrinfo(host, 443, socket.AF_INET6)[0][4][0]
print('目标 IPv6:', addr6)

class V6Conn(http.client.HTTPSConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM); self.sock.settimeout(10)
        self.sock.connect((addr6, 443))
        self.sock = self._context.wrap_socket(self.sock, server_hostname=host)

ctx = ssl.create_default_context()
from urllib.parse import urlparse
u = urlparse(d)
c = V6Conn(host, context=ctx)
t0=time.perf_counter(); n=0
c.putrequest('GET', u.path+'?'+u.query)
for k,v in H.items(): c.putheader(k,v)
c.putheader('Range','bytes=0-67108863'); c.endheaders()
r=c.getresponse(); print('HTTP', r.status)
while time.perf_counter()-t0 < 15:
    b=r.read(65536)
    if not b: break
    n+=len(b)
dt=time.perf_counter()-t0
print(f'IPv6 直连 {n/dt/1024:.1f} KB/s')
print('x-bs-client-ip:', r.getheader('x-bs-client-ip'))
c.close()
EOF
```

### 5.4 判读标准

| 观察 | 判读 |
|---|---|
| 连不上（超时） | 本机 IPv6 不通 → 回到 §5.2 |
| 连上但 **403 `31045`** | IPv6 入口**不认 IPv4 侧的 cookie/签名** → 需从 IPv6 侧重新登录取链（BDUSS 域绑定可能不同） |
| 连上且 206，速率 **≈75 KB/s** | IPv6 与 IPv4 同桶 → **IPv6 无收益** |
| 连上且 206，速率 **显著不同**（>1.5× 或 <0.5×） | **IPv6 走独立策略** → 有收益（或更差），值得深挖 |
| 观察到 `x-bs-client-ip` 变成 IPv6 形式 | **直接证明服务端按 IP 分桶**，与实验 A 结论互相印证 ⭐ |

> **额外判据**：比较 `x-bs-client-ip` 头在 v4/v6 下的值。若服务端把 v6 地址单独记录，说明两套入口是**不同记账单元**——这是 §2.1 推断的最直接验证。

---

## 6. 假设 vs 事实 一览（纪律检查）

| 陈述 | 类型 | 依据 |
|---|---|---|
| 百度不支持 HTTP/2 | **事实** | F4 实测 ALPN |
| 没有分块下载接口 | **事实** | F11 四个接口全测 |
| `dp-logid`/`dstime` 不是配额分桶键 | **事实（强证据）** | F10 直接 403；并发改参数聚合不变 |
| 百度提供 IPv6 下载入口 | **事实** | F1 AAAA |
| 本机无 IPv6 出口 | **事实** | F2 |
| 服务端记录客户端 IP | **事实** | F5 → `x-bs-client-ip` |
| 限速 = 单连接硬上限 + 账号/出口总上限 | **假设（有数据支持）** | F8，但"为什么总有一路满速"未解释 |
| 总上限绑定在 IP 上 | **假设（未验证）** | 仅 F5 的间接支持。**这正是实验 A 要证伪的** |
| 换 IP 能提速 | **未知** | ❓ 实验未做（代理未开） |
| IPv6 通道限速策略不同 | **未知** | ❓ 实验未做（无 IPv6 出口） |

---

## 7. 参考来源

本次 web_search 接口因 API key 失效（HTTP 401）**不可用**，全部结论来自本机实测。可外部核对的公开资料：

| 主题 | 来源 | 用途 |
|---|---|---|
| BaiduPCS-Go（第三方客户端，含 `-p` 并发 / IPv6 相关实现） | https://github.com/qjfoidnh/BaiduPCS-Go | 观察第三方实现是否尝试过多连接/换 endpoint |
| 百度网盘开放平台（PCS REST 文档，`superfile2` 官方定位为上传接口） | https://pan.baidu.com/union/doc/ | 佐证 §E 结论 |
| `h2` / ALPN 协商行为（RFC 7301, RFC 7540） | https://www.rfc-editor.org/rfc/rfc7301 | 佐证 F4 判定方法正确 |
| 中国运营商 IPv6 部署（`2409:8c00::/20` 为中国移动） | APNIC / `ip.ping0.cc` 段表 | 佐证 F1 归属 |
| 原项目实测记录 | `docs/BAIDU-SPEEDTEST.md`、`docs/FINDINGS.md` | 基线数据 |

---

## 8. 核心判断

### 换 IP 是否值得测？— **值得，但要按 §4 的严格方案测，且预期偏悲观**

- **值得测的理由**：`x-bs-client-ip` 这个响应头是本次唯一**指向"IP 是限速链路实参"**的硬证据。原报告 2.2 节证明"换签名不重置"——但签名和 IP 是两个维度，签名无效**不能推出** IP 无效。这是一个尚未被证伪的假设，而且**一次 2 路并发实验就能干净地证伪它**。
- **预期偏悲观**：F8 显示 3 路并发只把 75 拉到 131（1.75×）就到顶，说明存在**紧的总量闸**。若该闸按账号计，换 IP 白费。
- **成本极低**：冷却 15 分钟 + 4 个脚本约 20 分钟，无代码改动。
- **最大陷阱**：代理节点在境外 → 百度更严 → **测出"更慢"却误判为"换 IP 有害"**。必须用国内节点，或直接用手机热点。

### IPv6 是否值得测？— **值得，但要先花 10 分钟解决"本机无 IPv6"**

- **值得测的理由**：百度**确实有 IPv6 入口**（F1），用户**恰好在移动网络**（IPv6 覆盖最好），且**运营商 DNS 已是 IPv6**（F3）——说明只差路由器/光猫一个开关。
- **潜在收益不对称**：这是唯一可能"不花钱、不换账号、直接拿到另一套限速策略"的路径。若 v6 入口由不同 CDN 集群承载，限速策略可能确实不同。
- **前置成本**：若路由器无法开 IPv6（运营商定制固件限制），则**放弃**，不要为此折腾光猫桥接（对普通用户风险过高）。

### 不建议再投入的方向

| 方向 | 理由 |
|---|---|
| **D. HTTP/2 多路复用** | ✅ 已实测证伪：ALPN 只有 `http/1.1`。**不必测** |
| **E. 分块下载接口** | ✅ 已实测：`superfile2` 是上传接口，`chunk` 被忽略。**不存在** |
| **C. 会话标识分桶** | ✅ 实测：`dstime` 直接 403，`dp-logid` 并发无效果。**放弃** |
| 多账号 / 共享 | 用户已否决 |

### 一句话行动建议

> **先做 A 的"双出口并发"实验（20 分钟，用国内节点或手机热点）**——它能一次性判定限速是"账号级"还是"IP+账号级"，这个信息对本项目的价值远超速度本身（它决定"多出口加速"是不是一条可工程化的产品特性）。**同时并行开 IPv6**，若路由器能开，10 分钟就能拿到 §5.3 的探测结果。

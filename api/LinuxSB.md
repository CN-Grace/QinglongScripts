# linux.sb（烧饼社区）签到 API 文档

## 脚本信息

- **文件名**: `LinuxSB.py`
- **定时任务**: `0 9 * * *`（每天 09:00 执行，避开站点 0 点重置高峰）
- **功能**: linux.sb（烧饼社区，奖励单位「积分」）自动签到 + 点击侧栏有奖广告领取积分
- **说明**: 站点没有公开的 JSON API，全部操作都是「页面里真实存在的表单」。脚本从 HTML 动态解析表单 `action` 与隐藏字段（含 `_csrf`），因此站点改版只要表单结构不变就无需改代码。所有写操作一律不自动重试，超时只报告「结果未知」。

## 环境变量

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `LSB_COOKIE` | ✅ | linux.sb Cookie，多账号用 `&` 或换行分隔 |
| `LSB_RANDOM` | ❌ | 随机延迟开关（默认 `true`）：签到前随机等待 0–300 秒，点击广告间隔 3–8 秒 |
| `LSB_ADS` | ❌ | 点击有奖广告开关（默认 `true`）。设为 `false` 即「只签到」模式 |
| `LSB_AD_TOGGLE` | ❌ | 广告板显示/隐藏开关（默认 `true`） |
| `LSB_USER_AGENT` | ❌ | 自定义 User-Agent，需与登录设备一致（默认桌面 Chrome） |

> 💡 Cookie 获取：浏览器登录 linux.sb 后按 `F12` → Console 执行 `document.cookie`，或 Network → 任意请求 → 复制请求头里的完整 `Cookie`（至少包含 `bbs_csrf` 与 `server_session_*`）。也支持粘贴 `Cookie:` 前缀。
>
> ⚠️ 该站点有 Cloudflare 防护，请勿使用脚本自报 UA（如 `python-requests/x.y`），保持默认浏览器 UA。

## API 接口

> 站点为 PHP 论坛（Flarum 风格），所有写操作均为 `multipart/form-data`（对应浏览器 `FormData`），并需要请求头 `X-Requested-With: XMLHttpRequest`。

### 1. 访问首页（触发自动签到）

**请求:**
```
GET https://linux.sb/
```

**说明:** 站点会在服务端渲染时自动完成当日签到，并把回执写进内联脚本的 `window.__pageFlash`。脚本同时从该页面解析登录态与整个侧栏广告板。

**关键 DOM:**

| 选择器 / 属性 | 用途 |
|---|---|
| `.user-card` 内的 `.user-name` | 登录用户名（访客为 `访客` + `.visitor-avatar` / `.side-auth`） |
| `[data-sidebar-promotions-board]` | 侧栏广告板容器 |
| `[data-sidebar-promotions-board][data-sidebar-promotions-can-hide]` | 今日是否允许隐藏广告板（`1` / `0`） |
| `[data-sidebar-promotions-wrap]` | 广告板内容区，带 `hidden` 或 `is-hidden` 表示当前已隐藏 |
| `[data-sidebar-promotions-ad-link="1"]` | 广告位（访客版共 23 个） |
| `[data-sidebar-promotions-lottery-slot="1"]` | **有奖广告（红点）**，即需要点击领取的位置 |
| `[data-sidebar-promotions-order-id]` / `[data-promotion-id]` | 点击时必填的 `order_id` / `placement_key` |
| `[data-sidebar-promotions-lottery-done="1"]` | 该位置今日已领过 |

> 未登录时首页会被重定向到 `/login`，脚本据此判定 Cookie 失效。

### 2. 签到（首页 flash 的回退方案）

**请求:**
```
GET  https://linux.sb/daily_checkin
POST {签到表单的 action}      # 页面里第一个 action 含 checkin 的 POST 表单
Content-Type: multipart/form-data
```

**参数:** 表单自带的隐藏字段（按页面实际内容原样提交；签到表单不强制要求 `_csrf`）。

**判定:**

| 页面内容 | 处理 |
|---|---|
| `window.__pageFlash` 含 `已帮您完成自动签到` | 成功（首页已代签，无需再提交表单） |
| 页面文本含 `已签到` / `今日已签` / `明日再来` | 今日已签到，视为正常 |
| 探测或提交被重定向到 `/login` | Cookie 失效 |
| 找不到 `method=post` 且 action 含 `checkin` 的表单 | 报告「结果未知」，请站内人工核对 |

### 2.1 签到积分

每日签到**不是**固定额度。按站长公告：

> 每日签到奖励积分即日起由活跃度决定，活跃度越高签到积分越高！公式很简单，就是活跃度乘以 10。
> 活跃度查询：`https://linux.sb/community_activity`

积分会计入首页 flash 回执，真实格式如下（本站 UID 1 的实例帖就是这句话）：

```
您是社区主理人用户组，已帮您完成自动签到！积分+98
```

源码 `plugins.js` 只在 flash **以「您是」开头且含「已帮您完成自动签到」**时才弹提示，即这条文案就是「本次确实到账」的信号。脚本用 `积分\\s*[+＋]\\s*(\\d+)` 提取数量并计入报告总计。

> ⚠️ **只从 flash 或接口 JSON 里取积分，绝不扫签到记录页正文** —— 记录页列着历史每天的「积分+N」，扫正文会把昨天的奖励当成本次的。
>
> 旧文案（`已帮您完成自动签到，祝您今天有个好心情。`）不含数量，此时积分记为 0，不猜测。

### 3. 点击有奖广告（核心）
**请求:**
```
POST https://linux.sb/promotions_click
Content-Type: multipart/form-data
X-Requested-With: XMLHttpRequest
Origin: https://linux.sb
Referer: https://linux.sb/
```

**参数:**

| 字段 | 来源 | 说明 |
|---|---|---|
| `_csrf` | 表单隐藏字段 | 每次点击前重新读取首页获取最新值 |
| `reward_day` | 表单隐藏字段 | 站点声明的当天日期，用于跨日检测 |
| `placement_key` | `[data-promotion-id]` | 广告位标识 |
| `order_id` | `[data-sidebar-promotions-order-id]` | 广告订单号 |

**响应:**
```json
{
  "ok": true,
  "message": "广告抽奖中奖啦！",
  "rewarded": true,
  "points": 12,
  "already_drawn": false,
  "hidden": false,
  "lottery": {
    "total": 3,
    "completed": 1,
    "can_hide": false,
    "slots": { "user_sidebar_image_2": { "claimed": true } }
  },
  "range_min": 5,
  "range_max": 20,
  "triggered": true
}
```

**字段说明:**

| 字段 | 含义 |
|---|---|
| `ok` | 站点是否确认本次操作；为 `false` 时 `message` 为原因 |
| `rewarded` + `points` | 两者同时成立才算「明确新增积分」；`points` 为 `0` 时不计入 |
| `already_drawn` | 该位置今日已完成 |
| `hidden` | 为 `true` 时通常表示今日有奖广告已全部完成、广告板已自动隐藏；**但脚本不以此为完成凭据**：若仍有未领位置，会先 `POST /promotions_show` 恢复可见再继续领 |
| `lottery.total` / `lottery.completed` | 今日有奖广告总数 / 已完成数（**权威进度**） |
| `lottery.can_hide` | 是否已解锁隐藏广告板 |
| `lottery.slots[id].claimed` | 每个位置是否已领取 |
| `range_min` / `range_max` | 本次活跃度奖励区间 |

**领取凭据:** 每次点击后，脚本要求响应里出现下列任一项才算服务端承认本次领取 —— `rewarded=true` 且 `points>0`、`already_drawn=true`、或 `lottery.slots[该位置].claimed=true`。三者皆无时说明点击未被承认，脚本立即停止领饼并报「结果未知」，不会继续点下一个。

> 每次点击后脚本都会重新 `GET /` 刷新 `_csrf`、槽位状态与进度，并以服务端的 `lottery` 为准，因此同一天重复运行天然幂等。

> 广告板处于隐藏状态时，脚本会先 `POST /promotions_show` 恢复可见再领取 —— 站点按「广告是否被看到」计奖，隐藏状态下的点击可能不被计入。领取过程中收到 `hidden=true` 但仍有未领位置时，同样会先恢复可见再继续。

### 4. 显示 / 隐藏广告板

**请求:**
```
POST https://linux.sb/promotions_hide     # 隐藏
POST https://linux.sb/promotions_show     # 显示
Content-Type: multipart/form-data
```

**参数:** 各自表单的隐藏字段（含 `_csrf`）。

**说明:** 与站点前端逻辑保持一致 —— `hide` 仅在 `lottery.can_hide` 为真时才会真正生效（响应里 `triggered` 决定），`show` 总是生效。

脚本在领完广告后按「**显示 → 隐藏**」收尾，**结尾保持隐藏状态**。

为什么要先显示一次：站点要求「广告被看到」才计奖，而显示/隐藏这两个动作本身也可能触发暴击奖励（站内公告《广告隐藏功能上线可触发暴击得积分》），所以先把广告板亮一下拿奖励，再关掉。

完整时序（站点默认行为下）：

| 阶段 | 谁做的 | 广告板状态 |
|---|---|---|
| 领取前 | 脚本 `_ensure_visible` | 强制可见（隐藏态点击可能不计奖） |
| 领完 | **站点自动隐藏** | 隐藏 |
| 收尾① | 脚本 `promotions_show` | 可见（可能触发暴击奖励） |
| 收尾② | 脚本 `promotions_hide` | **隐藏（结束状态）** |

每一步都依据刷新后的真实 `hidden` 状态决定是否需要提交：**已是目标状态的步骤直接跳过**，不产生无谓写入。若站点领完没有自动隐藏（广告板仍可见），收尾的 `show` 会因已是目标状态而跳过，只发 `hide`。

> 隐藏不会影响下次领奖：`claim_ads` 里有 `_ensure_visible`，发现广告板处于隐藏态时会先 `POST /promotions_show` 恢复可见再开领。

## 输出报告格式

```
👤 账号 1 · 示例账号
  ✅ 签到：首页自动签到已回执（您是社区主理人用户组，已帮您完成自动签到！积分+98）
  🫓 每日签到：+98 积分
  🫓 有奖广告 1/3：明确新增 +12 积分
  ℹ️ 有奖广告 2/3：今日已完成该广告
  🫓 有奖广告 3/3：明确新增 +8 积分
  📺 广告板：隐藏广告板
  📈 今日有奖广告：3/3 已完成，剩余 0 次
  🫓 本次明确新增：+118 积分

📊 统计: 成功 1 · 失败 0 · 结果未知 0
──────────────────
🕒 执行时间: 2026-11-02 09:00:12
```

> 「本次明确新增」= 每日签到 + 有奖广告，两项均以站点明确回执为准（签到取 flash 里的 `积分+N`，广告取接口的 `points`）。站点未给出数字的（如旧版签到文案）不计入，避免虚构数值。

## 设计约束

- **写操作绝不自动重试。** 签到、领奖、显示/隐藏都是非幂等写操作，超时或 5xx 时无法确认服务端是否已生效，重试可能造成重复领取，因此只报告「结果未知，请站内核对」。脚本未装配 `urllib3.Retry`。
- **失败即停，不猜接口。** 提交前校验表单存在、`method` 为 POST、`_csrf`（及 `reward_day`）齐备；任一不满足就放弃并说明原因，而不是猜测接口盲发请求。表单 `action` 必须属于 `linux.sb` 且为 `https`。
- **结果未知时立刻收手。** 任一写操作结果未知（超时/断连/响应不可解析）后，后续步骤一律不再执行 —— 状态不明时继续写只会放大不确定性。
- **单账号异常不牵连全局。** 某个账号崩溃只标记该账号失败，其余账号继续执行，报告与通知照常发出。
- **不落本地进度文件。** 站点返回的 `already_drawn` / `hidden` / `lottery.completed` 是权威状态，本地记账反而会在多端并发时出错。
- **跨日即中止。** 执行期间若页面 `reward_day` 变化（跨过站点 0 点重置），立即停止领饼。
- **点击间隔 3–8 秒随机**，避免高频请求触发风控。
- **零额外依赖**，仅用 `requests` + 标准库 `html.parser`（不引入 bs4 / lxml）。
- **不把「没红点」当成「已完成」。** 广告板只有展示型广告时跳过领饼并计为正常；若连展示广告位都没渲染，说明页面结构可能变化，报告「结果未知」而非默默宣告成功。

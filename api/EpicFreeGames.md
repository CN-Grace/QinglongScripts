# Epic 免费游戏 API 文档

## 脚本信息

- **文件名**: `EpicFreeGames.py`
- **定时任务**: `0 8 * * 6`（每周六早上 8:00 执行）
- **功能**: 抓取 Epic Games 每周免费游戏，合成一张图片推送到 Telegram；图片失败时降级为文字推送

## 环境变量

| 变量名 | 必填 | 默认 | 说明 |
|--------|------|------|------|
| `EPIC_LOCALE` | ❌ | `zh-CN` | 语言地区 |
| `EPIC_COUNTRY` | ❌ | `CN` | 国家代码，影响价格与可用性 |

推送依赖 `notifier.py` 的 Telegram 通道（`TG_BOT_TOKEN` + `TG_CHAT_ID`），未配置时只输出日志。

## 依赖

除 `requests` 外还需要 **Pillow**（`from PIL import Image`）。未安装时脚本会在导入阶段直接失败：

```bash
pip install pillow requests
```

首次运行还会下载中文字体到 `config/font.ttf`（约 5 MB，来自 `fonts-wqy-microhei`），之后复用本地缓存。

## 接口

### 1. 免费游戏列表

```
GET https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions
    ?locale={EPIC_LOCALE}
    &country={EPIC_COUNTRY}
    &allowCountries={EPIC_COUNTRY}
```

Epic Store 的 GraphQL 静态镜像接口，无需鉴权。

**响应结构（关键路径）:**

```
data.Catalog.searchStore.elements[]
  ├── title
  ├── productSlug / urlSlug          -> 商店链接
  ├── price.totalPrice
  │     ├── originalPrice             -> 原价
  │     └── discountPrice             -> 现价，为 0 才算免费
  ├── promotions.promotionalOffers[]  -> 本周免费
  │     └── promotionalOffers[].promotionalOffers[].startDate / endDate
  └── promotions.upcomingPromotionalOffers[]  -> 即将免费
```

### 2. 判定规则

| 判定 | 条件 |
|---|---|
| 是否免费 | `discountPrice == 0` 且存在 `promotionalOffers` |
| 本周免费 | 当前时间落在 `promotionalOffers` 的活动区间内 |
| 即将免费 | 时间落在 `upcomingPromotionalOffers` 的区间内 |
| 折扣百分比 | 由 `originalPrice` 与 `discountPrice` 反算 |

时间统一转换为北京时间（UTC+8）后再比较。

## 出图

用 Pillow 合成一张竖版图：

```
封面卡片  280 × 380
外边距    24
底部信息条 56 高
深色底    #16161C
卡片底    #202028
本周强调色 #4682DC（蓝）
即将强调色 #DCA03C（橙）
```

每款游戏渲染为一张封面卡（封面图 + 标题 + 原价划掉 + 免费时间），本周区与即将区用不同强调色分隔。

封面图从接口给的 `keyImage` / `thumbnail` 里挑尺寸最合适的一张下载；下载失败时用纯色占位，不阻断整体出图。

## 推送

| 情况 | 行为 |
|---|---|
| 图片生成成功且 < 10 MB | `sendPhoto` 发图 + 说明文字 |
| 图片生成失败 / 超限 | 降级为文字推送（`build_text_report`） |
| 未配置 Telegram | 只打印日志 |

Telegram 图片上限 10 MB，超限会明确报错而不是静默丢弃。

## 输出报告格式

```
📷 图片推送说明文字：

🎮 Epic Games 每周免费游戏

🆓 2 款 | ⏳ 1 款

──────────────────
🕒 2026-11-07 08:00:12
```

文字降级形态：

```
Epic Games 每周免费游戏

[ 本周免费 2 款 ]
  Game A  (原价 ¥xx.xx)  2026-11-13 23:59 截止
  Game B  (原价 ¥xx.xx)  2026-11-13 23:59 截止
[ 即将免费 1 款 ]
  Game C  (原价 ¥xx.xx)  2026-11-14 00:00 起

store.epicgames.com/zh-CN/free-games
```

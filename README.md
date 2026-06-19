<div align="center">

# 🐉 QinglongScripts

**青龙面板自动任务订阅仓库，包含多个常用服务的自动签到/任务脚本。**

**A collection of auto check-in & task scripts for the Qinglong panel, covering various popular services.**

[![GitHub stars](https://img.shields.io/github/stars/CN-Grace/QinglongScripts?style=flat-square&logo=github)](https://github.com/CN-Grace/QinglongScripts/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/CN-Grace/QinglongScripts?style=flat-square&logo=github)](https://github.com/CN-Grace/QinglongScripts/network/members)
[![GitHub license](https://img.shields.io/github/license/CN-Grace/QinglongScripts?style=flat-square)](https://github.com/CN-Grace/QinglongScripts/blob/main/LICENSE)

</div>

---

## 📥 订阅方式 | How to Subscribe

在青龙面板中添加订阅：

| 配置项 | 值 |
|:---:|:---|
| **名称** | `QinglongScripts` |
| **类型** | 公开仓库 |
| **链接** | `https://github.com/CN-Grace/QinglongScripts.git` [📋 复制](#) |
| **定时类型** | crontab |
| **定时规则** | `0 0 * * *`（每天自动更新订阅） |
| **白名单** | 留空（拉取全部脚本） |
| **黑名单** | 留空 |

> 💡 青龙面板会根据脚本中的 `cron` 注释自动创建定时任务。`notifier.py` 和 `utils.py` 不含 cron 注释，不会被创建任务，可正常拉取和更新。

<details>
<summary>📋 一键复制订阅链接</summary>

```
https://github.com/CN-Grace/QinglongScripts.git
```

</details>

---

## 📜 脚本列表 | Script List

> 💡 脚本图标位于 [`docs/docs/src/scripts/`](docs/docs/src/scripts/) 目录

| 图标 | 脚本 | 功能 | 定时规则 | 说明 |
|:---:|:---:|:---:|:---:|:---|
| <img src="docs/src/scripts/airport.png" width="24"> | Airport.py | 机场签到 | `0 0 * * *` | 每天午夜执行 |
| <img src="docs/src/scripts/aliyun.png" width="24"> | Aliyun.py | 阿里云盘签到 | `0 0 * * *` | 每天午夜执行 |
| <img src="docs/src/scripts/bilibili.png" width="24"> | Bilibili.py | B站每日任务 | `0 0 * * *` | 每天午夜执行（漫画签到/投币/观看/分享） |
| <img src="docs/src/scripts/hellogithub.png" width="24"> | HelloGithub.py | HelloGithub 月刊 | `0 8 1 * *` | 每月1号早上8点执行 |
| <img src="docs/src/scripts/nodeseek.png" width="24"> | Nodeseek.py | Nodeseek 签到 | `0 0 * * *` | 每天午夜执行 |
| <img src="docs/src/scripts/ssl.png" width="24"> | SSL.py | SSL 证书检查 | `0 0 * * *` | 每天午夜执行，通过 Cloudflare API 自动获取域名列表并检查证书到期时间 |
| <img src="docs/src/scripts/skyland.png" width="24"> | skyland.py | 森空岛签到 | `0 8 * * *` | 每天早上8点执行（明日方舟/终末地） |
| <img src="docs/src/scripts/sync_password.png" width="24"> | Sync_Password.py | Bitwarden 备份 | `0 0 * * *` | 每天午夜执行，同步密码库 |
| <img src="docs/src/scripts/tieba.png" width="24"> | Tieba.py | 百度贴吧签到 | `0 0 * * *` | 每天午夜执行 |
| <img src="docs/src/scripts/valorantstore.png" width="24"> | ValorantStore.py | Valorant 商店 | `15 8 * * *` | 每天早上8:15执行，查询每日皮肤 |
| <img src="docs/src/scripts/yys.png" width="24"> | YysHuijuanTime.py | 阴阳师绘卷时间获取 | `0 0 * * *` | 每天午夜执行，获取活动时间 |
| — | notifier.py | 通知模块 | — | 公共依赖，非定时任务 |
| — | utils.py | 公共工具 | — | 公共依赖，非定时任务 |

---

## ⚙️ 环境变量 | Environment Variables

请在青龙面板的「环境变量」中配置各脚本所需的变量，详见 [`.env.example`](.env.example) 文件。

---

## 📡 API 文档 | API Documentation

各脚本调用的全部 API 接口详见 [`api/`](api/) 目录：

- [Airport.md](api/Airport.md) - 机场签到 API
- [Aliyun.md](api/Aliyun.md) - 阿里云盘签到 API
- [Bilibili.md](api/Bilibili.md) - Bilibili 每日任务 API
- [HelloGithub.md](api/HelloGithub.md) - HelloGithub 签到 API
- [Nodeseek.md](api/Nodeseek.md) - Nodeseek 签到 API
- [SSL.md](api/SSL.md) - SSL 证书监控 API
- [Skyland.md](api/Skyland.md) - 森空岛签到 API
- [Sync_Password.md](api/Sync_Password.md) - Bitwarden 密码同步 API
- [Tieba.md](api/Tieba.md) - 百度贴吧签到 API
- [ValorantStore.md](api/ValorantStore.md) - Valorant 商店查询 API
- [YysHuijuanTime.md](api/YysHuijuanTime.md) - 阴阳师绘卷时间获取 API

---

## 📣 通知推送 | Notifications

> 💡 通知渠道图标位于 [`docs/docs/src/notify/`](docs/docs/src/notify/) 目录

所有脚本通过 `notifier.py` 统一发送通知，支持以下 **23** 种渠道：

| 图标 | 渠道 | 必填环境变量 | 可选环境变量 |
|:---:|:---|:---|:---|
| <img src="docs/src/notify/bark.png" width="24"> | **Bark**（iOS 推送） | `BARK_PUSH` | `BARK_ARCHIVE` `BARK_GROUP` `BARK_SOUND` `BARK_ICON` `BARK_LEVEL` `BARK_URL` |
| <img src="docs/src/notify/console.png" width="24"> | **控制台输出** | `CONSOLE` | — |
| <img src="docs/src/notify/dingtalk.png" width="24"> | **钉钉机器人** | `DD_BOT_TOKEN` `DD_BOT_SECRET` | — |
| <img src="docs/src/notify/feishu.png" width="24"> | **飞书机器人** | `FSKEY` | — |
| <img src="docs/src/notify/go-cqhttp.png" width="24"> | **go-cqhttp** | `GOBOT_URL` `GOBOT_QQ` | `GOBOT_TOKEN` |
| <img src="docs/src/notify/gotify.png" width="24"> | **Gotify** | `GOTIFY_URL` `GOTIFY_TOKEN` | `GOTIFY_PRIORITY` |
| <img src="docs/src/notify/igot.png" width="24"> | **iGot** | `IGOT_PUSH_KEY` | — |
| <img src="docs/src/notify/serverchan.png" width="24"> | **Server酱** | `PUSH_KEY` | — |
| <img src="docs/src/notify/pushdeer.png" width="24"> | **PushDeer** | `DEER_KEY` | `DEER_URL` |
| <img src="docs/src/notify/synology.png" width="24"> | **Synology Chat** | `CHAT_URL` `CHAT_TOKEN` | — |
| <img src="docs/src/notify/pushplus.png" width="24"> | **PushPlus**（微信推送） | `PUSH_PLUS_TOKEN` | `PUSH_PLUS_USER` `PUSH_PLUS_TEMPLATE` `PUSH_PLUS_CHANNEL` `PUSH_PLUS_WEBHOOK` `PUSH_PLUS_CALLBACKURL` `PUSH_PLUS_TO` |
| <img src="docs/src/notify/weplusbot.png" width="24"> | **微加机器人** | `WE_PLUS_BOT_TOKEN` | `WE_PLUS_BOT_RECEIVER` `WE_PLUS_BOT_VERSION` |
| <img src="docs/src/notify/qmsg.png" width="24"> | **Qmsg酱** | `QMSG_KEY` `QMSG_TYPE` | — |
| <img src="docs/src/notify/wechat-work.png" width="24"> | **企业微信应用** | `QYWX_AM` | `QYWX_ORIGIN` |
| <img src="docs/src/notify/wechat-work.png" width="24"> | **企业微信机器人** | `QYWX_KEY` | `QYWX_ORIGIN` |
| <img src="docs/src/notify/telegram.png" width="24"> | **Telegram** | `TG_BOT_TOKEN` `TG_USER_ID` | `TG_API_HOST` `TG_PROXY_AUTH` `TG_PROXY_HOST` `TG_PROXY_PORT` |
| <img src="docs/src/notify/aibotk.png" width="24"> | **智能微秘书** | `AIBOTK_KEY` `AIBOTK_TYPE` `AIBOTK_NAME` | — |
| <img src="docs/src/notify/smtp.png" width="24"> | **SMTP 邮件** | `SMTP_SERVER` `SMTP_SSL` `SMTP_EMAIL` `SMTP_PASSWORD` `SMTP_NAME` | — |
| <img src="docs/src/notify/pushme.png" width="24"> | **PushMe** | `PUSHME_KEY` | `PUSHME_URL` |
| <img src="docs/src/notify/chronocat.png" width="24"> | **CHRONOCAT**（QQ 推送） | `CHRONOCAT_URL` `CHRONOCAT_QQ` `CHRONOCAT_TOKEN` | — |
| <img src="docs/src/notify/webhook.png" width="24"> | **自定义通知**（Webhook） | `WEBHOOK_URL` `WEBHOOK_METHOD` | `WEBHOOK_BODY` `WEBHOOK_HEADERS` `WEBHOOK_CONTENT_TYPE` |
| <img src="docs/src/notify/ntfy.png" width="24"> | **ntfy** | `NTFY_TOPIC` | `NTFY_URL` `NTFY_PRIORITY` |
| <img src="docs/src/notify/wxpusher.png" width="24"> | **WxPusher**（微信推送） | `WXPUSHER_APP_TOKEN` + (`WXPUSHER_TOPIC_IDS` 或 `WXPUSHER_UIDS`) | — |

**通知附加选项：**

| 选项 | 环境变量 | 说明 |
|:---|:---|:---|
| 💬 一言（随机句子） | `HITOKOTO` | 通知内容末尾附加一条随机句子，默认启用，设为 `false` 可关闭 |

只需在环境变量中配置对应渠道即可启用，详见 [`.env.example`](.env.example) 文件。✅

---

## 📁 项目结构 | Project Structure

```
QinglongScripts/
├── api/                    # API 文档目录
│   ├── Airport.md
│   ├── Aliyun.md
│   ├── Bilibili.md
│   ├── HelloGithub.md
│   ├── Nodeseek.md
│   ├── SSL.md
│   ├── Skyland.md
│   ├── Sync_Password.md
│   ├── Tieba.md
│   ├── ValorantStore.md
│   └── YysHuijuanTime.md
├── config/                 # 配置文件目录（自动创建，已 gitignore）
├── docs/                   # 项目文档
│   ├── index.html
│   └── src/                # 资源文件
│       ├── scripts/        # 脚本图标
│       └── notify/         # 通知渠道图标
├── .env.example            # 环境变量示例
├── .gitignore
├── Airport.py              # 机场签到
├── Aliyun.py               # 阿里云盘签到
├── Bilibili.py             # B站每日任务
├── HelloGithub.py          # HelloGithub 月刊
├── LICENSE                 # MIT 许可证
├── Nodeseek.py             # Nodeseek 签到
├── notifier.py             # 通知模块
├── README.md               # 项目说明
├── skyland.py              # 森空岛签到
├── SSL.py                  # SSL 证书检查
├── Sync_Password.py        # Bitwarden 备份
├── Tieba.py                # 百度贴吧签到
├── utils.py                # 公共工具
├── ValorantStore.py        # 掌瓦每日商店
└── YysHuijuanTime.py       # 阴阳师绘卷时间获取
```

---

## 🤝 贡献 | Contributing

欢迎提交 Issue 和 Pull Request！

## 📄 许可证 | License

本项目采用 [MIT License](LICENSE) 开源许可证。

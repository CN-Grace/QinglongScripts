# SSL 证书监控 API 文档

## 脚本信息

- **文件名**: `SSL.py`
- **定时任务**: `0 0 * * *` (每天0:00执行)
- **功能**: SSL 证书到期监控（自动从 Cloudflare 获取域名列表）

## 环境变量

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `CF_API_TOKEN` | 二选一 | Cloudflare API Token（需要 DNS:Read 权限） |
| `CF_API_EMAIL` | 二选一 | Cloudflare 账号邮箱（配合 Global API Key） |
| `CF_API_KEY` | 二选一 | Global API Key |
| `CF_ZONE_IDS` | ✅ | Cloudflare Zone ID，多个用逗号分隔 |
| `CF_DOMAINS` | ✅ | 自选顶级域名，多个用逗号分隔，与 CF_ZONE_IDS 一一对应 |
| `SSL_WARNING_DAYS` | ❌ | 证书到期警告天数，默认 30 天 |

### 认证方式

**方式1: API Token（推荐）**
```
CF_API_TOKEN=your_api_token_here
```

**方式2: Global API Key**
```
CF_API_EMAIL=your_email@example.com
CF_API_KEY=your_global_api_key_here
```

### 配置示例

```
CF_API_EMAIL=user@example.com
CF_API_KEY=1234567890abcdef1234567890abcdef
CF_ZONE_IDS=zone_id_1,zone_id_2
CF_DOMAINS=example.com,example.org
```

## 工作流程

### 1. 从 Cloudflare 获取域名列表

对每个顶级域名，调用 Cloudflare API 获取其下所有 A 记录：

**API 请求:**
```
GET https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=A
```

**请求头 (API Token):**
```
Authorization: Bearer {CF_API_TOKEN}
Content-Type: application/json
```

**请求头 (Global API Key):**
```
X-Auth-Email: {CF_API_EMAIL}
X-Auth-Key: {CF_API_KEY}
Content-Type: application/json
```

### 2. 检查 SSL 证书

对获取到的所有域名（自动去重），使用 Python ssl 模块连接 443 端口获取证书信息。

## Global API Key 获取方法

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com)
2. 点击右上角头像 → **My Profile** → **API Tokens**
3. 页面底部 **API Keys** 部分，点击 **Global API Key** 查看
4. 需要输入密码验证后才能查看

## Zone ID 获取方法

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com)
2. 选择你的域名
3. 在 **Overview** 页面右下角找到 **Zone ID**
4. 点击复制
5. 对每个需要监控的顶级域名重复上述步骤

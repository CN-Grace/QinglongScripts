# SSL 证书监控 API 文档

## 脚本信息

- **文件名**: `SSL.py`
- **定时任务**: `0 0 * * *` (每天0:00执行)
- **功能**: SSL 证书到期监控（自动从 Cloudflare 获取域名列表）

## 环境变量

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `CF_API_TOKEN` | ✅ | Cloudflare API Token（需要 DNS:Read 权限） |
| `CF_ZONE_IDS` | ✅ | Cloudflare Zone ID，多个用逗号分隔 |
| `CF_DOMAINS` | ✅ | 自选顶级域名，多个用逗号分隔，与 CF_ZONE_IDS 一一对应 |
| `SSL_WARNING_DAYS` | ❌ | 证书到期警告天数，默认 30 天 |

### 配置示例

```
CF_API_TOKEN=your_api_token_here
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

**请求头:**
```
Authorization: Bearer {CF_API_TOKEN}
Content-Type: application/json
```

**响应示例:**
```json
{
  "success": true,
  "result": [
    {
      "name": "www.example.com",
      "type": "A",
      "content": "192.168.1.1"
    },
    {
      "name": "api.example.com",
      "type": "A",
      "content": "192.168.1.2"
    }
  ],
  "result_info": {
    "page": 1,
    "per_page": 100,
    "total_pages": 1,
    "count": 2,
    "total": 2
  }
}
```

### 2. 检查 SSL 证书

对获取到的所有域名（自动去重），使用 Python ssl 模块连接 443 端口获取证书信息。

## 输出报告格式

```
🔔 SSL 证书检查报告

⏰ 检查时间: 2026-06-19 00:00:00
📊 总计: 5 个域名

❌ 已过期的证书:
   expired.example.com — 已过期 3 天 (到期: 2026-06-16)

⚠️ 即将过期的证书 (30天内):
   soon.example.com — 剩余 15 天 (到期: 2026-07-04 | 颁发: Let's Encrypt)

✅ 证书状态正常:
   www.example.com — 剩余 365 天
   api.example.org — 剩余 180 天

📈 统计信息:
   ✅ 正常: 2
   ⚠️ 警告: 1
   ❌ 过期: 1
   🔧 失败: 1

──────────────────
🕒 执行时间: 2026-06-19 00:00:00
```

## Cloudflare API Token 创建指南

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com)
2. 点击右上角头像 → **My Profile** → **API Tokens**
3. 点击 **Create Token**
4. 选择 **Edit zone DNS** 模板（或自定义权限）
5. 权限设置：`Zone - DNS - Read`
6. 区域选择：选择你需要监控的域名（可多选）
7. 创建后复制 Token（只显示一次）

## Zone ID 获取方法

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com)
2. 选择你的域名
3. 在 **Overview** 页面右下角找到 **Zone ID**
4. 点击复制
5. 对每个需要监控的顶级域名重复上述步骤

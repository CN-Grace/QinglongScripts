#!/usr/bin/env python3
"""
cron: 0 0 * * *
new Env("SSL证书检查")
SSL 证书检查脚本
- 通过 Cloudflare API 自动获取域名 A 记录列表
- 批量检查多个域名的 SSL 证书状态
- 获取证书到期时间、剩余天数、颁发者等信息
- 分类显示正常、警告、过期和检查失败的证书
"""

import os
import ssl
import socket
import requests
from datetime import datetime, timezone
from typing import List, Dict

from utils import log_info, log_success, log_warning, log_error, beijing_now, beijing_time_str
from notifier import send as notify_send

# ==================== 用户配置 ====================
# Cloudflare 配置（支持两种认证方式）
# 方式1: API Token（推荐，权限更细）
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
# 方式2: Global API Key（需要邮箱）
CF_API_EMAIL = os.environ.get("CF_API_EMAIL", "")
CF_API_KEY = os.environ.get("CF_API_KEY", "")
# 多个 Zone ID 用逗号分隔，与 CF_DOMAINS 一一对应
CF_ZONE_IDS = [z.strip() for z in os.environ.get("CF_ZONE_IDS", "").split(",") if z.strip()]
# 多个顶级域名用逗号分隔，与 CF_ZONE_IDS 一一对应
CF_DOMAINS = [d.strip() for d in os.environ.get("CF_DOMAINS", "").split(",") if d.strip()]

# SSL 检查配置
WARNING_THRESHOLD = int(os.environ.get("SSL_WARNING_DAYS", "30"))
CONNECTION_TIMEOUT = 10
# 常用 HTTPS 端口列表
HTTPS_PORTS = [443, 8443, 4443, 4433, 9443]

# Cloudflare API 基础地址
CF_API_BASE = "https://api.cloudflare.com/client/v4"


def get_cloudflare_a_records(zone_id: str, domain: str) -> List[str]:
    """通过 Cloudflare API 获取指定 Zone 下的所有 A 记录"""
    domains = []
    page = 1
    per_page = 100

    # 根据配置的认证方式设置请求头
    headers = {"Content-Type": "application/json"}
    if CF_API_TOKEN:
        headers["Authorization"] = f"Bearer {CF_API_TOKEN}"
    elif CF_API_EMAIL and CF_API_KEY:
        headers["X-Auth-Email"] = CF_API_EMAIL
        headers["X-Auth-Key"] = CF_API_KEY
    else:
        log_error("未配置 Cloudflare 认证信息，请设置 CF_API_TOKEN 或 CF_API_EMAIL + CF_API_KEY")
        return []

    try:
        while True:
            url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
            params = {
                "type": "A",
                "page": page,
                "per_page": per_page
            }

            response = requests.get(url, headers=headers, params=params, timeout=30)

            # 打印详细错误信息用于调试
            if response.status_code != 200:
                log_error(f"Cloudflare API HTTP 错误 ({domain}): {response.status_code}")
                log_error(f"响应内容: {response.text[:500]}")
                break

            data = response.json()

            if not data.get("success"):
                errors = data.get("errors", [])
                log_error(f"Cloudflare API 错误 ({domain}): {errors}")
                break

            records = data.get("result", [])
            if not records:
                break

            for record in records:
                name = record.get("name", "")
                if name and name not in domains:
                    domains.append(name)
                    log_info(f"发现 A 记录: {name} -> {record.get('content', '')}")

            # 检查是否有下一页
            result_info = data.get("result_info", {})
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        log_info(f"从 {domain} 获取到 {len(domains)} 个 A 记录域名")

    except requests.exceptions.RequestException as e:
        log_error(f"Cloudflare API 请求失败 ({domain}): {e}")
    except Exception as e:
        log_error(f"获取 Cloudflare A 记录时出错 ({domain}): {e}")

    return domains


def get_all_domains() -> List[str]:
    """从所有配置的 Cloudflare Zone 获取域名列表"""
    # 检查认证配置
    has_token = bool(CF_API_TOKEN)
    has_key = bool(CF_API_EMAIL and CF_API_KEY)
    if not has_token and not has_key:
        log_error("Cloudflare 认证配置不完整，请设置 CF_API_TOKEN 或 CF_API_EMAIL + CF_API_KEY")
        return []

    if not CF_ZONE_IDS or not CF_DOMAINS:
        log_error("Cloudflare 配置不完整，请检查 CF_ZONE_IDS 和 CF_DOMAINS 环境变量")
        return []

    if len(CF_ZONE_IDS) != len(CF_DOMAINS):
        log_error("CF_ZONE_IDS 和 CF_DOMAINS 数量不匹配，请检查配置")
        return []

    all_domains = []
    for zone_id, domain in zip(CF_ZONE_IDS, CF_DOMAINS):
        log_info(f"正在获取 {domain} 的 A 记录...")
        domains = get_cloudflare_a_records(zone_id, domain)
        for d in domains:
            if d not in all_domains:
                all_domains.append(d)

    log_info(f"共获取到 {len(all_domains)} 个不重复的 A 记录域名")
    return all_domains


def get_certificate_info(domain: str) -> Dict:
    """获取单个域名的 SSL 证书信息（自动尝试多个常用端口）"""
    # 如果域名已指定端口，只检查该端口
    if ":" in domain:
        host, port = domain.split(":")
        ports_to_try = [int(port)]
    else:
        host = domain
        ports_to_try = HTTPS_PORTS

    last_error = None
    for port in ports_to_try:
        result = {"domain": f"{host}:{port}", "expiry_date": datetime.min, "days_left": -1, "issuer": "", "is_valid": False, "error": None}
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=CONNECTION_TIMEOUT) as sock:
                with context.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()

            expiry_str = cert["notAfter"]
            expiry_date = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            days_left = (expiry_date - now).days
            issuer_dict = dict(x[0] for x in cert["issuer"])
            issuer = issuer_dict.get("organizationName", "Unknown")
            result.update({"expiry_date": expiry_date, "days_left": days_left, "issuer": issuer, "is_valid": days_left > 0})
            log_info(f"{host}:{port} 证书剩余 {days_left} 天")
            return result  # 找到有效端口就返回
        except Exception as e:
            last_error = str(e)
            continue  # 尝试下一个端口

    # 所有端口都失败
    result = {"domain": host, "expiry_date": datetime.min, "days_left": -1, "issuer": "", "is_valid": False, "error": f"所有端口检查失败: {last_error}"}
    log_error(f"{host} 所有端口检查失败")
    return result


def check_all_domains(domains: List[str]) -> List[Dict]:
    """检查所有域名的 SSL 证书"""
    results = []
    log_info(f"开始检查 {len(domains)} 个域名的 SSL 证书...")
    for domain in domains:
        log_info(f"正在检查: {domain}")
        cert_info = get_certificate_info(domain)
        results.append(cert_info)
        if cert_info["is_valid"]:
            if cert_info["days_left"] <= WARNING_THRESHOLD:
                log_warning(f"  ⚠️ {domain} 将在 {cert_info['days_left']} 天后过期")
            else:
                log_success(f"  ✅ {domain} 证书正常，剩余 {cert_info['days_left']} 天")
        elif cert_info["error"]:
            log_error(f"  🔧 {domain} 检查失败: {cert_info['error']}")
        else:
            log_error(f"  ❌ {domain} 证书已过期")
    return results


def categorize_certificates(certificates: List[Dict]) -> Dict[str, List[Dict]]:
    """将证书结果分类"""
    warning_certs, expired_certs, valid_certs, error_certs = [], [], [], []
    for cert in certificates:
        if cert["error"] is not None:
            error_certs.append(cert)
        elif cert["is_valid"]:
            if 0 < cert["days_left"] <= WARNING_THRESHOLD:
                warning_certs.append(cert)
            elif cert["days_left"] > WARNING_THRESHOLD:
                valid_certs.append(cert)
        else:
            expired_certs.append(cert)
    return {"warning": warning_certs, "expired": expired_certs, "valid": valid_certs, "error": error_certs}


def format_certificate_report(certificates: List[Dict]) -> str:
    """格式化证书检查报告（简化版：按主域名分组显示）"""
    cats = categorize_certificates(certificates)
    lines = ["🔔 SSL 证书检查报告", ""]

    # 按主域名分组
    domain_groups = {}
    for cert in certificates:
        # 从 A 解析域名中提取主域名（取最后两段）
        parts = cert["domain"].split(":")[0].split(".")
        if len(parts) >= 2:
            main_domain = ".".join(parts[-2:])
        else:
            main_domain = parts[0]

        if main_domain not in domain_groups:
            domain_groups[main_domain] = []
        domain_groups[main_domain].append(cert)

    # 按主域名显示
    for main_domain, certs in domain_groups.items():
        lines.append(f"主域名：{main_domain}")
        for cert in certs:
            # 提取 A 解析域名和端口
            domain_part = cert["domain"].split(":")[0]
            port = cert["domain"].split(":")[1] if ":" in cert["domain"] else "443"

            # 确定状态 emoji 和剩余天数
            if cert["error"]:
                emoji = "🔧"
                status = f"检查失败: {cert['error']}"
            elif not cert["is_valid"]:
                emoji = "❌"
                status = f"已过期 {abs(cert['days_left'])} 天"
            elif cert["days_left"] <= WARNING_THRESHOLD:
                emoji = "⚠️"
                status = f"剩余 {cert['days_left']} 天"
            else:
                emoji = "✅"
                status = f"剩余 {cert['days_left']} 天"

            lines.append(f"{emoji} {domain_part} - {port} - {status}")
        lines.append("")

    lines.append("─" * 18)
    lines.append(f"🕒 执行时间: {beijing_time_str()}")
    return "\n".join(lines)


def main():
    log_info("=" * 50)
    log_info("SSL 证书检查脚本开始执行")
    log_info("=" * 50)

    # 从 Cloudflare 获取域名列表
    domains_to_check = get_all_domains()

    if not domains_to_check:
        log_warning("未获取到任何域名，脚本结束")
        notify_send("SSL 证书检查报告", "⚠️ 未从 Cloudflare 获取到任何 A 记录域名，请检查配置")
        return

    cert_results = check_all_domains(domains_to_check)
    report = format_certificate_report(cert_results)
    notify_send("SSL 证书检查报告", report)

    cats = categorize_certificates(cert_results)
    print(f"\n{'=' * 60}")
    print(f"SSL 证书检查完成! 检查域名总数: {len(cert_results)}")
    print(f"正常: {len(cats['valid'])} | 警告: {len(cats['warning'])} | 过期: {len(cats['expired'])} | 失败: {len(cats['error'])}")
    print("=" * 60)

    if cats["expired"] or cats["warning"]:
        print("\n⚠️ 需要注意的域名:")
        for cert in cats["expired"]:
            print(f"  ❌ {cert['domain']} — 已过期")
        for cert in cats["warning"]:
            print(f"  ⚠️ {cert['domain']} — 剩余 {cert['days_left']} 天到期")

    log_info("=" * 50)
    log_info(f"脚本执行结束")
    log_info("=" * 50)


if __name__ == "__main__":
    main()

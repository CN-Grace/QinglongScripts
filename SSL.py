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
# Cloudflare 配置
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
# 多个 Zone ID 用逗号分隔，与 CF_DOMAINS 一一对应
CF_ZONE_IDS = [z.strip() for z in os.environ.get("CF_ZONE_IDS", "").split(",") if z.strip()]
# 多个顶级域名用逗号分隔，与 CF_ZONE_IDS 一一对应
CF_DOMAINS = [d.strip() for d in os.environ.get("CF_DOMAINS", "").split(",") if d.strip()]

# SSL 检查配置
WARNING_THRESHOLD = int(os.environ.get("SSL_WARNING_DAYS", "30"))
CONNECTION_TIMEOUT = 10

# Cloudflare API 基础地址
CF_API_BASE = "https://api.cloudflare.com/client/v4"


def get_cloudflare_a_records(zone_id: str, domain: str) -> List[str]:
    """通过 Cloudflare API 获取指定 Zone 下的所有 A 记录"""
    domains = []
    page = 1
    per_page = 100

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }

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
    if not CF_API_TOKEN or not CF_ZONE_IDS or not CF_DOMAINS:
        log_error("Cloudflare 配置不完整，请检查 CF_API_TOKEN、CF_ZONE_IDS、CF_DOMAINS 环境变量")
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
    """获取单个域名的 SSL 证书信息"""
    result = {"domain": domain, "expiry_date": datetime.min, "days_left": -1, "issuer": "", "is_valid": False, "error": None}
    try:
        hostname = domain if ":" in domain else f"{domain}:443"
        host, port = hostname.split(":")
        port = int(port)
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
        log_info(f"{domain} 证书剩余 {days_left} 天")
    except Exception as e:
        result["error"] = str(e)
        log_error(f"{domain} 检查失败: {e}")
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
    """格式化证书检查报告"""
    cats = categorize_certificates(certificates)
    lines = [f"🔔 SSL 证书检查报告", "", f"⏰ 检查时间: {beijing_time_str()}", f"📊 总计: {len(certificates)} 个域名", ""]

    if cats["expired"]:
        lines.append("❌ 已过期的证书:")
        for cert in cats["expired"]:
            expiry = cert["expiry_date"].strftime("%Y-%m-%d") if cert["expiry_date"] != datetime.min else "未知"
            lines.append(f"   {cert['domain']} — 已过期 {abs(cert['days_left'])} 天 (到期: {expiry})")
        lines.append("")

    if cats["warning"]:
        lines.append("⚠️ 即将过期的证书 (30天内):")
        for cert in cats["warning"]:
            expiry = cert["expiry_date"].strftime("%Y-%m-%d")
            lines.append(f"   {cert['domain']} — 剩余 {cert['days_left']} 天 (到期: {expiry} | 颁发: {cert['issuer']})")
        lines.append("")

    if cats["valid"]:
        lines.append("✅ 证书状态正常:")
        for cert in cats["valid"]:
            lines.append(f"   {cert['domain']} — 剩余 {cert['days_left']} 天")
        lines.append("")

    if cats["error"]:
        lines.append("🔧 检查失败的域名:")
        for cert in cats["error"]:
            lines.append(f"   {cert['domain']} — 错误: {cert['error']}")
        lines.append("")

    lines.append("📈 统计信息:")
    lines.append(f"   ✅ 正常: {len(cats['valid'])}")
    lines.append(f"   ⚠️ 警告: {len(cats['warning'])}")
    lines.append(f"   ❌ 过期: {len(cats['expired'])}")
    lines.append(f"   🔧 失败: {len(cats['error'])}")
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

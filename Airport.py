#!/usr/bin/env python3
"""
cron: 0 0 * * *
new Env("机场签到")
多服务机场自动签到脚本
- 自动登录多个指定服务网站
- 执行每日签到任务
- 获取剩余流量信息
- 发送格式化报告到通知渠道
"""

import os
import re
import sys
import time
import base64
import requests
from lxml import html
from typing import Dict, Any, List, Tuple

from utils import log_info, log_success, log_warning, log_error, beijing_time_str, create_session
from notifier import send as notify_send

# ==================== 服务配置 ====================
SERVICES = [
    {
        "name": "速鹰666",
        "base_url": "https://suying00.com",
        "login_path": "/auth/login",
        "checkin_path": "/user/checkin",
        "user_info_path": "/user",
        "traffic_xpath": '//*[@id="app"]/div/div[3]/section/div[3]/div[2]/div/div[2]/div[2]',
        "username": os.environ.get("SUYING_USERNAME", ""),
        "password": os.environ.get("SUYING_PASSWORD", ""),
    }
]

REQUEST_TIMEOUT = 10


# ==================== 功能函数 ====================
def login_to_service(service_config: Dict[str, str]) -> Tuple[bool, Any]:
    """登录到服务网站"""
    session = create_session()
    session.headers.update({"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})

    login_url = f"{service_config['base_url']}{service_config['login_path']}"
    login_data = {"email": service_config["username"], "passwd": service_config["password"], "remember_me": "on", "code": ""}

    try:
        log_info(f"正在登录 [{service_config['name']}] 账户: {service_config['username']}")
        response = session.post(login_url, data=login_data, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        login_success = False
        try:
            json_data = response.json()
            if json_data.get("ret") == 1 or json_data.get("success"):
                login_success = True
                log_success(f"[{service_config['name']}] 登录成功")
        except Exception:
            pass

        if not login_success:
            cookies = session.cookies.get_dict()
            if "uid" in cookies or "key" in cookies or "email" in cookies:
                login_success = True
                log_success(f"[{service_config['name']}] 登录成功")

        if not login_success and response.text:
            if any(kw in response.text.lower() for kw in ["登录成功", "login success", "success", "欢迎"]):
                login_success = True
                log_success(f"[{service_config['name']}] 登录成功")

        if login_success:
            return True, session
        log_error(f"[{service_config['name']}] 登录失败，响应: {response.text[:200]}")
        return False, "无法判断登录状态"
    except requests.exceptions.RequestException as e:
        log_error(f"[{service_config['name']}] 登录请求失败: {e}")
        return False, f"网络请求失败: {e}"
    except Exception as e:
        log_error(f"[{service_config['name']}] 登录未知错误: {e}")
        return False, f"未知错误: {e}"


def perform_checkin(session, service_config: Dict[str, str]) -> Dict[str, Any]:
    """执行签到"""
    checkin_url = f"{service_config['base_url']}{service_config['checkin_path']}"
    result = {"success": False, "message": "签到失败", "details": {}}
    try:
        log_info(f"[{service_config['name']}] 正在执行签到...")
        response = session.post(checkin_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        try:
            json_data = response.json()
            result["details"] = json_data
            result["message"] = json_data.get("msg", "签到完成")
            result["success"] = True
            log_success(f"[{service_config['name']}] 签到成功: {result['message']}")
        except ValueError:
            result["message"] = "签到完成，返回非标准格式"
            result["details"] = {"raw_response": response.text[:500]}
            result["success"] = True
            log_success(f"[{service_config['name']}] 签到完成")
    except requests.exceptions.RequestException as e:
        log_error(f"[{service_config['name']}] 签到请求失败: {e}")
        result["message"] = f"网络请求失败: {e}"
    except Exception as e:
        log_error(f"[{service_config['name']}] 签到未知错误: {e}")
        result["message"] = f"未知错误: {e}"
    return result


def extract_and_decode_base64(html_content):
    """从 HTML 中提取 base64 内容并解码"""
    pattern = r'var\s+originBody\s*=\s*["\']([^"\']+)["\']'
    match = re.search(pattern, html_content)
    if match:
        try:
            decoded_bytes = base64.b64decode(match.group(1))
            return decoded_bytes.decode("utf-8")
        except Exception as e:
            log_warning(f"Base64 解码失败: {e}")
            return None
    return None


def get_traffic_info(session, service_config: Dict[str, str]) -> Dict[str, Any]:
    """获取剩余流量信息"""
    info_url = f"{service_config['base_url']}{service_config['user_info_path']}"
    result = {"success": False, "traffic": "获取失败", "raw_html": None, "error": None}
    try:
        log_info(f"[{service_config['name']}] 正在获取流量信息...")
        response = session.get(info_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        raw_html = response.text
        result["raw_html"] = raw_html

        decoded_html = extract_and_decode_base64(raw_html)
        html_to_parse = decoded_html or raw_html
        tree = html.fromstring(html_to_parse)
        traffic_elements = tree.xpath(service_config["traffic_xpath"])

        if traffic_elements:
            traffic_text = traffic_elements[0].text_content().strip()
            result["traffic"] = traffic_text
            result["success"] = True
            log_success(f"[{service_config['name']}] 流量信息: {traffic_text}")
        else:
            result["error"] = "XPath 未找到匹配元素"
            log_warning(f"[{service_config['name']}] {result['error']}")
    except requests.exceptions.RequestException as e:
        log_error(f"[{service_config['name']}] 获取流量请求失败: {e}")
        result["error"] = f"网络请求失败: {e}"
    except Exception as e:
        log_error(f"[{service_config['name']}] 解析流量未知错误: {e}")
        result["error"] = f"解析错误: {e}"
    return result


def format_multi_checkin_report(results: List[Dict[str, Any]]) -> str:
    """格式化多服务签到报告"""
    lines = []

    for idx, res in enumerate(results, 1):
        name = res["name"]
        checkin = res["checkin_result"]
        traffic = res["traffic_result"]
        login_error = res.get("login_error")

        lines.append(f"{idx}. {name}")
        if login_error:
            lines.append(f"   🔐 登录: ❌ 失败 — {login_error}")
            lines.append(f"   📝 签到: 跳过 | 📊 流量: 跳过")
        else:
            ci_emoji = "✅" if checkin["success"] else "❌"
            lines.append(f"   📝 签到: {ci_emoji} — {checkin['message']}")
            if traffic["success"]:
                lines.append(f"   📊 流量: ✅ {traffic['traffic']}")
            else:
                lines.append(f"   📊 流量: ❌ 获取失败")
                if traffic.get("error"):
                    lines.append(f"      错误: {traffic['error']}")
        lines.append("")

    lines.append("─" * 18)
    lines.append(f"🕒 执行时间: {beijing_time_str()}")
    return "\n".join(lines)


def save_debug_info(service_name, traffic_result, checkin_result):
    """保存调试信息"""
    if traffic_result.get("raw_html"):
        filename = f"debug_{service_name}_{beijing_time_str('%Y%m%d_%H%M%S')}.html"
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"=== 调试信息 ===\n服务: {service_name}\n时间: {beijing_time_str()}\n签到: {checkin_result}\n流量: {traffic_result.get('traffic', 'N/A')}\n\n=== 原始HTML ===\n{traffic_result['raw_html']}")
            log_info(f"调试信息已保存到: {filename}")
            return filename
        except Exception as e:
            log_error(f"保存调试信息失败: {e}")
    return None


def main():
    log_info("=" * 50)
    log_info("多服务自动签到脚本开始执行")
    log_info("=" * 50)

    start_time = time.time()
    results = []

    for service in SERVICES:
        service_name = service["name"]
        log_info(f"--- 开始处理: {service_name} ---")

        login_success, login_result = login_to_service(service)
        if not login_success:
            log_error(f"[{service_name}] 登录失败: {login_result}")
            results.append({"name": service_name, "login_error": login_result, "checkin_result": None, "traffic_result": None})
            continue

        session = login_result
        checkin_result = perform_checkin(session, service)
        traffic_result = get_traffic_info(session, service)
        results.append({"name": service_name, "login_error": None, "checkin_result": checkin_result, "traffic_result": traffic_result})

        if not traffic_result["success"] and traffic_result.get("raw_html"):
            save_debug_info(service_name, traffic_result, checkin_result)

        log_info(f"--- {service_name} 处理完成 ---")

    report = format_multi_checkin_report(results)
    notify_send("📋 机场签到报告", report)

    print(f"\n{'=' * 60}\n{report}\n{'=' * 60}")

    execution_time = time.time() - start_time
    log_info(f"脚本执行完成，耗时: {execution_time:.2f}秒")

    any_success = any(res.get("checkin_result") and res["checkin_result"]["success"] for res in results)
    return 0 if any_success else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        log_info("用户中断执行")
        exit_code = 130
    except Exception as e:
        log_error(f"脚本异常: {e}")
        notify_send("机场签到脚本异常", f"错误: {str(e)[:200]}")
        exit_code = 1
    sys.exit(exit_code)

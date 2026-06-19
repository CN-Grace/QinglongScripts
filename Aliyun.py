#!/usr/bin/env python3
"""
cron: 0 0 * * *
new Env("阿里云盘签到")
阿里云盘 每日签到脚本
- 刷新 Access Token
- 获取累计签到天数和奖励信息
"""

import os
import requests
import urllib3
from typing import Dict, Any, Optional

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send

urllib3.disable_warnings()

# ==================== 用户配置 ====================
ALIYUN_REFRESH_TOKEN = os.environ.get("ALIYUN_REFRESH_TOKEN", "")


def get_access_token(refresh_token: str) -> Optional[str]:
    """使用 refresh_token 获取新的 access_token"""
    url = "https://auth.aliyundrive.com/v2/account/token"
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    try:
        result = requests.post(url=url, json=data, timeout=10).json()
        access_token = result.get("access_token")
        if access_token:
            log_success("Token 刷新成功")
            return access_token
        log_error("Token 刷新失败，响应中无 access_token")
        return None
    except Exception as e:
        log_error(f"Token 刷新异常: {e}")
        return None


def sign(access_token: str) -> Dict[str, Any]:
    """执行签到，返回累计天数和奖励信息"""
    url = "https://member.aliyundrive.com/v1/activity/sign_in_list"
    headers = {"Authorization": access_token, "Content-Type": "application/json"}
    try:
        result = requests.post(url=url, headers=headers, json={}, timeout=10).json()
        if not result.get("success"):
            msg = result.get("message", "未知错误")
            log_error(f"签到失败: {msg}")
            return {"success": False, "message": msg}

        sign_days = result["result"]["signInCount"]
        log_info(f"累计签到天数: {sign_days}")

        reward_name, reward_desc = "", ""
        for log in result["result"]["signInLogs"]:
            if log["status"] == "normal":
                today_log = log
                if today_log.get("reward"):
                    reward_name = today_log["reward"].get("name", "")
                    reward_desc = today_log["reward"].get("description", "")
                    log_success(f"今日奖励: {reward_name}{reward_desc}")
                break
        else:
            log_warning("今日奖励信息未找到")

        return {
            "success": True,
            "sign_days": sign_days,
            "reward_name": reward_name,
            "reward_desc": reward_desc,
        }
    except Exception as e:
        log_error(f"签到请求异常: {e}")
        return {"success": False, "message": str(e)}


def build_report(result: Dict[str, Any]) -> str:
    """构建签到报告"""
    lines = []
    if result.get("success"):
        lines.append(f"✅ 签到状态: 成功")
        lines.append(f"📅 累计签到: {result['sign_days']} 天")
        if result["reward_name"] or result["reward_desc"]:
            lines.append(f"🎁 今日奖励: {result['reward_name']}{result['reward_desc']}")
    else:
        lines.append(f"❌ 签到状态: 失败")
        lines.append(f"⚠️ 错误信息: {result.get('message', '未知错误')}")
    lines.append("")
    lines.append("─" * 18)
    lines.append(f"🕒 执行时间: {beijing_time_str()}")
    return "\n".join(lines)


def main() -> Dict[str, Any]:
    if not ALIYUN_REFRESH_TOKEN:
        log_error("未配置 ALIYUN_REFRESH_TOKEN")
        return {"success": False, "message": "未配置 refresh_token"}

    access_token = get_access_token(ALIYUN_REFRESH_TOKEN)
    if not access_token:
        return {"success": False, "message": "Token 刷新失败"}

    return sign(access_token)


if __name__ == "__main__":
    result = main()
    report = build_report(result)
    notify_send("📦 阿里云盘 签到报告", report)
    log_info(f"签到结果: {result}")

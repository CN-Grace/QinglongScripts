#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 0 0 * * *
# new Env("NodeSeek签到")
# NodeSeek 论坛自动签到脚本
# - 使用 curl_cffi 模拟浏览器指纹
# - 支持随机延迟签到

import os
import random
import time
from datetime import datetime
from typing import Tuple

import requests
from curl_cffi import requests as cffi_requests

from utils import log_info, log_success, log_warning, log_error, beijing_now, beijing_time_str
from notifier import send as notify_send

# ==================== 用户配置 ====================
NS_COOKIE = os.getenv("NS_COOKIE", "").strip()
NS_RANDOM = os.getenv("NS_RANDOM", "true").strip().lower()
NS_IMPERSONATE = os.getenv("NS_IMPERSONATE", "chrome110").strip()


def _curl_post_json(url: str, headers: dict, timeout: int = 25):
    return cffi_requests.post(url, headers=headers, json={}, timeout=timeout, impersonate=NS_IMPERSONATE)


def _normalize_random_flag(v: str) -> str:
    v = (v or "").strip().lower()
    return v if v in ("true", "false") else "true"


def _sleep_jitter_before_checkin() -> int:
    """签到前随机延迟 0~5 分钟"""
    delay_seconds = random.randint(0, 5 * 60)
    if delay_seconds <= 0:
        log_info("签到前随机延迟: 0s（跳过）")
        return 0
    minutes = delay_seconds // 60
    seconds = delay_seconds % 60
    log_info(f"签到前随机延迟: {minutes}m {seconds}s ...")
    time.sleep(delay_seconds)
    return delay_seconds


def nodeseek_checkin(cookie: str, ns_random: str) -> Tuple[bool, str, str]:
    """返回 (ok, status, message)"""
    if not cookie:
        return False, "invalid", "无有效 Cookie"

    random_flag = _normalize_random_flag(ns_random)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
        "origin": "https://www.nodeseek.com",
        "referer": "https://www.nodeseek.com/board",
        "Content-Type": "application/json",
        "Cookie": cookie,
    }

    url = f"https://www.nodeseek.com/api/attendance?random={random_flag}"
    try:
        start = datetime.now()
        r = _curl_post_json(url, headers=headers)
        elapsed_ms = int((datetime.now() - start).total_seconds() * 1000)

        data = None
        try:
            data = r.json()
        except Exception:
            pass

        msg = ""
        success_flag = None
        if isinstance(data, dict):
            msg = str(data.get("message") or "")
            success_flag = data.get("success")

        if "鸡腿" in msg or bool(success_flag) is True:
            return True, "success", msg or "签到成功"
        if "已完成签到" in msg:
            return True, "already", msg
        if isinstance(data, dict) and data.get("status") == 404:
            return False, "invalid", msg or "Cookie 已失效"
        if getattr(r, "status_code", 0) >= 400:
            snippet = (r.text or "").strip()[:200]
            return False, "error", f"HTTP {r.status_code}: {snippet or '请求失败'}"
        return False, "fail", msg or "签到失败"
    except Exception as e:
        return False, "error", f"签到异常: {e}"


def _format_result(ok: bool, status: str, message: str, elapsed_ms: int, delay_seconds: int) -> str:
    icon = "✅" if ok else "❌"
    delay_str = f"{delay_seconds // 60}m {delay_seconds % 60}s" if delay_seconds else "0s"
    lines = [
        f"{icon} 状态: {status}",
        f"⏱️ 耗时: {elapsed_ms} ms",
        f"🕯️ 延迟: {delay_str} (签到前随机延迟)",
        f"📝 说明: {message}",
        "",
        "─" * 18,
        f"🕒 执行时间: {beijing_time_str()}",
    ]
    return "\n".join(lines)


def main() -> int:
    log_info(f"NodeSeek 签到脚本启动（单账号）")

    if not NS_COOKIE:
        content = _format_result(False, "invalid", "未配置 NS_COOKIE，无法签到。", 0, 0)
        print("\n" + content)
        notify_send("🔗 NodeSeek 签到结果", content)
        return 1

    delay_seconds = _sleep_jitter_before_checkin()
    start = datetime.now()
    ok, status, message = nodeseek_checkin(NS_COOKIE, NS_RANDOM)
    elapsed_ms = int((datetime.now() - start).total_seconds() * 1000)

    content = _format_result(ok, status, message, elapsed_ms, delay_seconds)
    print("\n" + content)
    notify_send("🔗 NodeSeek 签到结果", content)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

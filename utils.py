#!/usr/bin/env python3
# QinglongScripts 公共工具模块

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# 北京时间时区 (UTC+8)
_TZ_BEIJING = timezone(timedelta(hours=8))

# ---------- 时间工具 ----------
def beijing_now() -> datetime:
    """返回当前北京时间 datetime"""
    return datetime.now(_TZ_BEIJING)


def beijing_time_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """返回当前北京时间字符串"""
    return beijing_now().strftime(fmt)


# ---------- 日志函数 ----------
def _log(emoji: str, msg: str):
    print(f"[{beijing_time_str()}] {emoji} {msg}")


def log_info(msg: str):
    _log("ℹ️", msg)


def log_success(msg: str):
    _log("✅", msg)


def log_warning(msg: str):
    _log("⚠️", msg)


def log_error(msg: str):
    _log("❌", msg)


# ---------- HTTP 工具 ----------
def create_session(
    cookie: Optional[str] = None,
    user_agent: Optional[str] = None,
    extra_headers: Optional[dict] = None,
) -> requests.Session:
    """创建带可选 Cookie 和自定义 Headers 的 requests.Session"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })
    if extra_headers:
        session.headers.update(extra_headers)
    if cookie:
        cookie_dict = {
            item.split("=")[0]: item.split("=")[1]
            for item in cookie.split("; ") if "=" in item
        }
        requests.utils.add_dict_to_cookiejar(session.cookies, cookie_dict)
    return session

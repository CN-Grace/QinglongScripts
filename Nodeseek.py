#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 0 0 * * *
# new Env("NodeSeek签到")
# NodeSeek 论坛自动签到脚本
# - 使用 curl_cffi 模拟浏览器指纹
# - 支持随机延迟签到
# - Cookie 失效时可用邮箱验证码保底登录（NS_MAIL + NS_CAPTCHA_KEY）

import imaplib
import json
import os
import random
import re
import time
from datetime import datetime
from email.header import decode_header, make_header
from email.parser import BytesParser
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from curl_cffi import requests as cffi_requests

from utils import log_info, log_success, log_warning, log_error, beijing_now, beijing_time_str
from notifier import send as notify_send

# ==================== 用户配置 ====================
NS_COOKIE = os.getenv("NS_COOKIE", "").strip()
NS_RANDOM = os.getenv("NS_RANDOM", "true").strip().lower()
NS_IMPERSONATE = os.getenv("NS_IMPERSONATE", "chrome110").strip()
# 保底登录：邮箱（读写验证码用）与解盾服务
NS_MAIL = os.getenv("NS_MAIL", "").strip()
NS_CAPTCHA_KEY = os.getenv("NS_CAPTCHA_KEY", "").strip()
NS_CAPTCHA_BASE = os.getenv("NS_CAPTCHA_BASE", "https://cn.yescaptcha.com").strip().rstrip("/")

# ==================== 站点常量 ====================
SITE = "https://www.nodeseek.com"
ATTENDANCE_URL = SITE + "/api/attendance"
EMAIL_API = SITE + "/api/email"
SIGNIN_API = SITE + "/api/account/emailSignIn"
EMAIL_SIGNIN_PAGE = SITE + "/emailSignIn.html"
# Cloudflare Turnstile sitekey（取自站点前端 bundle 的生产分支）
TURNSTILE_SITEKEY = "0x4AAAAAAAaNy7leGjewpVyR"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
)
TIMEOUT = 25

# Cookie 落盘（config/ 已在 .gitignore 中）
CONFIG_DIR = Path("config")
COOKIE_FILE = CONFIG_DIR / ".nodeseek_cookie"

# 保底登录相关阈值
MAIL_TIMEOUT = 120          # 等验证码邮件的总时长
MAIL_INTERVAL = 8           # 轮询间隔
CAPTCHA_TIMEOUT = 120       # 解盾等待上限

# 邮件特征：NodeSeek 验证码邮件（实测 From 含 nodeseek，标题为「邮箱动态验证登录」）
MAIL_SENDER_MARK = "nodeseek"
MAIL_SUBJECT_MARK = "验证"          # 标题只有「邮箱动态验证登录」，不含 nodeseek 字样
MAIL_BODY_MARK = "你的验证码是"
# NodeSeek 的登录验证码是 24 位十六进制串（不是 6 位数字），实测形如 1444e6a4e41814a2e07c9917
MAIL_CODE_RE = re.compile(r"\b([0-9a-f]{16,64})\b")

# 保底结果状态
FALLBACK_DISABLED = "disabled"      # 未配置 NS_MAIL / NS_CAPTCHA_KEY
FALLBACK_OK = "ok"
FALLBACK_COOKIE_OK = "cookie-ok"    # Cookie 本来就好使，无需保底
FALLBACK_CAPTCHA_FAILED = "captcha-failed"
FALLBACK_MAIL_REJECTED = "mail-rejected"
FALLBACK_NO_CODE = "no-code"
FALLBACK_LOGIN_FAILED = "login-failed"
FALLBACK_ERROR = "error"


# ==================== 通用 ====================
def _curl_post_json(url: str, headers: dict, timeout: int = TIMEOUT):
    return cffi_requests.post(url, headers=headers, json={}, timeout=timeout,
                              impersonate=NS_IMPERSONATE)


def _normalize_random_flag(v: str) -> str:
    v = (v or "").strip().lower()
    return v if v in ("true", "false") else "true"


def _sleep_jitter_before_checkin() -> int:
    """签到前随机延迟 0~5 分钟（受 NS_RANDOM 控制）"""
    if _normalize_random_flag(NS_RANDOM) != "true":
        log_info("签到前随机延迟: 已关闭（NS_RANDOM=false）")
        return 0
    delay_seconds = random.randint(0, 5 * 60)
    if delay_seconds <= 0:
        log_info("签到前随机延迟: 0s（跳过）")
        return 0
    minutes = delay_seconds // 60
    seconds = delay_seconds % 60
    log_info(f"签到前随机延迟: {minutes}m {seconds}s ...")
    time.sleep(delay_seconds)
    return delay_seconds


def mask_email(address: str) -> str:
    """日志里不出现完整邮箱"""
    if not address or "@" not in address:
        return address or "(空)"
    user, domain = address.split("@", 1)
    keep = user[:2] if len(user) > 2 else user[:1]
    return f"{keep}***@{domain}"


# ==================== Cookie 缓存 ====================
def load_cached_cookie() -> str:
    try:
        if COOKIE_FILE.exists():
            data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get("cookie") or "").strip()
    except (OSError, ValueError) as e:
        log_warning(f"读取 {COOKIE_FILE.name} 失败: {type(e).__name__}")
    return ""


def save_cached_cookie(cookie: str) -> bool:
    if not cookie:
        return False
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(
            json.dumps({"cookie": cookie, "saved_at": beijing_time_str()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except OSError as e:
        log_warning(f"保存 {COOKIE_FILE.name} 失败: {type(e).__name__}")
        return False


# ==================== 签到 ====================
def nodeseek_checkin(cookie: str, ns_random: str) -> Tuple[bool, str, str]:
    """返回 (ok, status, message)"""
    if not cookie:
        return False, "invalid", "无有效 Cookie"

    random_flag = _normalize_random_flag(ns_random)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "origin": SITE,
        "referer": SITE + "/board",
        "Content-Type": "application/json",
        "Cookie": cookie,
    }

    url = f"{ATTENDANCE_URL}?random={random_flag}"
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
        if "USER NOT FOUND" in msg.upper():
            return False, "invalid", "Cookie 已失效（USER NOT FOUND）"
        if getattr(r, "status_code", 0) >= 400:
            snippet = (r.text or "").strip()[:200]
            return False, "error", f"HTTP {r.status_code}: {snippet or '请求失败'}"
        return False, "fail", msg or "签到失败"
    except Exception as e:
        return False, "error", f"签到异常: {e}"


# ==================== 保底登录 ====================
def parse_mail_account(raw: str) -> Optional[Tuple[str, str]]:
    """解析 NS_MAIL：邮箱----IMAP授权码 / 邮箱----IMAP授权码----imap.example.com"""
    if not raw:
        return None
    parts = [p.strip() for p in raw.split("----")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    address = parts[0].lower()
    password = parts[1].replace(" ", "")   # Gmail 应用专用密码常带分组空格
    return address, password


def _imap_host(address: str) -> str:
    """按邮箱后缀推断 IMAP 服务器"""
    domain = address.split("@")[-1].lower()
    hosts = {
        "gmail.com": "imap.gmail.com",
        "googlemail.com": "imap.gmail.com",
        "qq.com": "imap.qq.com",
        "foxmail.com": "imap.qq.com",
        "163.com": "imap.163.com",
        "126.com": "imap.126.com",
        "outlook.com": "outlook.office365.com",
        "hotmail.com": "outlook.office365.com",
        "live.com": "outlook.office365.com",
        "yahoo.com": "imap.mail.yahoo.com",
        "icloud.com": "imap.mail.me.com",
    }
    return hosts.get(domain, "")


def _message_text(msg) -> str:
    """取出邮件的可读正文"""
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            chunks.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
    return "\n".join(chunks)


def extract_code(raw_mail: bytes) -> Tuple[Optional[str], str]:
    """从原始邮件提取 NodeSeek 登录验证码

    返回 (code, reason)。验证码是 24 位十六进制串，靠「发件方含 nodeseek」+「标题含验证主题」
    双重限定，避免误抓其它邮件里的十六进制串。
    """
    try:
        msg = BytesParser().parsebytes(raw_mail)
    except Exception as e:
        return None, f"邮件解析失败({type(e).__name__})"

    sender = str(make_header(decode_header(msg.get("From") or "")))
    subject = str(make_header(decode_header(msg.get("Subject") or "")))
    if MAIL_SENDER_MARK not in sender.lower():
        return None, "sender-unmatched"
    if MAIL_SUBJECT_MARK not in subject:
        return None, "subject-unmatched"

    body = re.sub(r"\s+", " ", _message_text(msg))
    # 标题本身就是「nodeseek邮箱动态验证登录，你的验证码是 xxx」，优先从正文按特征句定位
    index = body.find(MAIL_BODY_MARK)
    tail = body[index + len(MAIL_BODY_MARK):] if index >= 0 else body
    found = MAIL_CODE_RE.search(tail)
    if found:
        return found.group(1), "ok"
    found = MAIL_CODE_RE.search(body)
    if found:
        return found.group(1), "ok"
    return None, "code-not-found"


class ImapReader:
    """IMAP 收码：先记水位，只接受水位之后的新邮件"""

    def __init__(self, address: str, password: str, host: str = "") -> None:
        self.address = address
        self.password = password
        self.host = host or _imap_host(address)

    def _connect(self):
        if not self.host:
            raise RuntimeError(f"无法推断 {mask_email(self.address)} 的 IMAP 服务器")
        conn = imaplib.IMAP4_SSL(self.host, 993, timeout=30)
        conn.login(self.address, self.password)
        conn.select("INBOX", readonly=True)
        return conn

    def snapshot(self) -> Optional[int]:
        """记录当前最大 UID 作为水位"""
        conn = None
        try:
            conn = self._connect()
            typ, data = conn.uid("SEARCH", None, "ALL")
            uids = data[0].split() if data and data[0] else []
            return max((int(u) for u in uids), default=0)
        except Exception as e:
            log_warning(f"邮箱连接失败: {type(e).__name__}: {e}")
            return None
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass

    def wait_code(self, watermark: int, timeout: int = MAIL_TIMEOUT,
                  interval: int = MAIL_INTERVAL) -> Tuple[Optional[str], str]:
        deadline = time.monotonic() + timeout
        reasons = set()
        seen = 0
        while True:
            conn = None
            try:
                conn = self._connect()
                typ, data = conn.uid("SEARCH", None, f"UID {watermark + 1}:*")
                uids = [u for u in (data[0].split() if data and data[0] else []) if int(u) > watermark]
                seen = max(seen, len(uids))
                for uid in uids:
                    typ, item = conn.uid("FETCH", uid, "(RFC822)")
                    raw = next((bytes(p[1]) for p in item
                                if isinstance(p, tuple) and len(p) > 1), None)
                    if not raw:
                        continue
                    code, reason = extract_code(raw)
                    if code:
                        return code, "ok"
                    reasons.add(reason)
            except Exception as e:
                reasons.add(f"邮箱读取异常({type(e).__name__})")
            finally:
                if conn is not None:
                    try:
                        conn.logout()
                    except Exception:
                        pass

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not seen:
                    return None, "no-new-mail"
                return None, f"新邮件 {seen} 封均未匹配（{'/'.join(sorted(reasons)) or '未识别'}）"
            time.sleep(min(interval, remaining))


def _post_json_retry(url: str, payload: dict, retries: int = 3) -> dict:
    """POST JSON，连接层故障（SSL/超时）重试

    只对「请求根本没拿到响应」的异常重试，避免非幂等写操作被重复提交。
    """
    last = ""
    for i in range(retries):
        try:
            return requests.post(url, json=payload, timeout=TIMEOUT).json()
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = type(e).__name__
            time.sleep(2 + i * 2)
        except Exception as e:
            return {"errorId": -1, "errorCode": "HTTP_ERROR",
                    "errorDescription": f"{type(e).__name__}: {e}"}
    return {"errorId": -1, "errorCode": "NETWORK", "errorDescription": last or "无法连接"}


def solve_turnstile(client_key: str, base: str) -> Tuple[Optional[str], str]:
    """调用解盾服务解 Cloudflare Turnstile，返回 (token, reason)

    写操作性质：每次 createTask 都会扣点，调用方不得盲目重试。
    """
    payload = {
        "clientKey": client_key,
        "task": {
            "type": "TurnstileTaskProxyless",
            "websiteURL": EMAIL_SIGNIN_PAGE,
            "websiteKey": TURNSTILE_SITEKEY,
        },
    }
    data = _post_json_retry(base + "/createTask", payload)
    if data.get("errorId"):
        return None, f"{data.get('errorCode')} {data.get('errorDescription') or ''}".strip()

    task_id = data.get("taskId")
    if not task_id:
        return None, "解盾未返回 taskId"

    deadline = time.monotonic() + CAPTCHA_TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        data = _post_json_retry(base + "/getTaskResult",
                                {"clientKey": client_key, "taskId": task_id}, retries=2)
        if data.get("errorId"):
            return None, f"{data.get('errorCode')} {data.get('errorDescription') or ''}".strip()
        if data.get("status") == "ready":
            token = str((data.get("solution") or {}).get("token") or "")
            return (token, "ok") if token else (None, "解盾返回空 token")
        last = str(data.get("status") or "processing")
        time.sleep(2)
    return None, f"解盾超时（最后状态: {last}）"


def _api_post(url: str, payload: dict, cookie_jar: Optional[dict] = None) -> Tuple[dict, str]:
    """调 NodeSeek 的 JSON 接口；返回 (响应 dict, 收集到的 Cookie 串)"""
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Content-Type": "application/json",
        "origin": SITE,
        "referer": EMAIL_SIGNIN_PAGE,
    }
    if cookie_jar:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_jar.items())
    resp = cffi_requests.post(url, headers=headers, json=payload,
                              timeout=TIMEOUT, impersonate=NS_IMPERSONATE)
    try:
        data = resp.json()
    except Exception:
        data = {}
    jar = ""
    try:
        cookies = resp.cookies
        items = cookies.items() if hasattr(cookies, "items") else []
        if items:
            jar = "; ".join(f"{k}={v}" for k, v in items)
    except Exception:
        pass
    return (data if isinstance(data, dict) else {}), jar


def fallback_login() -> Tuple[Optional[str], str, str]:
    """Cookie 失效时的保底：解盾 → 发码 → 收码 → 登录

    返回 (cookie, status, message)。发码与登录都是非幂等写操作，失败不重试。
    """
    mail = parse_mail_account(NS_MAIL)
    if not mail:
        return None, FALLBACK_DISABLED, "未配置 NS_MAIL（格式：邮箱----IMAP授权码）"
    if not NS_CAPTCHA_KEY:
        return None, FALLBACK_DISABLED, "未配置 NS_CAPTCHA_KEY（解盾服务密钥）"
    address, password = mail
    if not _imap_host(address):
        return None, FALLBACK_ERROR, f"无法推断 {mask_email(address)} 的 IMAP 服务器"

    reader = ImapReader(address, password)
    watermark = reader.snapshot()
    if watermark is None:
        return None, FALLBACK_ERROR, "无法连接邮箱，保底未执行"

    log_info(f"向 {mask_email(address)} 申请登录验证码（需先解 Cloudflare 盾）")
    token, reason = solve_turnstile(NS_CAPTCHA_KEY, NS_CAPTCHA_BASE)
    if not token:
        return None, FALLBACK_CAPTCHA_FAILED, f"人机验证未通过：{reason}"
    log_info("人机验证已通过，请求发送验证码")

    try:
        data, _ = _api_post(EMAIL_API, {
            "email": address, "mode": "totp", "token": token,
            "source": "turnstile", "version": "v3",
        })
    except Exception as e:
        return None, FALLBACK_ERROR, f"发码请求异常({type(e).__name__})"
    if not data.get("success"):
        message = str(data.get("message") or "未知原因")
        return None, FALLBACK_MAIL_REJECTED, f"发码被拒：{message}"

    log_info(f"验证码已发出，等待邮件（最长 {MAIL_TIMEOUT}s）")
    code, reason = reader.wait_code(watermark)
    if not code:
        return None, FALLBACK_NO_CODE, f"{MAIL_TIMEOUT}s 内未取到验证码（{reason}）"

    log_info("已取到验证码，提交登录")
    try:
        data, jar = _api_post(SIGNIN_API, {"email": address, "code": code})
    except Exception as e:
        return None, FALLBACK_ERROR, f"登录请求异常({type(e).__name__})"
    if not data.get("success"):
        return None, FALLBACK_LOGIN_FAILED, f"登录失败：{data.get('message') or '未知原因'}"
    if not jar:
        return None, FALLBACK_LOGIN_FAILED, "登录成功但未拿到 Cookie"
    return jar, FALLBACK_OK, "邮箱验证码登录成功"


# ==================== 报告 ====================
def _format_result(ok: bool, status: str, message: str, elapsed_ms: int,
                   delay_seconds: int, fallback_line: str = "") -> str:
    icon = "✅" if ok else "❌"
    delay_str = f"{delay_seconds // 60}m {delay_seconds % 60}s" if delay_seconds else "0s"
    lines = []
    if fallback_line:
        lines.append(fallback_line)
    lines += [
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
    log_info("NodeSeek 签到脚本启动")

    candidates: List[str] = []
    if NS_COOKIE:
        candidates.append(NS_COOKIE)
    cached = load_cached_cookie()
    if cached and cached not in candidates:
        candidates.append(cached)

    fallback_enabled = bool(NS_MAIL and NS_CAPTCHA_KEY)
    if fallback_enabled:
        log_info(f"邮箱保底已启用: {mask_email(parse_mail_account(NS_MAIL)[0])}")
    if not candidates and not fallback_enabled:
        content = _format_result(False, "invalid",
                                 "未配置 NS_COOKIE，也未配置 NS_MAIL + NS_CAPTCHA_KEY 保底，无法签到。",
                                 0, 0)
        print("\n" + content)
        notify_send("🔗 NodeSeek 签到结果", content)
        return 1

    delay_seconds = _sleep_jitter_before_checkin()

    # 依次尝试候选 Cookie（环境变量 → 上次落盘）
    start = datetime.now()
    ok = False
    status = "invalid"
    message = "无可用 Cookie"
    used_cookie = ""
    for index, cookie in enumerate(candidates, 1):
        ok, status, message = nodeseek_checkin(cookie, NS_RANDOM)
        log_info(f"Cookie 候选 {index}/{len(candidates)}: {status} — {message}")
        if status != "invalid":
            used_cookie = cookie
            break
    elapsed_ms = int((datetime.now() - start).total_seconds() * 1000)

    fallback_line = ""
    if status == "invalid" and fallback_enabled:
        log_warning("Cookie 全部失效，尝试邮箱验证码保底登录")
        fb_start = datetime.now()
        new_cookie, fb_status, fb_message = fallback_login()
        fb_ms = int((datetime.now() - fb_start).total_seconds() * 1000)
        if new_cookie:
            used_cookie = new_cookie
            if save_cached_cookie(new_cookie):
                fb_line = f"🔑 邮箱保底: {fb_message}，新 Cookie 已写入 {COOKIE_FILE}"
                log_success(f"{fb_message}，新 Cookie 已落盘")
            else:
                fb_line = f"🔑 邮箱保底: {fb_message}（Cookie 落盘失败，仅本次有效）"
            fallback_line = fb_line
            c_start = datetime.now()
            ok, status, message = nodeseek_checkin(new_cookie, NS_RANDOM)
            elapsed_ms += int((datetime.now() - c_start).total_seconds() * 1000)
        else:
            fallback_line = f"📭 邮箱保底: {fb_message}"
            log_error(f"保底登录未成功: {fb_message}")
            status = fb_status
            message = fb_message
        elapsed_ms += fb_ms

    content = _format_result(ok, status, message, elapsed_ms, delay_seconds, fallback_line)
    print("\n" + content)
    notify_send("🔗 NodeSeek 签到结果", content)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

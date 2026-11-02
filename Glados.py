#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 0 0 * * *
# new Env("GLaDOS签到")
# GLaDOS / Railgun 自动签到脚本
# - 同一套 API 同时支持 glados.cloud 与新平台 railgun.info
# - 先探测 Cookie 归属域名再签到，避免在错误域名上误报失败
# - 服务端校验登录设备平台，遇到 device-mismatch 自动切换 User-Agent 重试
# - 支持多账号（& 或换行分隔）
# - Cookie 全部失效时可用邮箱验证码保底登录（GLADOS_MAIL），新 Cookie 落盘 config/.glados_cookie

import email
import email.header
import imaplib
import json
import os
import poplib
import re
import time
from dataclasses import dataclass, field
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send

# ==================== 用户配置 ====================
GLADOS_COOKIE = os.getenv("GLADOS_COOKIE", "").strip()
GLADOS_USER_AGENT = os.getenv("GLADOS_USER_AGENT", "").strip()
GLADOS_DOMAINS = os.getenv("GLADOS_DOMAINS", "").strip()
# 邮箱凭据（可选，用于 Cookie 失效时保底登录）：邮箱----授权码，多账号用 & 或换行分隔
GLADOS_MAIL = os.getenv("GLADOS_MAIL", "").strip()

# 默认域名：GLaDOS 主站 + 新平台 Railgun
DEFAULT_DOMAINS = ["glados.cloud", "railgun.info"]

# 默认 User-Agent（与仓库其他脚本保持一致的桌面 Chrome）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# 各平台合法 User-Agent：签到接口会校验登录设备平台（loginDevice）
PLATFORM_UA = {
    "Windows": DEFAULT_USER_AGENT,
    "macOS": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Linux": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "iPhone": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Android": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36"
    ),
}

# ==================== API 定义 ====================
CHECKIN_PATH = "/api/user/checkin"
STATUS_PATH = "/api/user/status"
POINTS_PATH = "/api/user/points"

CODE_SUCCESS = 0
CODE_REPEAT = 1
CODE_NO_PERMISSION = -2
CODE_DEVICE = 4
CODE_REGISTRATION_CLOSED = -6

# ==================== 邮箱保底登录定义 ====================
# 登录接口的 site 参数：服务端据此定位账号所属平台，各站取值不同
# （glados.cloud 与 railgun.info 的值是从各自 app.bundle.js 里实测确认的，不要假设一致）
SITE_BY_DOMAIN = {
    "glados.cloud": "glados.network",
    "glados.network": "glados.network",
    "railgun.info": "railgun.info",
    "rolling.network": "rolling.network",
}

AUTHORIZATION_PATH = "/api/authorization"
LOGIN_PATH = "/api/login"

# 验证码邮件特征。两个平台是不同品牌，发件方与标题都不同：
#   glados.cloud -> GLaDOS <noreply@glados.network> / "GLaDOS Authentication Code"
#   railgun.info -> Railgun <noreply@railgun.info> / "Railgun Authentication"
MAIL_BRAND_BY_DOMAIN = {
    "glados.cloud": "glados",
    "railgun.info": "railgun",
}
MAIL_BRANDS = ("glados", "railgun")
# 标题里必须含这个词，才能把验证码邮件和站内通知区分开：
# 「GLaDOS 服务到期提醒(13天)」「[GLaDOS]用户注册1周年优惠活动」这类都含数字，只靠发件方匹配会误抓
MAIL_SUBJECT_KEYWORD = "authentication"
# 正文特征句（新旧模板、两个品牌都带），用于第三优先级定位验证码段落。
# 必须品牌无关：railgun.info 的正文写的是 "requesting a Railgun authentication code"
MAIL_BODY_MARK_RE = re.compile(
    r"you are receiving this email because you are requesting a (?:glados|railgun) authentication code",
    re.IGNORECASE,
)
# railgun.info 侧在停用邮箱验证码时固定返回这个文案（前端据此展示「维护中」）
MAIL_UNAVAILABLE_MARK = "Failed to send email"

# 收信预算：每 10s 扫一次，最多等 120s
MAIL_TIMEOUT = 120
MAIL_INTERVAL = 10

# 邮箱服务商默认接入点 {域名: {协议: (host, port)}}，长格式可覆盖
MAIL_PROVIDERS: Dict[str, Dict[str, Tuple[str, int]]] = {
    "qq.com": {"imap": ("imap.qq.com", 993), "pop3": ("pop.qq.com", 995)},
    "vip.qq.com": {"imap": ("imap.qq.com", 993), "pop3": ("pop.qq.com", 995)},
    "foxmail.com": {"imap": ("imap.qq.com", 993), "pop3": ("pop.qq.com", 995)},
    "163.com": {"imap": ("imap.163.com", 993), "pop3": ("pop.163.com", 995)},
    "126.com": {"imap": ("imap.126.com", 993), "pop3": ("pop.126.com", 995)},
    "yeah.net": {"imap": ("imap.yeah.net", 993), "pop3": ("pop.yeah.net", 995)},
    "gmail.com": {"imap": ("imap.gmail.com", 993), "pop3": ("pop.gmail.com", 995)},
    "googlemail.com": {"imap": ("imap.gmail.com", 993), "pop3": ("pop.gmail.com", 995)},
    "outlook.com": {"imap": ("outlook.office365.com", 993), "pop3": ("outlook.office365.com", 995)},
    "hotmail.com": {"imap": ("outlook.office365.com", 993), "pop3": ("outlook.office365.com", 995)},
    "live.com": {"imap": ("outlook.office365.com", 993), "pop3": ("outlook.office365.com", 995)},
    "icloud.com": {"imap": ("imap.mail.me.com", 993), "pop3": ("pop.mail.me.com", 995)},
}

# 本地状态目录（已在 .gitignore 中，不入库）
CONFIG_DIR = Path(__file__).parent / "config"
COOKIE_FILE = CONFIG_DIR / ".glados_cookie"

# 无 code 字段时的兜底判定关键字（小写）
NORMAL_MESSAGES = ("checkin", "repeats", "observation logged", "签到成功", "已签到")

TIMEOUT = (10, 30)


# ==================== 工具函数 ====================
def _norm_num(value: Any, default: str = "0") -> str:
    """把接口返回的数字（可能是浮点/字符串）规整为整数字符串"""
    if value is None:
        return default
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value)


def _extract_single_cookie(raw: str) -> str:
    """规整单个 Cookie：兼容 `Cookie:` 前缀与 Cookie-Editor 的 JSON 导出"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^cookie\s*:\s*", "", raw, flags=re.IGNORECASE).strip()

    if raw.startswith(("{", "[")):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(data, list):
            pairs = [
                f"{item['name']}={item['value']}"
                for item in data
                if isinstance(item, dict) and item.get("name") and item.get("value") is not None
            ]
            return "; ".join(pairs)
        if isinstance(data, dict) and data.get("token"):
            # 旧版只导出 token 的情况
            return f"koa:sess={data['token']}"
        return raw

    return raw


def parse_cookies(raw: str) -> List[str]:
    """解析 GLADOS_COOKIE，多账号用 & 或换行分隔"""
    if not raw or not raw.strip():
        return []

    # Cookie-Editor 的 JSON 导出是单个文档，其中的换行/& 不是账号分隔符
    if raw.lstrip().startswith(("{", "[")):
        cookie = _extract_single_cookie(raw)
        return [cookie] if cookie else []

    sep = "\n" if "\n" in raw else "&"
    cookies: List[str] = []
    for item in raw.split(sep):
        cookie = _extract_single_cookie(item)
        if cookie:
            cookies.append(cookie)
    return cookies


def parse_domains(raw: str) -> List[str]:
    """解析 GLADOS_DOMAINS，缺省使用 DEFAULT_DOMAINS"""
    if not raw:
        return list(DEFAULT_DOMAINS)
    domains = [
        item.strip().replace("https://", "").replace("http://", "").strip("/")
        for item in re.split(r"[,\n&]", raw)
        if item.strip()
    ]
    return domains or list(DEFAULT_DOMAINS)


def mask_email(address: str) -> str:
    """脱敏邮箱，日志/通知里不出现完整地址"""
    address = address.strip()
    if "@" not in address:
        return (address[:2] + "***") if address else ""
    name, _, domain = address.partition("@")
    return f"{name[:2]}***@{domain}"


@dataclass
class MailAccount:
    """邮箱凭据（保底登录用）"""

    email: str
    password: str
    host: str
    port: int
    protocol: str = "imap"      # imap / pop3
    use_ssl: bool = True

    def masked(self) -> str:
        return mask_email(self.email)


def _provider_default(address: str, protocol: str) -> Tuple[str, int]:
    """按邮箱后缀推断接入点，未知服务商返回空 host"""
    domain = address.rsplit("@", 1)[-1].lower()
    entry = MAIL_PROVIDERS.get(domain)
    if not entry:
        return "", 0
    return entry.get(protocol) or entry["imap"]


def parse_mail_accounts(raw: str) -> List[MailAccount]:
    """解析 GLADOS_MAIL，与 GLADOS_COOKIE 同序对齐

    短格式: 邮箱----授权码            （按邮箱后缀自动推断服务器）
    长格式: 邮箱----授权码----host----port----protocol----ssl
    """
    if not raw:
        return []
    separator = "\n" if "\n" in raw else "&"
    accounts: List[MailAccount] = []
    for item in raw.split(separator):
        item = item.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split("----")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            log_warning("GLADOS_MAIL 条目格式不正确，应为「邮箱----授权码」")
            continue

        address, password = parts[0], parts[1]
        # 站点端本身会 lowercase 邮箱，本地也归一化，否则缓存 Cookie 的键会因大小写不一致而失配
        address = address.lower()
        # Gmail 应用专用密码常带分组空格，统一去掉
        password = password.replace(" ", "")

        protocol = (parts[4] if len(parts) > 4 and parts[4] else "imap").lower()
        if protocol not in ("imap", "pop3"):
            log_warning(f"GLADOS_MAIL 未知协议 {protocol!r}，回退 imap")
            protocol = "imap"

        use_ssl = True
        if len(parts) > 5 and parts[5]:
            use_ssl = parts[5].lower() not in ("0", "false", "no", "off")

        default_host, default_port = _provider_default(address, protocol)
        host = parts[2] if len(parts) > 2 and parts[2] else default_host
        port = default_port
        if len(parts) > 3 and parts[3]:
            try:
                port = int(parts[3])
            except ValueError:
                log_warning(f"GLADOS_MAIL 端口 {parts[3]!r} 不是数字，改用默认值 {default_port}")
                port = default_port
        if not port:
            port = 993 if (protocol == "imap" and use_ssl) else 143 if protocol == "imap" else 995 if use_ssl else 110
        if not host:
            log_warning(f"GLADOS_MAIL 无法推断 {mask_email(address)} 的 {protocol.upper()} 服务器，请用长格式指定 host")
            continue
        accounts.append(MailAccount(address, password, host, port, protocol, use_ssl))
    return accounts


def load_cached_cookies() -> Dict[str, str]:
    """读取 config/.glados_cookie（按邮箱索引），供下次运行直接复用"""
    try:
        if COOKIE_FILE.exists():
            data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(key).strip().lower(): str(value) for key, value in data.items() if value}
    except (OSError, ValueError) as e:
        log_warning(f"读取 {COOKIE_FILE.name} 失败: {type(e).__name__}")
    return {}


def save_cached_cookie(address: str, cookie: str) -> bool:
    """把邮箱保底登录拿到的新 Cookie 落盘（config/ 已在 .gitignore 中）"""
    if not address or not cookie:
        return False
    data = load_cached_cookies()
    data[address.strip().lower()] = cookie
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as e:
        log_warning(f"写入 {COOKIE_FILE.name} 失败: {type(e).__name__}")
        return False


def _sec_ch_ua_headers(user_agent: str) -> Dict[str, str]:
    """根据 User-Agent 生成 Client Hints，尽量贴近真实浏览器"""
    match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent)
    if not match:
        return {}
    major = match.group(1)
    if "Windows" in user_agent:
        platform = "Windows"
    elif "Macintosh" in user_agent:
        platform = "macOS"
    elif "Android" in user_agent:
        platform = "Android"
    elif "Linux" in user_agent:
        platform = "Linux"
    else:
        platform = "Unknown"
    return {
        "Sec-CH-UA": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"',
        "Sec-CH-UA-Mobile": "?1" if "Mobile" in user_agent else "?0",
        "Sec-CH-UA-Platform": f'"{platform}"',
    }


# ==================== 签到结果 ====================
@dataclass
class DomainResult:
    """单个账号在单个域名上的签到结果"""

    domain: str
    valid: bool = False          # Cookie 在该域名上是否有效
    status: str = "fail"         # success / repeat / fail
    code: Optional[int] = None
    message: str = ""
    gained_points: str = "0"
    left_days: str = "-"
    total_points: str = "-"


# 邮箱保底登录状态
FALLBACK_OK = "ok"
FALLBACK_CAPTCHA = "captcha"
FALLBACK_NO_MAIL = "no_mail"
FALLBACK_LOGIN_FAILED = "login_failed"
FALLBACK_MIGRATED = "migrated"
FALLBACK_CLOSED = "closed"
FALLBACK_UNAVAILABLE = "unavailable"
FALLBACK_ERROR = "error"

FALLBACK_ICON = {
    FALLBACK_OK: "🔑",
    FALLBACK_CAPTCHA: "🧩",
    FALLBACK_NO_MAIL: "📭",
    FALLBACK_LOGIN_FAILED: "🚫",
    FALLBACK_MIGRATED: "🔀",
    FALLBACK_CLOSED: "🚪",
    FALLBACK_UNAVAILABLE: "🛠️",
    FALLBACK_ERROR: "⚠️",
}

# 只有「账号/站点层面的硬拒绝」才停止换域名重试；
# 其余情况（迁移到 Railgun、本域名要人机验证、本域名维护中）换域名很可能就通了
FALLBACK_FATAL = (FALLBACK_CLOSED, FALLBACK_LOGIN_FAILED)


@dataclass
class FallbackOutcome:
    """邮箱保底登录的执行结果（status 为空表示未触发）"""

    status: str = ""
    message: str = ""
    cookie: str = ""
    saved: bool = False
    email: str = ""


@dataclass
class AccountResult:
    """单个账号的完整结果（各域名签到结果 + 保底登录情况）"""

    email: str = ""
    results: List[DomainResult] = field(default_factory=list)
    fallback: FallbackOutcome = field(default_factory=FallbackOutcome)

    @property
    def valid_results(self) -> List[DomainResult]:
        return [item for item in self.results if item.valid]


class GladosClient:
    """GLaDOS / Railgun 客户端（同一套 API）"""

    def __init__(
        self,
        cookie: str,
        domain: str,
        user_agent: str,
        session: Optional[requests.Session] = None,
    ):
        self.cookie = cookie
        self.domain = domain
        self.user_agent = user_agent
        # 邮箱保底登录传入已持有会话 Cookie 的 Session，此时不再单独拼 Cookie 头，
        # 保证 status/checkin 与 login 落在同一个会话、同一个 UA 上
        self.reuse_session = session is not None
        if session is not None:
            self.session = session
        else:
            self.session = requests.Session()
            retry = Retry(
                total=2,
                backoff_factor=1.0,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET", "POST"),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _headers(self, user_agent: Optional[str] = None) -> Dict[str, str]:
        ua = user_agent or self.user_agent
        headers = {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": f"https://{self.domain}",
            "Referer": f"https://{self.domain}/console/checkin",
            "Content-Type": "application/json;charset=UTF-8",
        }
        if not self.reuse_session:
            headers["Cookie"] = self.cookie
        headers.update(_sec_ch_ua_headers(ua))
        return headers

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Optional[dict]:
        url = f"https://{self.domain}{path}"
        try:
            if method == "GET":
                resp = self.session.get(url, headers=self._headers(), timeout=TIMEOUT)
            else:
                resp = self.session.post(url, headers=self._headers(), json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            log_warning(f"[{self.domain}] 请求异常: {type(e).__name__}")
            return None

        if resp.status_code != 200:
            log_warning(f"[{self.domain}] HTTP {resp.status_code}")
            return None
        try:
            return resp.json()
        except ValueError:
            log_warning(f"[{self.domain}] 响应不是合法 JSON")
            return None

    def status(self) -> Dict[str, Any]:
        """查询账号状态，用于判断 Cookie 是否归属该域名"""
        raw = self._request("GET", STATUS_PATH)
        if not isinstance(raw, dict):
            return {"authorized": False, "message": "状态查询失败"}
        data = raw.get("data")
        if raw.get("code") == CODE_SUCCESS and isinstance(data, dict) and data.get("leftDays") is not None:
            return {
                "authorized": True,
                "left_days": _norm_num(data.get("leftDays")),
                "email": data.get("email", ""),
            }
        return {
            "authorized": False,
            "code": raw.get("code"),
            "message": str(raw.get("message") or "").strip() or f"code={raw.get('code')}",
        }

    def checkin(self) -> Optional[dict]:
        """执行签到，token 必须与当前域名一致"""
        return self._request("POST", CHECKIN_PATH, {"token": self.domain})

    def points(self) -> Optional[dict]:
        """查询总积分"""
        return self._request("GET", POINTS_PATH)


def _parse_checkin(result: DomainResult, raw: Optional[dict]) -> None:
    """解析签到响应，写入 result"""
    if not isinstance(raw, dict):
        result.status = "fail"
        result.message = "请求失败或无响应"
        return

    code = raw.get("code")
    message = str(raw.get("message") or "").strip()
    lower = message.lower()
    result.code = code
    result.message = message
    if raw.get("points") is not None:
        result.gained_points = _norm_num(raw.get("points"))

    if code == CODE_SUCCESS:
        result.status = "success"
        result.message = message or "签到成功"
    elif code == CODE_REPEAT or (code is None and ("repeat" in lower or "已完成签到" in message)):
        result.status = "repeat"
        result.gained_points = "0"
        result.message = message or "今日已签到"
    elif code is None and any(k in lower for k in NORMAL_MESSAGES):
        # 兜底：无 code 字段但文案命中成功关键字（repeat 已在上面优先处理）
        result.status = "success"
        result.message = message or "签到成功"
    else:
        result.status = "fail"
        if code == CODE_NO_PERMISSION:
            result.message = message or "Cookie 已失效或无权限"
        elif code == CODE_DEVICE:
            if raw.get("reason") == "device-mismatch":
                result.message = (
                    f"登录设备不匹配（{raw.get('loginDevice', '未知')}），"
                    "请使用登录时的浏览器 UA 或重新登录后更新 Cookie"
                )
            else:
                result.message = message or f"风控拒绝（reason={raw.get('reason')}）"
        elif not message:
            result.message = f"签到失败（code={code}）"


def _checkin_authorized(client: "GladosClient", status: Dict[str, Any]) -> DomainResult:
    """在已确认 Cookie 有效的域名上执行「签到 → 积分查询」"""
    result = DomainResult(domain=client.domain, valid=True, left_days=status.get("left_days", "-"))

    raw = client.checkin()
    # 服务端校验登录设备平台，返回 device-mismatch 时按提示切换 UA 重试一次
    if isinstance(raw, dict) and raw.get("code") == CODE_DEVICE and raw.get("reason") == "device-mismatch":
        device = raw.get("loginDevice")
        alt_ua = PLATFORM_UA.get(device)
        if alt_ua and alt_ua != client.user_agent:
            log_warning(f"[{client.domain}] 登录设备为 {device}，切换 User-Agent 后重试")
            client.user_agent = alt_ua
            raw = client.checkin()
    _parse_checkin(result, raw)

    points = client.points()
    if isinstance(points, dict) and points.get("points") is not None:
        result.total_points = _norm_num(points.get("points"))

    return result


def _browser_headers(domain: str, user_agent: str, referer_path: str = "/login") -> Dict[str, str]:
    """构造浏览器形态的请求头，避免被站点当成脚本"""
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": f"https://{domain}",
        "Referer": f"https://{domain}{referer_path}",
        "Content-Type": "application/json;charset=UTF-8",
    }
    headers.update(_sec_ch_ua_headers(user_agent))
    return headers


def _post_json(
    session: requests.Session,
    domain: str,
    path: str,
    payload: dict,
    user_agent: str,
) -> Optional[dict]:
    """发码/登录都是非幂等写操作，故意不挂 Retry，失败就是失败"""
    url = f"https://{domain}{path}"
    try:
        resp = session.post(
            url,
            headers=_browser_headers(domain, user_agent),
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        log_warning(f"[{domain}] {path} 请求异常: {type(e).__name__}")
        return None
    if resp.status_code != 200:
        log_warning(f"[{domain}] {path} HTTP {resp.status_code}")
        return None
    try:
        return resp.json()
    except ValueError:
        log_warning(f"[{domain}] {path} 响应不是合法 JSON")
        return None


def _decode_header_value(value: Optional[str]) -> str:
    """解码 MIME 编码过的邮件头（RFC 2047）"""
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except (ValueError, TypeError):
        return value


def _message_text(msg: Any) -> str:
    """把邮件正文拍平成纯文本（兼容 multipart / 非 UTF-8 字符集 / quoted-printable）"""
    parts = msg.walk() if msg.is_multipart() else [msg]
    chunks: List[str] = []
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            raw = part.get_payload()
            text = raw if isinstance(raw, str) else ""
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, TypeError):
                text = payload.decode("utf-8", errors="replace")
        if ctype == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        chunks.append(text)
    return "\n".join(chunks)


# 验证码正文标签（新旧模板都带）："Verification Code: 610521" / "Your verification code is: 610521"
MAIL_CODE_LABEL = re.compile(r"verification\s*code\s*(?:is)?\s*[:：]\s*(\d{4,8})", re.IGNORECASE)
# 旧模板的星号包裹形态：*182514*
MAIL_CODE_STARRED = re.compile(r"\*(\d{4,8})\*")


def extract_code(raw_mail: bytes, account_email: str = "", brand: str = "") -> Tuple[Optional[str], str]:
    """从原始邮件里提取 GLaDOS / Railgun 验证码

    返回 (code, reason)：code 为 None 时 reason 说明跳过原因，便于日志排查。
    真实邮件（multipart/alternative）的 plain 段是「Verification Code: 610521」，
    html 段是「You are receiving this email ... Your verification code is: 610521」，
    旧模板则用星号包裹，形如 *182514*。

    brand 用于优先级判定：发件品牌与请求域名一致时 reason 为 "ok"，
    不一致时仍返回验证码但 reason 为 "ok-other-brand"，由调用方决定是否采纳。
    """
    try:
        msg = BytesParser().parsebytes(raw_mail)
    except Exception as e:
        return None, f"邮件解析失败({type(e).__name__})"

    sender = _decode_header_value(msg.get("From"))
    subject = _decode_header_value(msg.get("Subject")).strip()
    sender_l = sender.lower()
    # 发件方必须是两个平台官方品牌之一
    matched_brand = next((b for b in MAIL_BRANDS if b in sender_l), None)
    if not matched_brand:
        return None, "sender-unmatched"
    # 标题必须含 authentication，否则就是站内通知邮件
    if MAIL_SUBJECT_KEYWORD not in subject.lower():
        return None, "subject-unmatched"

    body = _message_text(msg)
    # 邮件常按 78 列硬换行，先把空白拍平再匹配，避免特征句/标签被换行截断
    flat = re.sub(r"\s+", " ", body)

    # 第一优先级：显式标签。标签文字不可能出现在邮箱地址里，误判率最低
    code = None
    found = MAIL_CODE_LABEL.search(flat)
    if found:
        code = found.group(1)
    else:
        # 第二优先级：旧模板的星号包裹形态
        found = MAIL_CODE_STARRED.search(flat)
        if found:
            code = found.group(1)
        else:
            # 第三优先级：特征句之后的独立数字（正文特征句能定位到验证码所在段落）
            marker = MAIL_BODY_MARK_RE.search(flat)
            if not marker:
                return None, "body-unmatched"
            tail = flat[marker.end():]
            # 跳过收件人地址里夹带的数字
            if account_email:
                tail = tail.split(account_email)[-1]
            for pattern in (r"\b(\d{5,6})\b", r"\b(\d{4,6})\b"):
                found = re.search(pattern, tail)
                if found:
                    code = found.group(1)
                    break
            if not code:
                return None, "code-not-found"

    if brand and matched_brand != brand:
        return code, "ok-other-brand"
    return code, "ok"


class MailboxClient:
    """只读邮箱客户端（IMAP / POP3，纯 stdlib）

    关键点：发码前先记下「水位」，只接受水位之后的新邮件，
    避免把上一次登录残留的旧验证码拿来提交。
    """

    def __init__(self, account: MailAccount):
        self.account = account

    def _connect(self) -> Any:
        acc = self.account
        if acc.protocol == "imap":
            conn = imaplib.IMAP4_SSL(acc.host, acc.port) if acc.use_ssl else imaplib.IMAP4(acc.host, acc.port)
            conn.login(acc.email, acc.password)
            return conn
        conn = poplib.POP3_SSL(acc.host, acc.port) if acc.use_ssl else poplib.POP3(acc.host, acc.port)
        conn.user(acc.email)
        conn.pass_(acc.password)
        return conn

    @staticmethod
    def _close(conn: Any) -> None:
        try:
            conn.logout() if isinstance(conn, imaplib.IMAP4) else conn.quit()
        except Exception:
            pass

    def snapshot(self) -> Optional[int]:
        """记录当前邮箱水位（IMAP 为最大 UID，POP3 为邮件数）；返回 None 表示连接失败"""
        try:
            conn = self._connect()
        except Exception as e:
            log_warning(f"邮箱 {self.account.masked()} 连接失败: {type(e).__name__}: {e}")
            return None
        try:
            if self.account.protocol == "imap":
                typ, _ = conn.select("INBOX", readonly=True)
                if typ != "OK":
                    return None
                typ, data = conn.uid("SEARCH", None, "ALL")
                if typ != "OK" or not data or not data[0]:
                    return 0
                return max(int(item) for item in data[0].split())
            count, _ = conn.stat()
            return int(count)
        except Exception as e:
            log_warning(f"邮箱 {self.account.masked()} 读取水位失败: {type(e).__name__}")
            return None
        finally:
            self._close(conn)

    def _fetch_new(self, conn: Any, watermark: int) -> List[bytes]:
        """取出水位之后的新邮件原文（新的在前）"""
        raws: List[bytes] = []
        if self.account.protocol == "imap":
            typ, _ = conn.select("INBOX", readonly=True)
            if typ != "OK":
                return raws
            typ, data = conn.uid("SEARCH", None, f"UID {watermark + 1}:*")
            if typ != "OK" or not data or not data[0]:
                return raws
            for uid in reversed(data[0].split()):
                try:
                    if int(uid) <= watermark:
                        continue
                except ValueError:
                    continue
                typ, item = conn.uid("FETCH", uid, "(RFC822)")
                if typ != "OK" or not item:
                    continue
                for part in item:
                    if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], (bytes, bytearray)):
                        raws.append(bytes(part[1]))
            return raws

        count, _ = conn.stat()
        for index in range(int(count), watermark, -1):
            try:
                _, lines, _ = conn.retr(index)
            except Exception:
                continue
            raws.append(b"\r\n".join(lines))
        return raws

    @staticmethod
    def _describe(max_new: int, reasons: set) -> str:
        """把「没取到验证码」的原因说清楚，避免 unrelated 新邮件把真实原因掩盖掉"""
        if not max_new:
            return "no-new-mail"
        return f"新邮件 {max_new} 封均未匹配（{'/'.join(sorted(reasons)) or '未识别'}）"

    def wait_code(
        self,
        watermark: int,
        account_email: str = "",
        brand: str = "",
        timeout: int = MAIL_TIMEOUT,
        interval: int = MAIL_INTERVAL,
    ) -> Tuple[Optional[str], str]:
        """轮询等待验证码邮件，返回 (code, reason)

        优先采纳发件品牌与当前域名一致的那封；品牌不符的只在没有更好选择时才用，
        避免「请求 railgun.info 的码、却拿了同时到达的 glados.cloud 的码」。
        """
        deadline = time.monotonic() + timeout
        fallback_code = ""
        max_new = 0
        reasons: set = set()
        while True:
            conn = None
            try:
                conn = self._connect()
                raws = self._fetch_new(conn, watermark)
                max_new = max(max_new, len(raws))
                for raw in raws:
                    code, reason = extract_code(raw, account_email, brand)
                    if not code:
                        reasons.add(reason)
                        continue
                    if reason == "ok":
                        return code, "ok"
                    fallback_code = fallback_code or code
            except Exception as e:
                reasons.add(f"邮箱读取异常({type(e).__name__})")
            finally:
                if conn is not None:
                    self._close(conn)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if fallback_code:
                    return fallback_code, "ok-other-brand"
                return None, self._describe(max_new, reasons)
            time.sleep(min(interval, remaining))


def _session_cookie(session: requests.Session) -> str:
    """把会话里的 Cookie 拼成字符串，便于落盘复用"""
    return "; ".join(f"{item.name}={item.value}" for item in session.cookies)


def _reject(
    domain: str,
    outcome: FallbackOutcome,
    status: str,
    message: str,
) -> FallbackOutcome:
    """记录「请求验证码被拒」的原因并返回 outcome

    每个提前返回分支都走这里，避免日志里只看得到「直接跳到下一个域名」。
    """
    outcome.status = status
    outcome.message = message
    log_warning(f"[{domain}] 请求验证码未成功: {message}")
    return outcome


def login_by_email(
    mail: MailAccount,
    domain: str,
    user_agent: str,
) -> Tuple[Optional[GladosClient], FallbackOutcome]:
    """Cookie 失效时的保底：邮箱验证码登录

    成功时返回复用同一 Session 的客户端（保证登录与签到同设备、同 UA）。
    发码与登录都是非幂等写操作，失败一律不重试。
    """
    outcome = FallbackOutcome(email=mail.masked())
    site = SITE_BY_DOMAIN.get(domain, domain)
    masked = mail.masked()

    mailbox = MailboxClient(mail)
    watermark = mailbox.snapshot()
    if watermark is None:
        outcome.status = FALLBACK_ERROR
        outcome.message = "无法连接邮箱，保底登录未执行"
        return None, outcome

    session = requests.Session()  # 不挂 Retry
    try:
        log_info(f"[{domain}] 向 {masked} 请求验证码（site={site}）")
        resp = _post_json(
            session,
            domain,
            AUTHORIZATION_PATH,
            {"address": mail.email.lower(), "site": site},
            user_agent,
        )
        if resp is None:
            return None, _reject(domain, outcome, FALLBACK_ERROR,
                                 "请求验证码失败（网络异常或非 JSON 响应）")
        if resp.get("captcha_required"):
            sitekey = str(resp.get("sitekey") or "")
            return None, _reject(
                domain, outcome, FALLBACK_CAPTCHA,
                "站点要求人机验证，无法自动完成" + (f"（sitekey={sitekey}）" if sitekey else ""),
            )
        if resp.get("code") != CODE_SUCCESS:
            message = str(resp.get("message") or "").strip()
            if resp.get("code") == CODE_REGISTRATION_CLOSED or "registration_closed" in message:
                status, text = FALLBACK_CLOSED, "站点已关闭邮箱验证码登录"
            elif "migrated" in message.lower():
                status, text = FALLBACK_MIGRATED, f"账号已迁移到其它平台（{message}）"
            elif message == MAIL_UNAVAILABLE_MARK:
                status, text = FALLBACK_UNAVAILABLE, "站点暂时停用邮箱验证码（维护中）"
            else:
                status, text = FALLBACK_ERROR, f"请求验证码被拒（code={resp.get('code')} {message}）"
            return None, _reject(domain, outcome, status, text)

        log_info(f"[{domain}] 验证码已发出，等待邮件（最长 {MAIL_TIMEOUT}s）")
        brand = MAIL_BRAND_BY_DOMAIN.get(domain, "")
        code, reason = mailbox.wait_code(watermark, mail.email, brand)
        if not code:
            outcome.status = FALLBACK_NO_MAIL
            outcome.message = f"{MAIL_TIMEOUT}s 内未取到验证码（{reason}）"
            return None, outcome
        if reason == "ok-other-brand":
            log_warning(f"[{domain}] 取到的是另一个平台的验证码，仍尝试提交")

        log_info(f"[{domain}] 已取到验证码，提交登录")
        resp = _post_json(
            session,
            domain,
            LOGIN_PATH,
            {"method": "email", "site": site, "email": mail.email.lower(), "mailcode": code},
            user_agent,
        )
        if resp is None:
            outcome.status = FALLBACK_ERROR
            outcome.message = "提交验证码失败（网络异常或非 JSON 响应）"
            return None, outcome
        if resp.get("code") != CODE_SUCCESS:
            message = str(resp.get("message") or "").strip()
            if "migrated" in message.lower():
                outcome.status = FALLBACK_MIGRATED
                outcome.message = f"账号已迁移到其它平台（{message}）"
            else:
                outcome.status = FALLBACK_LOGIN_FAILED
                outcome.message = f"验证码登录被拒（code={resp.get('code')} {message}）"
            return None, outcome

        cookie = _session_cookie(session)
        if not cookie:
            outcome.status = FALLBACK_LOGIN_FAILED
            outcome.message = "登录返回成功但未下发会话 Cookie"
            return None, outcome

        outcome.status = FALLBACK_OK
        outcome.cookie = cookie
        outcome.message = "邮箱验证码登录成功"
        log_success(f"[{domain}] 邮箱验证码登录成功")
        return GladosClient(cookie, domain, user_agent, session=session), outcome
    except requests.RequestException as e:
        outcome.status = FALLBACK_ERROR
        outcome.message = f"保底登录请求异常（{type(e).__name__}）"
        return None, outcome
    finally:
        if outcome.status != FALLBACK_OK:
            try:
                session.close()
            except Exception:
                pass


def checkin_account(
    cookies: List[str],
    mail: Optional[MailAccount],
    domains: List[str],
    base_ua: str,
) -> AccountResult:
    """单个账号：先用所有候选 Cookie 探测归属域名，全部失效时走邮箱保底登录"""
    account = AccountResult(email=mail.masked() if mail else "")
    authorized: List[Tuple[GladosClient, Dict[str, Any]]] = []

    for cookie in cookies:
        for domain in domains:
            client = GladosClient(cookie, domain, base_ua)
            status = client.status()
            if status.get("authorized"):
                authorized.append((client, status))
            else:
                client.close()

    if not authorized and mail is not None:
        log_warning("Cookie 全部失效，尝试邮箱验证码保底登录")
        for domain in domains:
            client, outcome = login_by_email(mail, domain, base_ua)
            account.fallback = outcome
            if client is None:
                # 账号/站点层面的拒绝，换域名也没意义
                if outcome.status in FALLBACK_FATAL:
                    break
                continue
            status = client.status()
            if status.get("authorized"):
                account.fallback.saved = save_cached_cookie(mail.email, outcome.cookie)
                if account.fallback.saved:
                    log_info(f"新 Cookie 已写入 {COOKIE_FILE.name}，下次运行直接复用")
                authorized.append((client, status))
                break
            client.close()
            account.fallback.status = FALLBACK_LOGIN_FAILED
            account.fallback.message = "登录成功但状态校验未通过"
            break

    if not authorized:
        if account.fallback.status:
            message = account.fallback.message
            log_error(f"保底登录未成功: {message}")
        else:
            message = "Cookie 无效或无权限"
            log_warning(f"{message}，已尝试: " + " / ".join(domains))
        account.results = [DomainResult(domain=domains[0], valid=False, message=message)]
        return account

    try:
        account.results = [_checkin_authorized(client, status) for client, status in authorized]
    finally:
        for client, _ in authorized:
            client.close()
    return account


# ==================== 报告 ====================
_STATUS_TEXT = {"success": "签到成功", "repeat": "重复签到", "fail": "签到失败"}
_STATUS_ICON = {"success": "✅", "repeat": "🔄", "fail": "❌"}


def build_report(accounts: List[AccountResult], domains: List[str]) -> str:
    """构建签到报告"""
    lines: List[str] = []
    stats = {"success": 0, "repeat": 0, "fail": 0}

    for index, account in enumerate(accounts, 1):
        title = f"👤 账号 {index}"
        if account.email:
            title += f"（{account.email}）"
        lines.append(title)

        if account.fallback.status:
            icon = FALLBACK_ICON.get(account.fallback.status, "❓")
            detail = f"  {icon} 邮箱保底: {account.fallback.message}"
            if account.fallback.saved:
                detail += f"，新 Cookie 已写入 config/{COOKIE_FILE.name}"
            lines.append(detail)

        valid = account.valid_results
        if not valid:
            stats["fail"] += 1
            message = account.results[0].message if account.results else "Cookie 无效或无权限"
            lines.append(f"  ❌ {message}（已尝试 {' / '.join(domains)}）")
            lines.append("")
            continue

        for result in valid:
            stats[result.status] = stats.get(result.status, 0) + 1
            icon = _STATUS_ICON.get(result.status, "❌")
            detail = f"  {icon} {result.domain} · {_STATUS_TEXT.get(result.status, result.status)}"
            if result.status == "success" and result.gained_points not in ("0", "-"):
                detail += f" · +{result.gained_points} 积分"
            detail += f" · 剩余 {result.left_days} 天 · 总 {result.total_points} 积分"
            lines.append(detail)
            if result.status == "fail" and result.message:
                lines.append(f"     └ {result.message}")
        lines.append("")

    lines.append(f"📊 统计: 成功 {stats['success']} · 重复 {stats['repeat']} · 失败 {stats['fail']}")
    lines.append("─" * 18)
    lines.append(f"🕒 执行时间: {beijing_time_str()}")
    return "\n".join(lines)


def main() -> int:
    log_info("GLaDOS / Railgun 签到脚本启动")

    cookies = parse_cookies(GLADOS_COOKIE)
    mails = parse_mail_accounts(GLADOS_MAIL)
    if not cookies and not mails:
        content = (
            "❌ 未配置 GLADOS_COOKIE，也未配置 GLADOS_MAIL 保底，无法签到。\n\n"
            + "─" * 18
            + f"\n🕒 执行时间: {beijing_time_str()}"
        )
        print("\n" + content)
        notify_send("🎫 GLaDOS / Railgun 签到结果", content)
        return 1

    domains = parse_domains(GLADOS_DOMAINS)
    base_ua = GLADOS_USER_AGENT or DEFAULT_USER_AGENT

    # 账号数取两者较大值，按序对齐；候选 Cookie = 环境变量里的 + 上次保底落盘的
    cached = load_cached_cookies()
    total = max(len(cookies), len(mails))
    plan: List[Tuple[List[str], Optional[MailAccount]]] = []
    for index in range(total):
        mail = mails[index] if index < len(mails) else None
        candidates: List[str] = []
        if index < len(cookies) and cookies[index]:
            candidates.append(cookies[index])
        if mail:
            cached_cookie = cached.get(mail.email, "")
            if cached_cookie and cached_cookie not in candidates:
                candidates.append(cached_cookie)
        plan.append((candidates, mail))

    log_info(f"共 {total} 个账号，目标域名: {' / '.join(domains)}")
    if mails:
        log_info("邮箱保底已启用: " + ", ".join(mail.masked() for mail in mails))

    accounts: List[AccountResult] = []
    for index, (candidates, mail) in enumerate(plan, 1):
        log_info(f"===== 账号 {index} =====")
        try:
            account = checkin_account(candidates, mail, domains, base_ua)
        except Exception as e:
            log_error(f"账号 {index} 执行异常: {type(e).__name__}: {e}")
            account = AccountResult(email=mail.masked() if mail else "")
            account.results = [
                DomainResult(domain=domains[0], valid=False, message=f"执行异常（{type(e).__name__}）")
            ]
        for result in account.results:
            if not result.valid:
                continue
            if result.status == "success":
                log_success(f"[{result.domain}] 签到成功，获得 {result.gained_points} 积分，剩余 {result.left_days} 天")
            elif result.status == "repeat":
                log_info(f"[{result.domain}] 今日已签到，剩余 {result.left_days} 天")
            else:
                log_error(f"[{result.domain}] 签到失败: {result.message}")
        accounts.append(account)

    content = build_report(accounts, domains)
    print("\n" + content)
    notify_send("🎫 GLaDOS / Railgun 签到结果", content)

    fail_count = sum(1 for account in accounts if not account.valid_results) + sum(
        1 for account in accounts for result in account.valid_results if result.status == "fail"
    )
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

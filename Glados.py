#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 0 0 * * *
# new Env("GLaDOS签到")
# GLaDOS / Railgun 自动签到脚本
# - 同一套 API 同时支持 glados.cloud 与新平台 railgun.info
# - 先探测 Cookie 归属域名再签到，避免在错误域名上误报失败
# - 服务端校验登录设备平台，遇到 device-mismatch 自动切换 User-Agent 重试
# - 支持多账号（& 或换行分隔）

import json
import os
import re
from dataclasses import dataclass
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


class GladosClient:
    """GLaDOS / Railgun 客户端（同一套 API）"""

    def __init__(self, cookie: str, domain: str, user_agent: str):
        self.cookie = cookie
        self.domain = domain
        self.user_agent = user_agent
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
            "Cookie": self.cookie,
        }
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


def checkin_account(cookie: str, domains: List[str], base_ua: str) -> List[DomainResult]:
    """单个账号：先探测 Cookie 归属域名，再在归属域名上签到"""
    authorized: List[Tuple[GladosClient, Dict[str, Any]]] = []
    for domain in domains:
        client = GladosClient(cookie, domain, base_ua)
        status = client.status()
        if status.get("authorized"):
            authorized.append((client, status))
        else:
            client.close()

    if not authorized:
        log_warning("Cookie 无效或无权限，已尝试: " + " / ".join(domains))
        return [DomainResult(domain=domains[0], valid=False, message="Cookie 无效或无权限")]

    try:
        return [_checkin_authorized(client, status) for client, status in authorized]
    finally:
        for client, _ in authorized:
            client.close()


# ==================== 报告 ====================
_STATUS_TEXT = {"success": "签到成功", "repeat": "重复签到", "fail": "签到失败"}
_STATUS_ICON = {"success": "✅", "repeat": "🔄", "fail": "❌"}


def build_report(accounts: List[List[DomainResult]], domains: List[str]) -> str:
    """构建签到报告"""
    lines: List[str] = []
    stats = {"success": 0, "repeat": 0, "fail": 0}

    for index, results in enumerate(accounts, 1):
        lines.append(f"👤 账号 {index}")
        valid = [r for r in results if r.valid]

        if not valid:
            stats["fail"] += 1
            lines.append(f"  ❌ Cookie 无效或无权限（已尝试 {' / '.join(domains)}）")
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
    if not cookies:
        content = (
            "❌ 未配置 GLADOS_COOKIE，无法签到。\n\n"
            + "─" * 18
            + f"\n🕒 执行时间: {beijing_time_str()}"
        )
        print("\n" + content)
        notify_send("🎫 GLaDOS / Railgun 签到结果", content)
        return 1

    domains = parse_domains(GLADOS_DOMAINS)
    base_ua = GLADOS_USER_AGENT or DEFAULT_USER_AGENT
    log_info(f"共 {len(cookies)} 个账号，目标域名: {' / '.join(domains)}")

    accounts: List[List[DomainResult]] = []
    for index, cookie in enumerate(cookies, 1):
        log_info(f"===== 账号 {index} =====")
        results = checkin_account(cookie, domains, base_ua)
        for result in results:
            if not result.valid:
                continue
            if result.status == "success":
                log_success(f"[{result.domain}] 签到成功，获得 {result.gained_points} 积分，剩余 {result.left_days} 天")
            elif result.status == "repeat":
                log_info(f"[{result.domain}] 今日已签到，剩余 {result.left_days} 天")
            else:
                log_error(f"[{result.domain}] 签到失败: {result.message}")
        accounts.append(results)

    content = build_report(accounts, domains)
    print("\n" + content)
    notify_send("🎫 GLaDOS / Railgun 签到结果", content)

    fail_count = sum(1 for results in accounts if not any(r.valid for r in results)) + sum(
        1 for results in accounts for r in results if r.valid and r.status == "fail"
    )
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

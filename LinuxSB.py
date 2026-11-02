#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 0 9 * * *
# new Env("烧饼社区签到")
# linux.sb（烧饼社区）自动签到 + 点击有奖广告领取烧饼
# - 签到：访问首页触发站点自动签到，未签到时回退提交 /daily_checkin 的签到表单
# - 领饼：解析侧栏「有奖广告」（红点）槽位，逐个提交站点自带的领取表单
# - 所有写操作（签到 / 领奖 / 显示隐藏广告）一律不自动重试，超时只报告「结果未知」
# - 接口地址与表单字段全部从页面动态解析，站点改版后无需改代码
# - 支持多账号（& 或换行分隔）

import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

from utils import create_session, log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send

# ==================== 用户配置 ====================
LSB_COOKIE = os.getenv("LSB_COOKIE", "").strip()
LSB_RANDOM = os.getenv("LSB_RANDOM", "true").strip().lower()
LSB_ADS = os.getenv("LSB_ADS", "true").strip().lower()
LSB_AD_TOGGLE = os.getenv("LSB_AD_TOGGLE", "true").strip().lower()
LSB_USER_AGENT = os.getenv("LSB_USER_AGENT", "").strip()

# ==================== 站点常量 ====================
SITE = "https://linux.sb"
HOME_URL = SITE + "/"
CHECKIN_URL = SITE + "/daily_checkin"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

TIMEOUT = (10, 30)

# 有奖广告点击次数兜底上限（真实数量以页面渲染为准，仅防异常时扩大范围）
MAX_AD_CLICKS = 10
# 点击广告之间的随机间隔（秒）
AD_DELAY_RANGE = (3.0, 8.0)
# 签到前随机延迟上限（秒）
START_DELAY_MAX = 300

# 侧栏广告板的三个表单：点击领奖 / 显示广告 / 隐藏广告
PROMO_FORM_KINDS = ("click", "show", "hide")
PROMO_ATTR = "data-sidebar-promotions-{}"

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
}

# 签到状态关键字
CHECKIN_DONE_RE = re.compile(r"已签到|今日已签|明日再来")
CHECKIN_FLASH_RE = re.compile(r"已帮您完成自动签到|签到成功|签到获得")
CHECKIN_FORM_RE = re.compile(r"check.?in", re.IGNORECASE)

CHECKIN_ICON = {"success": "✅", "repeat": "ℹ️", "fail": "❌", "unknown": "❓"}


# ==================== 异常定义 ====================
class LsbError(Exception):
    """明确的失败：站点已给出否定结果，继续执行没有意义。"""


class LsbUnknown(Exception):
    """结果未知：写操作可能已经生效，禁止重试，只能请用户到站内核对。"""


# ==================== 工具函数 ====================
def _enabled(value: str, default: bool = True) -> bool:
    """环境变量开关判定：仅 false/0/no/off 视为关闭；留空时取默认值。"""
    text = (value or "").strip().lower()
    if not text:
        return default
    return text not in ("false", "0", "no", "off")


def _num(value: Any) -> int:
    """把接口返回的数字（可能是字符串/浮点）规整为非负整数。"""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _confirmed(value: Any) -> bool:
    """兼容布尔值以及 1 / "1" / "true" 等明确真值，不把字符串 false 当成功。"""
    if value is True:
        return True
    if isinstance(value, int) and value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in ("1", "true")


def _split_cookies(raw: str) -> List[str]:
    """多账号拆分：& 或换行分隔（Cookie 值只含 ; 和 =，不含 &）。"""
    return [item.strip() for item in re.split(r"[&\n]+", raw or "") if item.strip()]


def _load_cookies(session: requests.Session, raw_cookie: str) -> None:
    """把浏览器 Cookie 请求头无损导入 CookieJar。

    用 partition("=") 而不是 split("=") 以免截断含 = 的值；并显式绑定
    linux.sb 域名，使服务端 Set-Cookie 能正常覆盖同名 Cookie。
    """
    raw = re.sub(r"^cookie\s*:\s*", "", (raw_cookie or "").strip(), flags=re.IGNORECASE)
    for item in raw.split(";"):
        name, sep, value = item.strip().partition("=")
        if not sep or not name.strip():
            continue
        session.cookies.set(name.strip(), value, domain="linux.sb", path="/")


# ==================== 页面解析 ====================
@dataclass
class PromoForm:
    """页面上真实存在的表单：action / method / 隐藏字段全部动态读取。"""

    kind: str
    action: str
    method: str
    fields: Dict[str, str] = field(default_factory=dict)


@dataclass
class AdSlot:
    """侧栏有奖广告槽位（红点）。"""

    placement_key: str
    order_id: str
    done: bool = False


class LsbPage(HTMLParser):
    """解析 linux.sb 页面：登录态、侧栏广告板、表单与有奖广告槽位。

    不使用 bs4/lxml 等额外依赖，保持与仓库其它脚本一致（仅 requests）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: List[Tuple[str, Dict[str, str]]] = []
        self._form_stack: List[PromoForm] = []
        self._name_parts: List[str] = []
        self._script_parts: List[str] = []
        self._in_script = False

        # 登录态
        self.has_user_card = False
        self.visitor = False
        # 首页 flash 提示（站点自动签到的回执）
        self.flash = ""
        # 侧栏广告板
        self.board_seen = False
        self.board_hidden = False
        self.can_hide = False
        # 表单：promo 三类 + 页面上所有表单
        self.forms: Dict[str, PromoForm] = {}
        self.forms_all: List[PromoForm] = []
        # 有奖广告槽位
        self.slots: List[AdSlot] = []
        self.ad_link_count = 0

    # ---------- 解析辅助 ----------
    def _inside_class(self, name: str) -> bool:
        return any(name in attrs.get("class", "").split() for _, attrs in self._stack)

    def _inside_attr(self, name: str) -> bool:
        return any(name in attrs for _, attrs in self._stack)

    def handle_startendtag(self, tag: str, attrs_list) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs_list) -> None:
        # 属性值缺省（如 hidden）统一为空串，便于用 in 判断存在性
        attrs = {key: ("" if value is None else value) for key, value in attrs_list}
        classes = attrs.get("class", "").split()

        if tag == "script":
            self._in_script = True
            self._script_parts = []

        # 登录态：user-card 内的用户名 / 访客标记
        if "user-card" in classes:
            self.has_user_card = True
        if self._inside_class("user-card") and ("visitor-avatar" in classes or "side-auth" in classes):
            self.visitor = True

        in_board = self._inside_attr(PROMO_ATTR.format("board"))
        if PROMO_ATTR.format("board") in attrs:
            self.board_seen = True
            self.can_hide = attrs.get(PROMO_ATTR.format("can-hide")) == "1"
        elif in_board:
            # 广告板显示状态：hidden 属性或 is-hidden 类
            if PROMO_ATTR.format("wrap") in attrs:
                self.board_hidden = ("hidden" in attrs) or ("is-hidden" in classes)
            if PROMO_ATTR.format("can-hide") in attrs:
                self.can_hide = attrs.get(PROMO_ATTR.format("can-hide")) == "1"

        # 表单：三类推广表单 + 其余表单（用于动态定位签到表单）
        if tag == "form":
            kind = ""
            for candidate in PROMO_FORM_KINDS:
                if PROMO_ATTR.format(f"{candidate}-form") in attrs:
                    kind = candidate
                    break
            form = PromoForm(
                kind=kind or "form",
                action=urljoin(HOME_URL, attrs.get("action", "")),
                method=(attrs.get("method") or "get").strip().lower(),
            )
            self.forms_all.append(form)
            if kind:
                self.forms[kind] = form
            self._form_stack.append(form)

        # 表单字段：归属到最内层表单
        if tag == "input" and self._form_stack:
            name = attrs.get("name")
            checked = attrs.get("type") not in ("checkbox", "radio") or "checked" in attrs
            if name and "disabled" not in attrs and checked:
                self._form_stack[-1].fields[name] = attrs.get("value", "")

        # 有奖广告槽位
        if tag == "a" and in_board and attrs.get(PROMO_ATTR.format("ad-link")) == "1":
            self.ad_link_count += 1
            if attrs.get(PROMO_ATTR.format("lottery-slot")) == "1":
                key = attrs.get("data-promotion-id", "")
                order_id = attrs.get(PROMO_ATTR.format("order-id"), "")
                if key and order_id:
                    self.slots.append(AdSlot(
                        placement_key=key,
                        order_id=order_id,
                        done=attrs.get(PROMO_ATTR.format("lottery-done")) == "1",
                    ))

        if tag not in VOID_TAGS:
            self._stack.append((tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script:
            self._in_script = False
            self._parse_flash("".join(self._script_parts))
            self._script_parts = []
        if tag == "form" and self._form_stack:
            self._form_stack.pop()
        # 容忍页面嵌套不规范：回退到最近的同名开标签
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_parts.append(data)
            return
        if self._inside_class("user-card") and self._inside_class("user-name"):
            self._name_parts.append(data)

    def _parse_flash(self, script: str) -> None:
        """从内联脚本里提取 window.__pageFlash 的提示文本。"""
        if self.flash:
            return
        match = re.search(r'__pageFlash\s*=\s*("(?:\\.|[^"\\])*")', script)
        if not match:
            return
        try:
            self.flash = json.loads(match.group(1))
        except (ValueError, TypeError):
            self.flash = ""

    # ---------- 便捷属性 ----------
    @property
    def username(self) -> str:
        return "".join(self._name_parts).strip()

    @property
    def logged_in(self) -> bool:
        name = self.username
        return self.has_user_card and bool(name) and name != "访客" and not self.visitor

    @property
    def reward_day(self) -> str:
        """站点自己声明的当天日期，用于跨日检测；不做本地时间猜测。"""
        form = self.forms.get("click")
        return (form.fields.get("reward_day", "") if form else "").strip()

    @property
    def pending_slots(self) -> List[AdSlot]:
        return [slot for slot in self.slots if not slot.done]


# ==================== HTTP 封装 ====================
def _get(session: requests.Session, url: str, referer: str = HOME_URL) -> requests.Response:
    """GET 请求：不自动重试（首页访问本身会触发站点自动签到）。"""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer,
    }
    try:
        return session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        raise LsbUnknown(f"请求 {url} 失败（{exc.__class__.__name__}），未重试") from None


def _post_form(
    session: requests.Session,
    url: str,
    fields: Dict[str, str],
    label: str,
    referer: str = HOME_URL,
) -> requests.Response:
    """以 multipart/form-data 提交表单（与浏览器 FormData 行为一致）。

    所有写操作都不自动重试：超时或断连时结果未知，重试可能造成重复领取。
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "linux.sb":
        raise LsbError(f"{label}：表单地址不属于本站（{url}），已停止")
    files = {name: (None, str(value)) for name, value in fields.items()}
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": SITE,
        "Referer": referer,
    }
    try:
        return session.post(
            url, files=files, headers=headers, timeout=TIMEOUT, allow_redirects=False
        )
    except requests.RequestException as exc:
        raise LsbUnknown(f"{label}：请求失败（{exc.__class__.__name__}），结果未知，未重试") from None


def _post_json(
    session: requests.Session,
    url: str,
    fields: Dict[str, str],
    label: str,
    referer: str = HOME_URL,
) -> Dict[str, Any]:
    """提交表单并解析 JSON 回执；区分「明确失败」与「结果未知」。"""
    response = _post_form(session, url, fields, label, referer)

    data: Optional[Dict[str, Any]] = None
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            data = parsed
    except ValueError:
        data = None

    if data is not None:
        if not data.get("ok"):
            reason = data.get("message") or data.get("error") or "站点未确认成功"
            raise LsbError(f"{label}：{reason}")
        return data

    if response.status_code >= 400:
        raise LsbError(f"{label}：HTTP {response.status_code}")
    raise LsbUnknown(f"{label}：响应不是 JSON（可能需要重新登录），结果未知，未重试")


def _form_ready(
    form: Optional[PromoForm],
    label: str,
    required: Tuple[str, ...] = (),
    require_csrf: bool = True,
) -> Optional[str]:
    """提交前的形式校验：不满足就放弃，而不是猜测接口盲发请求。

    返回错误说明；None 表示可以提交。签到表单不强制要求 _csrf（站点自身也未带）。
    """
    if form is None:
        return f"{label}：页面未提供该表单"
    if form.method != "post":
        return f"{label}：表单 method 不是 POST（{form.method or '空'}）"
    if require_csrf and not form.fields.get("_csrf"):
        return f"{label}：表单缺少 _csrf 字段"
    for name in required:
        if not form.fields.get(name):
            return f"{label}：表单缺少 {name} 字段"
    return None


def parse_page(text: str) -> LsbPage:
    """解析一段 HTML，返回页面状态对象。"""
    page = LsbPage()
    page.feed(text)
    page.close()
    return page


def fetch_home(session: requests.Session) -> LsbPage:
    """访问首页：触发站点自动签到，并解析登录态 / 广告板 / 表单。"""
    response = _get(session, HOME_URL)
    if "/login" in response.url:
        raise LsbError("Cookie 无效或已过期（首页被重定向到登录页）")
    page = parse_page(response.text)
    if not page.logged_in:
        raise LsbError("未确认登录：Cookie 可能失效、IP 被安全验证或页面结构已变化")
    return page


# ==================== 签到 ====================
def do_checkin(session: requests.Session, page: LsbPage) -> Tuple[str, str]:
    """签到：优先采信首页 flash，其次回退提交 /daily_checkin 的签到表单。

    返回 (状态, 说明)，状态取 success / repeat / fail / unknown。
    """
    if CHECKIN_FLASH_RE.search(page.flash):
        return "success", f"首页自动签到已回执（{page.flash.strip()}）"

    try:
        response = _get(session, CHECKIN_URL)
    except LsbUnknown as exc:
        return "unknown", str(exc)

    if "/login" in response.url:
        return "fail", "签到页被重定向到登录页，Cookie 可能已失效"

    text = response.text
    if CHECKIN_DONE_RE.search(text):
        return "repeat", "今日已签到"

    checkin_page = parse_page(text)
    form = next(
        (
            item for item in checkin_page.forms_all
            if item.method == "post" and CHECKIN_FORM_RE.search(urlsplit(item.action).path)
        ),
        None,
    )
    problem = _form_ready(form, "签到", require_csrf=False)
    if problem:
        return "unknown", f"{problem}，无法签到；请在站内人工核对"
    assert form is not None  # _form_ready 已保证

    try:
        result = _post_form(session, form.action, dict(form.fields), "签到", referer=CHECKIN_URL)
    except LsbUnknown as exc:
        return "unknown", str(exc)
    except LsbError as exc:
        return "fail", str(exc)

    # 会话失效时站点会 3xx 到登录页，不能当成签到成功
    if result.is_redirect and "/login" in (result.headers.get("Location") or ""):
        return "fail", "签到请求被重定向到登录页，Cookie 可能已失效"

    # 签到回执可能是 JSON，也可能是一次 303 后的 HTML 页面
    try:
        data = result.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        if not data.get("ok"):
            reason = data.get("message") or data.get("error") or "站点未确认签到成功"
            return ("repeat" if CHECKIN_DONE_RE.search(str(reason)) else "fail"), str(reason)
        return "success", str(data.get("message") or "签到成功")

    if result.status_code >= 400:
        return "fail", f"签到失败：HTTP {result.status_code}"
    if CHECKIN_DONE_RE.search(result.text):
        return "repeat", "今日已签到"
    return "success", "签到请求已提交（站点未返回明确提示，请在站内核对）"


# ==================== 有奖广告 ====================
def _sleep_between_ads() -> None:
    delay = random.uniform(*AD_DELAY_RANGE)
    log_info(f"随机延迟 {delay:.1f}s 后继续 ...")
    time.sleep(delay)


def _ensure_visible(
    session: requests.Session,
    page: LsbPage,
    report: "AccountReport",
    label: str,
) -> Tuple[LsbPage, bool]:
    """广告板隐藏时恢复可见。

    站点按「广告是否被看到」计奖，隐藏状态下的点击可能不计入，
    因此领饼前与领饼中遇 hidden 都要先恢复可见。
    返回 (page, 是否可继续)；失败时已写入报告并置状态为 unknown。
    """
    if not page.board_hidden:
        return page, True
    problem = _form_ready(page.forms.get("show"), label)
    if problem:
        report.lines.append(f"  ⚠️ {problem}")
        report.status = "unknown"
        return page, False
    form = page.forms["show"]
    try:
        _post_json(session, form.action, dict(form.fields), label)
        return fetch_home(session), True
    except (LsbError, LsbUnknown) as exc:
        report.lines.append(f"  ❓ {exc}")
        report.status = "unknown"
        return page, False


def claim_ads(session: requests.Session, page: LsbPage, report: "AccountReport") -> LsbPage:
    """逐个点击侧栏有奖广告（红点）领取烧饼。

    每点击一次都重新读取首页，以站点的实际状态（页面标记 + 接口 lottery）为准，
    而不是本地记账，因此当天重复执行天然幂等。
    """
    if not page.board_seen:
        report.lines.append("  ⚠️ 未识别到侧栏广告板，跳过领饼（可能是权限或页面结构变化）")
        report.status = "unknown"
        return page
    if not page.slots:
        if page.ad_link_count:
            report.lines.append("  ℹ️ 广告板只有展示型广告，无有奖广告槽位，跳过领饼")
        else:
            # 连展示广告都没渲染，不能把「没红点」当成「今日已全部完成」
            report.lines.append("  ⚠️ 页面未渲染任何广告位，结构可能变化；无法确认领饼状态，请站内核对")
            report.status = "unknown"
        return page

    day = page.reward_day
    total = len(page.slots)
    claimed = {slot.placement_key for slot in page.slots if slot.done}
    attempted: set = set()
    gain_total = 0

    # 广告板处于隐藏状态时先显示，否则隐藏状态下的点击可能不计入
    page, visible = _ensure_visible(session, page, report, "领取前显示广告板")
    skip_claim = not visible

    while not skip_claim and len(attempted) < MAX_AD_CLICKS:
        pending = [
            slot for slot in page.slots
            if slot.placement_key not in claimed and slot.placement_key not in attempted
        ]
        if not pending:
            break
        if total and len(claimed) >= total:
            break

        slot = pending[0]
        attempted.add(slot.placement_key)
        form = page.forms.get("click")
        problem = _form_ready(form, "点击领奖", required=("reward_day",))
        if problem:
            report.lines.append(f"  ❌ {problem}，已放弃领饼")
            report.status = "unknown"
            break
        assert form is not None  # _form_ready 已保证

        label = f"有奖广告 {min(len(claimed) + 1, total)}/{total}"
        fields = dict(form.fields)
        fields["placement_key"] = slot.placement_key
        fields["order_id"] = slot.order_id

        _sleep_between_ads()
        try:
            data = _post_json(session, form.action, fields, label)
        except LsbUnknown as exc:
            report.lines.append(f"  ❓ {exc}")
            report.status = "unknown"
            break
        except LsbError as exc:
            report.lines.append(f"  ❌ {exc}")
            report.status = "fail"
            break

        gain = _num(data.get("points"))
        lottery = data.get("lottery")
        server_claimed: set = set()
        if isinstance(lottery, dict):
            total = _num(lottery.get("total")) or total
            slots = lottery.get("slots")
            if isinstance(slots, dict):
                for key, item in slots.items():
                    if isinstance(item, dict) and _confirmed(item.get("claimed")):
                        server_claimed.add(key)
            if _confirmed(lottery.get("can_hide")):
                page.can_hide = True

        # 必须拿到发奖或「已领取」凭据；否则说明点击未被服务端承认，停下来人工核对
        if _confirmed(data.get("rewarded")) and gain > 0:
            gain_total += gain
            report.lines.append(f"  🫓 {label}：明确新增 +{gain} 烧饼")
            log_success(f"{label}：+{gain} 烧饼")
        elif _confirmed(data.get("already_drawn")):
            report.lines.append(f"  ℹ️ {label}：今日已完成该广告")
        elif slot.placement_key in server_claimed:
            report.lines.append(f"  ℹ️ {label}：站点已标记该广告今日完成")
        else:
            report.lines.append(f"  ❓ {label}：请求已响应但无发奖或已领取凭据，停止领饼（未重试）")
            report.status = "unknown"
            break
        claimed.add(slot.placement_key)

        # 重新读取首页：刷新 CSRF、槽位状态，并检测跨日
        try:
            page = fetch_home(session)
        except (LsbError, LsbUnknown) as exc:
            report.lines.append(f"  ❓ 刷新首页失败（{exc}），已停止领饼；请站内核对")
            report.status = "unknown"
            break
        if day and page.reward_day and page.reward_day != day:
            report.lines.append("  ⚠️ 执行期间站点日期已变化，停止领饼")
            report.status = "unknown"
            break
        claimed |= {item.placement_key for item in page.slots if item.done}
        if data.get("hidden") is True:
            # hidden 通常伴随全部完成，但不以此作为完成凭据：
            # 还有未领位置时先恢复可见再继续，避免漏领。
            if total and len(claimed) >= total:
                break
            page, visible = _ensure_visible(session, page, report, "恢复显示广告板")
            if not visible:
                break
            if day and page.reward_day and page.reward_day != day:
                report.lines.append("  ⚠️ 执行期间站点日期已变化，停止领饼")
                report.status = "unknown"
                break
            claimed |= {item.placement_key for item in page.slots if item.done}

    report.points += gain_total
    report.ads_total = total
    report.ads_done = len(claimed)
    report.remaining = max(0, total - len(claimed))
    return page


def toggle_ads(session: requests.Session, page: LsbPage, report: "AccountReport") -> LsbPage:
    """显示 → 隐藏 → 恢复显示广告板，每一步都以站点返回结果为准。"""
    if not page.board_seen:
        return page

    steps: List[str] = []
    stopped = False

    def step(kind: str, label: str) -> None:
        nonlocal page, stopped
        if stopped:
            return
        if kind == "hide" and not page.can_hide:
            report.lines.append("  ⏭️ 隐藏广告板：站点今日未开放，跳过")
            return
        # 已是目标状态就不重复提交（与站点插件行为一致，避免无谓写入）
        if page.board_hidden == (kind == "hide"):
            report.lines.append(f"  ⏭️ {label}：当前已是目标状态，跳过")
            return
        form = page.forms.get(kind)
        problem = _form_ready(form, label)
        if problem:
            report.lines.append(f"  ⏭️ {problem}，跳过")
            return
        assert form is not None  # _form_ready 已保证
        try:
            data = _post_json(session, form.action, dict(form.fields), label)
        except LsbUnknown as exc:
            report.lines.append(f"  ❓ {exc}")
            report.status = "unknown"
            stopped = True  # 结果未知，不能再接着写
            return
        except LsbError as exc:
            report.lines.append(f"  ❌ {exc}")
            report.status = "fail"
            stopped = True
            return

        gain = _num(data.get("points"))
        if _confirmed(data.get("rewarded")) and gain > 0:
            report.points += gain
            report.lines.append(f"  🫓 {label}：+{gain} 烧饼")
            log_success(f"{label}：+{gain} 烧饼")
        else:
            steps.append(label)
        lottery = data.get("lottery")
        if isinstance(lottery, dict) and _confirmed(lottery.get("can_hide")):
            page.can_hide = True
        try:
            page = fetch_home(session)
        except (LsbError, LsbUnknown) as exc:
            report.lines.append(f"  ❓ 刷新首页失败（{exc}），已停止广告板切换")
            report.status = "unknown"
            stopped = True
            return

    if page.board_hidden:
        step("show", "显示广告板")
    step("hide", "隐藏广告板")
    # 只有确实被隐藏了才需要恢复显示；hide 未生效时不做多余写入
    if page.board_hidden:
        step("show", "恢复显示广告板")

    if steps:
        report.lines.append("  📺 广告板：" + " → ".join(steps))
    return page


# ==================== 结果与报告 ====================
@dataclass
class AccountReport:
    index: int
    username: str = ""
    status: str = "success"
    lines: List[str] = field(default_factory=list)
    points: int = 0
    ads_total: int = 0
    ads_done: int = 0
    remaining: int = 0


def run_account(index: int, raw_cookie: str) -> AccountReport:
    report = AccountReport(index=index)
    session = create_session(user_agent=LSB_USER_AGENT or DEFAULT_USER_AGENT)
    _load_cookies(session, raw_cookie)

    # ---- 1. 首页：触发自动签到并解析页面 ----
    try:
        page = fetch_home(session)
    except (LsbError, LsbUnknown) as exc:
        report.status = "fail" if isinstance(exc, LsbError) else "unknown"
        report.lines.append(f"  {'❌' if isinstance(exc, LsbError) else '❓'} {exc}")
        return report

    report.username = page.username
    log_success(f"已确认登录：{page.username}")

    # ---- 2. 签到 ----
    state, message = do_checkin(session, page)
    report.lines.append(f"  {CHECKIN_ICON.get(state, '❓')} 签到：{message}")
    if state in ("success", "repeat"):
        log_success(f"签到：{message}")
    else:
        log_warning(f"签到：{message}")
        if state == "unknown":
            report.status = "unknown"

    # ---- 3. 有奖广告 ----
    if not _enabled(LSB_ADS):
        report.lines.append("  ⏭️ 有奖广告：LSB_ADS 已关闭，跳过")
    else:
        try:
            page = claim_ads(session, page, report)
            if _enabled(LSB_AD_TOGGLE):
                toggle_ads(session, page, report)
            else:
                report.lines.append("  ⏭️ 广告板显示/隐藏：LSB_AD_TOGGLE 已关闭，跳过")
        except LsbUnknown as exc:
            report.lines.append(f"  ❓ {exc}")
            report.status = "unknown"
        except LsbError as exc:
            report.lines.append(f"  ❌ {exc}")
            report.status = "fail"

    if report.ads_total:
        report.lines.append(f"  📈 今日有奖广告：{report.ads_done}/{report.ads_total} 已完成，剩余 {report.remaining} 次")
    return report


def build_report(reports: List[AccountReport]) -> str:
    lines: List[str] = []
    stats = {"success": 0, "fail": 0, "unknown": 0}
    for report in reports:
        stats[report.status] = stats.get(report.status, 0) + 1
        title = report.username or "未确认登录"
        lines.append(f"👤 账号 {report.index} · {title}")
        lines.extend(report.lines)
        if report.points:
            lines.append(f"  🫓 本次明确新增：+{report.points} 烧饼")
        lines.append("")

    lines.append(f"📊 统计: 成功 {stats['success']} · 失败 {stats['fail']} · 结果未知 {stats['unknown']}")
    lines.append("─" * 18)
    lines.append(f"🕒 执行时间: {beijing_time_str()}")
    return "\n".join(lines)


# ==================== 入口 ====================
def main() -> int:
    log_info("linux.sb（烧饼社区）签到脚本启动")

    cookies = _split_cookies(LSB_COOKIE)
    if not cookies:
        content = (
            "❌ 未配置 LSB_COOKIE，无法执行。\n\n"
            "请在青龙「环境变量」中添加 linux.sb 的 Cookie（多账号用 & 或换行分隔）。\n\n"
            + "─" * 18
            + f"\n🕒 执行时间: {beijing_time_str()}"
        )
        print("\n" + content)
        notify_send("🫓 linux.sb 烧饼签到", content)
        return 1

    if _enabled(LSB_RANDOM):
        delay = random.uniform(0, START_DELAY_MAX)
        if delay > 1:
            minutes, seconds = divmod(int(delay), 60)
            log_info(f"签到前随机延迟: {minutes}m {seconds}s ...")
            time.sleep(delay)

    log_info(f"共 {len(cookies)} 个账号")
    reports: List[AccountReport] = []
    for index, cookie in enumerate(cookies, 1):
        log_info(f"===== 账号 {index} =====")
        try:
            reports.append(run_account(index, cookie))
        except Exception as exc:  # 单个账号异常不影响其余账号，也不丢报告
            log_error(f"账号 {index} 执行异常：{exc.__class__.__name__}: {exc}")
            broken = AccountReport(index=index, status="fail")
            broken.lines.append(f"  ❌ 执行异常：{exc.__class__.__name__}: {exc}")
            reports.append(broken)

    content = build_report(reports)
    print("\n" + content)
    notify_send("🫓 linux.sb 烧饼签到", content)

    return 0 if all(report.status == "success" for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 0 10 * * *
# new Env("明日方舟寻访公告")
# 明日方舟限时寻访（卡池）公告监控
# - 逆向官网 API (https://ak.hypergryph.com/api/news) 获取活动公告列表
# - 识别"寻访"类公告，提取每期卡池数据（名称/类型/活动时间/UP干员/出率/保底/兑换所）
# - 检测到新卡池公告时通过 notifier 推送通知
#
# 逆向所得接口：
#   列表 GET https://ak.hypergryph.com/api/news?category={LATEST|ANNOUNCEMENT|ACTIVITY|NEWS}&page={n}
#     响应 {code:0, data:{list:[{cid,tab,sticky,title,author,displayTime,cover,extraCover,brief}], total, end}}
#     每页 6 条，tab: 0=公告 1=活动 2=新闻；end=true 表示末页
#   详情 GET https://ak.hypergryph.com/news/{cid}  （SSR HTML，正文 <p> 段落）

import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from utils import (
    log_info,
    log_success,
    log_warning,
    log_error,
    beijing_time_str,
    create_session,
)
from notifier import send as notify_send

# ==================== 用户配置 ====================
SCRIPT_DIR = Path(__file__).parent
CONFIG_DIR = SCRIPT_DIR / "config"
STATE_FILE = CONFIG_DIR / ".arknights_banner_state.json"
BASE_URL = "https://ak.hypergryph.com"
LIST_API = f"{BASE_URL}/api/news"
DETAIL_URL = f"{BASE_URL}/news"
SCAN_PAGES = 3          # 每次扫描活动公告页数（每页 6 条，3 页覆盖约 3-6 个月）
BANNER_KEYWORD = "寻访"  # 卡池公告标题关键词
MAX_MESSAGE_LENGTH = 3900
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN = 0.5

_TZ_BEIJING = timezone(timedelta(hours=8))


# ==================== 状态持久化 ====================
def load_state() -> Dict:
    default = {"known_cids": [], "last_run": "", "notified_cids": []}
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                s.setdefault("known_cids", [])
                s.setdefault("notified_cids", [])
                return s
    except Exception as e:
        log_error(f"读取状态文件失败: {e}")
    return default


def save_state(state: Dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        state["last_run"] = beijing_time_str()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log_success(f"状态文件已更新，已知卡池 {len(state['known_cids'])} 个")
    except Exception as e:
        log_error(f"保存状态文件失败: {e}")


# ==================== 接口请求 ====================
def fetch_news_list(session, category: str = "ACTIVITY", page: int = 1) -> Dict:
    """获取公告列表（逆向接口）"""
    resp = session.get(LIST_API, params={"category": category, "page": page}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0 or not data.get("data"):
        raise RuntimeError(f"公告列表 API 返回异常: code={data.get('code')} msg={data.get('msg')}")
    return data["data"]


def fetch_gacha_banners(session, pages: int = SCAN_PAGES) -> List[Dict]:
    """扫描活动公告，返回寻访（卡池）类公告列表，按发布时间升序"""
    banners: List[Dict] = []
    seen = set()
    for page in range(1, pages + 1):
        log_info(f"正在获取活动公告第 {page}/{pages} 页...")
        try:
            result = fetch_news_list(session, "ACTIVITY", page)
        except Exception as e:
            log_error(f"获取第 {page} 页失败: {e}")
            break
        for item in result.get("list", []):
            cid = item.get("cid")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            if BANNER_KEYWORD in item.get("title", ""):
                banners.append(item)
        log_info(f"  第 {page} 页获取 {len(result.get('list', []))} 条，累计卡池公告 {len(banners)} 条")
        if result.get("end"):
            log_info("已到末页，停止扫描")
            break
        time.sleep(SLEEP_BETWEEN)
    banners.sort(key=lambda x: x.get("displayTime", 0))
    return banners


def fetch_banner_detail(session, cid: str) -> Dict:
    """获取公告详情页 HTML 并提取正文段落与封面"""
    url = f"{DETAIL_URL}/{cid}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    html = resp.text
    # 移除所有 <script> 块（含 Next.js RSC flight data，避免正文重复）
    no_script = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    # 以日期标记（如 2026 // 07 / 05）作为正文起点
    date_match = re.search(r"\d{4} // \d{2} / \d{2}", no_script)
    seg = no_script[date_match.start():] if date_match else no_script
    # 截断到健康提示页脚前
    health_idx = seg.find("本网络游戏适合")
    if health_idx > 0:
        seg = seg[:health_idx]
    # 提取所有 <p> 段落纯文本
    paragraphs: List[str] = []
    for m in re.finditer(r"<p[^>]*>([\s\S]*?)</p>", seg, flags=re.IGNORECASE):
        text = re.sub(r"<[^>]+>", "", m.group(1))
        text = (text.replace("&nbsp;", " ")
                    .replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&#x27;", "'")
                    .replace("&quot;", '"'))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            paragraphs.append(text)
    # 提取卡池封面图（data-width="1560" 的横幅图）
    cover = None
    cover_m = re.search(r'<img[^>]*?src="([^"]+)"[^>]*?data-width="1560"', seg, flags=re.IGNORECASE)
    if cover_m:
        cover = cover_m.group(1)
    return {"cid": cid, "cover": cover, "paragraphs": paragraphs, "url": url}


# ==================== 数据解析 ====================
def _ts_to_beijing(ts: int) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, _TZ_BEIJING).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def parse_banner(item: Dict, detail: Dict) -> Dict:
    """从公告标题与正文段落解析卡池结构化数据"""
    title = item.get("title", "")
    # 标题形如：[狩猎凯旋干员寻访]【砺火成锋】限时寻访即将复刻开启
    m = re.match(r"\[([^\]]+)\]\s*【([^】]+)】", title)
    banner_type = m.group(1).strip() if m else ""
    banner_name = m.group(2).strip() if m else ""

    paragraphs = detail.get("paragraphs", [])

    # 活动时间
    activity_time = ""
    for p in paragraphs:
        if p.startswith("活动时间："):
            activity_time = p[len("活动时间："):].strip()
            break

    # 6★ UP（恰好 6 个 ★ 后紧跟「：」）
    six_star = [p for p in paragraphs if re.match(r"^★{6}：", p)]
    # 5★ UP（恰好 5 个 ★ 后紧跟「：」，排除 6★ 段落）
    five_star = [p for p in paragraphs if re.match(r"^★{5}：", p)]

    # 寻访数据契约兑换所
    exchange = [p for p in paragraphs
                if "寻访数据契约" in p and ("交换所" in p or re.match(r"^★{5,6}（", p) or "可兑换干员" in p)]

    # 保底与规则：◆ 开头的注意事项 + 含「必定/累计寻访」的保底说明 + 「【...】说明」小节标题
    rules: List[str] = []
    for p in paragraphs:
        if p.startswith("◆"):
            rules.append(p)
        elif "累计寻访" in p and ("必定" in p or "额外" in p):
            rules.append(p)
        elif re.match(r"^【.+?】说明$", p):
            rules.append(p)
    # 去重保序
    seen = set()
    rules = [x for x in rules if not (x in seen or seen.add(x))]

    return {
        "cid": item.get("cid"),
        "title": title,
        "banner_type": banner_type,
        "banner_name": banner_name,
        "activity_time": activity_time,
        "cover": detail.get("cover"),
        "six_star": six_star,
        "five_star": five_star,
        "exchange": exchange,
        "rules": rules,
        "announce_time": item.get("displayTime", 0),
        "brief": item.get("brief", ""),
        "url": detail.get("url", f"{DETAIL_URL}/{item.get('cid')}"),
    }


# ==================== 报告格式化 ====================
def format_banner_report(b: Dict, index: int = 0, total: int = 0) -> str:
    lines = []
    head = "🎰 明日方舟 新卡池公告"
    if total > 1:
        head += f"（{index}/{total}）"
    lines.append(head)
    lines.append("")
    if b["banner_type"]:
        lines.append(f"🏷️ 寻访类型: {b['banner_type']}")
    if b["banner_name"]:
        lines.append(f"📛 寻访名称: {b['banner_name']}")
    if b["activity_time"]:
        lines.append(f"📅 活动时间: {b['activity_time']}")
    if b["announce_time"]:
        lines.append(f"📰 发布时间: {_ts_to_beijing(b['announce_time'])}")
    if b.get("cover"):
        lines.append(f"🖼️ 卡池封面: {b['cover']}")
    lines.append("")

    if b["six_star"]:
        lines.append("⭐ 6★ UP干员（出现率上升）")
        for p in b["six_star"]:
            lines.append(f"  {p}")
        lines.append("")
    if b["five_star"]:
        lines.append("⭐ 5★ UP干员（出现率上升）")
        for p in b["five_star"]:
            lines.append(f"  {p}")
        lines.append("")
    if b["rules"]:
        lines.append("📋 保底与规则")
        for p in b["rules"]:
            lines.append(f"  {p}")
        lines.append("")
    if b["exchange"]:
        lines.append("🔄 寻访数据契约兑换所")
        for p in b["exchange"]:
            lines.append(f"  {p}")
        lines.append("")

    lines.append(f"🔗 公告链接: {b['url']}")
    lines.append("─" * 18)
    lines.append(f"🕒 推送时间: {beijing_time_str()}")
    return "\n".join(lines)


def split_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> List[str]:
    if len(text) <= max_len:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > max_len and cur:
            parts.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        parts.append(cur)
    return parts


# ==================== 主流程 ====================
def main():
    log_info("===== 明日方舟寻访公告监控开始 =====")
    state = load_state()
    known_cids = set(state.get("known_cids", []))
    notified_cids = set(state.get("notified_cids", []))
    is_first_run = len(known_cids) == 0

    session = create_session()

    try:
        banners = fetch_gacha_banners(session, SCAN_PAGES)
    except Exception as e:
        log_error(f"获取卡池公告列表失败: {e}")
        notify_send("明日方舟寻访公告", f"❌ 获取公告列表失败: {e}")
        return

    if not banners:
        log_warning("未扫描到任何寻访类公告")
        return

    log_info(f"共扫描到 {len(banners)} 个寻访类公告")

    current_cids = {b["cid"] for b in banners}
    # 新公告 = 本次扫描到且尚未知的
    new_banners = [b for b in banners if b["cid"] not in known_cids]

    if is_first_run:
        # 首次运行：记录全部已知，仅推送最新一期作为当前状态
        log_info("首次运行，记录当前全部卡池，推送最新一期")
        latest = banners[-1]
        try:
            detail = fetch_banner_detail(session, latest["cid"])
            parsed = parse_banner(latest, detail)
            report = format_banner_report(parsed)
            print("\n" + report + "\n")
            for i, part in enumerate(split_message(report), 1):
                notify_send(f"明日方舟 寻访公告（首次初始化）", part)
                if i < len(split_message(report)):
                    time.sleep(2)
            notified_cids.add(latest["cid"])
        except Exception as e:
            log_error(f"处理最新卡池 {latest.get('cid')} 失败: {e}")
        state["known_cids"] = sorted(current_cids)
        state["notified_cids"] = sorted(notified_cids | current_cids)
        save_state(state)
        log_info("===== 首次初始化完成 =====")
        return

    if not new_banners:
        log_info(f"暂无新卡池公告（已知 {len(known_cids)} 个）")
        state["known_cids"] = sorted(known_cids | current_cids)
        save_state(state)
        return

    log_success(f"发现 {len(new_banners)} 个新卡池公告！")
    total = len(new_banners)
    success = 0
    for idx, item in enumerate(new_banners, 1):
        cid = item["cid"]
        log_info(f"正在处理新卡池 [{idx}/{total}] cid={cid}：{item.get('title')}")
        try:
            detail = fetch_banner_detail(session, cid)
            parsed = parse_banner(item, detail)
            report = format_banner_report(parsed, idx, total)
            print("\n" + report + "\n")
            parts = split_message(report)
            for i, part in enumerate(parts, 1):
                title = f"明日方舟 新卡池公告（{idx}/{total}）"
                if len(parts) > 1:
                    title += f"（{i}/{len(parts)}）"
                notify_send(title, part)
                if i < len(parts):
                    time.sleep(2)
            notified_cids.add(cid)
            success += 1
            log_success(f"卡池 cid={cid} 推送完成")
        except Exception as e:
            log_error(f"处理卡池 cid={cid} 失败: {e}")
        time.sleep(SLEEP_BETWEEN)

    state["known_cids"] = sorted(known_cids | current_cids)
    state["notified_cids"] = sorted(notified_cids)
    save_state(state)
    log_info(f"===== 任务完成：新推送 {success}/{total} =====")


if __name__ == "__main__":
    main()

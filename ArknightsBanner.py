#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 0 10 * * *
# new Env("明日方舟寻访公告")
# 明日方舟限时寻访（卡池）公告监控
# - 逆向官网 API (https://ak.hypergryph.com/api/news) 获取活动公告列表
# - 三层过滤识别卡池公告：① 标题含"寻访" ② 摘要含卡池关键词 ③ 详情确认含卡池数据
#   （可抓到"活动附带寻访"等标题无"寻访"但内容是卡池的公告）
# - 提取每期卡池数据（名称/类型/活动时间/UP干员/出率/保底/兑换所）
# - 增量扫描：首次全量建库，日常遇已知 cid 即停；有新卡池时通过 notifier 推送通知
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
MAX_SCAN_PAGES = 50       # 扫描活动公告页数上限（每页 6 条）；日常增量扫描遇已知 cid 即停
BANNER_KEYWORD = "寻访"   # 卡池公告标题关键词（第一层过滤）
# 摘要关键词（第二层过滤：标题无"寻访"但摘要含卡池信号，如"活动附带寻访"公告）
GACHA_BRIEF_KW = [
    "出现率上升", "占6★出率", "占5★出率", "占4★出率",
    "寻访开启", "限时寻访", "限定寻访", "标准寻访", "中坚寻访",
    "联合寻访", "干员寻访", "寻访数据契约", "出率提升", "6★出率", "5★出率",
]
# 详情确认关键词（第三层：确认正文确实含卡池数据，避免摘要误判）
GACHA_CONFIRM_KW = ["出现率上升", "占6★出率", "占5★出率", "占4★出率", "6★出率", "5★出率", "4★出率"]
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


def is_gacha_candidate(item: Dict) -> bool:
    """两层过滤：标题含"寻访" 或 摘要含卡池关键词（抓活动附带的寻访公告）"""
    title = item.get("title", "")
    if BANNER_KEYWORD in title:
        return True
    brief = item.get("brief", "")
    return any(k in brief for k in GACHA_BRIEF_KW)


def confirm_gacha_by_detail(paragraphs: List[str]) -> bool:
    """第三层确认：正文确实含卡池数据关键词（避免摘要误判）"""
    text = " ".join(paragraphs)
    return any(k in text for k in GACHA_CONFIRM_KW)


def fetch_gacha_candidates(session, known_cids: set) -> tuple:
    """增量扫描活动公告，返回 (卡池候选列表, 所有扫描到的 cid 集合)

    - 首次运行(known_cids 为空)：扫描全部页，建立完整已知库
    - 日常运行：扫到某页全部 cid 已知时提前停止（新公告必在前几页）
    - 返回 all_cids 含所有扫描到的公告（含非卡池），用作下次增量停止基线
    """
    banners: List[Dict] = []
    all_cids: set = set()
    seen: set = set()
    for page in range(1, MAX_SCAN_PAGES + 1):
        log_info(f"正在获取活动公告第 {page} 页...")
        try:
            result = fetch_news_list(session, "ACTIVITY", page)
        except Exception as e:
            log_error(f"获取第 {page} 页失败: {e}")
            break
        page_cids: List[str] = []
        for item in result.get("list", []):
            cid = item.get("cid")
            if not cid:
                continue
            all_cids.add(cid)
            page_cids.append(cid)
            if cid in seen or cid in known_cids:
                continue
            seen.add(cid)
            if is_gacha_candidate(item):
                banners.append(item)
        log_info(f"  第 {page} 页 {len(result.get('list', []))} 条，累计卡池候选 {len(banners)} 条")
        # 增量停止：本页全部 cid 已知 → 后续页更旧，无需再扫
        if known_cids and page_cids and all(c in known_cids for c in page_cids):
            log_info("本页全部为已知公告，停止扫描")
            break
        if result.get("end"):
            log_info("已到末页，停止扫描")
            break
        time.sleep(SLEEP_BETWEEN)
    banners.sort(key=lambda x: x.get("displayTime", 0))
    return banners, all_cids


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
    """从公告标题与正文段落解析卡池结构化数据

    适配两种格式：
      - 独立寻访公告：★★★★★★：麒麟R夜刀（占6★出率的50%）
      - 活动附带寻访：★★★★★★（6★出率：2%）：琴柳 / 号角 / 白铁
    """
    title = item.get("title", "")
    paragraphs = detail.get("paragraphs", [])

    # 寻访名称：优先从"活动说明：活动期间【xxx】...寻访开启"提取，回退到标题
    banner_name = ""
    for p in paragraphs:
        m = re.search(r"活动期间【([^】]+)】.*?寻访开启", p)
        if m:
            banner_name = m.group(1).strip()
            break
    if not banner_name:
        m = re.match(r"\[([^\]]+)\]\s*【([^】]+)】", title)
        if m:
            banner_name = m.group(2).strip()

    # 寻访类型：优先从"◆...寻访为【xxx】"提取，回退到标题前缀
    banner_type = ""
    for p in paragraphs:
        m = re.search(r"寻访为【([^】]+)】", p)
        if m:
            banner_type = m.group(1).strip()
            break
    if not banner_type:
        m = re.match(r"\[([^\]]+)\]", title)
        if m:
            banner_type = m.group(1).strip()

    # 活动时间：定位寻访说明段，取其前面最近的"活动时间："（避免多子活动取错）
    activity_time = ""
    xf_idx = None
    for i, p in enumerate(paragraphs):
        if "寻访" in p and ("开启" in p or "出现率上升" in p or "以下干员" in p):
            xf_idx = i
            break
    if xf_idx is not None:
        for j in range(xf_idx, -1, -1):
            if paragraphs[j].startswith("活动时间："):
                activity_time = paragraphs[j][len("活动时间："):].strip()
                break
    if not activity_time:
        for p in paragraphs:
            if p.startswith("活动时间："):
                activity_time = p[len("活动时间："):].strip()
                break

    # 6★/5★ UP：★5-6 开头，排除兑换所行（兼容"★★★★★★："与"★★★★★★（...）："两种）
    star = [p for p in paragraphs if re.match(r"^★{5,6}", p) and "寻访数据契约" not in p]
    six_star = [p for p in star if p.startswith("★★★★★★")]
    five_star = [p for p in star if not p.startswith("★★★★★★")]

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
    seen_r = set()
    rules = [x for x in rules if not (x in seen_r or seen_r.add(x))]

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
        candidates, all_cids = fetch_gacha_candidates(session, known_cids)
    except Exception as e:
        log_error(f"获取卡池公告列表失败: {e}")
        notify_send("明日方舟寻访公告", f"❌ 获取公告列表失败: {e}")
        return

    log_info(f"扫描完成：本次扫描 {len(all_cids)} 条公告，新卡池候选 {len(candidates)} 个")
    # 更新已知 cid（所有扫描到的，含非卡池，用作下次增量停止基线）
    new_known = known_cids | all_cids

    # ---------- 首次运行：记录全部已知，仅推送最新一期 ----------
    if is_first_run:
        log_info("首次运行，记录当前全部公告 cid，推送最新卡池")
        if candidates:
            latest = candidates[-1]
            try:
                detail = fetch_banner_detail(session, latest["cid"])
                if BANNER_KEYWORD in latest.get("title", "") or confirm_gacha_by_detail(detail["paragraphs"]):
                    parsed = parse_banner(latest, detail)
                    report = format_banner_report(parsed)
                    print("\n" + report + "\n")
                    parts = split_message(report)
                    for i, part in enumerate(parts, 1):
                        notify_send("明日方舟 寻访公告（首次初始化）", part)
                        if i < len(parts):
                            time.sleep(2)
                    notified_cids.add(latest["cid"])
                else:
                    log_warning(f"最新候选 cid={latest.get('cid')} 详情未确认卡池数据，跳过")
            except Exception as e:
                log_error(f"处理最新卡池 {latest.get('cid')} 失败: {e}")
        state["known_cids"] = sorted(new_known)
        state["notified_cids"] = sorted(notified_cids)
        save_state(state)
        log_info("===== 首次初始化完成 =====")
        return

    # ---------- 日常运行：候选详情确认 + 推送 ----------
    if not candidates:
        log_info(f"暂无新卡池公告（已知 {len(new_known)} 个）")
        state["known_cids"] = sorted(new_known)
        save_state(state)
        return

    log_success(f"发现 {len(candidates)} 个新卡池候选，开始详情确认")
    confirmed = []  # [(item, detail), ...]
    for item in candidates:
        cid = item["cid"]
        title_hit = BANNER_KEYWORD in item.get("title", "")
        try:
            detail = fetch_banner_detail(session, cid)
            # 标题命中直接采信；仅 brief 命中的需详情确认，避免误报
            if not title_hit and not confirm_gacha_by_detail(detail["paragraphs"]):
                log_info(f"候选 cid={cid} 详情未确认卡池数据，跳过：{item.get('title', '')[:40]}")
                continue
            confirmed.append((item, detail))
        except Exception as e:
            log_error(f"获取候选 cid={cid} 详情失败: {e}")
        time.sleep(SLEEP_BETWEEN)

    if not confirmed:
        log_info("候选均未通过详情确认，无新卡池可推送")
        state["known_cids"] = sorted(new_known)
        save_state(state)
        return

    total = len(confirmed)
    log_success(f"确认 {total} 个新卡池公告，开始推送")
    success = 0
    for idx, (item, detail) in enumerate(confirmed, 1):
        cid = item["cid"]
        log_info(f"正在推送新卡池 [{idx}/{total}] cid={cid}：{item.get('title', '')[:40]}")
        try:
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

    state["known_cids"] = sorted(new_known)
    state["notified_cids"] = sorted(notified_cids)
    save_state(state)
    log_info(f"===== 任务完成：新推送 {success}/{total} =====")


if __name__ == "__main__":
    main()

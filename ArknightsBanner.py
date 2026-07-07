#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 0 10 * * *
# new Env("明日方舟寻访公告")
# 明日方舟限时寻访（卡池）公告监控
# - 扫描首页(LATEST)每个公告的详情内容，按内容判断是否卡池（不靠标题/摘要预筛）
# - 提取每期卡池数据（名称/类型/活动时间/UP干员/出率/保底/兑换所）
# - 有卡池封面图则推送图片(Telegram)+文字描述(全通道)，无图则纯文字
# - 增量扫描：首次记录全部已知 cid，日常遇已知即停；新卡池时通过 notifier 推送
#
# 逆向所得接口：
#   列表 GET https://ak.hypergryph.com/api/news?category={LATEST|ANNOUNCEMENT|ACTIVITY|NEWS}&page={n}
#     响应 {code:0, data:{list:[{cid,tab,sticky,title,author,displayTime,cover,extraCover,brief}], total, end}}
#     每页 6 条，tab: 0=公告 1=活动 2=新闻；end=true 表示末页
#   详情 GET https://ak.hypergryph.com/news/{cid}  （SSR HTML，正文 <p> 段落，含卡池封面 img）

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
from notifier import send as notify_send, send_photos as notify_send_photos

# ==================== 用户配置 ====================
SCRIPT_DIR = Path(__file__).parent
CONFIG_DIR = SCRIPT_DIR / "config"
STATE_FILE = CONFIG_DIR / ".arknights_banner_state.json"
BASE_URL = "https://ak.hypergryph.com"
LIST_API = f"{BASE_URL}/api/news"
DETAIL_URL = f"{BASE_URL}/news"
MAX_SCAN_PAGES = 50       # 扫描页数上限（每页 6 条）；LATEST 全部 + ACTIVITY 增量遇已知即停
ACTIVITY_FALLBACK_PAGES = 5  # ACTIVITY 兜底扫描页数（防 LATEST 滚动漏判）
# 内容确认关键词：正文含卡池数据特征才判定为卡池公告（不依赖标题/摘要预筛）
GACHA_CONFIRM_KW = [
    "出现率上升", "占6★出率", "占5★出率", "占4★出率",
    "6★出率", "5★出率", "4★出率", "概率提升",
]
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


def confirm_gacha_by_detail(paragraphs: List[str]) -> bool:
    """根据正文内容判断是否为卡池公告（不依赖标题/摘要，直接看详情数据）"""
    text = " ".join(paragraphs)
    return any(k in text for k in GACHA_CONFIRM_KW)


def fetch_new_news(session, known_cids: set) -> tuple:
    """扫描首页(LATEST)全部 + ACTIVITY 兜底，返回 (新 cid 公告列表, 所有扫描到的 cid 集合)

    - 不做标题/摘要预筛，返回所有新 cid 公告，由 main 抓详情后按内容判断
    - LATEST：首页全部（新公告必在此），首次扫到 end，日常也只 2 页
    - ACTIVITY：兜底前 N 页增量扫描（防 LATEST 滚动导致漏判），遇整页已知即停
    - all_cids 含所有扫描到的公告，用作下次增量停止基线
    """
    new_items: List[Dict] = []
    all_cids: set = set()
    seen: set = set()

    def collect(result, cat, page):
        page_cids = []
        for item in result.get("list", []):
            cid = item.get("cid")
            if not cid:
                continue
            all_cids.add(cid)
            page_cids.append(cid)
            if cid in seen or cid in known_cids:
                continue
            seen.add(cid)
            new_items.append(item)
        return page_cids

    # 1. 扫描首页 LATEST 全部
    for page in range(1, MAX_SCAN_PAGES + 1):
        log_info(f"正在获取首页(LATEST)第 {page} 页...")
        try:
            result = fetch_news_list(session, "LATEST", page)
        except Exception as e:
            log_error(f"获取 LATEST 第 {page} 页失败: {e}")
            break
        collect(result, "LATEST", page)
        log_info(f"  LATEST 第 {page} 页 {len(result.get('list', []))} 条")
        if result.get("end"):
            log_info("LATEST 已到末页")
            break
        time.sleep(SLEEP_BETWEEN)

    # 2. ACTIVITY 兜底（增量停止）
    for page in range(1, ACTIVITY_FALLBACK_PAGES + 1):
        log_info(f"正在获取活动(ACTIVITY)第 {page} 页...")
        try:
            result = fetch_news_list(session, "ACTIVITY", page)
        except Exception as e:
            log_error(f"获取 ACTIVITY 第 {page} 页失败: {e}")
            break
        page_cids = collect(result, "ACTIVITY", page)
        log_info(f"  ACTIVITY 第 {page} 页 {len(result.get('list', []))} 条，累计新公告 {len(new_items)} 条")
        if known_cids and page_cids and all(c in known_cids for c in page_cids):
            log_info("ACTIVITY 本页全部为已知公告，停止扫描")
            break
        if result.get("end"):
            break
        time.sleep(SLEEP_BETWEEN)

    new_items.sort(key=lambda x: x.get("displayTime", 0))
    return new_items, all_cids


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

    # 活动时间：定位寻访说明段（含"出现率上升/以下干员/活动期间...寻访"），取其前面最近的"活动时间："
    activity_time = ""
    xf_idx = None
    for i, p in enumerate(paragraphs):
        if "出现率上升" in p or "以下干员" in p or ("活动期间" in p and "寻访" in p):
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

    # 6★/5★ UP：★5-6 开头，排除兑换所行与纯干员列表（UP 段必含"出率"）
    star = [p for p in paragraphs
            if re.match(r"^★{5,6}", p) and "寻访数据契约" not in p and "出率" in p]
    six_star = [p for p in star if p.startswith("★★★★★★")]
    five_star = [p for p in star if not p.startswith("★★★★★★")]

    # 寻访数据契约兑换所
    exchange = [p for p in paragraphs
                if "寻访数据契约" in p and ("交换所" in p or re.match(r"^★{5,6}（", p) or "可兑换干员" in p)]

    # 保底与规则：只保留卡池相关 ◆ 段落（过滤时装/活动/信赖等无关内容）
    RULES_KEEP = ["寻访", "出率", "保底", "必定", "累计", "六星", "五星", "获得干员", "寻访数据契约"]
    RULES_SKIP = ["时装", "回顾展", "信赖获取", "合成玉", "入场券", "故事集收录", "关卡开放", "凭证过期"]
    rules: List[str] = []
    for p in paragraphs:
        keep = False
        if p.startswith("◆"):
            if any(k in p for k in RULES_KEEP) and not any(k in p for k in RULES_SKIP):
                keep = True
        elif "累计寻访" in p and ("必定" in p or "额外" in p):
            keep = True
        elif re.match(r"^【.+?】说明$", p):
            keep = True
        if keep:
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
        lines.append("🖼️ 卡池封面: 见推送图片")
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


# ==================== 推送与主流程 ====================
def push_banner(item: Dict, detail: Dict, idx: int = 0, total: int = 0, first_run: bool = False) -> bool:
    """解析卡池数据并推送：有封面图则推图(Telegram)+文字描述(全通道)，无图则纯文字"""
    try:
        parsed = parse_banner(item, detail)
        report = format_banner_report(parsed, idx, total)
        print("\n" + report + "\n")
        title = "明日方舟 寻访公告（首次初始化）" if first_run else "明日方舟 新卡池公告"
        if total > 1:
            title += f"（{idx}/{total}）"
        cover = parsed.get("cover")
        parts = split_message(report)
        for i, part in enumerate(parts, 1):
            t = title + (f"（{i}/{len(parts)}）" if len(parts) > 1 else "")
            # 仅第一条带图片，避免重复推图
            photos = []
            if cover and i == 1:
                cap = parsed.get("banner_name") or parsed.get("banner_type") or "卡池"
                photos = [{"image": cover, "caption": f"【{cap}】"}]
            if photos:
                notify_send_photos(t, part, photos)
            else:
                notify_send(t, part)
            if i < len(parts):
                time.sleep(2)
        return True
    except Exception as e:
        log_error(f"推送卡池 cid={item.get('cid')} 失败: {e}")
        return False


def main():
    log_info("===== 明日方舟寻访公告监控开始 =====")
    state = load_state()
    known_cids = set(state.get("known_cids", []))
    notified_cids = set(state.get("notified_cids", []))
    is_first_run = len(known_cids) == 0

    session = create_session()
    try:
        new_items, all_cids = fetch_new_news(session, known_cids)
    except Exception as e:
        log_error(f"获取公告列表失败: {e}")
        notify_send("明日方舟寻访公告", f"❌ 获取公告列表失败: {e}")
        return

    log_info(f"扫描完成：本次扫描 {len(all_cids)} 条公告，新公告 {len(new_items)} 个")
    new_known = known_cids | all_cids

    # ---------- 首次运行：记录全部已知 cid，仅推送最新一期卡池 ----------
    if is_first_run:
        log_info("首次运行，记录当前全部公告 cid，按内容定位最新卡池并推送")
        # new_items 按时间升序，从最新往前找第一个内容确认为卡池的
        for item in reversed(new_items):
            try:
                detail = fetch_banner_detail(session, item["cid"])
                if confirm_gacha_by_detail(detail["paragraphs"]):
                    if push_banner(item, detail, first_run=True):
                        notified_cids.add(item["cid"])
                    break
                log_info(f"cid={item['cid']} 非卡池，继续向前查找：{item.get('title', '')[:40]}")
            except Exception as e:
                log_error(f"处理 cid={item.get('cid')} 失败: {e}")
        state["known_cids"] = sorted(new_known)
        state["notified_cids"] = sorted(notified_cids)
        save_state(state)
        log_info("===== 首次初始化完成 =====")
        return

    if not new_items:
        log_info(f"暂无新公告（已知 {len(new_known)} 个）")
        state["known_cids"] = sorted(new_known)
        save_state(state)
        return

    # ---------- 日常：对每个新公告抓详情，按内容判断是否卡池 ----------
    log_info(f"对 {len(new_items)} 个新公告抓取详情，按内容判断是否卡池")
    confirmed = []  # [(item, detail), ...]
    for item in new_items:
        cid = item["cid"]
        try:
            detail = fetch_banner_detail(session, cid)
            if confirm_gacha_by_detail(detail["paragraphs"]):
                confirmed.append((item, detail))
                log_info(f"cid={cid} ✅ 确认为卡池：{item.get('title', '')[:40]}")
            else:
                log_info(f"cid={cid} ⏭️ 非卡池，跳过：{item.get('title', '')[:40]}")
        except Exception as e:
            log_error(f"获取 cid={cid} 详情失败: {e}")
        time.sleep(SLEEP_BETWEEN)

    if not confirmed:
        log_info("新公告均非卡池，无卡池可推送")
        state["known_cids"] = sorted(new_known)
        save_state(state)
        return

    total = len(confirmed)
    log_success(f"确认 {total} 个新卡池公告，开始推送（含图片）")
    success = 0
    for idx, (item, detail) in enumerate(confirmed, 1):
        log_info(f"正在推送新卡池 [{idx}/{total}] cid={item['cid']}：{item.get('title', '')[:40]}")
        if push_banner(item, detail, idx, total):
            notified_cids.add(item["cid"])
            success += 1
            log_success(f"卡池 cid={item['cid']} 推送完成")

    state["known_cids"] = sorted(new_known)
    state["notified_cids"] = sorted(notified_cids)
    save_state(state)
    log_info(f"===== 任务完成：新推送 {success}/{total} =====")


if __name__ == "__main__":
    main()

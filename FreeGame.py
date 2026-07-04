#!/usr/bin/env python3
# cron: 0 10 * * *
# new Env("免费游戏喜加一")
# 聚合 zui.re/game.html 的免费游戏信息
# - Epic 喜加一（本周 + 即将）
# - GamerPower 全平台免费游戏
# - Steam 免费游戏（在线人数排行）
#
# 环境变量:
#   FREEGAME_TG_SUMMARY  - 是否发送 Telegram 游戏封面图片（需 TG 通知渠道已配置）
#   FREEGAME_MIN_WORTH   - 最低价值过滤（美元），默认 0 不过滤

import os
import time
import requests
from datetime import datetime, timezone, timedelta

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send

# ==================== 用户配置 ====================
TG_SUMMARY = os.environ.get("FREEGAME_TG_SUMMARY", "false").lower() == "true"
MIN_WORTH = float(os.environ.get("FREEGAME_MIN_WORTH", "0"))

# API 端点
EPIC_API = "https://60s.viki.moe/v2/epic"
GP_API = "https://www.gamerpower.com/api/giveaways?type=game"
RAPIDAPI_EPIC = "https://game-deals-freebies-api.p.rapidapi.com/epic"
RAPIDAPI_KEY = "d0a4c2bf89mshed677307d0e7c69p14346ejsn1127eceaeb41"
STEAM_PROXY = "https://api.allorigins.win/raw?url="
STEAM_API = (
    "https://store.steampowered.com/api/storesearch/"
    "?term=&maxprice=free&category1=998&cc=us&l=english&count=50"
)

# 平台标签映射
PLATFORM_TAGS = {
    "epic": "🎮 Epic",
    "steam": "🔧 Steam",
    "gog": "📀 GOG",
    "ubisoft": "🎯 Ubisoft",
    "origin": "🔶 Origin",
    "battlenet": "🔷 Battle.net",
    "itchio": "🎲 Itch.io",
    "xbox": "🟢 Xbox",
    "playstation": "🔵 PS",
    "switch": "🔴 Switch",
    "android": "📱 Android",
    "ios": "🍎 iOS",
    "drm-free": "🔓 DRM-Free",
}

PLATFORM_ORDER = [
    "epic", "steam", "gog", "ubisoft", "origin", "battlenet",
    "itchio", "xbox", "playstation", "switch", "android", "ios",
]


# ---------- API 请求 ----------
def fetch_epic_viki():
    """60s.viki.moe — Epic 喜加一（中文价格）"""
    try:
        resp = requests.get(EPIC_API, timeout=15)
        data = resp.json()
        if data.get("code") == 200:
            return data.get("data", [])
        log_error(f"Epic viki API 返回异常: {data.get('message', '')}")
        return []
    except Exception as e:
        log_error(f"Epic viki API 请求失败: {e}")
        return []


def fetch_gamerpower():
    """GamerPower — 全平台免费游戏"""
    try:
        resp = requests.get(GP_API, timeout=15)
        return resp.json()
    except Exception as e:
        log_error(f"GamerPower API 请求失败: {e}")
        return []


def fetch_epic_rapidapi():
    """RapidAPI — Epic 备选源"""
    try:
        headers = {
            "x-rapidapi-host": "game-deals-freebies-api.p.rapidapi.com",
            "x-rapidapi-key": RAPIDAPI_KEY,
        }
        resp = requests.get(RAPIDAPI_EPIC, headers=headers, timeout=15)
        return resp.json()
    except Exception as e:
        log_error(f"RapidAPI Epic 请求失败: {e}")
        return {}


def fetch_steam_free():
    """Steam 免费游戏（通过 allorigins 代理）"""
    try:
        url = STEAM_PROXY + requests.utils.quote(STEAM_API, safe="")
        resp = requests.get(url, timeout=20)
        data = resp.json()
        return data.get("items", [])
    except Exception as e:
        log_error(f"Steam API 请求失败: {e}")
        return []


# ---------- 数据解析 ----------
def parse_platforms(platforms_str):
    """将 GamerPower 的平台字符串解析为标准 key 列表"""
    if not platforms_str:
        return []
    raw = platforms_str.lower()
    keys = []
    for key in PLATFORM_ORDER:
        if key in raw:
            keys.append(key)
    # 检查未在 ORDER 里的
    if "pc" in raw and not keys:
        pass  # PC 通用，不单独标记
    if "drm-free" in raw:
        keys.append("drm-free")
    return keys


def parse_worth(worth_str):
    """解析价值字符串为浮点数"""
    if not worth_str:
        return 0.0
    try:
        return float(worth_str.replace("$", "").replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def bj_time_from_ms(ts_ms):
    """将毫秒时间戳转为北京时间字符串"""
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def bj_time_from_iso(iso_str):
    """将 ISO 时间字符串转为北京时间字符串"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        bj = dt.astimezone(timezone(timedelta(hours=8)))
        return bj.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str[:16] if iso_str else ""


# ---------- 去重 & 合并 ----------
def merge_games(epic_data, gp_data, epic_rapid):
    """合并多个源的去重游戏列表"""
    seen = set()
    games = []

    # 1. Epic viki 数据
    for g in epic_data:
        key = g.get("title", "")
        if key and key not in seen:
            seen.add(key)
            is_free_now = g.get("is_free_now", True)
            games.append({
                "title": g.get("title", ""),
                "platforms": ["epic"],
                "worth": g.get("original_price", 0),
                "worth_str": g.get("original_price_desc", ""),
                "description": (g.get("description", "") or "")[:80],
                "free_start": bj_time_from_ms(g.get("free_start_at", 0)) if is_free_now else "即将开始",
                "free_end": bj_time_from_ms(g.get("free_end_at", 0)),
                "status": "🆓 本周免费" if is_free_now else "⏳ 即将免费",
                "link": g.get("link", ""),
                "source": "Epic",
            })

    # 2. GamerPower 数据
    for g in gp_data:
        title = g.get("title", "")
        # 简单去重 key
        key = title.lower().split("(")[0].strip()
        if key and key not in seen:
            worth = parse_worth(g.get("worth", "0"))
            if worth < MIN_WORTH:
                continue
            seen.add(key)
            platforms = parse_platforms(g.get("platforms", ""))
            end_date = g.get("end_date", "N/A")
            if end_date and end_date != "N/A":
                try:
                    dt = datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
                    end_date = dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            games.append({
                "title": title,
                "platforms": platforms,
                "worth": worth,
                "worth_str": g.get("worth", ""),
                "description": (g.get("description", "") or "")[:100],
                "free_start": "未知",
                "free_end": end_date or "N/A",
                "status": "🆓 限免中" if g.get("status") == "Active" else "⏳",
                "link": g.get("open_giveaway_url", g.get("gamerpower_url", "")),
                "source": "GamerPower",
            })

    # 3. RapidAPI Epic 数据（仅补漏）
    if epic_rapid:
        for section in ["active", "upcoming"]:
            for g in epic_rapid.get(section, []):
                title = g.get("title", "")
                if title and title not in seen:
                    seen.add(title)
                    is_active = section == "active"
                    games.append({
                        "title": title,
                        "platforms": ["epic"],
                        "worth": parse_worth(g.get("originalPrice", "0")),
                        "worth_str": g.get("originalPrice", ""),
                        "description": (g.get("description", "") or "")[:80],
                        "free_start": bj_time_from_iso(g.get("startDate", "")),
                        "free_end": bj_time_from_iso(g.get("endDate", "")),
                        "status": "🆓 本周免费" if is_active else "⏳ 即将免费",
                        "link": g.get("storeUrl", ""),
                        "source": "Epic(R)",
                    })

    return games


def parse_steam_games(items):
    """整理 Steam 免费游戏（按玩家数排序，取 Top 10）"""
    result = []
    for g in items[:10]:
        name = g.get("name", "")
        app_id = g.get("id", "")
        result.append({
            "title": name,
            "app_id": app_id,
            "link": f"https://store.steampowered.com/app/{app_id}",
            "platform": "steam",
        })
    return result


# ---------- 报告构建 ----------
def build_game_row(g, index):
    """构建单行游戏信息"""
    platforms_str = " ".join(
        PLATFORM_TAGS.get(p, f"[{p}]") for p in g.get("platforms", [])
    )
    worth = g.get("worth_str", "")
    price_tag = f" 💰{worth}" if worth else ""
    status = g.get("status", "")

    lines = [
        f"{index}. {status} {g['title']}",
        f"   平台: {platforms_str}{price_tag}",
    ]

    desc = g.get("description", "")
    if desc:
        lines.append(f"   简介: {desc}")

    end = g.get("free_end", "")
    if end and end != "N/A":
        lines.append(f"   截止: {end}")

    link = g.get("link", "")
    if link:
        lines.append(f"   链接: {link}")

    return "\n".join(lines)


def build_report(games, steam_games):
    """构建完整报告"""
    now = beijing_time_str()

    # 分类
    active_games = [g for g in games if "本周" in g.get("status", "") or "限免" in g.get("status", "")]
    upcoming_games = [g for g in games if "即将" in g.get("status", "")]

    lines = [
        "🎮 免费游戏喜加一",
        f"🕒 {now}",
        "",
        "━" * 36,
        f"🔥 当前可领 ({len(active_games)} 款)",
        "━" * 36,
    ]

    for i, g in enumerate(active_games, 1):
        lines.append(build_game_row(g, i))
        lines.append("")

    if upcoming_games:
        lines.append("━" * 36)
        lines.append(f"⏳ 即将免费 ({len(upcoming_games)} 款)")
        lines.append("━" * 36)

        for i, g in enumerate(upcoming_games, 1):
            # 简化即将免费的信息
            platforms_str = " ".join(
                PLATFORM_TAGS.get(p, f"[{p}]") for p in g.get("platforms", [])
            )
            worth = g.get("worth_str", "")
            price_tag = f" 💰{worth}" if worth else ""
            start = g.get("free_start", "")
            lines.append(
                f"{i}. {g['title']}\n"
                f"   平台: {platforms_str}{price_tag}\n"
                f"   开始: {start}"
            )
            lines.append("")

    if steam_games:
        lines.append("━" * 36)
        lines.append(f"🔧 Steam 热门免费游戏 (Top {len(steam_games)})")
        lines.append("━" * 36)
        for i, g in enumerate(steam_games, 1):
            lines.append(f"{i}. {g['title']}\n   链接: {g['link']}")

    lines.append("")
    lines.append("━" * 36)
    lines.append("📊 数据来源: zui.re")
    lines.append("   Epic: 60s.viki.moe + RapidAPI")
    lines.append("   全平台: GamerPower.com")
    lines.append("   Steam: store.steampowered.com")

    return "\n".join(lines)


def build_summary(games):
    """构建简短汇总（用于 TG 等渠道快速浏览）"""
    active = [g for g in games if "本周" in g.get("status", "") or "限免" in g.get("status", "")]
    lines = [f"🎮 本周可领免费游戏 ({len(active)} 款)", ""]
    for g in active:
        platforms_str = " ".join(
            PLATFORM_TAGS.get(p, f"[{p}]") for p in g.get("platforms", [])
        )
        worth = g.get("worth_str", "")
        price_tag = f" (原价 {worth})" if worth else ""
        lines.append(f"• {g['title']}")
        lines.append(f"  {platforms_str}{price_tag}")
        end = g.get("free_end", "")
        if end and end != "N/A":
            lines.append(f"  截止: {end}")
    return "\n".join(lines)


# ---------- 主流程 ----------
def main():
    log_info("===== 免费游戏喜加一开始 =====")

    # 1. 请求所有数据源
    log_info("正在获取 Epic (viki)...")
    epic_data = fetch_epic_viki()
    log_success(f"Epic viki: {len(epic_data)} 条")

    log_info("正在获取 GamerPower...")
    gp_data = fetch_gamerpower()
    log_success(f"GamerPower: {len(gp_data)} 条")

    log_info("正在获取 Epic (RapidAPI)...")
    epic_rapid = fetch_epic_rapidapi()

    log_info("正在获取 Steam 免费游戏...")
    steam_items = fetch_steam_free()
    steam_games = parse_steam_games(steam_items)
    log_success(f"Steam: {len(steam_games)} 条")

    # 2. 合并去重
    games = merge_games(epic_data, gp_data, epic_rapid)
    log_success(f"去重后共 {len(games)} 款游戏")

    # 3. 构建报告
    report = build_report(games, steam_games)
    summary = build_summary(games)

    # 4. 打印日志
    log_info("")
    for line in report.split("\n"):
        if line.startswith("━") or line.startswith("🔥") or line.startswith("⏳") or line.startswith("🔧"):
            log_info(line)
        elif line.startswith(("1.", "2.", "3.", "4.", "5.")):
            log_info(line)
        elif "本周" in line or "限免" in line:
            log_success(line)

    # 5. 发送通知
    notify_send("🎮 免费游戏喜加一", summary)

    return report


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# cron: 0 8 * * 6
# new Env("Epic免费游戏")
# Epic Games 每周免费游戏提醒
# 直接调用 Epic Store GraphQL API，无第三方依赖
#
# 环境变量:
#   EPIC_LOCALE    - 语言地区，默认 zh-CN
#   EPIC_COUNTRY   - 国家代码，默认 CN

import os
import requests
from datetime import datetime, timezone, timedelta

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send

# ==================== 配置 ====================
LOCALE = os.environ.get("EPIC_LOCALE", "zh-CN")
COUNTRY = os.environ.get("EPIC_COUNTRY", "CN")
EPIC_URL = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"

# 北京时间时区
_TZ_BJ = timezone(timedelta(hours=8))


# ---------- API ----------
def fetch_epic_data():
    """请求 Epic 免费游戏数据"""
    params = {
        "locale": LOCALE,
        "country": COUNTRY,
        "allowCountries": COUNTRY,
    }
    try:
        resp = requests.get(EPIC_URL, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log_error(f"Epic API 请求失败: {e}")
        return None


# ---------- 解析 ----------
def bj_time(iso_str: str) -> str:
    """ISO UTC 时间 → 北京时间字符串 (月-日 时:分)"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        bj = dt.astimezone(_TZ_BJ)
        return bj.strftime("%m-%d %H:%M")
    except Exception:
        return iso_str


def bj_datetime(iso_str: str) -> datetime:
    """ISO UTC 时间 → 北京时间 datetime"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(_TZ_BJ)
    except Exception:
        return datetime.max.replace(tzinfo=_TZ_BJ)


def find_image(images, preferred_types=("OfferImageTall", "Thumbnail", "DieselStoreFrontWide")):
    """从 keyImages 数组中找到最佳封面图"""
    for img_type in preferred_types:
        for img in images:
            if img.get("type") == img_type:
                return img.get("url", "")
    # fallback: 第一张
    return images[0].get("url", "") if images else ""


def extract_game_price_price(elem):
    """提取价格信息"""
    price_info = elem.get("price", {}).get("totalPrice", {})
    fmt = price_info.get("fmtPrice", {})
    return {
        "original": fmt.get("originalPrice", "N/A"),
        "discount": fmt.get("discountPrice", "N/A"),
        "currency": price_info.get("currencyCode", ""),
    }


def extract_promotion_dates(promotional_offers):
    """从 promotionalOffers 提取起止时间"""
    dates = []
    for block in promotional_offers:
        for offer in block.get("promotionalOffers", []):
            start = offer.get("startDate", "")
            end = offer.get("endDate", "")
            if start and end:
                dates.append((bj_time(start), bj_time(end), bj_datetime(end)))
    return dates


def parse_free_games(data):
    """
    解析免费游戏，返回:
        current: list of dict  — 本周可领
        upcoming: list of dict — 即将免费
    """
    if not data:
        return [], []

    elements = (
        data.get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
    )

    current = []
    upcoming = []

    for elem in elements:
        # 跳过非基础游戏 / 已下架
        if elem.get("offerType") != "BASE_GAME":
            continue
        if elem.get("status") != "ACTIVE":
            continue

        title = elem.get("title", "未知游戏")
        description = elem.get("description", "")
        url_slug = elem.get("urlSlug") or elem.get("productSlug", "").replace("/home", "")
        store_url = f"https://store.epicgames.com/zh-CN/p/{url_slug}" if url_slug else ""

        # 构建基础对象
        game = {
            "title": title,
            "desc": description[:120] if description else "",
            "url": store_url,
            "image": find_image(elem.get("keyImages", [])),
            "seller": elem.get("seller", {}).get("name", ""),
            "price": extract_game_price_price(elem),
        }

        promotions = elem.get("promotions")
        if not promotions:
            continue

        # 判断是否本周免费
        promo_offers = promotions.get("promotionalOffers", [])
        if promo_offers:
            dates = extract_promotion_dates(promo_offers)
            if dates:
                game["free_start"] = dates[0][0]
                game["free_end"] = dates[0][1]
                game["free_end_dt"] = dates[0][2]
                current.append(game)

        # 判断是否即将免费
        upcoming_offers = promotions.get("upcomingPromotionalOffers", [])
        if upcoming_offers:
            dates = extract_promotion_dates(upcoming_offers)
            if dates:
                # 跳过已经在本周列表里的
                if not promo_offers:
                    game["free_start"] = dates[0][0]
                    game["free_end"] = dates[0][1]
                    game["free_end_dt"] = dates[0][2]
                    upcoming.append(game)

    # 去重
    seen = set()
    current_dedup = []
    for g in current:
        if g["title"] not in seen:
            seen.add(g["title"])
            current_dedup.append(g)

    # 即将列表排除已在本周出现的
    upcoming_dedup = []
    for g in upcoming:
        if g["title"] not in seen:
            seen.add(g["title"])
            upcoming_dedup.append(g)

    return current_dedup, upcoming_dedup


# ---------- 报告 ----------
def build_report(current, upcoming):
    """构建通知文本"""
    now_str = beijing_time_str()
    lines = [f"🎮 Epic 每周免费游戏", f"🕒 {now_str}", ""]

    # 本周可领
    if current:
        lines.append("━" * 32)
        lines.append(f"🆓 本周免费 ({len(current)} 款)")
        lines.append("━" * 32)
        for i, g in enumerate(current, 1):
            price = g["price"]
            original = price["original"]
            lines.append(f"{i}. {g['title']}")
            if g["desc"]:
                lines.append(f"   📝 {g['desc']}")
            lines.append(f"   💰 原价 {original} → 免费")
            lines.append(f"   📅 {g['free_start']} ~ {g['free_end']}")
            if g["url"]:
                lines.append(f"   🔗 {g['url']}")
            lines.append("")
    else:
        lines.append("⚠️ 本周暂无免费游戏")

    # 即将免费
    if upcoming:
        lines.append("━" * 32)
        lines.append(f"⏳ 即将免费 ({len(upcoming)} 款)")
        lines.append("━" * 32)
        for i, g in enumerate(upcoming, 1):
            price = g["price"]
            original = price["original"]
            lines.append(f"{i}. {g['title']}")
            lines.append(f"   💰 原价 {original} → 免费")
            lines.append(f"   📅 {g['free_start']} ~ {g['free_end']}")
            if g["url"]:
                lines.append(f"   🔗 {g['url']}")
            lines.append("")

    lines.append("━" * 32)
    lines.append("📡 数据来源: Epic Games Store")
    lines.append("🔗 领取地址: store.epicgames.com/zh-CN/free-games")

    return "\n".join(lines)


def build_summary(current, upcoming):
    """简短汇总（用于通知摘要）"""
    lines = []
    if current:
        lines.append(f"🆓 本周免费 ({len(current)} 款):")
        for g in current:
            original = g["price"]["original"]
            lines.append(f"  • {g['title']} (原价 {original})")
        lines.append("")
    if upcoming:
        lines.append(f"⏳ 即将免费 ({len(upcoming)} 款):")
        for g in upcoming:
            original = g["price"]["original"]
            lines.append(f"  • {g['title']} (原价 {original}) — {g['free_start']} 起")
    return "\n".join(lines)


# ---------- 主流程 ----------
def main():
    log_info("===== Epic 免费游戏检查 =====")

    # 1. 请求
    log_info("正在请求 Epic Store API...")
    data = fetch_epic_data()
    if not data:
        log_error("无法获取 Epic 数据")
        return

    # 2. 解析
    current, upcoming = parse_free_games(data)
    log_success(
        f"解析完成: 本周 {len(current)} 款, 即将 {len(upcoming)} 款"
    )

    # 3. 打印日志
    for g in current:
        log_success(f"[本周] {g['title']} — 截止 {g['free_end']}")
    for g in upcoming:
        log_info(f"[即将] {g['title']} — {g['free_start']} ~ {g['free_end']}")

    if not current and not upcoming:
        log_warning("无免费游戏信息")
        return

    # 4. 构建 & 发送通知
    report = build_report(current, upcoming)
    summary = build_summary(current, upcoming)

    print()
    print(report)

    notify_send("🎮 Epic 免费游戏", summary)

    return report


if __name__ == "__main__":
    main()

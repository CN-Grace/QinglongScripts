#!/usr/bin/env python3
# cron: 0 8 * * 6
# new Env("Epic免费游戏")
# Epic Games 每周免费游戏提醒
# - 直接调用 Epic Store GraphQL API
# - 生成游戏封面拼图 → Telegram 图片推送
# - 文字报告推送至全部通知渠道
#
# 环境变量:
#   EPIC_LOCALE    - 语言地区，默认 zh-CN
#   EPIC_COUNTRY   - 国家代码，默认 CN

import os
import re
import tempfile
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from PIL import Image as PILImage, ImageDraw, ImageFont

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send, send_photos as notify_send_photos

# ==================== 配置 ====================
LOCALE = os.environ.get("EPIC_LOCALE", "zh-CN")
COUNTRY = os.environ.get("EPIC_COUNTRY", "CN")
EPIC_URL = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"

CONFIG_DIR = Path(__file__).parent / "config"
FONT_URL = "https://github.com/anthonyfok/fonts-wqy-microhei/raw/master/wqy-microhei.ttc"
FONT_FILE = CONFIG_DIR / "font.ttf"

_TZ_BJ = timezone(timedelta(hours=8))


# ---------- 字体 ----------
def ensure_font() -> str:
    """确保中文字体存在"""
    if FONT_FILE.exists():
        return str(FONT_FILE)
    log_info("字体文件不存在，开始下载...")
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        resp = requests.get(FONT_URL, timeout=30)
        resp.raise_for_status()
        FONT_FILE.write_bytes(resp.content)
        log_success(f"字体下载完成: {FONT_FILE}")
        return str(FONT_FILE)
    except Exception as e:
        log_error(f"字体下载失败: {e}")
        return None


# ---------- API ----------
def fetch_epic_data():
    params = {"locale": LOCALE, "country": COUNTRY, "allowCountries": COUNTRY}
    try:
        resp = requests.get(EPIC_URL, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log_error(f"Epic API 请求失败: {e}")
        return None


# ---------- 解析 ----------
def bj_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(_TZ_BJ).strftime("%m-%d %H:%M")
    except Exception:
        return iso_str


def bj_datetime(iso_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(_TZ_BJ)
    except Exception:
        return datetime.max.replace(tzinfo=_TZ_BJ)


def find_image(images):
    """从 keyImages 中找最佳封面 (优先 DieselStoreFrontWide → OfferImageTall → Thumbnail)"""
    for t in ("DieselStoreFrontWide", "OfferImageTall", "Thumbnail", "OfferImageWide", "featuredMedia"):
        for img in images:
            if img.get("type") == t:
                return img.get("url", "")
    return images[0].get("url", "") if images else ""


def extract_price(elem):
    info = elem.get("price", {}).get("totalPrice", {})
    fmt = info.get("fmtPrice", {})
    return {
        "original": fmt.get("originalPrice", "N/A"),
        "discount": fmt.get("discountPrice", "N/A"),
        "currency": info.get("currencyCode", ""),
    }


def parse_discount_percentage(discount_setting) -> int | None:
    if not discount_setting:
        return None
    if isinstance(discount_setting, dict):
        pct = discount_setting.get("discountPercentage")
        return int(pct) if pct is not None else None
    if isinstance(discount_setting, str):
        m = re.search(r"discountPercentage=(\d+)", discount_setting)
        if m:
            return int(m.group(1))
    return None


def is_free_offer(offer: dict) -> bool:
    pct = parse_discount_percentage(offer.get("discountSetting", ""))
    return pct == 0


def extract_free_dates(promotional_offers):
    dates = []
    for block in promotional_offers:
        for offer in block.get("promotionalOffers", []):
            if not is_free_offer(offer):
                continue
            start = offer.get("startDate", "")
            end = offer.get("endDate", "")
            if start and end:
                dates.append((bj_time(start), bj_time(end), bj_datetime(end)))
    dates.sort(key=lambda x: x[1])
    return dates


def parse_free_games(data):
    if not data:
        return [], []

    elements = (
        data.get("data", {})
        .get("Catalog", {})
        .get("searchStore", {})
        .get("elements", [])
    )

    current, upcoming = [], []

    for elem in elements:
        if elem.get("offerType") != "BASE_GAME" or elem.get("status") != "ACTIVE":
            continue

        title = elem.get("title", "未知游戏")
        url_slug = elem.get("urlSlug") or elem.get("productSlug", "").replace("/home", "")
        store_url = f"https://store.epicgames.com/zh-CN/p/{url_slug}" if url_slug else ""

        game = {
            "title": title,
            "desc": (elem.get("description", "") or "")[:120],
            "url": store_url,
            "image": find_image(elem.get("keyImages", [])),
            "seller": elem.get("seller", {}).get("name", ""),
            "price": extract_price(elem),
        }

        promotions = elem.get("promotions")
        if not promotions:
            continue

        # 本周免费
        promo_offers = promotions.get("promotionalOffers", [])
        if promo_offers:
            price_info = elem.get("price", {}).get("totalPrice", {})
            if price_info.get("discountPrice", -1) == 0:
                dates = extract_free_dates(promo_offers)
                if dates:
                    game["free_start"] = dates[0][0]
                    game["free_end"] = dates[0][1]
                    game["free_end_dt"] = dates[0][2]
                    current.append(game)

        # 即将免费
        upcoming_offers = promotions.get("upcomingPromotionalOffers", [])
        if upcoming_offers:
            dates = extract_free_dates(upcoming_offers)
            if dates and game["title"] not in {g["title"] for g in current}:
                game["free_start"] = dates[0][0]
                game["free_end"] = dates[0][1]
                game["free_end_dt"] = dates[0][2]
                upcoming.append(game)

    return current, upcoming


# ---------- 图片处理 ----------
def download_image(url: str, timeout: int = 15) -> str:
    """下载图片到临时文件"""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        suffix = ".png" if "png" in resp.headers.get("content-type", "") else ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(resp.content)
            return f.name
    except Exception as e:
        log_error(f"下载图片失败 [{url[:60]}...]: {e}")
        return None


def build_card(games, label, font_path):
    """
    构建 N 款游戏的横向卡片图。
    - 1 款: 单张封面 + 信息
    - 多款: 封面并排 + 下方信息列表
    返回图片临时文件路径
    """
    if not games:
        return None

    try:
        font_title = ImageFont.truetype(font_path, 28)
        font_info = ImageFont.truetype(font_path, 20)
        font_small = ImageFont.truetype(font_path, 16)
    except Exception:
        font_title = font_info = font_small = ImageFont.load_default()

    # 下载所有封面
    cover_paths = []
    for g in games:
        path = download_image(g["image"])
        if path:
            cover_paths.append((g, path))
        else:
            cover_paths.append((g, None))

    COVER_W, COVER_H = 300, 400  # 单张封面缩放尺寸
    PAD = 20
    HEADER_H = 60
    INFO_H = 140  # 下方信息区高度

    n = len(games)
    total_w = PAD * 2 + COVER_W * n + max(0, (n - 1) * 10)
    total_h = HEADER_H + COVER_H + INFO_H + PAD

    canvas = PILImage.new("RGB", (total_w, total_h), color=(18, 18, 24))
    draw = ImageDraw.Draw(canvas)

    # 顶部标签栏
    bar_colors = {"current": (0, 116, 204), "upcoming": (255, 153, 0)}
    bar_color = bar_colors.get(label, (100, 100, 100))
    draw.rectangle([(0, 0), (total_w, HEADER_H)], fill=bar_color)
    label_text = "🆓 本周免费" if label == "current" else "⏳ 即将免费"
    # 文字垂直居中
    bbox = draw.textbbox((0, 0), label_text, font=font_title)
    text_h = bbox[3] - bbox[1]
    draw.text((PAD, (HEADER_H - text_h) // 2), label_text, fill="white", font=font_title)

    for i, (game, cover_path) in enumerate(cover_paths):
        x = PAD + i * (COVER_W + 10)

        # 封面图片
        if cover_path:
            try:
                cover = PILImage.open(cover_path)
                cover = cover.resize((COVER_W, COVER_H), PILImage.LANCZOS if hasattr(PILImage, "LANCZOS") else PILImage.Resampling.LANCZOS)
                canvas.paste(cover, (x, HEADER_H))
            except Exception:
                draw.rectangle([(x, HEADER_H), (x + COVER_W, HEADER_H + COVER_H)], fill=(40, 40, 50))
        else:
            draw.rectangle([(x, HEADER_H), (x + COVER_W, HEADER_H + COVER_H)], fill=(40, 40, 50))
            draw.text((x + 10, HEADER_H + COVER_H // 2), "无封面", fill=(150, 150, 150), font=font_info)

        # 下方信息
        info_y = HEADER_H + COVER_H + 10
        draw.text((x, info_y), game["title"][:20], fill=(255, 255, 255), font=font_info)

        price = game["price"]
        original = price["original"]
        if label == "current":
            date_line = f"{game.get('free_end', '')} 截止"
        else:
            date_line = f"{game.get('free_start', '')} 起"

        draw.text((x, info_y + 30), f"原价 {original} → 免费", fill=(0, 200, 100), font=font_small)
        draw.text((x, info_y + 55), date_line, fill=(180, 180, 180), font=font_small)

    # 底部水印
    draw.text((PAD, total_h - 22), "Epic Games Store · store.epicgames.com", fill=(100, 100, 100), font=font_small)

    # 保存
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        canvas.save(f, quality=90)
        result_path = f.name

    # 清理临时文件
    for _, cp in cover_paths:
        if cp and os.path.exists(cp):
            os.remove(cp)

    return result_path


# ---------- 报告 ----------
def build_summary(current, upcoming):
    lines = []
    if current:
        lines.append(f"🆓 本周免费 ({len(current)} 款):")
        for g in current:
            lines.append(f"  • {g['title']} (原价 {g['price']['original']}) — {g.get('free_end', '')} 截止")
        lines.append("")
    if upcoming:
        lines.append(f"⏳ 即将免费 ({len(upcoming)} 款):")
        for g in upcoming:
            lines.append(f"  • {g['title']} (原价 {g['price']['original']}) — {g.get('free_start', '')} 起")
    if not current and not upcoming:
        lines.append("⚠️ 本周暂无免费游戏")
    lines.append("")
    lines.append("🔗 领取: store.epicgames.com/zh-CN/free-games")
    return "\n".join(lines)


# ---------- 主流程 ----------
def main():
    log_info("===== Epic 免费游戏检查 =====")

    font_path = ensure_font()

    # 1. 请求
    log_info("正在请求 Epic Store API...")
    data = fetch_epic_data()
    if not data:
        log_error("无法获取 Epic 数据")
        return

    # 2. 解析
    current, upcoming = parse_free_games(data)
    log_success(f"解析完成: 本周 {len(current)} 款, 即将 {len(upcoming)} 款")

    for g in current:
        log_success(f"[本周] {g['title']} — 截止 {g['free_end']}")
    for g in upcoming:
        log_info(f"[即将] {g['title']} — {g['free_start']} ~ {g['free_end']}")

    if not current and not upcoming:
        log_warning("无免费游戏信息")
        return

    # 3. 文字报告 → 全渠道
    summary = build_summary(current, upcoming)
    print("\n" + summary)

    # 4. 图片报告 → Telegram
    if font_path:
        photos = []

        # 本周免费卡片
        img_current = build_card(current, "current", font_path)
        if img_current:
            game_names = " · ".join(g["title"] for g in current)
            photos.append({"image": img_current, "caption": f"🆓 本周免费: {game_names}"})

        # 即将免费卡片
        img_upcoming = build_card(upcoming, "upcoming", font_path)
        if img_upcoming:
            game_names = " · ".join(g["title"] for g in upcoming)
            photos.append({"image": img_upcoming, "caption": f"⏳ 即将免费: {game_names}"})

        if photos:
            # 发文本到所有渠道 + 图片到 Telegram
            notify_send_photos("🎮 Epic 免费游戏", summary, photos)

            # 清理临时文件
            for p in photos:
                if os.path.exists(p["image"]):
                    os.remove(p["image"])
            log_success("图片推送完成")
        else:
            notify_send("🎮 Epic 免费游戏", summary)
            log_warning("图片生成失败，已发送文字报告")
    else:
        notify_send("🎮 Epic 免费游戏", summary)

    log_info("===== 任务完成 =====")
    return summary


if __name__ == "__main__":
    main()

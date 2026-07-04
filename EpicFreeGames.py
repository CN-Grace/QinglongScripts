#!/usr/bin/env python3
# cron: 0 8 * * 6
# new Env("Epic免费游戏")
# Epic Games 每周免费游戏提醒
# - 直接调用 Epic Store GraphQL API
# - 生成一张合成图片推送到 Telegram
# - 图片失败时降级为文字推送
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
from notifier import send as notify_send

# ==================== 配置 ====================
LOCALE = os.environ.get("EPIC_LOCALE", "zh-CN")
COUNTRY = os.environ.get("EPIC_COUNTRY", "CN")
EPIC_URL = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"

CONFIG_DIR = Path(__file__).parent / "config"
FONT_URL = "https://github.com/anthonyfok/fonts-wqy-microhei/raw/master/wqy-microhei.ttc"
FONT_FILE = CONFIG_DIR / "font.ttf"

_TZ_BJ = timezone(timedelta(hours=8))

# 设计参数
COVER_W, COVER_H = 320, 450
PAD = 24
BAR_H = 56
ROW_INFO_H = 90
LAYOUT_RESAMPLE = PILImage.LANCZOS if hasattr(PILImage, "LANCZOS") else PILImage.Resampling.LANCZOS
DARK_BG = (22, 22, 28)
CARD_BG = (32, 32, 40)
TEXT_PRIMARY = (255, 255, 255)
TEXT_MUTED = (150, 150, 158)
ACCENT_CURRENT = (70, 130, 220)
ACCENT_UPCOMING = (220, 160, 60)
ACCENT_FREE = (80, 200, 120)


# ---------- 字体 ----------
def ensure_font():
    if FONT_FILE.exists():
        return str(FONT_FILE)
    log_info("下载中文字体...")
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        resp = requests.get(FONT_URL, timeout=30)
        resp.raise_for_status()
        FONT_FILE.write_bytes(resp.content)
        log_success(f"字体已下载: {FONT_FILE}")
        return str(FONT_FILE)
    except Exception as e:
        log_error(f"字体下载失败: {e}")
        return None


def get_fonts(font_path):
    try:
        return (
            ImageFont.truetype(font_path, 30),
            ImageFont.truetype(font_path, 20),
            ImageFont.truetype(font_path, 16),
        )
    except Exception:
        df = ImageFont.load_default()
        return df, df, df


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
def bj_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(_TZ_BJ).strftime("%m-%d %H:%M")
    except Exception:
        return iso_str


def find_image(images):
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
        "currency": info.get("currencyCode", ""),
    }


def parse_discount_percentage(discount_setting):
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


def is_free_offer(offer):
    return parse_discount_percentage(offer.get("discountSetting", "")) == 0


def extract_free_dates(promotional_offers):
    dates = []
    for block in promotional_offers:
        for offer in block.get("promotionalOffers", []):
            if not is_free_offer(offer):
                continue
            start = offer.get("startDate", "")
            end = offer.get("endDate", "")
            if start and end:
                dates.append((bj_time(start), bj_time(end)))
    dates.sort(key=lambda x: x[1])
    return dates


def parse_free_games(data):
    if not data:
        return [], []

    elements = (
        data.get("data", {}).get("Catalog", {})
        .get("searchStore", {}).get("elements", [])
    )

    current, upcoming = [], []

    for elem in elements:
        if elem.get("offerType") != "BASE_GAME" or elem.get("status") != "ACTIVE":
            continue

        title = elem.get("title", "Unknown")
        url_slug = elem.get("urlSlug") or elem.get("productSlug", "").replace("/home", "")
        store_url = f"https://store.epicgames.com/zh-CN/p/{url_slug}" if url_slug else ""

        game = {
            "title": title,
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
            dp = elem.get("price", {}).get("totalPrice", {}).get("discountPrice", -1)
            if dp == 0:
                dates = extract_free_dates(promo_offers)
                if dates:
                    game["free_start"] = dates[0][0]
                    game["free_end"] = dates[0][1]
                    current.append(game)

        # 即将免费
        upcoming_offers = promotions.get("upcomingPromotionalOffers", [])
        if upcoming_offers:
            dates = extract_free_dates(upcoming_offers)
            if dates and game["title"] not in {g["title"] for g in current}:
                game["free_start"] = dates[0][0]
                game["free_end"] = dates[0][1]
                upcoming.append(game)

    return current, upcoming


# ---------- 图片构建 ----------
def download_image(url, timeout=15):
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        suffix = ".png" if "png" in resp.headers.get("content-type", "") else ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(resp.content)
            return f.name
    except Exception as e:
        log_error(f"下载封面失败 [{url[:60]}...]: {e}")
        return None


def draw_section_header(draw, y, width, text, accent_color, font):
    """绘制分区标题栏"""
    draw.rectangle([(0, y), (width, y + BAR_H)], fill=accent_color)
    bbox = draw.textbbox((0, 0), text, font=font)
    th = bbox[3] - bbox[1]
    draw.text((PAD, y + (BAR_H - th) // 2), text, fill="white", font=font)
    return y + BAR_H


def draw_game_row(draw, y, game, font_title, font_info, font_small, canvas_width):
    """绘制单款游戏的卡片行：左侧封面 + 右侧信息"""
    # 下载封面
    cover_path = download_image(game["image"])
    card_h = COVER_H + PAD * 2

    # 卡片背景
    draw.rectangle([(PAD, y), (canvas_width - PAD, y + card_h)], fill=CARD_BG)

    row_x = PAD + 10
    row_y = y + 10

    # 封面
    if cover_path:
        try:
            cover = PILImage.open(cover_path)
            cover = cover.resize((COVER_W, COVER_H), LAYOUT_RESAMPLE)
            # paste 到画布需要合并，这里用简单的 paste
            # 把 cover 贴在对应位置
            draw._image.paste(cover, (row_x, row_y))
        except Exception:
            draw.rectangle([(row_x, row_y), (row_x + COVER_W, row_y + COVER_H)], fill=(50, 50, 60))
            draw.text((row_x + COVER_W // 2 - 30, row_y + COVER_H // 2), "no cover", fill=TEXT_MUTED, font=font_info)
    else:
        draw.rectangle([(row_x, row_y), (row_x + COVER_W, row_y + COVER_H)], fill=(50, 50, 60))
        draw.text((row_x + COVER_W // 2 - 30, row_y + COVER_H // 2), "no cover", fill=TEXT_MUTED, font=font_info)

    if cover_path and os.path.exists(cover_path):
        os.remove(cover_path)

    # 右侧信息
    info_x = row_x + COVER_W + 20
    info_w = canvas_width - info_x - PAD - 10

    # 标题（截断）
    title_text = game["title"]
    # 简单截断
    bbox = draw.textbbox((0, 0), title_text, font=font_title)
    while bbox[2] - bbox[0] > info_w and len(title_text) > 3:
        title_text = title_text[:-1]
        bbox = draw.textbbox((0, 0), title_text + "...", font=font_title)
    if title_text != game["title"]:
        title_text += "..."

    draw.text((info_x, row_y + 10), title_text, fill=TEXT_PRIMARY, font=font_title)

    # 原价 -> 免费
    draw.rectangle([(info_x, row_y + 55), (info_x + 56, row_y + 75)], fill=ACCENT_FREE)
    draw.text((info_x + 6, row_y + 56), "FREE", fill="white", font=font_small)
    draw.text((info_x + 66, row_y + 55), f"原价 {game['price']['original']}", fill=TEXT_MUTED, font=font_info)

    # 时间
    if "free_end" in game and "free_start" in game:
        draw.text((info_x, row_y + 85), f"{game['free_end']} 截止", fill=TEXT_MUTED, font=font_small)

    return y + card_h + 8


def build_image(current, upcoming, font_path):
    """构建一张完整的合成图片：本周免费 + 即将免费"""
    if not current and not upcoming:
        return None

    font_title, font_info, font_small = get_fonts(font_path)

    # 计算画布尺寸
    canvas_w = max(COVER_W + 400, 720)
    section_padding = PAD * 2 + BAR_H

    rows_height = 0
    if current:
        rows_height += section_padding + len(current) * (COVER_H + PAD * 2 + 8)
    if upcoming:
        rows_height += section_padding + len(upcoming) * (COVER_H + PAD * 2 + 8)

    # 标题区 + 底部
    header_h = 50
    footer_h = 30
    canvas_h = header_h + rows_height + footer_h

    canvas = PILImage.new("RGB", (canvas_w, canvas_h), color=DARK_BG)
    draw = ImageDraw.Draw(canvas)

    cursor_y = 10

    # 顶部标题
    title_text = f"Epic Games 免费游戏  {beijing_time_str('%Y-%m-%d')}"
    bbox = draw.textbbox((0, 0), title_text, font=font_info)
    th = bbox[3] - bbox[1]
    draw.text((PAD, cursor_y + (header_h - th) // 2), title_text, fill=TEXT_MUTED, font=font_info)
    cursor_y += header_h

    # 本周免费
    if current:
        cursor_y = draw_section_header(
            draw, cursor_y, canvas_w,
            f"本周免费  ({len(current)} 款)", ACCENT_CURRENT, font_title
        )
        for g in current:
            cursor_y = draw_game_row(draw, cursor_y, g, font_title, font_info, font_small, canvas_w)

    # 即将免费
    if upcoming:
        cursor_y = draw_section_header(
            draw, cursor_y, canvas_w,
            f"即将免费  ({len(upcoming)} 款)", ACCENT_UPCOMING, font_title
        )
        for g in upcoming:
            cursor_y = draw_game_row(draw, cursor_y, g, font_title, font_info, font_small, canvas_w)

    # 底部
    draw.text((PAD, canvas_h - 22), "store.epicgames.com/zh-CN/free-games", fill=TEXT_MUTED, font=font_small)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        canvas.save(f, quality=92)
        return f.name


# ---------- 文字报告（降级用）----------
def build_text_report(current, upcoming):
    lines = [f"Epic 免费游戏  {beijing_time_str()}", ""]
    if current:
        lines.append(f"[ 本周免费 {len(current)} 款 ]")
        for g in current:
            lines.append(f"  {g['title']}  (原价 {g['price']['original']})  {g.get('free_end', '')} 截止")
    if upcoming:
        lines.append(f"[ 即将免费 {len(upcoming)} 款 ]")
        for g in upcoming:
            lines.append(f"  {g['title']}  (原价 {g['price']['original']})  {g.get('free_start', '')} 起")
    lines.append("")
    lines.append("store.epicgames.com/zh-CN/free-games")
    return "\n".join(lines)


# ---------- 主流程 ----------
def main():
    log_info("===== Epic 免费游戏 =====")

    # 请求 API
    data = fetch_epic_data()
    if not data:
        log_error("无法获取 Epic 数据")
        return

    # 解析
    current, upcoming = parse_free_games(data)
    log_success(f"本周 {len(current)} 款, 即将 {len(upcoming)} 款")

    for g in current:
        log_success(f"[本周] {g['title']} 截止 {g['free_end']}")
    for g in upcoming:
        log_info(f"[即将] {g['title']}  {g['free_start']} ~ {g['free_end']}")

    if not current and not upcoming:
        log_warning("无免费游戏")
        return

    # 尝试生成图片
    img_path = None
    font_path = ensure_font()
    if font_path:
        try:
            img_path = build_image(current, upcoming, font_path)
        except Exception as e:
            log_error(f"图片生成异常: {e}")

    # 推送
    if img_path and os.path.exists(img_path):
        # 图片成功 → 仅 Telegram，不发文字
        from notifier import _send_telegram_photo
        caption = f"Epic Games 免费游戏  {beijing_time_str()}"
        ok = _send_telegram_photo(caption, img_path)
        os.remove(img_path)
        if ok:
            log_success("图片推送完成 (Telegram)")
        else:
            log_warning("Telegram 图片推送失败，降级为文字")
            notify_send("Epic 免费游戏", build_text_report(current, upcoming))
    else:
        # 图片失败 → 文字推送全渠道
        log_warning("图片生成失败，使用文字推送")
        notify_send("Epic 免费游戏", build_text_report(current, upcoming))

    log_info("===== 任务完成 =====")


if __name__ == "__main__":
    main()

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
ACCENT_DISCOUNT = (180, 80, 180)
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


def parse_discounts(data, free_titles):
    """
    解析打折促销游戏（discountPercentage > 0 且在 promotionalOffers 或 upcomingPromotionalOffers 中）。
    free_titles: 已出现在免费列表中的标题集合，避免重复。
    返回 list of dict，每个包含 discount_pct 和价格。
    """
    if not data:
        return []

    elements = (
        data.get("data", {}).get("Catalog", {})
        .get("searchStore", {}).get("elements", [])
    )

    discounts = []
    seen = set(free_titles)

    for elem in elements:
        if elem.get("offerType") != "BASE_GAME" or elem.get("status") != "ACTIVE":
            continue

        title = elem.get("title", "Unknown")
        if title in seen:
            continue

        price_info = elem.get("price", {}).get("totalPrice", {})
        price = extract_price(elem)

        promotions = elem.get("promotions")
        if not promotions:
            continue

        best = None  # (discount_pct, start, end)

        # 查找 promotionalOffers + upcomingPromotionalOffers 中最大折扣
        for section in ("promotionalOffers", "upcomingPromotionalOffers"):
            for block in promotions.get(section, []):
                for offer in block.get("promotionalOffers", []):
                    pct = parse_discount_percentage(offer.get("discountSetting", ""))
                    if pct and pct > 0:
                        start = offer.get("startDate", "")
                        end = offer.get("endDate", "")
                        if start and end:
                            if best is None or pct > best[0]:
                                best = (pct, start, end)

        if best:
            pct, start, end = best
            url_slug = elem.get("urlSlug") or elem.get("productSlug", "").replace("/home", "")
            store_url = f"https://store.epicgames.com/zh-CN/p/{url_slug}" if url_slug else ""
            # 计算折后价
            original = price_info.get("originalPrice", 0)
            discounted = round(original * (100 - pct) / 100)

            discounts.append({
                "title": title,
                "url": store_url,
                "image": find_image(elem.get("keyImages", [])),
                "price": price,
                "discount_pct": pct,
                "discounted_price": discounted,
                "free_start": bj_time(start),
                "free_end": bj_time(end),
            })
            seen.add(title)

    # 按折扣从大到小排序
    discounts.sort(key=lambda g: -g["discount_pct"])
    return discounts


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


def draw_game_card(draw, x, y, game, font_title, font_info, font_small, card_w):
    """在指定位置绘制单张游戏卡片：上封面 + 下信息。card_w 为卡片总宽。"""
    inner_w = card_w - PAD
    cover_path = download_image(game["image"])

    # 封面
    if cover_path:
        try:
            cover = PILImage.open(cover_path)
            cover = cover.resize((inner_w, COVER_H), LAYOUT_RESAMPLE)
            draw._image.paste(cover, (x, y))
        except Exception:
            draw.rectangle([(x, y), (x + inner_w, y + COVER_H)], fill=CARD_BG)
            draw.text((x + inner_w // 2 - 30, y + COVER_H // 2), "no cover", fill=TEXT_MUTED, font=font_info)
    else:
        draw.rectangle([(x, y), (x + inner_w, y + COVER_H)], fill=CARD_BG)
        draw.text((x + inner_w // 2 - 30, y + COVER_H // 2), "no cover", fill=TEXT_MUTED, font=font_info)

    if cover_path and os.path.exists(cover_path):
        os.remove(cover_path)

    # 信息区域
    info_y = y + COVER_H + 8

    # 标题（支持两行换行）
    title_text = game["title"]
    bbox = draw.textbbox((0, 0), title_text, font=font_title)
    title_w = bbox[2] - bbox[0]
    title_h = bbox[3] - bbox[1]

    if title_w <= inner_w:
        # 一行够用
        draw.text((x, info_y), title_text, fill=TEXT_PRIMARY, font=font_title)
        info_y += title_h + 6
    else:
        # 需要换行：找到合适的断点
        wrap_at = len(title_text)
        while wrap_at > 0:
            test = title_text[:wrap_at]
            if draw.textbbox((0, 0), test, font=font_title)[2] <= inner_w:
                break
            wrap_at -= 1
        # 第1行
        line1 = title_text[:wrap_at]
        draw.text((x, info_y), line1, fill=TEXT_PRIMARY, font=font_title)
        info_y += title_h + 2
        # 第2行（放不下则截断加 ...）
        line2 = title_text[wrap_at:]
        if draw.textbbox((0, 0), line2, font=font_title)[2] > inner_w:
            while line2 and draw.textbbox((0, 0), line2 + "...", font=font_title)[2] > inner_w:
                line2 = line2[:-1]
            line2 += "..."
        draw.text((x, info_y), line2, fill=TEXT_PRIMARY, font=font_title)
        info_y += title_h + 4

    # 标签 + 价格
    discount_pct = game.get("discount_pct", 0)
    if discount_pct:
        # 折扣标签
        tag_text = f"-{discount_pct}%"
        bbox = draw.textbbox((0, 0), tag_text, font=font_info)
        tag_w = bbox[2] - bbox[0] + 12
        tag_h = 26
        draw.rectangle([(x, info_y), (x + tag_w, info_y + tag_h)], fill=ACCENT_DISCOUNT)
        draw.text((x + 6, info_y + 2), tag_text, fill="white", font=font_info)
        # 原价 → 折后价
        draw.text((x + tag_w + 6, info_y),
                  f"原价 {game['price']['original']}  {game['discounted_price']}元",
                  fill=TEXT_MUTED, font=font_info)
    else:
        # FREE 标签
        free_w = 56
        free_h = 26
        draw.rectangle([(x, info_y), (x + free_w, info_y + free_h)], fill=ACCENT_FREE)
        draw.text((x + 6, info_y + 2), "FREE", fill="white", font=font_info)
        draw.text((x + free_w + 6, info_y), f"原价 {game['price']['original']}", fill=TEXT_MUTED, font=font_info)
    info_y += 32

    # 截止时间
    if "free_end" in game:
        label = "截止" if not discount_pct else "活动截止"
        draw.text((x, info_y), f"{game['free_end']} {label}", fill=TEXT_MUTED, font=font_info)
        info_y += 28

    return info_y + 16


def build_image(current, upcoming, discounts, font_path):
    """构建完整图片：每行两张卡片"""
    if not current and not upcoming and not discounts:
        return None

    font_title, font_info, font_small = get_fonts(font_path)

    CARD_GAP = 16
    CARD_W = 380
    canvas_w = max(PAD * 2 + CARD_W * 2 + CARD_GAP, 800)
    header_h, footer_h = 50, 40

    # 估算高度
    est_h = header_h + footer_h + 20
    for lst in (current, upcoming, discounts):
        if lst:
            est_h += BAR_H + ((len(lst) + 1) // 2) * (COVER_H + 120)

    canvas = PILImage.new("RGB", (canvas_w, est_h), color=DARK_BG)
    draw = ImageDraw.Draw(canvas)

    cur_y = 10

    # 顶部
    draw.text((PAD, cur_y + 8), "Epic Games 免费游戏", fill=TEXT_PRIMARY, font=font_title)
    date_str = beijing_time_str("%Y-%m-%d")
    tw = draw.textbbox((0, 0), date_str, font=font_small)[2]
    draw.text((canvas_w - PAD - tw, cur_y + 14), date_str, fill=TEXT_MUTED, font=font_small)
    cur_y += header_h

    # 绘制各分区
    for games_list, title, accent in (
        (current, f"本周免费  {len(current)} 款", ACCENT_CURRENT),
        (upcoming, f"即将免费  {len(upcoming)} 款", ACCENT_UPCOMING),
        (discounts, f"折扣活动  {len(discounts)} 款", ACCENT_DISCOUNT),
    ):
        if not games_list:
            continue
        cur_y = draw_section_header(draw, cur_y, canvas_w, title, accent, font_title)
        for i in range(0, len(games_list), 2):
            row_y = cur_y
            for j, g in enumerate(games_list[i : i + 2]):
                card_x = PAD + j * (CARD_W + CARD_GAP)
                bottom = draw_game_card(draw, card_x, row_y, g, font_title, font_info, font_small, CARD_W)
                cur_y = max(cur_y, bottom)

    # 裁剪到实际高度
    actual_h = cur_y + footer_h
    canvas = canvas.crop((0, 0, canvas_w, actual_h))
    draw = ImageDraw.Draw(canvas)
    draw.text((PAD, actual_h - 24), "store.epicgames.com/zh-CN/free-games", fill=TEXT_MUTED, font=font_small)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        canvas.save(f, quality=92)
        return f.name


# ---------- 文字报告（降级用）----------
def build_text_report(current, upcoming, discounts):
    lines = [f"Epic Games  {beijing_time_str()}", ""]
    if current:
        lines.append(f"[ 本周免费 {len(current)} 款 ]")
        for g in current:
            lines.append(f"  {g['title']}  (原价 {g['price']['original']})  {g.get('free_end', '')} 截止")
    if upcoming:
        lines.append(f"[ 即将免费 {len(upcoming)} 款 ]")
        for g in upcoming:
            lines.append(f"  {g['title']}  (原价 {g['price']['original']})  {g.get('free_start', '')} 起")
    if discounts:
        lines.append(f"[ 折扣活动 {len(discounts)} 款 ]")
        for g in discounts:
            lines.append(f"  {g['title']}  -{g['discount_pct']}%  {g['price']['original']} -> {g['discounted_price']}元  {g.get('free_end', '')} 截止")
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
    free_titles = {g["title"] for g in current} | {g["title"] for g in upcoming}
    discounts = parse_discounts(data, free_titles)
    log_success(f"本周 {len(current)} 款, 即将 {len(upcoming)} 款, 折扣 {len(discounts)} 款")

    for g in current:
        log_success(f"[本周] {g['title']} 截止 {g['free_end']}")
    for g in upcoming:
        log_info(f"[即将] {g['title']}  {g['free_start']} ~ {g['free_end']}")
    for g in discounts:
        log_info(f"[折扣] {g['title']}  -{g['discount_pct']}%  {g['free_end']} 截止")

    if not current and not upcoming and not discounts:
        log_warning("无免费游戏或折扣")
        return

    # 尝试生成图片
    img_path = None
    font_path = ensure_font()
    if font_path:
        try:
            img_path = build_image(current, upcoming, discounts, font_path)
        except Exception as e:
            log_error(f"图片生成异常: {e}")

    # 推送
    if img_path and os.path.exists(img_path):
        # 图片成功 → 仅 Telegram，不发文字
        from notifier import _send_telegram_photo
        caption = f"Epic Games  {beijing_time_str()}"
        ok = _send_telegram_photo(caption, img_path)
        os.remove(img_path)
        if ok:
            log_success("图片推送完成 (Telegram)")
        else:
            log_warning("Telegram 图片推送失败，降级为文字")
            notify_send("Epic Games", build_text_report(current, upcoming, discounts))
    else:
        # 图片失败 → 文字推送全渠道
        log_warning("图片生成失败，使用文字推送")
        notify_send("Epic Games", build_text_report(current, upcoming, discounts))

    log_info("===== 任务完成 =====")


if __name__ == "__main__":
    main()

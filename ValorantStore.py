#!/usr/bin/env python3
# cron: 15 8 * * *
# new Env("掌瓦每日商店推送")
# 掌上无畏契约 每日商店自动推送
# - 获取每日商店 4 款武器皮肤
# - 文字报告推送至全部通知渠道
# - 皮肤图片单独推送至 Telegram

import os
import json
import requests
import tempfile
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from PIL import Image as PILImage, ImageDraw, ImageFont, ImageOps

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send, send_photos as notify_send_photos, _send_telegram_photo

# ==================== 用户配置 ====================
VALORANT_COOKIE = os.environ.get("VALORANT_COOKIE", "")
TZ_BEIJING = timezone(timedelta(hours=8))

# 品质映射
QUALITY_MAP = {
    "orange": ("传奇", "🟧"),
    "purple": ("卓越", "🟪"),
    "blue": ("精选", "🟦"),
    "green": ("奢华", "🟩"),
    "yellow": ("终极", "🟨"),
}

API_BASE = "https://app.mval.qq.com"
COMMON_PARAMS = "source_game_zone=agame&game_zone=agame"
CONFIG_DIR = Path(__file__).parent / "config"
CT_FILE = CONFIG_DIR / ".valorant_ct"
AT_FILE = CONFIG_DIR / ".valorant_at"  # 持久化 access_token


def parse_cookie(cookie: str) -> dict:
    """解析 cookie 字符串为字典"""
    cookie_dict = {}
    for item in cookie.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookie_dict[k.strip()] = v.strip()
    return cookie_dict


def load_ct(cookie_dict: dict) -> str:
    """加载 ct: 优先环境变量，其次本地文件"""
    ct = cookie_dict.get("ct", "")
    if ct:
        return ct
    if CT_FILE.exists():
        ct = CT_FILE.read_text().strip()
        if ct:
            return ct
    return ""


def save_ct(ct: str):
    """保存 ct 到本地文件，供下次运行使用"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CT_FILE.write_text(ct)


def load_at(cookie_dict: dict) -> str:
    """加载 access_token: 优先环境变量，其次本地文件"""
    at = cookie_dict.get("access_token", "")
    if at:
        return at
    if AT_FILE.exists():
        at = AT_FILE.read_text().strip()
        if at:
            return at
    return ""


def save_at(at: str):
    """保存 access_token 到本地文件，供下次运行使用"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AT_FILE.write_text(at)


def create_session(cookie: str) -> requests.Session:
    """创建带 Cookie 的 Session，优先使用持久化的 access_token"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "mval/2.6.0.10062 Channel/5 Mozilla/5.0 (Linux; Android 16; wv) AppleWebKit/537.36",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json; charset=utf-8",
    })
    cookie_dict = parse_cookie(cookie)
    # ct 不是标准 cookie，不加入 session cookies
    cookie_dict.pop("ct", None)
    # 优先使用持久化的 access_token（更可能是最新的）
    saved_at = load_at(cookie_dict)
    if saved_at:
        cookie_dict["access_token"] = saved_at
    requests.utils.add_dict_to_cookiejar(session.cookies, cookie_dict)
    return session


def api_post(session: requests.Session, path: str, body: dict = None) -> dict:
    """通用 POST 请求"""
    url = f"{API_BASE}{path}?{COMMON_PARAMS}"
    try:
        resp = session.post(url, json=body or {}, timeout=15)
        try:
            return resp.json()
        except json.JSONDecodeError:
            # 服务端偶尔返回重复 JSON，取第一个合法对象
            text = resp.text.strip()
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(text)
            return obj
    except Exception as e:
        log_error(f"API 请求失败 [{path}]: {e}")
        return {}


def refresh_web_ticket(session: requests.Session, ct: str) -> tuple:
    """刷新 web ticket (tid) 和 client ticket (ct)

    流程（基于 HAR 抓包）:
    1. refresh_client_ticket (用旧 ct) → 新 ct + wt (= tid cookie)
    2. get_client_tmp_ticket (用新 ct) → ctt + sk

    Returns: (new_ct, success)
    """
    cookie = {c.name: c.value for c in session.cookies}
    user_id = cookie.get("userId", "")

    # Step 1: refresh_client_ticket → 新 ct + wt
    rct_body = {
        "config_params": {"lang_type": 0},
        "ct": ct,
        "local_is_new_user": 0,
        "user_id": user_id,
        "source_game_zone": "agame",
        "game_zone": "agame",
    }
    rct_result = api_post(session, "/go/auth/refresh_client_ticket", rct_body)
    if rct_result.get("result") != 0:
        log_warning(f"refresh_client_ticket 失败: {rct_result.get('msg', rct_result.get('err_msg', '未知'))}")
        return ct, False

    rct_data = rct_result.get("data", {})
    ct_info = rct_data.get("ct_info", rct_data)
    new_ct = ct_info.get("ct", "")
    wt = ct_info.get("wt", "")

    if new_ct:
        log_success(f"client ticket (ct) 刷新成功")
    if wt:
        # 更新 tid cookie: 优先修改已有 cookie，否则新建
        tid_set = False
        for c in session.cookies:
            if c.name == "tid":
                c.value = wt
                tid_set = True
                break
        if not tid_set:
            session.cookies.set("tid", wt, domain="app.mval.qq.com", path="/")
        log_success(f"web ticket (tid) 刷新成功 (有效期 {ct_info.get('refresh_wt_span', '?')}s)")

    if not new_ct:
        log_warning("refresh_client_ticket 未返回新 ct")
        return ct, False

    # Step 2: get_client_tmp_ticket → ctt + sk
    ctt_body = {
        "config_params": {"lang_type": 0},
        "ct": new_ct,
    }
    api_post(session, "/go/auth/get_client_tmp_ticket", ctt_body)

    return new_ct, bool(wt)


def refresh_token(session: requests.Session) -> str:
    """刷新 access_token，返回新的 token"""
    cookie = {c.name: c.value for c in session.cookies}
    body = {
        "type": cookie.get("acctype", "qc"),
        "uuid": cookie.get("userId", ""),
        "openid": cookie.get("openid", ""),
        "source_game_zone": "agame",
        "game_zone": "agame",
    }
    result = api_post(session, "/go/auth/refresh_third_token", body)
    if result.get("result") == 0:
        token = result.get("data", {}).get("access_token", "")
        if token:
            session.cookies.set("access_token", token, domain="app.mval.qq.com")
            save_at(token)  # 持久化保存新 token
            log_success("access_token 刷新成功")
            return token
    log_warning(f"刷新 token 失败: {result.get('msg', result.get('err_msg', '未知'))}")
    return ""


def get_daily_store(session: requests.Session) -> tuple:
    """获取每日商店内容，返回 (items, end_ts)"""
    result = api_post(session, "/go/mlol_store/agame/user_store", {
        "scene": "",
        "source_game_zone": "agame",
        "game_zone": "agame",
    })
    if result.get("result") != 0:
        log_error(f"获取商店失败: {result.get('msg', '未知')}")
        return [], 0

    for section in result.get("data", []):
        if section["key"] == "dailystore":
            items = section.get("list", [])
            end_ts = section.get("end_ts", 0)
            log_success(f"获取到 {len(items)} 款每日商店皮肤")
            return items, end_ts
    return [], 0


def build_report(items: list, nickname: str, end_ts: int) -> str:
    """构建文字报告"""
    end_time = datetime.fromtimestamp(end_ts, tz=TZ_BEIJING).strftime("%Y-%m-%d %H:%M") if end_ts else "未知"

    lines = [f"👤 账号: {nickname}", f"⏰ 刷新时间: {end_time}", "", "─" * 18, ""]

    for i, item in enumerate(items):
        name = item.get("goods_name", "未知")
        price = item.get("rmb_price", "?")
        quality = item.get("quality", "")
        likes = item.get("like_num", "")
        _, quality_emoji = QUALITY_MAP.get(quality, ("未知", "⬜️"))

        lines.append(f"{i+1}. {quality_emoji} {name}")
        lines.append(f"   💰 {price} 点券 | ❤️ {likes}")
        lines.append("")

    lines.append("─" * 18)
    lines.append(f"🕒 执行时间: {beijing_time_str()}")
    return "\n".join(lines)


# ==================== 图片生成配置 ====================
CARD_W = 720
HEADER_H = 140
GAP = 16
BG_COLOR = (20, 20, 24)
GOODS_WIDTH_RATIO = 0.78
GOODS_PADDING = 40
BG_TEXT_RATIO = 0.18


def _download_image_pil(url: str, timeout: int = 10):
    """下载图片并返回 PIL Image 对象，失败返回 None"""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        img = PILImage.open(BytesIO(resp.content))
        img = ImageOps.exif_transpose(img)
        return img
    except Exception as e:
        log_error(f"下载图片失败: {e}")
        return None


def _contain_resize(img, target_w: int, target_h: int):
    """等比缩放使整图完整放入目标尺寸，不裁切，返回 (缩放图, 居中x, 居中y)"""
    scale = min(target_w / img.width, target_h / img.height)
    new_w = int(img.width * scale)
    new_h = int(img.height * scale)
    img_resized = img.resize((new_w, new_h), PILImage.LANCZOS)
    x = (target_w - new_w) // 2
    y = (target_h - new_h) // 2
    return img_resized, x, y


def _draw_text_with_outline(draw, position, text, font, fill, outline=(0, 0, 0, 255), offset: int = 2):
    """绘制带描边的文字（8 方向偏移）"""
    x, y = position
    for dx in (-offset, 0, offset):
        for dy in (-offset, 0, offset):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, fill=outline, font=font)
    draw.text((x, y), text, fill=fill, font=font)


def _fit_font(text: str, font_path: str, base_size: int, max_width: int, min_size: int = 16):
    """根据最大宽度动态缩放字号，返回合适字体"""
    size = base_size
    while size > min_size:
        try:
            font = ImageFont.truetype(font_path, size)
        except IOError:
            return ImageFont.load_default()
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 2
    try:
        return ImageFont.truetype(font_path, min_size)
    except IOError:
        return ImageFont.load_default()


def _load_fonts(font_path: str) -> dict:
    """一次性加载所有字体"""
    def make(size):
        try:
            return ImageFont.truetype(font_path, size)
        except IOError:
            return ImageFont.load_default()
    return {
        "font_path": font_path,
        "title": make(40),
        "subtitle": make(28),
        "card_name": make(36),
        "price": make(34),
    }


FONT_URL = "https://github.com/anthonyfok/fonts-wqy-microhei/raw/master/wqy-microhei.ttc"
FONT_FILE = CONFIG_DIR / "font.ttf"


def ensure_font() -> str:
    """确保字体文件存在，不存在则下载"""
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


def _render_card(item: dict, bg_img, goods_img, fonts: dict):
    """渲染单张商品卡片，返回 RGBA Image。利用背景自带底部文字条，不加额外 footer"""
    name = item.get("goods_name", "未知")
    price = item.get("rmb_price", "0")

    # 卡片高度由背景图决定（宽度 CARD_W，高度按背景原始比例），无额外 footer
    if bg_img is not None:
        bg = bg_img.convert("RGBA")
        bg_h = int(CARD_W * bg.height / bg.width)
        bg_resized = bg.resize((CARD_W, bg_h), PILImage.LANCZOS)
    else:
        bg_h = 400
        bg_resized = None

    card = PILImage.new("RGBA", (CARD_W, bg_h), BG_COLOR + (255,))

    # 背景图（铺满整卡）
    if bg_resized is not None:
        card.alpha_composite(bg_resized, (0, 0))

    # 背景底部自带文字条区域
    text_strip_h = int(bg_h * BG_TEXT_RATIO)
    text_strip_top = bg_h - text_strip_h

    # 商品图（contain 模式，限制在文字条上方区域，留 PADDING 不顶住背景边和文字条）
    if goods_img is not None:
        goods = goods_img.convert("RGBA")
        max_w = int(CARD_W * GOODS_WIDTH_RATIO)
        max_h = max(text_strip_top - 2 * GOODS_PADDING, 1)
        goods_resized, _, _ = _contain_resize(goods, max_w, max_h)
        x = (CARD_W - goods_resized.width) // 2
        y = (text_strip_top - goods_resized.height) // 2
        card.alpha_composite(goods_resized, (x, y))

    # 缺图兜底：底部文字条加半透明黑条保证文字可读
    if bg_resized is None:
        fallback_bar = PILImage.new("RGBA", (CARD_W, text_strip_h), (0, 0, 0, 175))
        card.alpha_composite(fallback_bar, (0, text_strip_top))

    draw = ImageDraw.Draw(card)

    # 文字颜色：有背景用深色（背景文字条偏白），无背景用浅色
    if bg_resized is not None:
        name_color = (40, 40, 50, 255)
        price_color = (120, 80, 10, 255)
        outline_color = (255, 255, 255, 200)
    else:
        name_color = (255, 255, 255, 255)
        price_color = (255, 215, 0, 255)
        outline_color = (0, 0, 0, 255)

    # 价格（右下，文字条内）
    price_text = f"{price} 点券"
    price_font = fonts["price"]
    pbbox = draw.textbbox((0, 0), price_text, font=price_font)
    pw = pbbox[2] - pbbox[0]
    ph = pbbox[3] - pbbox[1]
    px = CARD_W - pw - 28
    py = text_strip_top + (text_strip_h - ph) // 2
    _draw_text_with_outline(draw, (px, py), price_text, price_font, price_color, outline=outline_color)

    # 名称（左下，动态缩放避免与价格重叠）
    name_max_w = px - 28 - 28
    name_font = _fit_font(name, fonts["font_path"], 36, name_max_w)
    nbbox = draw.textbbox((0, 0), name, font=name_font)
    nh = nbbox[3] - nbbox[1]
    ny = text_strip_top + (text_strip_h - nh) // 2
    _draw_text_with_outline(draw, (28, ny), name, name_font, name_color, outline=outline_color)

    return card


def _render_header(nickname: str, fonts: dict):
    """渲染标题头，返回 RGBA Image"""
    header = PILImage.new("RGBA", (CARD_W, HEADER_H), BG_COLOR + (255,))
    draw = ImageDraw.Draw(header)

    _draw_text_with_outline(draw, (28, 20), "掌瓦每日商店", fonts["title"], (255, 255, 255, 255))
    _draw_text_with_outline(draw, (28, 78), f"账号: {nickname}", fonts["subtitle"], (200, 200, 200, 255))

    return header


def build_shop_image(items: list, nickname: str, end_ts: int) -> str:
    """构建商店长图，返回 PNG 文件路径"""
    font_path = ensure_font()
    if not font_path:
        log_error("无法获取字体，跳过图片生成")
        return None
    fonts = _load_fonts(font_path)

    # 并行下载所有图片（bg + goods 交替入队）
    urls = []
    for item in items:
        urls.append(item.get("bg_image", ""))
        urls.append(item.get("goods_pic", ""))
    with ThreadPoolExecutor(max_workers=8) as ex:
        downloaded = list(ex.map(_download_image_pil, urls))

    # 配对为 (bg, goods)
    item_imgs = [(downloaded[i * 2], downloaded[i * 2 + 1]) for i in range(len(items))]

    # 渲染卡片（缺图时用纯色兜底，保证 4 格完整）
    cards = []
    for idx, (item, (bg, goods)) in enumerate(zip(items, item_imgs)):
        try:
            card = _render_card(item, bg, goods, fonts)
        except Exception as e:
            log_error(f"商品 {item.get('goods_name', '未知')} 卡片渲染失败: {e}")
            card = _render_card(item, None, None, fonts)
        cards.append(card)
        log_info(f"商品 {item.get('goods_name', '未知')} 卡片处理完成")

    if not cards:
        log_error("没有商品卡片渲染成功")
        return None

    # 标题头
    header = _render_header(nickname, fonts)

    # 垂直拼接（标题头 + 卡片，间隙用深色填充）
    sections = [header] + cards
    total_h = sum(s.height for s in sections) + (len(sections) - 1) * GAP
    merged = PILImage.new("RGB", (CARD_W, total_h), BG_COLOR)

    y = 0
    for s in sections:
        merged.paste(s.convert("RGB"), (0, y))
        y += s.height + GAP

    # 保存为 PNG
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            merged.save(f, format="PNG")
            merged_path = f.name
        log_info(f"商店图片生成完成: {merged_path}")
        return merged_path
    except Exception as e:
        log_error(f"商店图片保存失败: {e}")
        return None


def main():
    if not VALORANT_COOKIE:
        log_error("未配置 VALORANT_COOKIE，请在环境变量中设置")
        notify_send("掌瓦每日商店 错误", "❌ 未配置 VALORANT_COOKIE")
        return

    cookie_dict = parse_cookie(VALORANT_COOKIE)
    ct = load_ct(cookie_dict)
    if not ct:
        log_error("未配置 ct (client ticket)，请在 VALORANT_COOKIE 中添加 ct=xxx，或放到文件 " + str(CT_FILE))
        notify_send("掌瓦每日商店 错误", "❌ 缺少 ct 参数，请在 VALORANT_COOKIE 中添加 ct=xxx")
        return

    session = create_session(VALORANT_COOKIE)

    # 刷新认证: 先刷新 access_token，再用新 AT 刷新 ct
    # 这样即使 AT 快过期，也能先续上，再用新 AT 刷新 ct
    new_at = refresh_token(session)
    if not new_at:
        log_warning("access_token 刷新失败，尝试用旧 token 继续...")

    new_ct, ct_ok = refresh_web_ticket(session, ct)
    if new_ct and new_ct != ct:
        save_ct(new_ct)
        log_success(f"ct 已更新并保存")
    elif not ct_ok:
        log_warning("ct 刷新失败，可能需要重新抓包")
        notify_send("掌瓦商店 Token 告警", "⚠️ access_token 或 ct 刷新失败，请尽快重新抓包，否则下次将无法获取商店")

    # 获取绑定账号
    bind_result = api_post(session, "/go/auth/bind_relation_list")
    bind_list = bind_result.get("data", {}).get("list", [])
    nickname = bind_list[0].get("nickName", "未知") if bind_list else "未知"
    log_info(f"绑定账号: {nickname}")

    # 获取每日商店
    items, end_ts = get_daily_store(session)
    if not items:
        log_warning("未获取到商店内容，可能今日未刷新")
        notify_send("🔫 掌瓦每日商店", "⚠️ 未获取到商店内容，请检查 Cookie 或稍后重试")
        return

    # 构建商店图片
    shop_image_path = build_shop_image(items, nickname, end_ts)

    if shop_image_path:
        end_time = datetime.fromtimestamp(end_ts, tz=TZ_BEIJING).strftime("%Y-%m-%d %H:%M") if end_ts else "未知"
        caption = f"🔫 掌瓦每日商店\n\n👤 账号: {nickname}\n⏰ 刷新时间: {end_time}\n\n{'─' * 18}\n🕒 执行时间: {beijing_time_str()}"
        try:
            _send_telegram_photo(caption, shop_image_path)
            log_info("推送完成: 商店图片")
        finally:
            if os.path.exists(shop_image_path):
                os.remove(shop_image_path)
    else:
        # 图片生成失败，回退到文字报告
        log_warning("商店图片生成失败，使用文字报告")
        report = build_report(items, nickname, end_ts)
        notify_send("🔫 掌瓦每日商店", report)
        log_info("推送完成: 文字报告")


if __name__ == "__main__":
    main()

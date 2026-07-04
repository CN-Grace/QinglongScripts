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
from pathlib import Path
from datetime import datetime, timezone, timedelta
from PIL import Image as PILImage, ImageDraw, ImageFont

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send, send_photos as notify_send_photos

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


def download_image(url: str, timeout: int = 10) -> str:
    """下载图片到临时文件，返回文件路径"""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        suffix = ".png" if "png" in resp.headers.get("content-type", "") else ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(resp.content)
            return f.name
    except Exception as e:
        log_error(f"下载图片失败: {e}")
        return None


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


def build_shop_image(items: list) -> str:
    """构建商店图片，返回图片文件路径"""
    processed_images = []

    # 确保字体存在
    font_path = ensure_font()
    if not font_path:
        log_error("无法获取字体，跳过图片生成")
        return None

    for i, item in enumerate(items):
        name = item.get("goods_name", "未知")
        price = item.get("rmb_price", "0")
        quality = item.get("quality", "")
        bg_url = item.get("bg_image", "")
        goods_url = item.get("goods_pic", "")

        if not bg_url or not goods_url:
            log_warning(f"商品 {name} 缺少图片URL，跳过")
            continue

        # 下载图片
        bg_path = download_image(bg_url)
        goods_path = download_image(goods_url)

        if not bg_path or not goods_path:
            log_warning(f"商品 {name} 图片下载失败，跳过")
            continue

        try:
            # 打开图片
            bg_img = PILImage.open(bg_path)
            goods_img = PILImage.open(goods_path)

            # 调整商品图尺寸（宽度 = 背景图宽度 - 20px）
            target_width = bg_img.width - 20
            height = int((goods_img.height * target_width) / goods_img.width)
            goods_resized = goods_img.resize((target_width, height))

            # 计算居中粘贴位置
            x = (bg_img.width - goods_resized.width) // 2
            y = (bg_img.height - goods_resized.height) // 2

            # 创建新图像
            new_img = PILImage.new('RGB', bg_img.size)
            new_img.paste(bg_img, (0, 0))

            # 粘贴商品图（支持透明通道）
            if goods_resized.mode in ('RGBA', 'LA'):
                new_img.paste(goods_resized, (x, y), mask=goods_resized)
            else:
                new_img.paste(goods_resized, (x, y))

            # 绘制文字
            draw = ImageDraw.Draw(new_img)

            # 加载字体
            try:
                font = ImageFont.truetype(font_path, 36) if font_path else ImageFont.load_default()
            except IOError:
                font = ImageFont.load_default()

            # 商品名称
            text_position = (36, new_img.height - 50)
            text_color = (255, 255, 255)  # 白色
            draw.text(text_position, name, fill=text_color, font=font)

            # 商品价格
            price_text = f"{price} 点券"
            price_bbox = draw.textbbox((0, 0), price_text, font=font)
            price_width = price_bbox[2] - price_bbox[0]
            price_position = (new_img.width - price_width - 36, new_img.height - 50)
            draw.text(price_position, price_text, fill=text_color, font=font)

            # 保存处理后的图片
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
                new_img.save(f)
                processed_images.append(f.name)

            log_info(f"商品 {name} 图片处理完成")

        except Exception as e:
            log_error(f"商品 {name} 图片处理失败: {e}")
        finally:
            # 清理临时文件
            for path in [bg_path, goods_path]:
                if path and os.path.exists(path):
                    os.remove(path)

    if not processed_images:
        log_error("没有商品图片处理成功")
        return None

    # 合并所有图片
    images = [PILImage.open(img_path) for img_path in processed_images]

    # 计算合并后图片尺寸
    max_width = max(img.width for img in images)
    total_height = sum(img.height for img in images) + (len(images) - 1) * 20  # 20px 间距

    # 创建合并后的图片
    merged_image = PILImage.new('RGB', (max_width, total_height), color='white')

    # 将所有图片垂直拼接
    y_offset = 0
    for img in images:
        merged_image.paste(img, (0, y_offset))
        y_offset += img.height + 20

    # 保存合并后的图片
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        merged_image.save(f)
        merged_image_path = f.name

    # 清理临时文件
    for path in processed_images:
        if os.path.exists(path):
            os.remove(path)

    log_info(f"商店图片生成完成: {merged_image_path}")
    return merged_image_path


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
    shop_image_path = build_shop_image(items)

    if shop_image_path:
        # 构建图片描述
        end_time = datetime.fromtimestamp(end_ts, tz=TZ_BEIJING).strftime("%Y-%m-%d %H:%M") if end_ts else "未知"
        caption = f"🔫 掌瓦每日商店\n\n👤 账号: {nickname}\n⏰ 刷新时间: {end_time}\n\n{'─' * 18}\n🕒 执行时间: {beijing_time_str()}"

        # 直接发送图片
        from notifier import _send_telegram_photo
        _send_telegram_photo(caption, shop_image_path)
        # 清理临时文件
        if os.path.exists(shop_image_path):
            os.remove(shop_image_path)
        log_info("推送完成: 商店图片")
    else:
        # 图片生成失败，回退到文字报告
        log_warning("商店图片生成失败，使用文字报告")
        report = build_report(items, nickname, end_ts)
        notify_send("🔫 掌瓦每日商店", report)
        log_info("推送完成: 文字报告")


if __name__ == "__main__":
    main()

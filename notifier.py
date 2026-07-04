#!/usr/bin/env python3
# QinglongScripts 统一通知模块
# 支持 23 种通知渠道，自动检测已配置渠道并全部推送。
#
# 核心 API:
#     send(title, content)        — 文本通知
#     send_file(title, content, file_path) — 带附件的通知
#
# 渠道配置详见 env.json

import base64
import hashlib
import hmac
import json
import os
import smtplib
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ---------- 本地日志 fallback（不依赖 utils.py）----------
def _log(emoji: str, msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {emoji} {msg}")


def _log_error(msg: str):
    _log("❌", msg)


# ---------- 一言 ----------
def _fetch_hitokoto() -> str:
    if os.environ.get("HITOKOTO", "").strip().lower() == "false":
        return ""
    try:
        resp = requests.get("https://v1.hitokoto.cn/", timeout=3)
        if resp.ok:
            data = resp.json()
            line = data.get("hitokoto", "")
            source = data.get("from", "")
            return f"\n📜 {line}" + (f" —— {source}" if source else "")
    except Exception:
        pass
    return ""


# ---------- 核心调度 ----------
def send(title: str, content: str) -> None:
    """发送通知到所有已配置渠道"""
    full_text = f"{title}\n\n{content}"

    hitokoto = _fetch_hitokoto()
    if hitokoto:
        full_text += hitokoto

    channels = [
        _send_bark,
        _send_console,
        _send_dingtalk,
        _send_feishu,
        _send_gocqhttp,
        _send_gotify,
        _send_igot,
        _send_serverchan,
        _send_pushdeer,
        _send_synology_chat,
        _send_pushplus,
        _send_weplusbot,
        _send_qmsg,
        _send_qywx_app,
        _send_qywx_bot,
        _send_telegram_text,
        _send_aibotk,
        _send_smtp,
        _send_pushme,
        _send_chronocat,
        _send_webhook,
        _send_ntfy,
        _send_wxpusher,
    ]

    any_success = False
    for chan in channels:
        try:
            if chan(title, content):
                any_success = True
        except Exception as e:
            _log_error(f"通知通道 {chan.__name__} 异常: {e}")

    if not any_success:
        print(f"\n{full_text}\n")


def send_file(title: str, content: str, file_path: str) -> None:
    """发送带附件的通知（Telegram / SMTP 发文件，其余通道发文本）"""
    hitokoto = _fetch_hitokoto()
    full_text = f"{title}\n\n{content}"
    if hitokoto:
        full_text += hitokoto

    # 文件通道
    file_ok = False
    try:
        file_ok = _send_telegram_file(title, content, file_path)
    except Exception:
        pass
    try:
        file_ok = _send_smtp_file(title, content, file_path) or file_ok
    except Exception:
        pass

    # 文本通道（跳过已发文件的 Telegram 和 SMTP）
    skip = {_send_telegram_text, _send_smtp}
    text_channels = [c for c in [
        _send_bark, _send_console, _send_dingtalk, _send_feishu, _send_gocqhttp,
        _send_gotify, _send_igot, _send_serverchan, _send_pushdeer, _send_synology_chat,
        _send_pushplus, _send_weplusbot, _send_qmsg, _send_qywx_app, _send_qywx_bot,
        _send_aibotk, _send_pushme, _send_chronocat, _send_webhook, _send_ntfy, _send_wxpusher,
    ] if c not in skip]

    any_text = False
    for chan in text_channels:
        try:
            if chan(title, content):
                any_text = True
        except Exception:
            pass

    if not file_ok and not any_text:
        print(f"\n{full_text}\n")


# ==================== 1. Bark ====================
def _send_bark(title: str, content: str) -> bool:
    bark_push = os.environ.get("BARK_PUSH", "").strip()
    if not bark_push:
        return False
    url = bark_push.rstrip("/") + "/" + title + "/" + content
    params = {}
    for key in ("BARK_ARCHIVE", "BARK_GROUP", "BARK_SOUND", "BARK_ICON", "BARK_LEVEL", "BARK_URL"):
        val = os.environ.get(key, "").strip()
        if val:
            params[key.lower().replace("bark_", "")] = val
    try:
        resp = requests.get(url, params=params, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 2. 控制台 ====================
def _send_console(title: str, content: str) -> bool:
    if os.environ.get("CONSOLE", "").strip().lower() != "true":
        return False
    print(f"\n{'─' * 40}\n{title}\n{'─' * 40}\n{content}\n{'─' * 40}\n")
    return True


# ==================== 3. 钉钉机器人 ====================
def _send_dingtalk(title: str, content: str) -> bool:
    token = os.environ.get("DD_BOT_TOKEN", "").strip()
    secret = os.environ.get("DD_BOT_SECRET", "").strip()
    if not token or not secret:
        return False
    ts = str(round(time.time() * 1000))
    sign = base64.b64encode(
        hmac.new(secret.encode(), (ts + "\n" + secret).encode(), hashlib.sha256).digest()
    ).decode()
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}&timestamp={ts}&sign={sign}"
    payload = {"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 4. 飞书机器人 ====================
def _send_feishu(title: str, content: str) -> bool:
    fskey = os.environ.get("FSKEY", "").strip()
    if not fskey:
        return False
    url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{fskey}"
    payload = {"msg_type": "text", "content": {"text": f"{title}\n\n{content}"}}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 5. go-cqhttp ====================
def _send_gocqhttp(title: str, content: str) -> bool:
    gobot_url = os.environ.get("GOBOT_URL", "").strip()
    gobot_qq = os.environ.get("GOBOT_QQ", "").strip()
    if not gobot_url or not gobot_qq:
        return False
    payload = {"message": f"{title}\n\n{content}"}
    if "group_id" in gobot_qq:
        payload["group_id"] = gobot_qq.split("=")[1]
    else:
        payload["user_id"] = gobot_qq.split("=")[1]
    token = os.environ.get("GOBOT_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.post(gobot_url, json=payload, headers=headers, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 6. Gotify ====================
def _send_gotify(title: str, content: str) -> bool:
    gotify_url = os.environ.get("GOTIFY_URL", "").strip()
    gotify_token = os.environ.get("GOTIFY_TOKEN", "").strip()
    if not gotify_url or not gotify_token:
        return False
    priority = os.environ.get("GOTIFY_PRIORITY", "0").strip()
    url = f"{gotify_url.rstrip('/')}/message?token={gotify_token}"
    data = {"title": title, "message": content, "priority": int(priority)}
    try:
        resp = requests.post(url, data=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 7. iGot ====================
def _send_igot(title: str, content: str) -> bool:
    igot_key = os.environ.get("IGOT_PUSH_KEY", "").strip()
    if not igot_key:
        return False
    url = f"https://push.hellyw.com/{igot_key}"
    data = {"title": title, "content": content}
    try:
        resp = requests.post(url, data=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 8. Server酱 ====================
def _send_serverchan(title: str, content: str) -> bool:
    push_key = os.environ.get("PUSH_KEY", "").strip()
    if not push_key:
        return False
    url = f"https://sctapi.ftqq.com/{push_key}.send"
    data = {"title": title, "desp": content.replace("\n", "\n\n")}
    try:
        resp = requests.post(url, data=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 9. PushDeer ====================
def _send_pushdeer(title: str, content: str) -> bool:
    deer_key = os.environ.get("DEER_KEY", "").strip()
    if not deer_key:
        return False
    deer_url = os.environ.get("DEER_URL", "https://api2.pushdeer.com").strip().rstrip("/")
    url = f"{deer_url}/message/push"
    data = {"pushkey": deer_key, "text": title, "desp": content, "type": "text"}
    try:
        resp = requests.post(url, data=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 10. Synology Chat ====================
def _send_synology_chat(title: str, content: str) -> bool:
    chat_url = os.environ.get("CHAT_URL", "").strip()
    chat_token = os.environ.get("CHAT_TOKEN", "").strip()
    if not chat_url or not chat_token:
        return False
    payload = {"text": f"{title}\n\n{content}"}
    params = {"token": chat_token}
    try:
        resp = requests.post(chat_url, params=params, json=payload, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 11. PushPlus ====================
def _send_pushplus(title: str, content: str) -> bool:
    pushplus_token = os.environ.get("PUSH_PLUS_TOKEN", "").strip()
    if not pushplus_token:
        return False
    url = "http://www.pushplus.plus/send"
    data = {
        "token": pushplus_token,
        "title": title,
        "content": content.replace("\n", "<br>"),
        "template": os.environ.get("PUSH_PLUS_TEMPLATE", "html").strip(),
        "channel": os.environ.get("PUSH_PLUS_CHANNEL", "").strip(),
        "webhook": os.environ.get("PUSH_PLUS_WEBHOOK", "").strip(),
        "callbackUrl": os.environ.get("PUSH_PLUS_CALLBACKURL", "").strip(),
        "to": os.environ.get("PUSH_PLUS_TO", "").strip(),
    }
    user = os.environ.get("PUSH_PLUS_USER", "").strip()
    if user:
        data["user"] = user
    data = {k: v for k, v in data.items() if v}
    try:
        resp = requests.post(url, json=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 12. 微加机器人 ====================
def _send_weplusbot(title: str, content: str) -> bool:
    token = os.environ.get("WE_PLUS_BOT_TOKEN", "").strip()
    if not token:
        return False
    version = os.environ.get("WE_PLUS_BOT_VERSION", "pro").strip()
    receiver = os.environ.get("WE_PLUS_BOT_RECEIVER", "").strip()
    if version == "pro":
        url = f"http://www.botweixin.cn/api/bot_client/send_message?token={token}"
    else:
        url = f"http://www.botweixin.cn/api/bot_client/send_message_lite?token={token}"
    data = {"content": f"{title}\n\n{content}"}
    if receiver:
        data["receiver"] = receiver
    try:
        resp = requests.post(url, json=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 13. Qmsg酱 ====================
def _send_qmsg(title: str, content: str) -> bool:
    qmsg_key = os.environ.get("QMSG_KEY", "").strip()
    if not qmsg_key:
        return False
    qmsg_type = os.environ.get("QMSG_TYPE", "").strip()
    url = f"https://qmsg.zendee.cn/send/{qmsg_key}"
    data = {"msg": f"{title}\n\n{content}"}
    if qmsg_type:
        data["type"] = qmsg_type
    try:
        resp = requests.post(url, data=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 14. 企业微信应用 ====================
def _send_qywx_app(title: str, content: str) -> bool:
    qywx_am = os.environ.get("QYWX_AM", "").strip()
    if not qywx_am:
        return False
    parts = qywx_am.split(",")
    if len(parts) < 4:
        return False
    corpid, corpsecret, touser, agentid = parts[0], parts[1], parts[2], parts[3]
    media_id = parts[4] if len(parts) > 4 else ""
    origin = os.environ.get("QYWX_ORIGIN", "").strip()
    base = origin if origin else "https://qyapi.weixin.qq.com"

    # 获取 access_token
    try:
        token_resp = requests.get(
            f"{base}/cgi-bin/gettoken", params={"corpid": corpid, "corpsecret": corpsecret}, timeout=10
        )
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return False
    except Exception:
        return False

    payload = {
        "touser": touser,
        "agentid": int(agentid),
        "msgtype": "text",
        "text": {"content": f"{title}\n\n{content}"},
    }
    try:
        resp = requests.post(
            f"{base}/cgi-bin/message/send?access_token={access_token}", json=payload, timeout=10
        )
        return resp.ok
    except Exception:
        return False


# ==================== 15. 企业微信机器人 ====================
def _send_qywx_bot(title: str, content: str) -> bool:
    qywx_key = os.environ.get("QYWX_KEY", "").strip()
    if not qywx_key:
        return False
    origin = os.environ.get("QYWX_ORIGIN", "").strip()
    base = origin if origin else "https://qyapi.weixin.qq.com"
    url = f"{base}/cgi-bin/webhook/send?key={qywx_key}"
    payload = {"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 16. Telegram ====================
def _get_telegram_kwargs() -> tuple:
    """返回 (base_url, chat_id, proxy_dict) 或 (None, None, None)"""
    bot_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_USER_ID", "").strip() or os.environ.get("TG_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return None, None, None

    api_host = os.environ.get("TG_API_HOST", "").strip()
    base_url = f"https://{api_host}/bot{bot_token}" if api_host else f"https://api.telegram.org/bot{bot_token}"

    proxies = None
    proxy_host = os.environ.get("TG_PROXY_HOST", "").strip()
    proxy_port = os.environ.get("TG_PROXY_PORT", "").strip()
    if proxy_host and proxy_port:
        proxy_auth = os.environ.get("TG_PROXY_AUTH", "").strip()
        proxy_url = f"http://{proxy_host}:{proxy_port}"
        if proxy_auth:
            proxy_url = f"http://{proxy_auth}@{proxy_host}:{proxy_port}"
        proxies = {"http": proxy_url, "https": proxy_url}

    return base_url, chat_id, proxies


def _send_telegram_text(title: str, content: str) -> bool:
    base_url, chat_id, proxies = _get_telegram_kwargs()
    if not base_url:
        return False
    url = f"{base_url}/sendMessage"
    text = f"{title}\n\n{content}"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    try:
        resp = requests.post(url, data=payload, proxies=proxies, timeout=15)
        return resp.ok
    except Exception:
        return False


def _send_telegram_file(title: str, content: str, file_path: str) -> bool:
    base_url, chat_id, proxies = _get_telegram_kwargs()
    if not base_url or not os.path.isfile(file_path):
        return False
    url = f"{base_url}/sendDocument"
    payload = {"chat_id": chat_id, "caption": f"{title}\n\n{content}"}
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url, data=payload,
                files={"document": (os.path.basename(file_path), f, "application/json")},
                proxies=proxies, timeout=30
            )
        return resp.ok
    except Exception:
        return False


def _send_telegram_photo(title: str, image_source: str) -> bool:
    """通过 Telegram 发送单张图片（支持 URL 或本地文件路径）"""
    base_url, chat_id, proxies = _get_telegram_kwargs()
    if not base_url or not image_source:
        return False
    try:
        # 判断是本地文件还是 URL
        if os.path.isfile(image_source):
            # 本地文件
            with open(image_source, "rb") as f:
                photo_data = f.read()
            filename = os.path.basename(image_source)
        else:
            # URL，下载图片
            img_resp = requests.get(image_source, timeout=15, proxies=proxies)
            if not img_resp.ok:
                return False
            photo_data = img_resp.content
            filename = "image.png"

        url = f"{base_url}/sendPhoto"
        resp = requests.post(url, data={"chat_id": chat_id, "caption": title},
                            files={"photo": (filename, photo_data, "image/png")},
                            proxies=proxies, timeout=20)
        return resp.json().get("ok", False)
    except Exception:
        return False


# ==================== 17. 智能微秘书 ====================
def _send_aibotk(title: str, content: str) -> bool:
    aibotk_key = os.environ.get("AIBOTK_KEY", "").strip()
    aibotk_type = os.environ.get("AIBOTK_TYPE", "").strip()
    aibotk_name = os.environ.get("AIBOTK_NAME", "").strip()
    if not aibotk_key or not aibotk_type or not aibotk_name:
        return False
    url = f"https://api-bot.aibotk.com/openapi/v1/chat/msg"
    headers = {"Authorization": f"Bearer {aibotk_key}"}
    data = {"type": aibotk_type, "name": aibotk_name, "msg": f"{title}\n\n{content}"}
    try:
        resp = requests.post(url, json=data, headers=headers, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 18. SMTP 邮件 ====================
def _build_smtp_message(title: str, content: str) -> MIMEText:
    msg = MIMEText(content, "plain", "utf-8")
    msg["From"] = os.environ.get("SMTP_NAME", "") or os.environ.get("SMTP_EMAIL", "")
    msg["To"] = os.environ.get("SMTP_EMAIL", "")
    msg["Subject"] = title
    return msg


def _send_smtp(title: str, content: str) -> bool:
    server = os.environ.get("SMTP_SERVER", "").strip()
    email = os.environ.get("SMTP_EMAIL", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not server or not email or not password:
        return False
    use_ssl = os.environ.get("SMTP_SSL", "false").strip().lower() == "true"

    try:
        msg = _build_smtp_message(title, content)
        host, _, port_str = server.partition(":")
        port = int(port_str) if port_str else (465 if use_ssl else 25)
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(host, port, timeout=10) as s:
            if not use_ssl:
                s.starttls()
            s.login(email, password)
            s.send_message(msg)
        return True
    except Exception:
        return False


def _send_smtp_file(title: str, content: str, file_path: str) -> bool:
    server = os.environ.get("SMTP_SERVER", "").strip()
    email = os.environ.get("SMTP_EMAIL", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not server or not email or not password or not os.path.isfile(file_path):
        return False
    use_ssl = os.environ.get("SMTP_SSL", "false").strip().lower() == "true"

    try:
        msg = MIMEMultipart()
        msg["From"] = os.environ.get("SMTP_NAME", "") or email
        msg["To"] = email
        msg["Subject"] = title
        msg.attach(MIMEText(content, "plain", "utf-8"))
        with open(file_path, "rb") as f:
            attachment = MIMEApplication(f.read())
            attachment.add_header("Content-Disposition", "attachment", filename=os.path.basename(file_path))
            msg.attach(attachment)

        host, _, port_str = server.partition(":")
        port = int(port_str) if port_str else (465 if use_ssl else 25)
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(host, port, timeout=30) as s:
            if not use_ssl:
                s.starttls()
            s.login(email, password)
            s.send_message(msg)
        return True
    except Exception:
        return False


# ==================== 19. PushMe ====================
def _send_pushme(title: str, content: str) -> bool:
    pushme_key = os.environ.get("PUSHME_KEY", "").strip()
    if not pushme_key:
        return False
    pushme_url = os.environ.get("PUSHME_URL", "https://push.i-i.me").strip().rstrip("/")
    url = f"{pushme_url}/{pushme_key}"
    data = {"title": title, "content": content}
    try:
        resp = requests.post(url, data=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 20. CHRONOCAT ====================
def _send_chronocat(title: str, content: str) -> bool:
    chronocat_url = os.environ.get("CHRONOCAT_URL", "").strip()
    chronocat_qq = os.environ.get("CHRONOCAT_QQ", "").strip()
    if not chronocat_url or not chronocat_qq:
        return False
    payload = {"message": f"{title}\n\n{content}"}
    if "group_id" in chronocat_qq:
        payload["group_id"] = chronocat_qq.split("=")[1]
    else:
        payload["user_id"] = chronocat_qq.split("=")[1]
    token = os.environ.get("CHRONOCAT_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = requests.post(chronocat_url, json=payload, headers=headers, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 21. 自定义 Webhook ====================
def _send_webhook(title: str, content: str) -> bool:
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    webhook_method = os.environ.get("WEBHOOK_METHOD", "POST").strip().upper()
    if not webhook_url or not webhook_method:
        return False
    webhook_body = os.environ.get("WEBHOOK_BODY", "").strip()
    if webhook_body:
        body = webhook_body.replace("\\n", "\n").replace("{title}", title).replace("{content}", content)
    else:
        body = f"{title}\n\n{content}"
    headers_str = os.environ.get("WEBHOOK_HEADERS", "").strip()
    headers = {}
    if headers_str:
        try:
            headers = json.loads(headers_str)
        except Exception:
            pass
    content_type = os.environ.get("WEBHOOK_CONTENT_TYPE", "").strip()
    try:
        if webhook_method == "GET":
            resp = requests.get(webhook_url, params={"title": title, "content": content}, headers=headers, timeout=10)
        else:
            if content_type:
                headers["Content-Type"] = content_type
            resp = requests.post(webhook_url, data=body.encode("utf-8"), headers=headers, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 22. ntfy ====================
def _send_ntfy(title: str, content: str) -> bool:
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not ntfy_topic:
        return False
    ntfy_url = os.environ.get("NTFY_URL", "https://ntfy.sh").strip().rstrip("/")
    priority = os.environ.get("NTFY_PRIORITY", "3").strip()
    url = f"{ntfy_url}/{ntfy_topic}"
    headers = {"Title": title, "Priority": priority}
    try:
        resp = requests.post(url, data=content.encode("utf-8"), headers=headers, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 23. WxPusher ====================
def _send_wxpusher(title: str, content: str) -> bool:
    app_token = os.environ.get("WXPUSHER_APP_TOKEN", "").strip()
    if not app_token:
        return False
    topic_ids = os.environ.get("WXPUSHER_TOPIC_IDS", "").strip()
    uids = os.environ.get("WXPUSHER_UIDS", "").strip()
    if not topic_ids and not uids:
        return False
    url = "https://wxpusher.zjiecode.com/api/send/message"
    data = {
        "appToken": app_token,
        "content": f"{title}\n\n{content}",
        "contentType": 1,
        "summary": title[:50],
    }
    if topic_ids:
        data["topicIds"] = [int(t) for t in topic_ids.split(";") if t.strip().isdigit()]
    if uids:
        data["uids"] = [u.strip() for u in uids.split(";") if u.strip()]
    try:
        resp = requests.post(url, json=data, timeout=10)
        return resp.ok
    except Exception:
        return False


# ==================== 图片推送 ====================

def send_photos(title: str, text_content: str, photos: list, caption_with_title: bool = True) -> None:
    """
    发送图片通知：文本推送到所有通道，图片仅 Telegram。
    photos: [{"image": "url或本地路径", "caption": "图片说明"}, ...]
    caption_with_title: 是否在图片说明中包含标题
    """
    # 文本推送所有通道
    send(title, text_content)

    # 图片仅 Telegram
    for i, photo in enumerate(photos):
        image = photo.get("image", "")
        caption = photo.get("caption", f"Photo {i+1}")
        if caption_with_title:
            full_caption = f"{title}\n{caption}"
        else:
            full_caption = caption
        try:
            _send_telegram_photo(full_caption, image)
        except Exception:
            pass

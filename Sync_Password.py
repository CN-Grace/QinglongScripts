#!/usr/bin/env python3
"""
cron: 0 0 * * *
new Env("Bitwarden备份")
Bitwarden 自动备份脚本（带哈希比较，避免重复同步）
- 自动登录 Bitwarden 服务器并获取所有密码数据
- 将密码数据保存到本地 JSON 文件
- 比较今天与昨天的备份文件哈希，若无变化则跳过 WebDAV 同步
- 将备份文件同步到 WebDAV 服务器（仅当有更新时）
"""

import os
import json
import hashlib
import requests
import datetime
from requests.auth import HTTPBasicAuth
from typing import Dict, Any, Tuple

from utils import log_info, log_success, log_warning, log_error, beijing_now, beijing_time_str
from notifier import send as notify_send, send_file as notify_send_file

# ==================== 用户配置 ====================
BITWARDEN_SERVER = os.environ.get("BITWARDEN_SERVER", "")
BITWARDEN_USERNAME = os.environ.get("BITWARDEN_USERNAME", "")
BITWARDEN_PASSWORD = os.environ.get("BITWARDEN_PASSWORD", "")
WEBDAV_SERVER = os.environ.get("WEBDAV_SERVER", "")
WEBDAV_USERNAME = os.environ.get("WEBDAV_USERNAME", "")
WEBDAV_PASSWORD = os.environ.get("WEBDAV_PASSWORD", "")
WEBDAV_REMOTE_DIR = os.environ.get("WEBDAV_REMOTE_DIR", "")
LOCAL_BACKUP_PATH = os.environ.get("SYNC_LOCAL_DIR", "Password/")
BACKUP_FILENAME_FORMAT = "%Y-%m-%d_bitwarden_backup.json"


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json, text/plain, */*"})
    return session


def bitwarden_login(session: requests.Session) -> Tuple[bool, str]:
    login_url = f"{BITWARDEN_SERVER.rstrip('/')}/identity/connect/token"
    login_data = {
        "scope": "api offline_access", "client_id": "web", "deviceType": "12",
        "deviceIdentifier": "65ff3d73-fd5f-4835-8a50-fa7f41581f48", "deviceName": "edge",
        "grant_type": "password", "username": BITWARDEN_USERNAME, "password": BITWARDEN_PASSWORD,
    }
    try:
        log_info(f"正在登录 Bitwarden: {BITWARDEN_SERVER}")
        resp = session.post(login_url, data=login_data, timeout=30)
        resp.raise_for_status()
        access_token = resp.json().get("access_token")
        if access_token:
            session.headers.update({"Authorization": f"Bearer {access_token}"})
            log_success("Bitwarden 登录成功")
            return True, "✅ Bitwarden 登录成功"
        log_error("响应中未找到 access_token")
        return False, "❌ Bitwarden 登录失败: 未找到 access_token"
    except Exception as e:
        log_error(f"Bitwarden 登录失败: {e}")
        return False, f"❌ Bitwarden 登录失败: {e}"


def get_bitwarden_data(session: requests.Session) -> Tuple[bool, Any, str]:
    sync_url = f"{BITWARDEN_SERVER.rstrip('/')}/api/sync"
    try:
        log_info("正在获取 Bitwarden 数据...")
        resp = session.get(sync_url, params={"excludeDomains": "true"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, dict):
            item_count = len(data.get("ciphers", []))
            log_success(f"成功获取数据 ({item_count} 个密码项)")
            return True, data, f"✅ 成功获取数据 ({item_count} 个密码项)"
        return False, None, "⚠️ 数据为空或格式不正确"
    except Exception as e:
        log_error(f"获取数据失败: {e}")
        return False, None, f"❌ 获取数据失败: {e}"


def save_backup_locally(data: Dict[str, Any]) -> Tuple[bool, str, str]:
    if not os.path.exists(LOCAL_BACKUP_PATH):
        os.makedirs(LOCAL_BACKUP_PATH, exist_ok=True)
    today = beijing_now()
    filename = today.strftime(BACKUP_FILENAME_FORMAT)
    filepath = os.path.join(LOCAL_BACKUP_PATH, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        file_size = os.path.getsize(filepath) / (1024 * 1024)
        log_success(f"本地备份: {filename} ({file_size:.2f} MB)")
        return True, filepath, f"✅ 本地备份成功: {filename} ({file_size:.2f} MB)"
    except Exception as e:
        log_error(f"本地备份失败: {e}")
        return False, "", f"❌ 本地备份失败: {e}"


def get_file_hash(filepath: str) -> str:
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def sync_to_webdav(local_filepath: str) -> Tuple[bool, str]:
    if not os.path.exists(local_filepath):
        return False, "❌ WebDAV 同步失败: 本地文件不存在"
    filename = os.path.basename(local_filepath)
    remote_dir = WEBDAV_REMOTE_DIR.rstrip("/") if WEBDAV_REMOTE_DIR else "123Pan/Password"
    remote_path = f"{WEBDAV_SERVER.rstrip('/')}/{remote_dir}/{filename}"
    try:
        with open(local_filepath, "rb") as f:
            file_data = f.read()
        log_info(f"正在上传到 WebDAV: {remote_path}")
        resp = requests.put(url=remote_path, data=file_data, auth=HTTPBasicAuth(WEBDAV_USERNAME, WEBDAV_PASSWORD), headers={"Content-Type": "application/octet-stream"}, timeout=60)
        if resp.status_code in (200, 201, 204):
            log_success("WebDAV 同步成功")
            return True, f"✅ WebDAV 同步成功: {filename}"
        return False, f"❌ WebDAV 同步失败: 状态码 {resp.status_code}"
    except Exception as e:
        log_error(f"WebDAV 同步失败: {e}")
        return False, f"❌ WebDAV 同步失败: {e}"


def build_report_content(log_messages: list) -> str:
    lines = [
        f"📅 备份时间: {beijing_time_str()}",
        f"👤 账户: {BITWARDEN_USERNAME}", "",
        "─" * 18, "",
        "📋 备份步骤状态:",
    ]
    for i, msg in enumerate(log_messages, 1):
        lines.append(f"  {i}. {msg}")

    success_count = sum(1 for m in log_messages if m.startswith("✅"))
    info_count = sum(1 for m in log_messages if m.startswith("ℹ️") or m.startswith("⚠️"))
    skip_count = sum(1 for m in log_messages if m.startswith("⏭️"))

    lines.extend(["", "─" * 18, f"📊 统计: 成功 {success_count} | 信息 {info_count} | 跳过 {skip_count} | 总计 {len(log_messages)}", "", f"🕒 执行时间: {beijing_time_str()}"])
    return "\n".join(lines)


def perform_backup() -> Tuple[bool, list, str, bool]:
    log_messages = []
    backup_filepath = None
    has_update = True

    session = create_session()
    login_success, login_msg = bitwarden_login(session)
    log_messages.append(login_msg)
    if not login_success:
        return False, log_messages, None, has_update

    data_success, bitwarden_data, data_msg = get_bitwarden_data(session)
    log_messages.append(data_msg)
    if not data_success or bitwarden_data is None:
        return False, log_messages, None, has_update

    save_success, filepath, save_msg = save_backup_locally(bitwarden_data)
    log_messages.append(save_msg)
    backup_filepath = filepath if save_success else None

    if save_success and backup_filepath:
        today = beijing_now()
        yesterday = today - datetime.timedelta(days=1)
        yesterday_filename = yesterday.strftime(BACKUP_FILENAME_FORMAT)
        yesterday_filepath = os.path.join(LOCAL_BACKUP_PATH, yesterday_filename)
        if os.path.exists(yesterday_filepath):
            if get_file_hash(backup_filepath) == get_file_hash(yesterday_filepath):
                has_update = False
                log_info("备份文件与昨天相同，无更新")
                log_messages.append("ℹ️ 备份文件与昨日一致，无新内容")
            else:
                log_info("备份文件有更新")
        else:
            log_info("昨日备份不存在，视为有更新")
        if has_update:
            sync_success, sync_msg = sync_to_webdav(backup_filepath)
            log_messages.append(sync_msg)
        else:
            log_messages.append("⏭️ WebDAV 同步跳过: 文件无变化")
    else:
        log_messages.append("⏭️ WebDAV 同步跳过: 本地备份失败")

    all_success = all(msg.startswith("✅") or msg.startswith("⏭️") or msg.startswith("ℹ️") for msg in log_messages)
    return all_success, log_messages, backup_filepath, has_update


def main():
    log_info("=" * 50)
    log_info("Bitwarden 自动备份脚本开始执行")
    log_info("=" * 50)

    start_time = beijing_now()
    backup_success, log_messages, backup_filepath, has_update = perform_backup()

    title = f"Bitwarden 备份报告"
    content = build_report_content(log_messages)

    if has_update and backup_filepath and os.path.exists(backup_filepath):
        notify_send_file(title, content, backup_filepath)
    else:
        notify_send(title, content)

    print(f"\n{'=' * 60}\nBitwarden 备份完成报告:\n{'=' * 60}")
    for i, msg in enumerate(log_messages, 1):
        print(f"{i}. {msg}")
    print(f"\n执行时间: {(beijing_now() - start_time).total_seconds():.2f}秒")
    print("=" * 60)

    return 0 if backup_success else 1


if __name__ == "__main__":
    import sys
    try:
        exit_code = main()
    except KeyboardInterrupt:
        log_info("用户中断执行")
        exit_code = 130
    except Exception as e:
        log_error(f"脚本异常: {e}")
        exit_code = 1
    log_info(f"脚本执行结束，退出代码: {exit_code}")
    sys.exit(exit_code)

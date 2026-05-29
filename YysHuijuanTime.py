#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 0 8 * * *
new Env("阴阳师绘卷时间获取")
从阴阳师官网获取最新绘卷活动时间信息
"""
import json
import os
import re
import requests
from datetime import datetime

from utils import log_info, log_success, log_warning, log_error
from notifier import send as notify_send

# 活动时间JSON路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIVITY_JSON = os.path.join(SCRIPT_DIR, "yys_huijuan_time.json")

# 公告列表页
UPDATE_LIST_URL = "https://yys.163.com/news/update/"

report_data = {}


def get_latest_announcements():
    """获取最新公告列表"""
    log_info("获取公告列表...")
    try:
        resp = requests.get(UPDATE_LIST_URL, timeout=15)
        resp.encoding = 'utf-8'
        log_info(f"请求状态码: {resp.status_code}")
        # 提取公告链接和标题
        pattern = r'<a[^>]+href="(?:(?:https?:)?//yys\.163\.com)?/news/update/(\d+/\d+_\d+\.html)"[^>]*title="([^"]*)"'
        matches = re.findall(pattern, resp.text)
        # 过滤出更新公告
        announcements = []
        for path, title in matches:
            title = title.strip()
            if "维护更新公告" in title:
                url = f"https://yys.163.com/news/update/{path}"
                announcements.append({"url": url, "title": title})
        log_info(f"找到 {len(announcements)} 条维护更新公告")
        return announcements[:5]
    except Exception as e:
        log_error(f"获取公告列表失败: {e}")
        return []


def get_announcement_content(url):
    """获取公告内容"""
    try:
        log_info(f"获取公告内容: {url}")
        resp = requests.get(url, timeout=15)
        resp.encoding = 'utf-8'
        # 提取正文内容
        patterns = [
            r'<div[^>]*class="artText"[^>]*>(.*?)</div>',
            r'<div[^>]*id="NIE-art"[^>]*>(.*?)</div>',
            r'<div[^>]*class="art-con"[^>]*>(.*?)</div>',
        ]
        for pattern in patterns:
            match = re.search(pattern, resp.text, re.DOTALL)
            if match:
                content = re.sub(r'<[^>]+>', '\n', match.group(1))
                content = re.sub(r'\n+', '\n', content).strip()
                if len(content) > 100:
                    log_info(f"提取到内容，长度: {len(content)}")
                    return content
        log_error("未找到公告内容")
        return ""
    except Exception as e:
        log_error(f"获取公告内容失败: {e}")
        return ""


def parse_huijuan_time(content, title):
    """解析绘卷活动时间"""
    log_info("解析绘卷活动时间...")
    # 查找绘卷相关内容
    huijuan_pattern = r'((?:SP|SSR|SR)?[一-龥]+追忆绘卷活动)'
    match = re.search(huijuan_pattern, content)
    if not match:
        huijuan_pattern2 = r'([\w\W]{0,50}追忆绘卷活动)'
        match = re.search(huijuan_pattern2, content)

    if not match:
        log_error("未找到追忆绘卷活动相关内容")
        return None

    name = match.group(1).strip()
    name = re.sub(r'[\n\r]+', '', name).strip()
    if len(name) > 30:
        name = name[-30:]
    log_info(f"找到绘卷活动: {name}")

    # 获取绘卷活动周围的文本用于提取时间
    start_pos = max(0, match.start() - 100)
    end_pos = min(len(content), match.end() + 300)
    huijuan_text = content[start_pos:end_pos]

    # 解析时间
    time_pattern = r'(\d{1,2})月(\d{1,2})日维护后\s*[-—~]\s*(\d{1,2})月(\d{1,2})日(\d{1,2}:\d{2})'
    time_match = re.search(time_pattern, huijuan_text)
    if not time_match:
        time_pattern2 = r'(\d{1,2})月(\d{1,2})日[^\d]*[-—~][^\d]*(\d{1,2})月(\d{1,2})日\s*(\d{1,2}:\d{2})'
        time_match = re.search(time_pattern2, huijuan_text)

    if time_match:
        start_month = int(time_match.group(1))
        start_day = int(time_match.group(2))
        end_month = int(time_match.group(3))
        end_day = int(time_match.group(4))
        end_time = time_match.group(5)

        # 从标题提取年份
        year_match = re.search(r'(\d{4})年', title)
        year = int(year_match.group(1)) if year_match else datetime.now().year

        if end_month < start_month:
            end_year = year + 1
        else:
            end_year = year

        start_date = f"{year}-{start_month:02d}-{start_day:02d}"
        end_date = f"{end_year}-{end_month:02d}-{end_day:02d} {end_time}"

        log_success(f"解析成功: {name} {start_date} - {end_date}")
        return {
            "name": name,
            "start_time": start_date,
            "end_time": end_date,
            "start_note": "维护后",
            "end_note": end_time
        }
    log_error("未找到时间信息")
    return None


def load_existing_data():
    """加载现有数据"""
    try:
        if os.path.exists(ACTIVITY_JSON):
            with open(ACTIVITY_JSON, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        log_error(f"加载JSON失败: {e}")
    return {"last_update": "", "latest_announcement": {}, "huijuan_activity": {}, "history": []}


def save_data(data):
    """保存数据到JSON文件"""
    try:
        with open(ACTIVITY_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log_success(f"数据已保存到: {ACTIVITY_JSON}")
        if os.path.exists(ACTIVITY_JSON):
            file_size = os.path.getsize(ACTIVITY_JSON)
            log_info(f"文件大小: {file_size} bytes")
    except Exception as e:
        log_error(f"保存JSON失败: {e}")


def build_report(data):
    """构建报告"""
    lines = [
        "🎮 阴阳师绘卷活动时间查询报告",
        "",
        f"📋 活动名称: {data.get('活动名称', '未知')}",
        f"📅 开始时间: {data.get('开始时间', '未知')} (维护后)",
        f"📅 结束时间: {data.get('结束时间', '未知')}",
        "",
        f"📰 最新公告: {data.get('最新公告', '未知')}",
        "",
        "─" * 18,
        f"🕒 查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return "\n".join(lines)


def main():
    global report_data
    report_data = {}

    log_info("=" * 40)
    log_info("阴阳师绘卷活动时间获取")
    log_info("=" * 40)

    log_info(f"JSON文件路径: {ACTIVITY_JSON}")

    # 加载现有数据
    data = load_existing_data()

    # 获取公告列表
    announcements = get_latest_announcements()
    if not announcements:
        log_error("未找到公告")
        return

    # 遍历公告查找绘卷活动
    for ann in announcements:
        log_info(f"检查公告: {ann['title']}")
        content = get_announcement_content(ann['url'])
        if not content:
            continue

        huijuan_info = parse_huijuan_time(content, ann['title'])
        if huijuan_info:
            log_success(f"找到绘卷活动: {huijuan_info['name']}")
            log_success(f"开始时间: {huijuan_info['start_time']}")
            log_success(f"结束时间: {huijuan_info['end_time']}")

            # 更新数据
            today = datetime.now().strftime("%Y-%m-%d")
            data["last_update"] = today
            data["latest_announcement"] = {
                "title": ann['title'],
                "url": ann['url'],
                "date": today
            }
            data["huijuan_activity"] = huijuan_info

            history_entry = {
                "date": today,
                "name": huijuan_info['name'],
                "start": huijuan_info['start_time'],
                "end": huijuan_info['end_time']
            }
            if not any(h['name'] == huijuan_info['name'] and h['start'] == huijuan_info['start_time'] for h in data['history']):
                data['history'].append(history_entry)

            save_data(data)

            # 构建报告数据
            report_data["活动名称"] = huijuan_info['name']
            report_data["开始时间"] = huijuan_info['start_time']
            report_data["结束时间"] = huijuan_info['end_time']
            report_data["最新公告"] = ann['title']

            log_success("========== 查询完成 ==========")
            for k, v in report_data.items():
                log_info(f"{k}: {v}")

            # 发送通知
            tg_text = build_report(report_data)
            notify_send("阴阳师绘卷活动时间", tg_text)
            return

    log_error("未找到绘卷活动信息")


if __name__ == "__main__":
    main()

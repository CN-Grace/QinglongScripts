#!/usr/bin/env python3
"""
cron: 0 0 * * *
new Env("百度贴吧签到")
百度贴吧 每日自动签到脚本
- 获取用户登录状态
- 获取关注的贴吧列表（含等级、经验值）
- 对每个贴吧进行签到
- 统计签到结果和等级分布
- 签到失败时保存调试数据到 debug 目录
"""

import hashlib
import json
import os
import random
import time
import requests
from datetime import datetime
from typing import Optional, List, Dict, Any

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send

# 调试文件保存目录
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")

# ==================== 用户配置 ====================
TIEBA_COOKIE = os.environ.get("TIEBA_COOKIE", "")


def create_session(cookie: str) -> requests.Session:
    """创建带 Cookie 的 requests.Session（贴吧专用 User-Agent 和 Host）"""
    session = requests.Session()
    session.headers.update({
        "Host": "tieba.baidu.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.71 Safari/537.36",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    })
    cookie_dict = {item.split("=")[0]: item.split("=")[1] for item in cookie.split("; ") if "=" in item}
    requests.utils.add_dict_to_cookiejar(session.cookies, cookie_dict)
    return session


def encode_data(data: Dict, sign_key: str = "tiebaclient!!!") -> Dict:
    """对请求数据进行签名"""
    s = ""
    for key in sorted(data.keys()):
        s += f"{key}={data[key]}"
    sign = hashlib.md5((s + sign_key).encode("utf-8")).hexdigest().upper()
    data.update({"sign": sign})
    return data


def request(session: requests.Session, url: str, method: str = "get", data: Optional[Dict] = None, retry: int = 3) -> Dict:
    """带重试的请求函数"""
    for i in range(retry):
        try:
            if method.lower() == "get":
                response = session.get(url, timeout=10)
            else:
                response = session.post(url, data=data, timeout=10)
            response.raise_for_status()
            if not response.text.strip():
                raise ValueError("空响应内容")
            return response.json()
        except Exception as e:
            if i == retry - 1:
                raise Exception(f"请求失败: {e!s}")
            wait_time = 1.5 * (2 ** i) + random.uniform(0, 1)
            time.sleep(wait_time)
    raise Exception(f"请求失败，已达最大重试次数 {retry}")


def save_debug_data(forum_name: str, request_data: Dict, response_data: Dict, error_code: str):
    """保存签到请求和响应数据到 debug 文件夹"""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{forum_name}_{error_code}_{timestamp}.json"
        filepath = os.path.join(DEBUG_DIR, filename)

        debug_data = {
            "forum_name": forum_name,
            "error_code": error_code,
            "timestamp": timestamp,
            "request": request_data,
            "response": response_data,
        }

        # 移除敏感信息
        if "BDUSS" in debug_data["request"]:
            debug_data["request"]["BDUSS"] = "***"
        if "sign" in debug_data["request"]:
            debug_data["request"]["sign"] = "***"

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(debug_data, f, ensure_ascii=False, indent=2)
        log_info(f"调试数据已保存: {filepath}")
    except Exception as e:
        log_error(f"保存调试数据失败: {e}")


# ---------- 核心功能 ----------
def get_user_info(session: requests.Session) -> tuple:
    """获取用户登录信息，返回 (tbs, user_name) 或 (False, 错误信息)"""
    try:
        result = request(session, "http://tieba.baidu.com/dc/common/tbs")
        if result.get("is_login", 0) == 0:
            return False, "登录失败，Cookie 异常"
        tbs = result.get("tbs", "")
        try:
            user_info = request(session, "https://tieba.baidu.com/f/user/json_userinfo")
            user_data = user_info.get("data", "")
            user_name = user_data.get("show_nickname", "未知用户") if isinstance(user_data, dict) else "未知用户"
        except Exception:
            user_name = "未知用户"
        return tbs, user_name
    except Exception as e:
        return False, f"登录验证异常: {e}"


def get_favorite(session: requests.Session, bduss: str) -> List[Dict]:
    """获取用户关注的贴吧列表，包含等级信息"""
    forums = []
    page_no = 1
    like_url = "http://c.tieba.baidu.com/c/f/forum/like"

    while True:
        data = encode_data({
            "BDUSS": bduss,
            "_client_type": "2",
            "_client_id": "wappc_1534235498291_488",
            "_client_version": "9.7.8.0",
            "_phone_imei": "000000000000000",
            "from": "1008621y",
            "page_no": str(page_no),
            "page_size": "200",
            "model": "MI+5",
            "net_type": "1",
            "timestamp": str(int(time.time())),
            "vcode_tag": "11",
        })

        try:
            res = request(session, like_url, "post", data)
            if "forum_list" in res:
                for forum_type in ["non-gconforum", "gconforum"]:
                    if forum_type in res["forum_list"]:
                        items = res["forum_list"][forum_type]
                        if isinstance(items, list):
                            for f in items:
                                f["_is_signed"] = (forum_type == "gconforum")
                            forums.extend(items)
                        elif isinstance(items, dict):
                            items["_is_signed"] = (forum_type == "gconforum")
                            forums.append(items)
            if res.get("has_more") != "1":
                break
            page_no += 1
            time.sleep(random.uniform(1, 2))
        except Exception as e:
            log_error(f"获取贴吧列表出错: {e}")
            break

    log_info(f"共获取到 {len(forums)} 个关注的贴吧")
    return forums


def build_level_summary(forums: List[Dict]) -> Dict:
    """从贴吧列表中提取等级汇总"""
    total_exp = 0
    level_stats = {}
    forum_levels = []

    for f in forums:
        name = f.get("name", "")
        level_id = int(f.get("level_id", 0))
        cur_score = int(f.get("cur_score", 0))
        levelup_score = int(f.get("levelup_score", 0))
        total_exp += cur_score

        if level_id not in level_stats:
            level_stats[level_id] = {"count": 0, "total_exp": 0}
        level_stats[level_id]["count"] += 1
        level_stats[level_id]["total_exp"] += cur_score

        forum_levels.append({
            "name": name,
            "level_id": level_id,
            "cur_score": cur_score,
            "levelup_score": levelup_score,
            "exp_percent": round(cur_score / levelup_score * 100, 1) if levelup_score > 0 else 0,
        })

    sorted_levels = sorted(level_stats.items(), key=lambda x: x[0], reverse=True)
    return {
        "forum_count": len(forums),
        "total_exp": total_exp,
        "level_stats": sorted_levels,
        "forum_levels": forum_levels,
    }


def sign_forums(session: requests.Session, bduss: str, forums: List[Dict], tbs: str) -> Dict[str, Any]:
    """对贴吧列表进行签到"""
    success_count = 0
    error_count = 0
    exist_count = 0
    shield_count = 0
    total = len(forums)
    details = []

    log_info(f"开始签到 {total} 个贴吧")

    base_data = {
        "_client_type": "2",
        "_client_version": "9.7.8.0",
        "_phone_imei": "000000000000000",
        "model": "MI+5",
        "net_type": "1",
    }

    last_request_time = time.time()
    for idx, forum in enumerate(forums):
        elapsed = time.time() - last_request_time
        delay = max(0, 1.0 + random.uniform(0.5, 1.5) - elapsed)
        time.sleep(delay)
        last_request_time = time.time()

        if (idx + 1) % 10 == 0:
            extra_delay = random.uniform(5, 10)
            log_info(f"已签到 {idx + 1}/{total} 个贴吧，休息 {extra_delay:.2f} 秒")
            time.sleep(extra_delay)

        forum_name = forum.get("name", "")
        forum_id = forum.get("id", "")
        level_id = forum.get("level_id", "?")
        log_prefix = f"【{forum_name}】吧(Lv.{level_id})({idx + 1}/{total})"

        try:
            data = base_data.copy()
            data.update({"BDUSS": bduss, "fid": forum_id, "kw": forum_name, "tbs": tbs, "timestamp": str(int(time.time()))})
            data = encode_data(data)

            result = request(session, "http://c.tieba.baidu.com/c/c/forum/sign", "post", data)
            error_code = result.get("error_code", "")
            rank = None

            if error_code == "0":
                success_count += 1
                if "user_info" in result:
                    rank = result["user_info"].get("user_sign_rank")
                    log_success(f"{log_prefix} 签到成功，第{rank}个签到" if rank else f"{log_prefix} 签到成功")
                else:
                    log_success(f"{log_prefix} 签到成功")
                details.append({"name": forum_name, "status": "success", "rank": rank, "level": level_id})
            elif error_code == "160002":
                exist_count += 1
                log_warning(f"{log_prefix} {result.get('error_msg', '今日已签到')}")
                details.append({"name": forum_name, "status": "exist", "rank": None, "level": level_id})
            elif error_code == "340006":
                shield_count += 1
                log_warning(f"{log_prefix} 贴吧已被屏蔽")
                details.append({"name": forum_name, "status": "shield", "rank": None, "level": level_id})
            else:
                error_count += 1
                log_error(f"{log_prefix} 签到失败，错误码: {error_code}，信息: {result.get('error_msg', '未知错误')}")
                details.append({"name": forum_name, "status": "error", "rank": None, "level": level_id})
                # 保存调试数据
                save_debug_data(forum_name, data, result, error_code)
        except Exception as e:
            error_count += 1
            log_error(f"{log_prefix} 签到异常: {e!s}")
            details.append({"name": forum_name, "status": "error", "rank": None, "level": level_id})
            # 保存异常调试数据
            save_debug_data(forum_name, data if 'data' in locals() else {}, {"exception": str(e)}, "exception")

    return {"total": total, "success": success_count, "exist": exist_count, "shield": shield_count, "error": error_count, "details": details}


def build_report(stats: Dict, user_name: str, details: List[Dict], level_summary: Optional[Dict] = None) -> str:
    """构建签到报告"""
    lines = ["📢 百度贴吧 签到报告", "", f"👤 账号: {user_name}", ""]

    lines.append("📊 签到统计")
    lines.append(f"📌 贴吧总数: {stats['total']}")
    lines.append(f"✅ 签到成功: {stats['success']}")
    lines.append(f"⚠️ 已经签到: {stats['exist']}")
    lines.append(f"🚫 被屏蔽的: {stats['shield']}")
    lines.append(f"❌ 签到失败: {stats['error']}")

    if level_summary:
        lines.append("")
        lines.append("🎯 等级汇总")
        lines.append(f"📈 总经验值: {level_summary['total_exp']:,}")
        top_levels = level_summary["level_stats"][:5]
        level_dist = "  |  ".join([f"Lv.{lv}: {info['count']}个吧" for lv, info in top_levels])
        lines.append(f"🏅 等级分布: {level_dist}")

    if details:
        lines.append("")
        lines.append("📋 详细签到情况")
        for d in details:
            name = d["name"]
            status = d["status"]
            rank = d.get("rank")
            level = d.get("level", "?")
            if status == "success":
                emoji = "✅"
                rank_text = f" (第{rank}个)" if rank else ""
            elif status == "exist":
                emoji = "⚠️"; rank_text = ""
            elif status == "shield":
                emoji = "🚫"; rank_text = ""
            else:
                emoji = "❌"; rank_text = ""
            lines.append(f"{emoji} {name} Lv.{level}{rank_text}")

    lines.append("")
    lines.append("─" * 18)
    lines.append(f"🕒 执行时间: {beijing_time_str()}")
    return "\n".join(lines)


def main() -> Dict:
    """主流程"""
    cookie_dict = {item.split("=")[0]: item.split("=")[1] for item in TIEBA_COOKIE.split("; ") if "=" in item}
    bduss = cookie_dict.get("BDUSS", "")
    if not bduss:
        log_error("Cookie 中未找到 BDUSS，请检查配置")
        return {"user_name": "未知", "stats": {"total": 0, "success": 0, "exist": 0, "shield": 0, "error": 0}, "details": [], "level_summary": None}

    session = create_session(TIEBA_COOKIE)

    tbs, user_name = get_user_info(session)
    if not tbs:
        log_error(user_name)
        return {"user_name": "登录失败", "stats": {"total": 0, "success": 0, "exist": 0, "shield": 0, "error": 0}, "details": [], "level_summary": None}

    log_success(f"登录成功，用户名: {user_name}")

    forums = get_favorite(session, bduss=bduss)
    if not forums:
        log_warning("未获取到任何贴吧，请检查 Cookie 或网络")
        return {"user_name": user_name, "stats": {"total": 0, "success": 0, "exist": 0, "shield": 0, "error": 0}, "details": [], "level_summary": None}

    level_summary = build_level_summary(forums)
    log_info(f"总经验值: {level_summary['total_exp']:,}，最高等级: Lv.{level_summary['level_stats'][0][0] if level_summary['level_stats'] else '?'}")

    result = sign_forums(session, bduss, forums, tbs)
    log_info(f"签到完成: 总数 {result['total']}，成功 {result['success']}，已签 {result['exist']}，屏蔽 {result['shield']}，失败 {result['error']}")

    return {"user_name": user_name, "stats": result, "details": result["details"], "level_summary": level_summary}


if __name__ == "__main__":
    result = main()
    if result:
        report = build_report(result["stats"], result["user_name"], result["details"], result.get("level_summary"))
        notify_send("百度贴吧 签到报告", report)

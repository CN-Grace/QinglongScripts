#!/usr/bin/env python3
# cron: 0 0 * * *
# new Env("B站每日任务")
# Bilibili 每日任务脚本
# - 漫画签到
# - 投币任务（支持指定投币数和投币来源）
# - 观看视频任务
# - 分享视频任务
# - 银瓜子兑换硬币（可选）
# - 获取今日经验、当前经验、预计升级天数

import os
import time
import requests

from utils import log_info, log_success, log_warning, log_error, beijing_time_str
from notifier import send as notify_send

# ==================== 用户配置 ====================
BILIBILI_COOKIE = os.environ.get("BILIBILI_COOKIE", "")
COIN_NUM = int(os.environ.get("BILIBILI_COIN_NUM", "0"))
COIN_TYPE = int(os.environ.get("BILIBILI_COIN_TYPE", "1"))
SILVER2COIN = os.environ.get("BILIBILI_SILVER2COIN", "false").lower() == "true"

report_data = {}


# ---------- Bilibili API ----------
def get_nav(session):
    """获取用户导航信息"""
    url = "https://api.bilibili.com/x/web-interface/nav"
    ret = session.get(url=url).json()
    data = ret.get("data", {})
    return data.get("uname"), data.get("mid"), data.get("isLogin"), data.get("money"), data.get("vipType"), data.get("level_info", {}).get("current_exp")


def get_today_exp(session):
    """获取今日经验明细"""
    url = "https://api.bilibili.com/x/member/web/exp/log?jsonp=jsonp"
    today = beijing_time_str("%Y-%m-%d")
    try:
        data_list = session.get(url=url).json().get("data", {}).get("list", [])
        return list(filter(lambda x: x["time"].split()[0] == today, data_list))
    except Exception:
        return []


def manga_sign(session, platform="android"):
    """漫画签到"""
    try:
        url = "https://manga.bilibili.com/twirp/activity.v1.Activity/ClockIn"
        ret = session.post(url=url, data={"platform": platform}).json()
        if ret["code"] == 0:
            return "✅ 签到成功"
        elif ret.get("msg") == "clockin clockin is duplicate":
            return "✅ 今天已经签到过了"
        else:
            msg = f"❌ 签到失败: {ret.get('msg', '未知错误')}"
            log_error(msg)
            return msg
    except Exception as e:
        msg = f"❌ 签到异常: {e!s}"
        log_error(msg)
        return msg


def report_task(session, bili_jct, aid, cid, progres=300):
    """上报视频观看进度"""
    url = "http://api.bilibili.com/x/v2/history/report"
    return session.post(url=url, data={"aid": aid, "cid": cid, "progres": progres, "csrf": bili_jct}).json()


def share_task(session, bili_jct, aid):
    """分享视频"""
    url = "https://api.bilibili.com/x/web-interface/share/add"
    return session.post(url=url, data={"aid": aid, "csrf": bili_jct}).json()


def get_followings(session, uid, pn=1, ps=50, order="desc", order_type="attention"):
    """获取关注的 up 主列表"""
    params = {"vmid": uid, "pn": pn, "ps": ps, "order": order, "order_type": order_type}
    return session.get(url="https://api.bilibili.com/x/relation/followings", params=params).json()


def space_arc_search(session, uid, pn=1, ps=30, tid=0, order="pubdate", keyword=""):
    """获取指定 up 主的视频投稿"""
    params = {"mid": uid, "pn": pn, "Ps": ps, "tid": tid, "order": order, "keyword": keyword}
    ret = session.get(url="https://api.bilibili.com/x/space/arc/search", params=params).json()
    data = ret.get("data")
    if not isinstance(data, dict):
        return [], 0
    vlist = data.get("list", {}).get("vlist", [])
    data_list = [
        {"aid": one.get("aid"), "cid": 0, "title": one.get("title"), "owner": one.get("author")}
        for one in vlist[:2]
    ]
    return data_list, 2


def coin_add(session, bili_jct, aid, num=1, select_like=1):
    """给视频投币"""
    url = "https://api.bilibili.com/x/web-interface/coin/add"
    return session.post(url=url, data={"aid": aid, "multiply": num, "select_like": select_like, "cross_domain": "true", "csrf": bili_jct}).json()


def live_status(session):
    """获取直播瓜子状态"""
    ret = session.get(url="https://api.live.bilibili.com/pay/v1/Exchange/getStatus").json()
    data = ret.get("data", {})
    return {"coin": data.get("coin", 0), "gold": data.get("gold", 0), "silver": data.get("silver", 0)}


def get_region(session, rid=1, num=6):
    """获取分区视频列表（dynamic/region 已废弃，使用 popular API 作为主源，ranking 作为备选）"""
    # 主 API：热门视频
    url = f"https://api.bilibili.com/x/web-interface/popular?pn=1&ps={num}"
    try:
        ret = session.get(url=url).json()
        data = ret.get("data")
        if isinstance(data, dict):
            video_list = data.get("list", [])
            if video_list:
                return [
                    {"aid": one.get("aid"), "cid": one.get("cid"), "title": one.get("title"),
                     "owner": one.get("owner", {}).get("name")}
                    for one in video_list
                ]
    except Exception:
        pass

    # 备选 API：排行榜
    try:
        ret = session.get(
            url="https://api.bilibili.com/x/web-interface/ranking/v2",
            params={"rid": rid, "type": "all"}
        ).json()
        data = ret.get("data")
        if isinstance(data, dict):
            video_list = data.get("list", [])
            if video_list:
                return [
                    {"aid": one.get("aid"), "cid": one.get("cid"), "title": one.get("title"),
                     "owner": one.get("owner", {}).get("name")}
                    for one in video_list
                ]
    except Exception:
        pass

    log_warning("无法获取视频列表，所有 API 均失败")
    return []


def silver2coin(session, bili_jct):
    """银瓜子兑换硬币"""
    url = "https://api.live.bilibili.com/xlive/revenue/v1/wallet/silver2coin"
    return session.post(url=url, data={"csrf": bili_jct}).json()


def main():
    global report_data
    report_data = {}

    bilibili_cookie = {item.split("=")[0]: item.split("=")[1] for item in BILIBILI_COOKIE.split("; ")}
    bili_jct = bilibili_cookie.get("bili_jct")

    session = requests.session()
    requests.utils.add_dict_to_cookiejar(session.cookies, bilibili_cookie)
    session.headers.update({
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.64",
        "Referer": "https://www.bilibili.com/",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Connection": "keep-alive",
    })

    uname, uid, is_login, coin, vip_type, current_exp = get_nav(session=session)
    if not is_login:
        log_error("登录失败，请检查 cookie")
        return "登录失败，请检查 cookie"

    log_success(f"登录成功，用户名: {uname}，UID: {uid}，当前硬币: {coin}")
    report_data["账号"] = uname

    # 漫画签到
    manhua_msg = manga_sign(session=session)
    log_info(f"漫画签到: {manhua_msg}")
    report_data["漫画签到"] = manhua_msg

    # 获取分区视频列表
    aid_list = get_region(session=session)

    # 计算今日已投币数
    today_exp_list = get_today_exp(session=session)
    coins_av_count = len(list(filter(lambda x: x.get("reason") == "视频投币奖励", today_exp_list)))
    need_coin_num = COIN_NUM - coins_av_count
    need_coin_num = need_coin_num if need_coin_num < coin else coin

    log_info(f"今日已投币 {coins_av_count} 个，目标投币 {COIN_NUM} 个，还需投 {need_coin_num} 个")

    # 根据 coin_type 选择投币来源
    if COIN_TYPE == 1:
        following_list = get_followings(session=session, uid=uid)
        count = 0
        data = following_list.get("data")
        if not isinstance(data, dict):
            data = {}
        for following in data.get("list", []):
            mid = following.get("mid")
            if mid:
                tmplist, tmpcount = space_arc_search(session=session, uid=mid)
                aid_list += tmplist
                count += tmpcount
                if count > need_coin_num:
                    log_info("已获取足够关注用户的视频")
                    break
        else:
            aid_list += get_region(session=session)

    # 投币循环
    success_count = 0
    if need_coin_num > 0:
        for one in aid_list[::-1]:
            ret = coin_add(session=session, aid=one.get("aid"), bili_jct=bili_jct)
            if ret.get("code") == 0:
                need_coin_num -= 1
                log_success(f"成功给《{one.get('title')}》投一个币")
                success_count += 1
            elif ret.get("code") == 34005:
                log_warning(f"投币《{one.get('title')}》失败: {ret.get('message')}，继续下一个")
                continue
            else:
                log_error(f"投币《{one.get('title')}》失败: {ret.get('message')}，跳过投币")
                break
            if need_coin_num <= 0:
                break
        coin_msg = f"今日成功投币 {success_count + coins_av_count}/{COIN_NUM} 个"
    else:
        coin_msg = f"今日成功投币 {coins_av_count}/{COIN_NUM} 个"
    log_info(coin_msg)
    report_data["投币任务"] = coin_msg

    # 观看视频任务
    if aid_list:
        first = aid_list[0]
        report_ret = report_task(session=session, bili_jct=bili_jct, aid=first.get("aid"), cid=first.get("cid"))
        report_msg = f"✅ 观看《{first.get('title')}》300秒" if report_ret.get("code") == 0 else "❌ 观看任务失败"
        log_success(report_msg) if report_ret.get("code") == 0 else log_error(report_msg)

        share_ret = share_task(session=session, bili_jct=bili_jct, aid=first.get("aid"))
        share_msg = f"✅ 分享《{first.get('title')}》成功" if share_ret.get("code") == 0 else "❌ 分享失败"
        log_success(share_msg) if share_ret.get("code") == 0 else log_error(share_msg)
    else:
        report_msg = "⚠️ 无视频可观看"; share_msg = "⚠️ 无视频可分享"
        log_warning(report_msg); log_warning(share_msg)
    report_data["观看视频"] = report_msg
    report_data["分享任务"] = share_msg

    # 银瓜子兑换硬币
    s2c_msg = "⚪ 未开启兑换"
    if SILVER2COIN:
        silver2coin_ret = silver2coin(session=session, bili_jct=bili_jct)
        if silver2coin_ret.get("code") == 0:
            s2c_msg = "✅ 银瓜子兑换硬币成功"
            log_success(s2c_msg)
        else:
            s2c_msg = f"❌ 兑换失败: {silver2coin_ret.get('message', '未知错误')}"
            log_error(s2c_msg)
    report_data["瓜子兑换"] = s2c_msg

    # 获取直播瓜子状态
    live_stats = live_status(session=session)
    report_data["硬币数量"] = live_stats["coin"]
    report_data["金瓜子数"] = live_stats["gold"]
    report_data["银瓜子数"] = live_stats["silver"]

    # 再次获取用户信息，更新经验
    uname, uid, is_login, new_coin, vip_type, new_current_exp = get_nav(session=session)
    today_exp = sum(map(lambda x: x.get("delta", 0), get_today_exp(session=session)))
    update_data = (28800 - new_current_exp) // (today_exp if today_exp else 1) if today_exp else "未知"

    report_data["今日经验"] = today_exp
    report_data["当前经验"] = new_current_exp
    report_data["升级还需"] = f"{update_data}天"

    log_success("========== 任务执行汇总 ==========")
    for k, v in report_data.items():
        log_info(f"{k}: {v}")
    return report_data


def build_report(data):
    """构建签到报告"""
    lines = [f"👤 账号: {data.get('账号', '未知')}", ""]

    tasks = [
        ("📖 漫画签到", data.get("漫画签到", "")),
        ("📺 观看视频", data.get("观看视频", "")),
        ("🔗 分享任务", data.get("分享任务", "")),
        ("💰 投币任务", data.get("投币任务", "")),
        ("💱 瓜子兑换", data.get("瓜子兑换", "")),
    ]
    for name, value in tasks:
        lines.append(f"{name}: {value}")

    lines.append("")
    lines.append("📊 数据统计")
    lines.append(f"⭐ 今日经验: {data.get('今日经验', 0)}")
    lines.append(f"📈 当前经验: {data.get('当前经验', 0)}")
    lines.append(f"⏳ 升级还需: {data.get('升级还需', '未知')}")

    lines.append("")
    lines.append("🥜 瓜子库存")
    lines.append(f"🪙 硬币数量: {data.get('硬币数量', 0)}")
    lines.append(f"✨ 金瓜子数: {data.get('金瓜子数', 0)}")
    lines.append(f"🥈 银瓜子数: {data.get('银瓜子数', 0)}")

    lines.append("")
    lines.append("─" * 18)
    lines.append(f"🕒 执行时间: {beijing_time_str()}")
    return "\n".join(lines)


if __name__ == "__main__":
    report = main()
    if report and isinstance(report, dict):
        tg_text = build_report(report)
        notify_send("🎯 Bilibili 每日任务报告", tg_text)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cron: 20 0 * * *
new Env("阴阳师绘卷查询")
阴阳师绘卷查询 - 自动根据活动时间查询
"""
import hashlib, json, os, re, time, uuid, requests
from datetime import datetime, timedelta

from utils import log_info, log_success, log_warning, log_error
from notifier import send as notify_send

# 活动时间JSON路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIVITY_JSON = os.path.join(SCRIPT_DIR, "yys_huijuan_time.json")

GL_UID = os.environ.get("YYS_GL_UID", "")
GL_TOKEN = os.environ.get("YYS_GL_TOKEN", "")
URS_CREDENTIALS = os.environ.get("YYS_URS_CREDENTIALS", "")
DEVICE_ID = "46758156facc4291a2acd9545b9ad90e"
GOD_API = "https://god.gameyw.netease.com"
TURING_API = "https://turing.gameyw.netease.com"
ROLE_ID = "585bd32afe992b27a4ce9bb3"
SERVER = "15004"
SIGN_SECRET = "affa62e3b7376a0cbd20ea2f6c07072f"
VERSION = "4.15.0"
SOP_ID = "68ea0f7c38aa9e6367e781a1"

TH = {"Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Linux; Android 16; 25102RKBEC Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/148.0.7778.120 Mobile Safari/537.36 Godlike/4.15.0 (channel/open_tencent) UEPay/com.netease.gl/android7.12.28",
    "Origin": "https://game.16163.com", "Referer": "https://game.16163.com/", "x-requested-with": "com.netease.gl"}

report_data = {}


def load_activity_info():
    """加载活动时间信息"""
    try:
        if not os.path.exists(ACTIVITY_JSON):
            log_error(f"活动时间文件不存在: {ACTIVITY_JSON}")
            return None
        with open(ACTIVITY_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get("huijuan_activity")
    except Exception as e:
        log_error(f"读取活动时间失败: {e}")
        return None


def check_activity_period(activity):
    """检查当前是否在活动时间内"""
    if not activity:
        return False, None, None

    today = datetime.now().strftime("%Y-%m-%d")
    start_date = activity.get("start_time", "")
    end_date_str = activity.get("end_time", "")

    # 提取结束日期（去掉时间部分）
    end_date = end_date_str.split(" ")[0] if end_date_str else ""

    if not start_date or not end_date:
        log_error("活动时间格式错误")
        return False, None, None

    log_info(f"当前日期: {today}")
    log_info(f"活动时间: {start_date} ~ {end_date}")

    if start_date <= today <= end_date:
        log_success("当前在活动时间内")
        return True, start_date, end_date
    elif today < start_date:
        log_warning(f"活动尚未开始，开始日期: {start_date}")
        return False, start_date, end_date
    else:
        log_warning(f"活动已结束，结束日期: {end_date}")
        return False, start_date, end_date


def gl_checksum(ct, cur, dev, nonce, src, token, uid, ver):
    return hashlib.sha1((SIGN_SECRET + ct + cur + dev + nonce + src + token + uid + ver).encode()).hexdigest()


def _headers(uid, token):
    t = int(time.time() * 1000)
    nonce = f"{t}_{uuid.uuid4().int}"
    cur = str(t)
    cs = gl_checksum("50", cur, DEVICE_ID, nonce, "URS", token, uid, VERSION)
    return {"Content-Type": "application/json; charset=utf-8", "User-Agent": "okhttp/4.9.1",
            "gl-clienttype": "50", "gl-curtime": cur, "gl-deviceid": DEVICE_ID, "gl-version": VERSION,
            "gl-nonce": nonce, "gl-channel": "open_tencent", "gl-activesquareid": "5bed6281d545682b8bb8a761",
            "gl-uid": uid, "gl-token": token, "gl-source": "URS", "gl-requesttime": str(t + 1), "gl-checksum": cs}


def god_post(path, body, uid, token):
    resp = requests.post(f"{GOD_API}{path}", json=body, headers=_headers(uid, token), timeout=15)
    data = resp.json()
    log_info(f"{path}: code={data.get('code')}")
    if data.get("code") != 200:
        raise RuntimeError(f"God API {data.get('code')}: {data.get('errmsg')}")
    return data["result"]


def login_by_urs(urs_id, urs_token, account, uid, token):
    log_info("URS 登录中...")
    body = {"urs": {"id": urs_id, "token": urs_token, "type": 10}, "account": account, "clientType": 50,
            "deviceId": DEVICE_ID, "os": "android", "version": VERSION, "osVersion": "16",
            "device": "25102RKBEC", "udid": DEVICE_ID[:16], "unisdkDeviceId": DEVICE_ID[:16],
            "autoLogin": True, "phoneUnbindCheck": False}
    result = god_post("/v1/app/base/user/login-by-urs-token", body, uid, token)
    log_success(f"登录成功: {result['userInfo']['user'].get('nick','')}")
    return result["userInfo"]["user"]["uid"], result["token"]


def get_gmsdk(uid, token):
    return god_post("/v1/app/gameRole/getGmsdkToken", {"roleId": ROLE_ID, "server": SERVER, "appKey": "g37"}, uid, token)["gmSdkToken"]


def get_sop_session(gm_token):
    log_info("获取 sopSession...")
    r = requests.get(f"https://game.16163.com/api/opd/sop/sopH5Tool/tokenIndex",
        params={"application": "god-gmsdk", "profile": "server", "from": "xiaoyi",
                "sopId": SOP_ID, "token": gm_token},
        headers={"User-Agent": TH["User-Agent"], "x-requested-with": "com.netease.gl"},
        timeout=30, allow_redirects=False)
    m = re.search(r'sopSession=([^&]+)', r.headers.get('Location', ''))
    if m:
        log_success(f"sopSession: {m.group(1)}")
        return m.group(1)
    raise RuntimeError("无法获取 sopSession")


def turing_init(sop_session):
    r = requests.post(f"{TURING_API}/sop-api/api/out/context/initBySession",
                      json={"sopSession": sop_session}, headers=TH, timeout=30)
    d = r.json()
    if d.get("code") == 200:
        i = d.get("item") or {}
        log_success(f"SOP: {i.get('sopName','').strip()}")
        return i.get("contextId"), i.get("sopSession")
    raise RuntimeError(f"SOP失败: {d.get('message')}")


def query_date(cid, ss, date):
    """查询单个日期"""
    url = f"{TURING_API}/sop-api/api/out/context/getAsyncProcessResultV2"
    proc_url = f"{TURING_API}/sop-api/api/out/context/process"
    body = {"contextId": cid, "sopSession": ss}

    log_info(f"查询 {date}...")
    requests.post(proc_url, json={**body, "async": True, "inputPayload": {"time": date}}, headers=TH, timeout=30)

    for _ in range(15):
        time.sleep(2)
        r = requests.post(url, json=body, headers=TH, timeout=30)
        data = r.json()
        for hr in (data.get("item") or {}).get("handlerResponseList") or []:
            if hr.get("type") == "response":
                res = hr.get("result") or {}
                if res.get("message") or res.get("tableData"):
                    return res
    return None


def parse_result(result):
    if not result:
        return {}, 0
    msg = re.sub(r"<[^>]+>", "", result.get("message") or "")
    s = {}
    m = re.search(r"绘卷碎片·小[：:]\s*(\d+)/(\d+)", msg)
    if m:
        s["小"] = f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"绘卷碎片·中[：:]\s*(\d+)", msg)
    if m:
        s["中"] = m.group(1)
    m = re.search(r"绘卷碎片·大[：:]\s*(\d+)", msg)
    if m:
        s["大"] = m.group(1)
    rows = ((result.get("tableData") or {}).get("rows") or [])
    return s, len(rows)


def build_report(data):
    """构建报告"""
    lines = [
        "🎮 阴阳师绘卷碎片查询报告",
        "",
        f"📋 活动名称: {data.get('活动名称', '未知')}",
        f"📅 活动时间: {data.get('活动时间', '未知')}",
        f"📅 查询范围: {data.get('查询范围', '未知')}",
        "",
    ]

    # 查询结果
    results = data.get("查询结果", [])
    if results:
        lines.append("📊 查询结果")
        for r in results:
            lines.append(f"  📅 {r.get('日期', '')}")
            fragments = r.get("碎片", {})
            for t, c in fragments.items():
                lines.append(f"    绘卷碎片·{t}: {c}")
            lines.append(f"    记录数: {r.get('记录数', 0)}")

    lines.append("")
    lines.append("─" * 18)
    lines.append(f"🕒 查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def main():
    global report_data
    report_data = {}

    log_info("=" * 40)
    log_info("阴阳师绘卷查询")
    log_info("=" * 40)

    # 1. 读取活动时间
    activity = load_activity_info()
    if not activity:
        log_error("无法获取活动时间信息，请先运行 YysHuijuanTime 获取活动时间")
        return

    log_info(f"活动名称: {activity.get('name', '未知')}")

    # 2. 检查是否在活动时间内
    in_period, start_date, end_date = check_activity_period(activity)

    # 3. 登录并更新token（不管是否在活动时间内都要执行）
    global GL_UID, GL_TOKEN
    uid, token = GL_UID, GL_TOKEN
    if not uid or not token:
        if not URS_CREDENTIALS:
            log_error("请设置环境变量")
            return
        p = URS_CREDENTIALS.split("|")
        if len(p) != 3:
            log_error("URS格式错误")
            return
        uid, token = login_by_urs(p[0], p[1], p[2], "", "")
        GL_UID, GL_TOKEN = uid, token

    try:
        try:
            gm = get_gmsdk(uid, token)
        except RuntimeError as e:
            if "824" in str(e) and URS_CREDENTIALS:
                log_warning("token过期，自动刷新...")
                p = URS_CREDENTIALS.split("|")
                uid, token = login_by_urs(p[0], p[1], p[2], uid, token)
                GL_UID, GL_TOKEN = uid, token
                gm = get_gmsdk(uid, token)
            else:
                raise
        log_success("gmSdkToken获取成功")

        # 4. 如果不在活动时间内，只更新token不查询
        if not in_period:
            log_warning("当前不在活动时间内，仅更新token，跳过查询")
            log_success("========== token更新完成 ==========")
            return

        # 5. 在活动时间内，执行查询
        sop_session = get_sop_session(gm)
        cid, ss = turing_init(sop_session)

        # 激活上下文
        log_info("激活上下文...")
        proc_url = f"{TURING_API}/sop-api/api/out/context/process"
        body = {"contextId": cid, "sopSession": ss}
        requests.post(proc_url, json={**body, "async": True, "inputPayload": None}, headers=TH, timeout=30)
        time.sleep(2)

        # 获取可用日期
        url = f"{TURING_API}/sop-api/api/out/context/getAsyncProcessResultV2"
        r = requests.post(url, json=body, headers=TH, timeout=30)
        data = r.json()
        dates = []
        for hr in (data.get("item") or {}).get("handlerResponseList") or []:
            if hr.get("type") == "slot":
                result = hr.get("result") or {}
                dates = list(((result.get("slotMapper") or {}).get("time") or {}).get("options") or {})
                break
        log_info(f"可用日期: {dates}")

        # 筛选目标日期：从活动开始日期到今天
        today = datetime.now().strftime("%Y-%m-%d")
        target_dates = [d for d in dates if start_date <= d <= today]
        if not target_dates:
            log_error("无目标日期可查询")
            return

        log_info(f"目标日期: {target_dates}")

        # 查询每个日期
        query_results = []
        for date in target_dates:
            result = query_date(cid, ss, date)
            summary, count = parse_result(result)
            query_results.append({
                "日期": date,
                "碎片": summary,
                "记录数": count
            })

        # 构建报告数据
        report_data["活动名称"] = activity.get('name', '未知')
        report_data["活动时间"] = f"{start_date} ~ {end_date}"
        report_data["查询范围"] = f"{start_date} ~ {today}"
        report_data["查询结果"] = query_results

        log_success("========== 查询完成 ==========")
        for k, v in report_data.items():
            if k != "查询结果":
                log_info(f"{k}: {v}")

        # 发送通知
        tg_text = build_report(report_data)
        notify_send("阴阳师绘卷查询报告", tg_text)

    except Exception as e:
        log_error(f"执行出错: {e}")


if __name__ == "__main__":
    main()

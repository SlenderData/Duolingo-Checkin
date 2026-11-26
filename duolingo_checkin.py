#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多邻国每日打卡 + 每日任务补齐（单文件，仅依赖 Python 3.9+ 标准库）

环境变量（青龙面板 → 环境变量）：
  DUO_JWT         必填。浏览器 Cookie 里的 jwt_token；多个账号用换行或 & 分隔
  DUO_TZ          可选。时区名，如 Asia/Shanghai；默认用账号资料里设置的时区
  DUO_QUEST_SKIP  可选。不补的指标前缀，逗号分隔（如 "CHESS_,MATH_"）；
                  不设置时按账号已加入的课程类型自动判断；设为空字符串表示全补
  DUO_NO_QUEST    可选。设为 1 只打卡，不处理每日任务（等同 --no-quest）
  DUO_DRY_RUN     可选。设为 1 只预览，不提交任何东西（等同 --dry-run）

本地运行时如果没设置 DUO_JWT，会读取同目录的 accounts.json（兼容 DuoHacker 的账号文件格式）。

Copyright (c) 2026 2pixel
Copyright (c) 2026 SlenderData
MIT License，见 LICENSE。部分代码改编自 DuoHacker。
"""
import os, sys, json, time, random, base64, http.client, urllib.parse
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = set(sys.argv[1:])


def _flag(name, env):
    return name in ARGS or os.environ.get(env, "").strip() in ("1", "true", "yes")


DRY_RUN = _flag("--dry-run", "DUO_DRY_RUN")
NO_QUEST = _flag("--no-quest", "DUO_NO_QUEST")


# ─── 日志 ─────────────────────────────────────────────────────────
_LOG = []

def log(msg, level="info"):
    mark = {"info": "·", "success": "✓", "warning": "!", "error": "✗"}.get(level, "·")
    line = f"{datetime.now():%H:%M:%S} {mark} {msg}"
    _LOG.append(line)
    print(line, flush=True)


# ─── HTTP ─────────────────────────────────────────────────────────
USER_AGENTS = [
    "Duolingo/5.158.4 (iPhone; iOS 17.4; Scale/3.00)",
    "Duolingo/5.157.0 (Android; Build/12; Model/Pixel 7)",
    "Duolingo/5.156.2 (iPhone; iOS 16.7; Scale/2.00)",
    "Duolingo/5.158.1 (Android; Build/13; Model/Samsung Galaxy S23)",
]


def http_request(method, host, path, headers=None, body=None, timeout=30):
    """返回 (status, 解析后的 JSON 或原始文本)。网络错误时 status=0。"""
    conn = None
    try:
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(text)
        except ValueError:
            return resp.status, text
    except Exception as e:
        return 0, str(e)
    finally:
        if conn:
            conn.close()


def www_headers(jwt, sub):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {jwt}",
        "User-Agent": random.choice(USER_AGENTS),
        "x-amzn-trace-id": f"User={sub}",
        "Cookie": f"jwt_token={jwt}",
        "Origin": "https://www.duolingo.com",
        "Referer": "https://www.duolingo.com/",
        "Accept-Encoding": "identity",
    }


def goals_headers(jwt, sub):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json; charset=UTF-8",
        "Authorization": f"Bearer {jwt}",
        "x-requested-with": "XMLHttpRequest",
        "x-amzn-trace-id": f"User={sub}",
    }


def www(method, path, jwt, sub, body=None):
    return http_request(method, "www.duolingo.com", path, www_headers(jwt, sub), body)


def goals(method, path, jwt, sub, body=None):
    return http_request(method, "goals-api.duolingo.com", path, goals_headers(jwt, sub), body)


# ─── JWT ──────────────────────────────────────────────────────────
def jwt_payload(token):
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def jwt_sub(token):
    sub = jwt_payload(token).get("sub")
    return str(sub) if sub is not None else None


def jwt_days_left(token):
    exp = jwt_payload(token).get("exp")
    return None if not exp else (exp - time.time()) / 86400


# ─── 时区 ─────────────────────────────────────────────────────────
class Clock:
    """按账号时区计算“今天”。goals-api 只认 IANA 时区名（如 Asia/Shanghai），
    不认 "UTC+08:00"；容器里系统时区通常是 UTC，所以以账号资料为准。"""

    def __init__(self, profile):
        self.name = os.environ.get("DUO_TZ") or profile.get("timezone") or "UTC"
        self.tz = None
        if ZoneInfo:
            try:
                self.tz = ZoneInfo(self.name)
            except Exception:
                pass
        if self.tz is None:  # 容器缺 tzdata 时，退回固定偏移
            off = str(profile.get("timezoneOffset") or "+0000")
            sign = -1 if off.startswith("-") else 1
            off = off.lstrip("+-").replace(":", "").ljust(4, "0")
            self.tz = timezone(sign * timedelta(hours=int(off[:2]), minutes=int(off[2:4])))

    def now(self):
        return datetime.now(self.tz)


# ─── 账号资料 ─────────────────────────────────────────────────────
PROFILE_FIELDS = ("id,username,streak,streakData{currentStreak},totalXp,gems,hasPlus,"
                  "timezone,timezoneOffset,fromLanguage,learningLanguage,courses")


def get_profile(jwt, sub):
    st, d = www("GET", f"/2023-05-23/users/{sub}?fields={urllib.parse.quote(PROFILE_FIELDS)}", jwt, sub)
    if st in (401, 403):
        raise PermissionError(f"JWT 被拒绝 (HTTP {st})，请重新获取")
    if st != 200 or not isinstance(d, dict):
        raise ConnectionError(f"获取账号资料失败 (HTTP {st})")
    return d


def get_streak_goal(jwt, sub):
    st, d = www("GET", f"/users/{sub}/streak-goal-current?_={int(time.time() * 1000)}", jwt, sub)
    return d.get("streakGoal") if st == 200 and isinstance(d, dict) else None


# ─── 打卡 ─────────────────────────────────────────────────────────
CHALLENGE_TYPES = [
    "assist", "characterIntro", "characterMatch", "characterPuzzle", "characterSelect",
    "characterTrace", "characterWrite", "completeReverseTranslation", "definition",
    "dialogue", "extendedMatch", "extendedListenMatch", "form", "freeResponse",
    "gapFill", "judge", "listen", "listenComplete", "listenMatch", "match", "name",
    "listenComprehension", "listenIsolation", "listenSpeak", "listenTap",
    "orderTapComplete", "partialListen", "partialReverseTranslate", "patternTapComplete",
    "radioBinary", "radioImageSelect", "radioListenMatch", "radioListenRecognize",
    "radioSelect", "readComprehension", "reverseAssist", "sameDifferent", "select",
    "selectPronunciation", "selectTranscription", "svgPuzzle", "syllableTap",
    "syllableListenTap", "speak", "tapCloze", "tapClozeTable", "tapComplete",
    "tapCompleteTable", "tapDescribe", "translate", "transliterate",
    "transliterationAssist", "typeCloze", "typeClozeTable", "typeComplete",
    "typeCompleteTable", "writeComprehension",
]


def practice_session(jwt, sub, profile):
    """创建一次 GLOBAL_PRACTICE 并提交为完成（约 +13 XP，续当天连胜，不影响课程路径）。"""
    body = {
        "challengeTypes": CHALLENGE_TYPES,
        "fromLanguage": profile.get("fromLanguage", "en"),
        "learningLanguage": profile.get("learningLanguage"),
        "isFinalLevel": False, "isV2": True, "juicy": True, "smartTipsVersion": 2,
        "type": "GLOBAL_PRACTICE",
    }
    st, session = www("POST", "/2017-06-30/sessions", jwt, sub, body)
    if st != 200 or not isinstance(session, dict) or not session.get("id"):
        return False
    end = int(time.time())
    update = {**session, "heartsLeft": 5, "startTime": end - 1, "endTime": end,
              "enableBonusPoints": False, "failed": False,
              "maxInLessonStreak": 9, "shouldLearnThings": True}
    st2, _ = www("PUT", f"/2017-06-30/sessions/{session['id']}", jwt, sub, update)
    return st2 == 200


# ─── 每日任务 ─────────────────────────────────────────────────────
def get_quest_schema(jwt, sub):
    st, d = goals("GET", f"/schema?ui_language=en&_={int(time.time() * 1000)}", jwt, sub)
    return d if st == 200 and isinstance(d, dict) else None


def get_quest_progress(jwt, sub, tz_name):
    st, d = goals("GET", f"/users/{sub}/progress?timezone={urllib.parse.quote(tz_name)}&ui_language=en", jwt, sub)
    return d if st == 200 and isinstance(d, dict) else None


def latest_daily_goals(schema, include_all=False):
    """goalId -> goal。同一 goalId 可能有多个 version（目标不同），取最新。"""
    latest = {}
    for g in schema.get("goals", []):
        if not include_all and "DAILY" not in (g.get("category") or ""):
            continue
        gid = g.get("goalId")
        if gid not in latest or (g.get("version") or 0) > (latest[gid].get("version") or 0):
            latest[gid] = g
    return latest


def goal_target(goal, difficulty):
    """目标值 = difficultyTiers[difficulty-1]（difficulty 来自 progress，1~4）。
    schema 里的 threshold 不是目标值。"""
    dt = goal.get("difficultyTiers") or []
    if dt and isinstance(difficulty, int) and difficulty >= 1:
        tiers = dt[min(difficulty, len(dt)) - 1].get("tiers") or []
        if tiers:
            return tiers[0]
    tiers = goal.get("tiers") or []
    return tiers[0] if tiers else goal.get("threshold", 1)


# App 按“当前课程”从不同任务池随机抽每日任务，但服务器端进度是全账号按 metric 共享的。
# 只跳过账号里根本没有的课程类型对应的 metric。
SUBJECT_METRIC_PREFIXES = {
    "chess": ("CHESS_",),
    "math":  ("MATH_SESSIONS",),
    "music": ("MUSIC_", "NINETY_ACCURACY_MUSIC", "PERFECT_SONGS", "THREE_STAR_SONGS"),
}


def skip_prefixes(profile):
    raw = os.environ.get("DUO_QUEST_SKIP")
    if raw is not None:
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    subjects = {c.get("subject") for c in profile.get("courses", []) if isinstance(c, dict)}
    skip = [p for s, ps in SUBJECT_METRIC_PREFIXES.items() if s not in subjects for p in ps]
    if not subjects & {"math", "music"}:
        skip.append("MATH_OR_MUSIC")
    return tuple(skip)


def quest_deficits(schema, progress, skip=()):
    """{metric: (当前, 目标, 差额)}，只含差额 > 0 的。
    同一 metric 的任务共用计数，目标取该 metric 下所有每日任务的最大值，
    这样不管 App 抽到哪个任务都能完成。"""
    difficulty = progress.get("difficulty") or 1
    pm = (progress.get("goals") or {}).get("progress") or {}
    per_metric = {}
    for gid, g in latest_daily_goals(schema).items():
        m = g.get("metric")
        if not m or (skip and m.startswith(skip)):
            continue
        cur = pm.get(gid, 0) or 0
        c0, t0 = per_metric.get(m, (0, 0))
        per_metric[m] = (max(c0, cur), max(t0, goal_target(g, difficulty)))
    return {m: (c, t, t - c) for m, (c, t) in per_metric.items() if t > c}


def monthly_challenge(schema, progress, today):
    """本月月度挑战 (当前, 目标)；找不到返回 None。"""
    gid = f"{today:%Y_%m}_monthly_challenge"
    versions = [g for g in schema.get("goals", []) if g.get("goalId") == gid]
    if not versions:
        return None
    g = max(versions, key=lambda x: x.get("version") or 0)
    cur = ((progress.get("goals") or {}).get("progress") or {}).get(gid, 0) or 0
    return cur, g.get("threshold", 0)


def do_quests(jwt, sub, profile, clock, r):
    schema = get_quest_schema(jwt, sub)
    progress = get_quest_progress(jwt, sub, clock.name)
    if not schema or not progress:
        r["quest"] = "❌ 获取任务数据失败"
        return False

    pm = (progress.get("goals") or {}).get("progress") or {}
    r["today_xp"] = pm.get("daily_goal_daily_quest")
    r["today_secs"] = pm.get("time_spent_core_daily_quest")

    monthly = monthly_challenge(schema, progress, clock.now())
    if monthly:
        r["monthly"] = f"{monthly[0]}/{monthly[1]}"
    if monthly and monthly[0] >= monthly[1]:
        r["quest"] = f"⏭️ 跳过（本月月度挑战已达成 {r['monthly']}）"
        log(f"本月月度挑战已达成（{r['monthly']}），跳过每日任务", "success")
        return True

    skip = skip_prefixes(profile)
    if skip:
        log(f"跳过指标（账号没有该课程）：{', '.join(skip)}")
    deficits = quest_deficits(schema, progress, skip)
    if not deficits:
        r["quest"] = "✅ 所有指标已达标"
        log("所有每日任务指标都已达标", "success")
        return True

    for m, (cur, target, d) in sorted(deficits.items()):
        log(f"  {m:32} {cur:>5} → {target:<5} (+{d})")
    increments = {m: d for m, (_, _, d) in deficits.items()}
    increments["QUESTS"] = 3  # 月度挑战按完成的每日任务数计；一天 3 个（推测）

    if DRY_RUN:
        r["quest"] = f"🔍 预览：将补齐 {len(deficits)} 个指标"
        log(f"(dry-run) 将上报 {len(increments)} 个指标，时区 {clock.name}", "warning")
        return True

    payload = {
        "metric_updates": [{"metric": m, "quantity": q} for m, q in increments.items()],
        "timezone": clock.name,
        "timestamp": clock.now().replace(tzinfo=None).isoformat(),
    }
    st, _ = goals("POST", f"/users/{sub}/progress/batch", jwt, sub, payload)
    ok = st in (200, 201)
    r["quest"] = f"✅ 已补齐 {len(deficits)} 个指标" if ok else f"❌ 上报失败 (HTTP {st})"
    log(f"每日任务补齐{'成功' if ok else f'失败 (HTTP {st})'}", "success" if ok else "error")
    return ok


# ─── 单个账号 ─────────────────────────────────────────────────────
def run_account(jwt):
    jwt = jwt.strip()
    sub = jwt_sub(jwt)
    r = {"name": sub or "?", "ok": False}
    if not sub:
        r["error"] = "JWT 格式不对"
        return r
    days = jwt_days_left(jwt)
    if days is not None and days <= 0:
        r["error"] = "JWT 已过期，请重新获取"
        return r
    r["jwt_days"] = days

    profile = get_profile(jwt, sub)
    clock = Clock(profile)
    r["name"] = profile.get("username") or sub
    r["xp_before"] = profile.get("totalXp")
    r["streak_before"] = profile.get("streak")
    log(f"{r['name']}: 连胜 {r['streak_before']} 天，时区 {clock.name}")

    # 1) 打卡（今天已续过连胜就跳过；“今天”按账号时区算）
    ok = True
    today = f"{clock.now():%Y-%m-%d}"
    last = ((profile.get("streakData") or {}).get("currentStreak") or {}).get("lastExtendedDate")
    if last == today:
        r["checkin"] = "⏭️ 今天已打卡，跳过"
        log(f"今天（{today}）已打卡，跳过", "success")
    elif DRY_RUN:
        r["checkin"] = "🔍 预览：跳过"
        log("(dry-run) 跳过打卡", "warning")
    else:
        ok = False
        for attempt in range(1, 4):
            if practice_session(jwt, sub, profile):
                ok = True
                break
            time.sleep(5 * attempt)
        r["checkin"] = f"✅ 成功（第 {attempt} 次）" if ok else "❌ 失败（重试 3 次）"
        log(f"打卡{'成功' if ok else '失败'}", "success" if ok else "error")
        time.sleep(3)  # 等服务器把本次练习计入进度

    # 2) 每日任务
    if NO_QUEST:
        r["quest"] = "⏭️ 已关闭"
    else:
        ok = do_quests(jwt, sub, profile, clock, r) and ok

    # 3) 运行后状态
    try:
        after = get_profile(jwt, sub)
        r["xp_after"] = after.get("totalXp")
        r["gems"] = after.get("gems")
        r["plus"] = after.get("hasPlus")
        r["streak_after"] = after.get("streak")
        cs = (after.get("streakData") or {}).get("currentStreak") or {}
        r["streak_start"] = cs.get("startDate")
        r["extended_today"] = cs.get("lastExtendedDate") == f"{clock.now():%Y-%m-%d}"
    except Exception as e:
        log(f"读取运行后状态失败：{e}", "warning")
    goal = get_streak_goal(jwt, sub)
    if goal and goal.get("nextSelectedGoal"):
        r["next_goal"] = goal["nextSelectedGoal"].get("length")

    r["ok"] = ok
    return r


# ─── 运行报告 ─────────────────────────────────────────────────────
def _fmt(n):
    return f"{n:,}" if isinstance(n, int) else "?"


def format_report(r):
    if r.get("error"):
        return f"❌ {r['name']}\n{r['error']}"
    lines = [f"{'✅' if r['ok'] else '❌'} {r['name']}"]
    lines.append(f"打卡：{r.get('checkin', '-')}")
    lines.append(f"每日任务：{r.get('quest', '-')}")

    streak = r.get("streak_after", r.get("streak_before"))
    s = f"🔥 连胜 {streak} 天"
    if "extended_today" in r:
        s += "（今天已续 ✓）" if r["extended_today"] else "（今天未续 ⚠️）"
    lines.append(s)
    if r.get("next_goal") and isinstance(streak, int):
        lines.append(f"🎯 下一个里程碑 {r['next_goal']} 天，还差 {r['next_goal'] - streak} 天")

    if r.get("today_xp") is not None:
        t = f"📈 今日 {r['today_xp']} XP"
        if r.get("today_secs") is not None:
            t += f" · 学习 {round(r['today_secs'] / 60)} 分钟"
        lines.append(t)
    if r.get("monthly"):
        lines.append(f"🏅 月度挑战 {r['monthly']}")

    xb, xa = r.get("xp_before"), r.get("xp_after")
    x = f"⭐ 总 XP {_fmt(xa if xa is not None else xb)}"
    if isinstance(xa, int) and isinstance(xb, int) and xa != xb:
        x += f"（+{xa - xb}）"
    lines.append(x)
    if r.get("gems") is not None:
        lines.append(f"💎 宝石 {_fmt(r['gems'])}")
    d = r.get("jwt_days")
    if d is not None and d <= 7:
        lines.append(f"🔑 JWT 还剩 {d:.0f} 天过期，记得更新")
    return "\n".join(lines)


# ─── 入口 ─────────────────────────────────────────────────────────
def load_jwts():
    raw = os.environ.get("DUO_JWT", "")
    tokens = [t for t in raw.replace("&", "\n").split("\n") if t.strip()]
    if tokens:
        return tokens
    path = os.path.join(HERE, "accounts.json")   # 本地：main.py 的账号文件
    if os.path.exists(path):
        try:
            with open(path) as f:
                return [a["jwt"] for a in json.load(f) if a.get("jwt")]
        except Exception as e:
            log(f"读取 accounts.json 失败：{e}", "error")
    return []


def main():
    jwts = load_jwts()
    if not jwts:
        log("没有账号：请设置环境变量 DUO_JWT", "error")
        sys.exit(2)

    log(f"=== 多邻国每日打卡：{len(jwts)} 个账号{'（预览模式）' if DRY_RUN else ''} ===")
    results = []
    for i, jwt in enumerate(jwts):
        if i:
            time.sleep(random.uniform(3, 8))
        try:
            results.append(run_account(jwt))
        except Exception as e:
            log(f"出错：{e}", "error")
            results.append({"name": jwt_sub(jwt) or "?", "ok": False, "error": str(e)})

    all_ok = all(r["ok"] for r in results)
    title = f"多邻国打卡 {'✅' if all_ok else '❌'}{'（预览）' if DRY_RUN else ''}"
    content = "\n\n".join(format_report(r) for r in results)
    print("\n" + title + "\n" + content, flush=True)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

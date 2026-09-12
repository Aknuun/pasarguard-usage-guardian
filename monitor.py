#!/usr/bin/env python3
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pymysql
import requests

def _load_dotenv(path):
    """Minimal .env loader (KEY=VALUE) — existing environment wins."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except OSError:
        pass


BASE_DIR = os.environ.get("PG_MONITOR_DIR") or os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(BASE_DIR, ".env"))

STATE_FILE = os.path.join(BASE_DIR, "state.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.csv")
LOG_FILE = os.path.join(BASE_DIR, "monitor.log")
LOCK_FILE = os.path.join(BASE_DIR, "monitor.lock")

DB_CONFIG = {
    "host": os.environ.get("PG_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PG_DB_PORT", "3306")),
    "user": os.environ.get("PG_DB_USER", "pasarguard"),
    "password": os.environ.get("PG_DB_PASSWORD", ""),
    "database": os.environ.get("PG_DB_NAME", "pasarguard"),
    "charset": "utf8mb4",
}

TELEGRAM_BOT_TOKEN = os.environ.get("PG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("PG_CHAT_ID", "")

MIN_DAILY_GB = 0.5

ABS_GB = 10.0
MULT = 6.0
FLOOR_GB = 1.0
WARMUP_HOURS = 24

DEL_MIN_24H_GB = 2.0
REPEAT_THRESHOLD = 3
WEEKLY_WEEKDAY = 5
TOP_DAILY = 25

# Tunables that can be changed at runtime from the Telegram settings bot.
# config.json (next to this file) overrides these defaults.
DEFAULTS = {
    "ABS_GB": ABS_GB,
    "MULT": MULT,
    "FLOOR_GB": FLOOR_GB,
    "WARMUP_HOURS": WARMUP_HOURS,
    "MIN_DAILY_GB": MIN_DAILY_GB,
    "DEL_MIN_24H_GB": DEL_MIN_24H_GB,
    "REPEAT_THRESHOLD": REPEAT_THRESHOLD,
    "TOP_DAILY": TOP_DAILY,
    "WEEKLY_WEEKDAY": WEEKLY_WEEKDAY,
}

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for k in DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
        except (json.JSONDecodeError, OSError) as e:
            log(f"config.json قابل خواندن نبود؛ از پیش‌فرض استفاده شد: {e}")
    return cfg


def apply_cfg():
    for k, v in load_cfg().items():
        if k in DEFAULTS:
            globals()[k] = v


DB_FILE = os.path.join(BASE_DIR, "history.db")

IRAN_TZ = ZoneInfo("Asia/Tehran")
GB = 1024 ** 3


def log(msg):
    line = f"{datetime.now(IRAN_TZ).strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def esc(s):
    return (s or "-").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def default_state():
    return {
        "snapshot": {},
        "snapshot_ts": None,
        "day_key": datetime.now(IRAN_TZ).strftime("%Y-%m-%d"),
        "day_totals": {},
        "last_sent_day": None,
    }


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            if all(k in st for k in ("snapshot", "day_key", "day_totals")):
                return st
        except (json.JSONDecodeError, OSError):
            log("state.json خراب بود؛ از نو ساخته شد")
    return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def acquire_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(LOCK_FILE) > 600:
                os.remove(LOCK_FILE)
                return acquire_lock()
        except OSError:
            pass
        return False


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def panel_file():
    return os.path.join(BASE_DIR, "panel.json")


def load_panel():
    """Panel credentials saved from the Telegram setup wizard, if any."""
    path = panel_file()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("url") and d.get("username") and d.get("password"):
                return d
        except (OSError, json.JSONDecodeError):
            pass
    return None


def panel_login(url, username, password):
    """Login to Pasarguard and return an access token (or None)."""
    base = str(url).rstrip("/")
    try:
        r = requests.post(
            f"{base}/api/admin/token",
            data={"username": username, "password": password},
            timeout=30,
        )
    except requests.RequestException:
        return None
    if not r.ok:
        return None
    try:
        return r.json().get("access_token")
    except ValueError:
        return None


def fetch_users_api(panel):
    base = str(panel["url"]).rstrip("/")
    token = panel_login(base, panel["username"], panel["password"])
    if not token:
        raise RuntimeError("ورود به پنل ناموفق بود (یوزر/پس یا آدرس پنل را بررسی کنید)")
    headers = {"Authorization": f"Bearer {token}"}
    out = {}
    limit = 500
    offset = 0
    while True:
        r = requests.get(
            f"{base}/api/users?limit={limit}&offset={offset}",
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        arr = data.get("users") if isinstance(data, dict) else data
        arr = arr or []
        for u in arr:
            uid = str(u.get("id"))
            adm = u.get("admin")
            adm_name = adm.get("username") if isinstance(adm, dict) else (adm or "-")
            out[uid] = {
                "username": u.get("username"),
                "used": int(u.get("used_traffic") or 0),
                "admin": adm_name or "-",
            }
        if len(arr) < limit:
            break
        offset += limit
        if offset > 500000:
            break
    return out


def fetch_users():
    panel = load_panel()
    if panel:
        return fetch_users_api(panel)
    if not DB_CONFIG.get("password"):
        raise RuntimeError(
            "هیچ منبعی تنظیم نشده؛ از ربات /start بزنید و یوزر/پس پنل را ثبت کنید "
            "(یا PG_DB_PASSWORD را در .env بگذارید)"
        )
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.used_traffic, a.username
                FROM users u
                LEFT JOIN admins a ON a.id = u.admin_id
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return {
        str(r[0]): {
            "username": r[1],
            "used": int(r[2] or 0),
            "admin": r[3] or "-",
        }
        for r in rows
    }


def append_csv(rows):
    new = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "type", "username", "admin", "gb"])
        for r in rows:
            w.writerow(r)


def sqlite_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hourly (
            user_id INTEGER NOT NULL,
            username TEXT,
            admin TEXT,
            ts TEXT NOT NULL,
            delta_gb REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_ts ON hourly(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_uid ON hourly(user_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            user_id INTEGER NOT NULL,
            username TEXT,
            admin TEXT,
            ts TEXT NOT NULL,
            gb REAL NOT NULL,
            reason TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)")
    conn.commit()
    return conn


def query_baselines(uids, now):
    if not uids:
        return {}
    cutoff7 = (now - timedelta(days=7)).strftime("%Y-%m-%d %H")
    cutoff24 = (now - timedelta(hours=WARMUP_HOURS)).strftime("%Y-%m-%d %H")
    conn = sqlite_conn()
    try:
        cur = conn.execute(
            """
            SELECT user_id, SUM(delta_gb), MIN(ts), COUNT(*)
            FROM hourly
            WHERE ts >= ?
            GROUP BY user_id
            """,
            (cutoff7,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    uidset = {int(u) for u in uids}
    return {
        r[0]: {"sum": r[1], "min_ts": r[2], "cnt": r[3], "warm": r[2] <= cutoff24}
        for r in rows
        if r[0] in uidset
    }


def save_hourly_rows(active, now):
    if not active:
        return
    ts = now.strftime("%Y-%m-%d %H")
    conn = sqlite_conn()
    try:
        conn.executemany(
            "INSERT INTO hourly (user_id, username, admin, ts, delta_gb) VALUES (?,?,?,?,?)",
            [
                (int(uid), i["username"], i["admin"], ts, round(i["gb"], 6))
                for uid, i in active.items()
            ],
        )
        prune = (now - timedelta(days=7)).strftime("%Y-%m-%d %H")
        conn.execute("DELETE FROM hourly WHERE ts < ?", (prune,))
        conn.commit()
    finally:
        conn.close()


def record_alerts(suspicious, now):
    if not suspicious:
        return
    ts = now.strftime("%Y-%m-%d %H:%M")
    conn = sqlite_conn()
    try:
        conn.executemany(
            "INSERT INTO alerts (user_id, username, admin, ts, gb, reason) VALUES (?,?,?,?,?,?)",
            [
                (int(uid), i["username"], i["admin"], ts, round(i["gb"], 4), reason)
                for uid, i, reason in suspicious
            ],
        )
        conn.commit()
    finally:
        conn.close()


def deletion_usage_24h(uids, now):
    if not uids:
        return {}
    cutoff = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H")
    conn = sqlite_conn()
    try:
        placeholders = ",".join("?" * len(uids))
        cur = conn.execute(
            f"SELECT user_id, SUM(delta_gb) FROM hourly WHERE ts >= ? AND user_id IN ({placeholders}) GROUP BY user_id",
            [cutoff] + [int(u) for u in uids],
        )
        return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def alert_counts_by_admin(day_key):
    conn = sqlite_conn()
    try:
        cur = conn.execute(
            "SELECT admin, COUNT(*) FROM alerts WHERE ts LIKE ? GROUP BY admin",
            (day_key + " %",),
        )
        return dict(cur.fetchall())
    finally:
        conn.close()


def repeat_abusers(now):
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    conn = sqlite_conn()
    try:
        cur = conn.execute(
            """
            SELECT user_id, MAX(username), MAX(admin), COUNT(*), SUM(gb)
            FROM alerts
            WHERE ts >= ?
            GROUP BY user_id
            HAVING COUNT(*) >= ?
            ORDER BY COUNT(*) DESC
            """,
            (cutoff, REPEAT_THRESHOLD),
        )
        return cur.fetchall()
    finally:
        conn.close()


def split_message(text, limit=3800):
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("PASTE"):
        log("توکن تلگرام تنظیم نشده است")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    ok = True
    for chunk in split_message(text):
        try:
            r = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=30,
            )
            if not r.ok:
                log(f"تلگرام خطا داد: {r.status_code} {r.text[:300]}")
                ok = False
        except requests.RequestException as e:
            log(f"ارسال به تلگرام ناموفق: {e}")
            ok = False
    return ok


def build_daily_message(day_key, totals, late=False):
    now = datetime.now(IRAN_TZ)
    top = sorted(
        (
            (uid, t["username"], t["admin"], t["gb"])
            for uid, t in totals.items()
            if t["gb"] >= MIN_DAILY_GB
        ),
        key=lambda x: -x[3],
    )
    reps = repeat_abusers(now)
    repeat_uids = {str(r[0]) for r in reps}
    alerts_by_admin = alert_counts_by_admin(day_key)

    admin_gb = {}
    for t in totals.values():
        if t["gb"] > 0:
            admin_gb[t["admin"]] = admin_gb.get(t["admin"], 0.0) + t["gb"]

    sections = []

    if top:
        lines = []
        for uid, u, a, g in top[:TOP_DAILY]:
            mark = " 🔁" if uid in repeat_uids else ""
            lines.append(f"▪️ <b>{esc(u)}</b>{mark} — {g:.2f} GB")
            lines.append(f"   ادمین: {esc(a)}")
        if len(top) > TOP_DAILY:
            lines.append(f"… و {len(top) - TOP_DAILY} نفر دیگر")
        sections.append("🔝 پرمصرف‌ترین‌های امروز:\n" + "\n".join(lines))

    if admin_gb:
        lines = []
        for a, g in sorted(admin_gb.items(), key=lambda x: -x[1]):
            cnt = alerts_by_admin.get(a, 0)
            lines.append(
                f"▪️ <b>{esc(a)}</b> — {g:.2f} GB" + (f" | {cnt} هشدار" if cnt else "")
            )
        sections.append("🏪 خلاصه رسلرها:\n" + "\n".join(lines))

    if reps:
        lines = []
        for uid, uname, adm, cnt, gsum in reps:
            lines.append(f"▪️ <b>{esc(uname)}</b> — {cnt} هشدار | {gsum:.2f} GB در ۷ روز")
            lines.append(f"   ادمین: {esc(adm)}")
        sections.append("🔁 سوءاستفاده‌کنندگان مکرر (۷ روز):\n" + "\n".join(lines))

    if not sections:
        return None
    tag = " (با تاخیر)" if late else ""
    parts = [f"📈 گزارش روزانه مصرف — {day_key}{tag}\n🕐 تا ساعت 23:00 به وقت ایران"]
    parts.extend(sections)
    return "\n\n".join(parts)


def send_daily(day_key, totals, late=False):
    msg = build_daily_message(day_key, totals, late)
    if not msg:
        log(f"گزارش روزانه {day_key}: موردی بالاتر از حد نصاب نبود")
        return False
    telegram_send(msg)
    ts = datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M")
    append_csv(
        [
            [ts, "daily_late" if late else "daily", t["username"], t["admin"], round(t["gb"], 4)]
            for t in totals.values()
            if t["gb"] >= MIN_DAILY_GB
        ]
    )
    return True


def send_weekly(now):
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H")
    conn = sqlite_conn()
    try:
        total = conn.execute(
            "SELECT COALESCE(SUM(delta_gb),0) FROM hourly WHERE ts >= ?", (cutoff,)
        ).fetchone()[0]
        topu = conn.execute(
            """
            SELECT MAX(h.username), MAX(h.admin), SUM(h.delta_gb)
            FROM hourly h
            WHERE h.ts >= ?
            GROUP BY h.user_id
            ORDER BY 3 DESC LIMIT 10
            """,
            (cutoff,),
        ).fetchall()
        topa = conn.execute(
            """
            SELECT MAX(h.admin), SUM(h.delta_gb)
            FROM hourly h
            WHERE h.ts >= ?
            GROUP BY h.admin
            ORDER BY 2 DESC LIMIT 10
            """,
            (cutoff,),
        ).fetchall()
        nalerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE ts >= ?", (cutoff,)
        ).fetchone()[0]
    finally:
        conn.close()

    if not topu:
        return
    lines = [
        f"📊 گزارش هفتگی — ۷ روز گذشته\n🕐 {now.strftime('%Y-%m-%d %H:%M')} به وقت ایران",
        f"کل مصرف: {total:.2f} GB | کل هشدارها: {nalerts}",
        "\n🔝 پرمصرف‌ترین‌ها:",
    ]
    for uname, adm, g in topu:
        lines.append(f"▪️ <b>{esc(uname)}</b> — {g:.2f} GB")
        lines.append(f"   ادمین: {esc(adm)}")
    lines.append("🏪 رسلرها:")
    for adm, g in topa:
        lines.append(f"▪️ <b>{esc(adm)}</b> — {g:.2f} GB")
    telegram_send("\n".join(lines))


def run_hourly():
    state = load_state()
    now = datetime.now(IRAN_TZ)
    today = now.strftime("%Y-%m-%d")
    first_run = not state.get("snapshot")

    if state["day_key"] != today:
        old_key = state["day_key"]
        if state["day_totals"] and state.get("last_sent_day") != old_key:
            send_daily(old_key, state["day_totals"], late=True)
            state["last_sent_day"] = old_key
        state["day_totals"] = {}
        state["day_key"] = today

    users = fetch_users()
    prev = state.get("snapshot") or {}

    new_users = {}
    for uid, info in users.items():
        if uid in prev:
            d = info["used"] - prev[uid]["used"]
            if d < 0:
                d = info["used"]
            gb = d / GB
        elif first_run:
            gb = 0.0
        else:
            gb = info["used"] / GB
        new_users[uid] = {"username": info["username"], "admin": info["admin"], "gb": gb}

    deleted_items = [(uid, prev[uid]) for uid in prev if uid not in users]

    active = {uid: i for uid, i in new_users.items() if i["gb"] > 0}
    baseline = query_baselines(active.keys(), now)

    suspicious = []
    for uid, info in active.items():
        b = baseline.get(int(uid))
        reason = None
        if info["gb"] >= ABS_GB:
            reason = f"{info['gb']:.2f} گیگ در یک ساعت (آستانه {ABS_GB:.0f} گیگ)"
        elif b and b["warm"]:
            avg = b["sum"] / (7 * 24.0)
            if info["gb"] >= FLOOR_GB and info["gb"] >= MULT * avg:
                reason = (
                    f"{info['gb']:.2f} گیگ در یک ساعت — بیش از {MULT:.0f} برابر میانگین ۷ روزه‌اش "
                    f"(میانگین ساعتی: {avg:.2f} گیگ)"
                )
        if reason:
            suspicious.append((uid, info, reason))

    record_alerts(suspicious, now)
    save_hourly_rows(active, now)

    for uid, info in new_users.items():
        if info["gb"] <= 0:
            continue
        t = state["day_totals"].get(uid)
        if t is None:
            t = {"username": info["username"], "admin": info["admin"], "gb": 0.0}
            state["day_totals"][uid] = t
        t["gb"] += info["gb"]
        t["username"] = info["username"]
        t["admin"] = info["admin"]

    from_txt = state.get("snapshot_ts") or "؟"
    to_txt = now.strftime("%H:%M")

    state["snapshot"] = users
    state["snapshot_ts"] = to_txt
    save_state(state)

    ts = now.strftime("%Y-%m-%d %H:%M")

    if first_run:
        telegram_send(
            "✅ مانیتورینگ مصرف شروع شد.\n"
            f"• گزارش ساعتی: فقط مصرف مشکوک ({ABS_GB:.0f}+ گیگ در ساعت یا {MULT:.0f} برابر میانگین ۷ روزه)\n"
            f"• حذف کانفیگ: فقط اگه مصرف ۲۴ ساعت اخیر ≥ {DEL_MIN_24H_GB:.0f} گیگ باشه\n"
            "• گزارش روزانه: ساعت ۲۳ (با خلاصه رسلرها و سوءاستفاده‌کنندگان مکرر)\n"
            "• گزارش هفتگی: شنبه‌شب ساعت ۲۳"
        )

    deleted_sorted = sorted(deleted_items, key=lambda x: -x[1]["used"])
    deleted_usage = deletion_usage_24h([uid for uid, _ in deleted_items], now)
    deleted_reported = [
        (uid, d, deleted_usage.get(int(uid), 0.0))
        for uid, d in deleted_sorted
        if deleted_usage.get(int(uid), 0.0) >= DEL_MIN_24H_GB
    ]

    csv_rows = []
    if suspicious:
        suspicious.sort(key=lambda x: -x[1]["gb"])
        lines = [f"🚨 مصرف مشکوک\n🕐 {from_txt} تا {to_txt} به وقت ایران"]
        for uid, info, reason in suspicious:
            lines.append(f"▪️ <b>{esc(info['username'])}</b> — {info['gb']:.2f} GB")
            lines.append(f"   ادمین: {esc(info['admin'])}")
            lines.append(f"   دلیل: {reason}")
            csv_rows.append([ts, "alert", info["username"], info["admin"], round(info["gb"], 4)])
        telegram_send("\n".join(lines))

    if deleted_reported:
        lines = ["⚠️ حذف کانفیگ با مصرف بالا:"]
        for uid, d, g24 in deleted_reported:
            total_gb = d["used"] / GB
            lines.append(
                f"▪️ <b>{esc(d['username'])}</b> — مصرف ۲۴ ساعت: {g24:.2f} GB | کل: {total_gb:.2f} GB"
            )
            lines.append(f"   ادمین: {esc(d['admin'])}")
            csv_rows.append([ts, "deleted", d["username"], d["admin"], round(g24, 4)])
        telegram_send("\n".join(lines))

    if csv_rows:
        append_csv(csv_rows)

    if not first_run and now.hour == 23 and state.get("last_sent_day") != today:
        if send_daily(today, state["day_totals"]):
            state["last_sent_day"] = today
            state["day_totals"] = {}
            save_state(state)

    if not first_run and now.hour == 23 and now.weekday() == WEEKLY_WEEKDAY:
        send_weekly(now)


def run_daily():
    state = load_state()
    today = datetime.now(IRAN_TZ).strftime("%Y-%m-%d")
    if state.get("day_key") == today and state.get("last_sent_day") != today:
        if send_daily(today, state["day_totals"]):
            state["last_sent_day"] = today
            state["day_totals"] = {}
            save_state(state)
    else:
        log("گزارش روزانه‌ای برای ارسال وجود ندارد")


def main():
    apply_cfg()
    mode = sys.argv[1] if len(sys.argv) > 1 else "hourly"
    if not acquire_lock():
        log("یک اجرای دیگر در حال انجام است؛ خروج")
        return
    try:
        if mode == "daily":
            run_daily()
        else:
            run_hourly()
    except Exception as e:
        log(f"خطا: {e}")
    finally:
        release_lock()


if __name__ == "__main__":
    main()

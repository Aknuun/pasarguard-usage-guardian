#!/usr/bin/env python3
"""
Settings bot for config-monitor.
Long-polls Telegram and lets the owner change monitor thresholds.

UX: a settings list is shown; tapping any setting asks for a new numeric value
(typed as a text message). Values are written straight to config.json which
monitor.py reads on every hourly run.
"""
import json
import os
import sys
import time
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor  # noqa: E402  (token/chat/config helpers live in monitor.py)

TOKEN = monitor.TELEGRAM_BOT_TOKEN
OWNER = int(monitor.TELEGRAM_CHAT_ID)
CFG = monitor.CONFIG_FILE
DEFAULTS = monitor.DEFAULTS

WD_FA = {0: "دوشنبه", 1: "سه\u200cشنبه", 2: "چهارشنبه", 3: "پنجشنبه", 4: "جمعه", 5: "شنبه", 6: "یکشنبه"}

META = [
    {"key": "ABS_GB", "label": "آستانه مطلق مصرف", "dec": 1, "min": 0.1, "max": 1000.0,
     "desc": "اگر مصرف یک کاربر در یک ساعت ≥ این مقدار (گیگ) شود، هشدار مطلق صادر می‌شود."},
    {"key": "MULT", "label": "چند برابر میانگین ۷روزه", "dec": 0, "min": 1.0, "max": 100.0,
     "desc": "هشدار نسبی: اگر مصرف یک ساعت ≥ کف (FLOOR) و ≥ این ضریب × میانگین ساعتی ۷ روزهٔ خود کاربر باشد."},
    {"key": "FLOOR_GB", "label": "کف مصرف هشدار نسبی", "dec": 1, "min": 0.1, "max": 100.0,
     "desc": "حداقل گیگ در ساعت تا «هشدار نسبی» (چند برابر میانگین) اصلاً بررسی شود."},
    {"key": "MIN_DAILY_GB", "label": "حداقل مصرف در گزارش روزانه", "dec": 1, "min": 0.0, "max": 100.0,
     "desc": "کاربرانی که مصرف امروزشان کمتر از این مقدار (گیگ) باشد در گزارش روزانه نمایش داده نمی‌شوند."},
    {"key": "DEL_MIN_24H_GB", "label": "حداقل مصرف برای اخطارِ حذف", "dec": 1, "min": 0.0, "max": 1000.0,
     "desc": "اگر کاربری حذف شود ولی مصرف ۲۴ ساعت اخیرش ≥ این مقدار (گیگ) باشد، اخطار «حذف با مصرف بالا» داده می‌شود."},
    {"key": "WARMUP_HOURS", "label": "ساعت گرم‌آپ (warm-up)", "dec": 0, "min": 0, "max": 168,
     "desc": "برای کاربرِ تازه‌کار تا این تعداد ساعت فقط ثبت می‌شود و هشدار نسبی نمی‌گیرد (هشدار مطلق همچنان فعال است)."},
    {"key": "REPEAT_THRESHOLD", "label": "تعداد هشدار برای «مکرر»", "dec": 0, "min": 1, "max": 100,
     "desc": "کاربری که در ۷ روز این تعداد بار یا بیشتر هشدار بگیرد، در بخش «سوءاستفاده‌کنندگان مکرر» می‌آید."},
    {"key": "TOP_DAILY", "label": "تعداد آیتم‌های گزارش", "dec": 0, "min": 1, "max": 100,
     "desc": "چند نفر اول پرمصرف در گزارش روزانه و هفتگی نمایش داده شوند."},
    {"key": "WEEKLY_WEEKDAY", "label": "روز گزارش هفتگی", "dec": 0, "min": 0, "max": 6, "fmt": "wd",
     "desc": "گزارش هفتگی شبِ این روز ارسال می‌شود (دوشنبه=۰ … یکشنبه=۶)."},
]

# chat_id -> setting key currently awaiting a numeric reply
PENDING = {}

# First-run panel setup wizard state: chat_id -> {"step", "url", "username"}
SETUP = {}
PANEL_FILE = os.path.join(monitor.BASE_DIR, "panel.json")


def panel_configured():
    return monitor.load_panel() is not None


def save_panel(d):
    tmp = PANEL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PANEL_FILE)


def start_setup(cid):
    SETUP[cid] = {"step": "url"}
    api(
        "sendMessage",
        chat_id=cid,
        text="🔧 راه‌اندازی اولیه\n\nآدرس پنل Pasarguard را بفرستید (مثلاً https://panel.example.com):",
        parse_mode="HTML",
    )


def handle_setup(cid, text, message):
    if text.startswith("/"):
        start_setup(cid)
        return
    st = SETUP.get(cid)
    if not st:
        start_setup(cid)
        return
    if st["step"] == "url":
        url = text if text.startswith("http") else "https://" + text
        st["url"] = url.rstrip("/")
        st["step"] = "user"
        api("sendMessage", chat_id=cid, text="👤 یوزرنیم ادمین پنل:", parse_mode="HTML")
        return
    if st["step"] == "user":
        st["username"] = text
        st["step"] = "pass"
        api(
            "sendMessage",
            chat_id=cid,
            text="🔑 پسورد ادمین پنل را بفرستید (بعد از خواندن، پیام پاک می‌شود):",
            parse_mode="HTML",
        )
        return
    if st["step"] == "pass":
        api("deleteMessage", chat_id=cid, message_id=message.get("message_id"))
        st["password"] = text
        tok = monitor.panel_login(st["url"], st["username"], st["password"])
        if not tok:
            api(
                "sendMessage",
                chat_id=cid,
                text="❌ ورود ناموفق بود. یوزرنیم را دوباره بفرستید:",
                parse_mode="HTML",
            )
            st["step"] = "user"
            return
        save_panel({"url": st["url"], "username": st["username"], "password": st["password"]})
        SETUP.pop(cid, None)
        api("sendMessage", chat_id=cid, text="✅ پنل Pasarguard با موفقیت ثبت شد.", parse_mode="HTML")
        send_settings(cid)
        return


def log(msg):
    line = f"{datetime.now(monitor.IRAN_TZ).strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)


def api(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if "reply_markup" in params and isinstance(params["reply_markup"], (dict, list)):
        params["reply_markup"] = json.dumps(params["reply_markup"], ensure_ascii=False)
    try:
        return requests.post(url, data=params, timeout=30)
    except requests.RequestException as e:
        log(f"api error {method}: {e}")
        return None


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG):
        try:
            with open(CFG, encoding="utf-8") as f:
                data = json.load(f)
            for k in DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
        except (json.JSONDecodeError, OSError) as e:
            log(f"config read error: {e}")
    return cfg


def save_cfg(cfg):
    tmp = CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG)


def fmt_val(m, v):
    v = float(v)
    if m.get("fmt") == "wd":
        return WD_FA.get(int(v), str(int(v)))
    if m["dec"] == 0:
        return str(int(round(v)))
    return f"{v:.{m['dec']}f}"


def ib(text, data):
    return {"text": text, "callback_data": data}


def settings_view():
    cfg = load_cfg()
    kb = []
    for m in META:
        val = cfg.get(m["key"], DEFAULTS[m["key"]])
        kb.append([ib(f"⚙️ {m['label']}: {fmt_val(m, val)}", f"edit:{m['key']}")])
    kb.append([ib("🔄 وضعیت", "status"), ib("❓ راهنما", "help"), ib("✖️ بستن", "close")])
    text = (
        "⚙️ تنظیمات مانیتور مصرف\n\n"
        "روی هر تنظیم بزنید، سپس مقدار جدید را به‌صورت عدد بنویسید.\n"
        "تغییرات بلافاصله ذخیره و در اجرای بعدی مونیتور (ساعت :30) اعمال می‌شود."
    )
    return text, kb


def status_text():
    try:
        with open(monitor.STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        snap = st.get("snapshot") or {}
        totals = st.get("day_totals") or {}
        day_gb = sum(t.get("gb", 0) for t in totals.values())
        return (
            "📊 وضعیت مانیتور\n"
            f"• آخرین به‌روزرسانی: {st.get('snapshot_ts') or '—'}\n"
            f"• کاربران دارای مصرف (base): {len(snap):,}\n"
            f"• مصرف ثبت‌شدهٔ امروز: {day_gb:.2f} GB\n"
            f"• روز جاری: {st.get('day_key')}\n"
            "• اجرای بعدی: هر ساعت دقیقهٔ :30"
        )
    except Exception as e:
        return f"وضعیت در دسترس نیست: {e}"


def help_text():
    return (
        "ℹ️ راهنمای دکمه‌ها\n"
        "روی هر تنظیم بزنید و عدد جدید را تایپ کنید (لغو با /cancel).\n\n"
        "📌 هشدارها: مطلق (ABS_GB گیگ در ساعت) یا نسبی (MULT × میانگین ۷روزه، با کف FLOOR_GB).\n"
        "📌 گزارش روزانه ساعت ۲۳ و هفتگی شبِ روزِ WEEKLY_WEEKDAY ارسال می‌شود.\n"
        "📌 «حذف با مصرف بالا» فقط وقتی مصرف ۲۴س > DEL_MIN_24H_GB باشد.\n\n"
        "هر تغییر همان لحظه در config.json ذخیره می‌شود."
    )


def set_value(key, raw):
    """Parse/validate a numeric input for a setting. Returns (value, error)."""
    m = next((x for x in META if x["key"] == key), None)
    if not m:
        return None, "تنظیم ناشناخته"
    try:
        num = float(raw.replace(",", ".").replace("٫", "."))
    except ValueError:
        return None, "این یک عدد معتبر نیست."
    if num < m["min"] or num > m["max"]:
        return None, f"عدد باید بین {fmt_val(m, m['min'])} و {fmt_val(m, m['max'])} باشد."
    if m["dec"] == 0:
        num = int(round(num))
    else:
        num = round(num, m["dec"])
    cfg = load_cfg()
    cfg[key] = num
    save_cfg(cfg)
    return num, None


def send_settings(chat_id):
    text, kb = settings_view()
    api("sendMessage", chat_id=chat_id, text=text, reply_markup={"inline_keyboard": kb}, parse_mode="HTML")


def edit_settings(message_id, chat_id):
    text, kb = settings_view()
    api("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
        reply_markup={"inline_keyboard": kb}, parse_mode="HTML")


def ask_value(chat_id, key):
    m = next((x for x in META if x["key"] == key), None)
    if not m:
        return
    cfg = load_cfg()
    cur = fmt_val(m, cfg.get(key, DEFAULTS[key]))
    PENDING[chat_id] = key
    text = (
        f"✏️ <b>{m['label']}</b>\n"
        f"مقدار فعلی: <b>{cur}</b>\n"
        f"{m['desc']}\n\n"
        f"عدد جدید (بین {fmt_val(m, m['min'])} تا {fmt_val(m, m['max'])}) را بفرستید؛ "
        f"برای لغو /cancel"
    )
    api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def handle_callback(cb):
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    cid = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    qid = cb.get("id")
    if cid != OWNER:
        api("answerCallbackQuery", callback_query_id=qid, text="⛔ دسترسی ندارید")
        return

    if data.startswith("edit:"):
        key = data.split(":", 1)[1]
        api("answerCallbackQuery", callback_query_id=qid, text="مقدار جدید را بنویسید")
        ask_value(cid, key)
    elif data == "status":
        api("answerCallbackQuery", callback_query_id=qid)
        api("editMessageText", chat_id=cid, message_id=mid, text=status_text(),
            reply_markup={"inline_keyboard": [[ib("⚙️ تنظیمات", "back")]]}, parse_mode="HTML")
    elif data == "help":
        api("answerCallbackQuery", callback_query_id=qid)
        api("editMessageText", chat_id=cid, message_id=mid, text=help_text(),
            reply_markup={"inline_keyboard": [[ib("⚙️ تنظیمات", "back")]]}, parse_mode="HTML")
    elif data == "close":
        api("answerCallbackQuery", callback_query_id=qid, text="بسته شد")
        api("deleteMessage", chat_id=cid, message_id=mid)
    elif data == "back" or data == "open_settings":
        api("answerCallbackQuery", callback_query_id=qid)
        if mid is not None:
            edit_settings(mid, cid)


def handle_message(message):
    cid = message.get("chat", {}).get("id")
    if cid != OWNER:
        return
    text = (message.get("text") or "").strip()

    if text == "/panel":
        SETUP.pop(cid, None)
        start_setup(cid)
        return
    if not panel_configured():
        handle_setup(cid, text, message)
        return

    if text in ("/cancel",):
        if PENDING.pop(cid, None):
            api("sendMessage", chat_id=cid, text="✅ لغو شد.", parse_mode="HTML")
        send_settings(cid)
        return

    key = PENDING.get(cid)
    if key:
        val, err = set_value(key, text)
        if err:
            api("sendMessage", chat_id=cid, text=f"❌ {err}\nعدد جدید را دوباره بفرستید یا /cancel", parse_mode="HTML")
            return
        PENDING.pop(cid, None)
        m = next((x for x in META if x["key"] == key))
        api("sendMessage", chat_id=cid, text=f"✅ {m['label']} → <b>{fmt_val(m, val)}</b> ذخیره شد.", parse_mode="HTML")
        send_settings(cid)
        return

    if text == "/settings" or text == "⚙️ تنظیمات":
        send_settings(cid)
    elif text == "/status":
        api("sendMessage", chat_id=cid, text=status_text(), parse_mode="HTML")
    elif text in ("/start", "/help", "❓ راهنما"):
        api("sendMessage", chat_id=cid, text=help_text(),
            reply_markup={"inline_keyboard": [[ib("⚙️ تنظیمات", "open_settings")]]}, parse_mode="HTML")


def set_commands():
    cmds = [
        {"command": "settings", "description": "تغییر تنظیمات و آستانه‌های مانیتور"},
        {"command": "status", "description": "وضعیت فعلی مانیتور"},
        {"command": "panel", "description": "ثبت/تغییر اتصال پنل Pasarguard"},
        {"command": "help", "description": "راهنما"},
    ]
    api("setMyCommands", commands=json.dumps(cmds))


def poll_forever():
    offset = 0
    while True:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                data={"timeout": 25, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=40,
            )
            if not r.ok:
                log(f"getUpdates http {r.status_code}")
                time.sleep(3)
                continue
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    handle_callback(u["callback_query"])
                elif "message" in u:
                    handle_message(u["message"])
        except Exception as e:
            log(f"poll error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    set_commands()
    log("settings bot started")
    poll_forever()

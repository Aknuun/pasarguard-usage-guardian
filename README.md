<div dir="rtl">

# نگهبان مصرف پاسارگارد

> **PasarGuard Usage Guardian** — مانیتورینگ خودکار مصرف کاربران پنل Pasarguard و ارسال گزارش به تلگرام.

این پروژه مصرف کاربران را از دیتابیس **Pasarguard** می‌خواند، هر ساعت اختلاف مصرف را محاسبه می‌کند و موارد مشکوک، حذف‌های پرمصرف، گزارش روزانه و هفتگی را به تلگرام می‌فرستد. یک ربات تلگرامی سبک هم برای تغییر آستانه‌ها از داخل چت دارد.

## امکانات

- **هشدار مصرف مشکوک (ساعتی):** آستانهٔ مطلق (`ABS_GB` گیگ در ساعت) یا نسبی (`MULT` برابر میانگین ۷ روزه با کف `FLOOR_GB`).
- **گزارش روزانه (ساعت ۲۳):** پرمصرف‌ترین کاربران + خلاصهٔ رسلرها + سوءاستفاده‌کنندگان مکرر.
- **گزارش هفتگی:** شبِ روزِ انتخابی (`WEEKLY_WEEKDAY`).
- **اخطار «حذف با مصرف بالا»:** اگر کاربری حذف شود و مصرف ۲۴ ساعت اخیرش از حدی بیشتر باشد.
- **تاریخچه:** ذخیرهٔ ساعتی در SQLite (`history.db`) و CSV.
- **ربات تنظیمات تلگرام:** تغییر آستانه‌ها بدون دست‌زدن به کد/فایل.
- **قفل اجرا (lock):** جلوگیری از اجرای هم‌زمان.
- **بدون DST:** زمان‌ها بر اساس `Asia/Tehran`.

## پیش‌نیازها

- Python 3.10+ (تست‌شده روی 3.12)
- دسترسی به دیتابیس Pasarguard (MySQL/MariaDB)
- یک ربات تلگرام (توکن از [@BotFather](https://t.me/BotFather)) و آیدی عددی ادمین

## نصب

</div>

```bash
git clone https://github.com/Aknuun/pasarguard-usage-guardian.git
cd pasarguard-usage-guardian
python3 -m pip install -r requirements.txt
cp .env.example .env
# مقادیر .env را پر کنید (DB + توکن ربات + chat id)
cp config.example.json config.json   # اختیاری؛ مقادیر پیش‌فرض داخل کد هستند
```

<div dir="rtl">

## اجرا

مانیتور را می‌توان به‌صورت ساعتی با cron اجرا کرد:

</div>

```cron
30 * * * * /usr/bin/python3 /root/config-monitor/monitor.py hourly >> /root/config-monitor/cron.log 2>&1
```

<div dir="rtl">

ربات تنظیمات به‌صورت سرویس systemd:

</div>

```bash
sudo cp systemd/pasarguard-usage-guardian-settings.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pasarguard-usage-guardian-settings
```

<div dir="rtl">

> مسیرها را در فایل سرویس و cron مطابق نصب خودتان تنظیم کنید.

## تنظیمات (`.env`)

| متغیر | توضیح |
|---|---|
| `PG_DB_HOST` | هاست دیتابیس Pasarguard |
| `PG_DB_PORT` | پورت (پیش‌فرض 3306) |
| `PG_DB_USER` | یوزر دیتابیس |
| `PG_DB_PASSWORD` | رمز دیتابیس |
| `PG_DB_NAME` | نام دیتابیس (پیش‌فرض `pasarguard`) |
| `PG_BOT_TOKEN` | توکن ربات تلگرام |
| `PG_CHAT_ID` | آیدی عددی دریافت‌کنندهٔ گزارش‌ها |
| `PG_MONITOR_DIR` | مسیر داده‌ها (اختیاری؛ پیش‌فرض پوشهٔ اسکریپت) |

آستانه‌ها در `config.json` هستند و از ربات (`/settings`) هم قابل تغییرند:
`ABS_GB`, `MULT`, `FLOOR_GB`, `WARMUP_HOURS`, `MIN_DAILY_GB`, `DEL_MIN_24H_GB`, `REPEAT_THRESHOLD`, `TOP_DAILY`, `WEEKLY_WEEKDAY`.

## دستورات ربات

- `/settings` — تغییر آستانه‌ها
- `/status` — وضعیت مانیتور
- `/help` — راهنما

## ساختار

</div>

```
monitor.py        # قلب مانیتور: خواندن دیتابیس، محاسبه، هشدار، گزارش
settings_bot.py   # ربات تلگرام برای تغییر آستانه‌ها
config.json       # آستانه‌های قابل‌تغییر (gitignore)
.history.db       # تاریخچهٔ ساعتی SQLite (gitignore)
systemd/          # فایل سرویس ربات تنظیمات
```

<div dir="rtl">

## امنیت

- فایل `.env` و داده‌های زمان اجرا هرگز commit نمی‌شوند (`.gitignore`).
- هرگز توکن ربات و رمز دیتابیس را در کد نگذارید.

## لایسنس

MIT

</div>

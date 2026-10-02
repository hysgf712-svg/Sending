"""Small health/status web app that runs alongside the Telegram bot."""

import asyncio
import html
import json
import os
from pathlib import Path
from aiohttp import web

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_DIR / "config.json"
ENV_FILE = PROJECT_DIR / ".env"
REQUIRED = ["API_ID", "API_HASH", "BOT_TOKEN", "OWNER_ID"]


def load_env_file():
    """Load a private .env file without overriding variables set by the host."""
    if not ENV_FILE.exists():
        return
    try:
        for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '\"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        print(f"Could not read .env file: {exc}", flush=True)


load_env_file()

bot_task = None
bot_error = ""


def load_config_file():
    """Optional local configuration fallback. Keep secrets out of committed config files."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def merged_config():
    config = load_config_file()
    return {key: os.getenv(key) or str(config.get(key, "")).strip() for key in REQUIRED}


def missing_config():
    values = merged_config()
    missing = [key for key in REQUIRED if not str(values.get(key, "")).strip()]
    for key in ("API_ID", "OWNER_ID"):
        value = str(values.get(key, "")).strip()
        if value and not value.isdigit() and key not in missing:
            missing.append(f"{key} (باید عدد باشد)")
    return missing


def apply_config_to_env():
    for key, value in merged_config().items():
        if str(value).strip():
            os.environ[key] = str(value).strip()


async def monitor_bot_task(task):
    global bot_error
    try:
        await task
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        bot_error = str(exc)
        print(f"BOT TASK CRASHED: {exc}", flush=True)


async def start_bot_if_ready():
    global bot_task, bot_error
    if bot_task and not bot_task.done():
        return True
    if missing_config():
        return False
    try:
        apply_config_to_env()
        import bot_app

        bot_task = asyncio.create_task(bot_app.main(), name="telegram-bot")
        asyncio.create_task(monitor_bot_task(bot_task), name="telegram-bot-monitor")
        bot_error = ""
        return True
    except Exception as exc:
        bot_error = str(exc)
        print(f"BOT START ERROR: {exc}", flush=True)
        return False


STYLE = """
body{font-family:Tahoma,Arial,sans-serif;background:#0f172a;color:#e5e7eb;margin:0;direction:rtl}
.wrap{max-width:760px;margin:45px auto;padding:24px}
.card{background:#111827;border:1px solid #334155;border-radius:18px;padding:24px;box-shadow:0 20px 60px #0008}
h1{margin-top:0;color:#fff;font-size:26px}.muted{color:#cbd5e1;line-height:2}
.ok{background:#064e3b;border:1px solid #10b981;color:#d1fae5;padding:12px;border-radius:12px;margin:12px 0}
.err{background:#7f1d1d;border:1px solid #ef4444;color:#fee2e2;padding:12px;border-radius:12px;margin:12px 0}
code{background:#020617;padding:2px 6px;border-radius:6px;direction:ltr;display:inline-block}.ltr{direction:ltr;text-align:left}
"""


def page(content):
    return web.Response(
        text=(
            "<!doctype html><html lang='fa'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>وضعیت ربات</title><style>" + STYLE + "</style></head>"
            "<body><div class='wrap'><div class='card'>" + content + "</div></div></body></html>"
        ),
        content_type="text/html",
    )


async def index(request):
    started = await start_bot_if_ready()
    missing = missing_config()
    if missing:
        names = ", ".join(html.escape(name) for name in missing)
        return page(
            "<h1>⚙️ راه‌اندازی ربات</h1>"
            "<div class='err'>متغیرهای تنظیمات ناقص است: " + names + "</div>"
            "<p class='muted'>برای امنیت، اطلاعات حساس از طریق این صفحهٔ عمومی دریافت یا ذخیره نمی‌شود. "
            "در تنظیمات سرویس میزبانی، متغیرهای محیطی <code>API_ID</code>، <code>API_HASH</code>، "
            "<code>BOT_TOKEN</code> و <code>OWNER_ID</code> را تنظیم و سرویس را راه‌اندازی مجدد کنید.</p>"
            "<p class='muted'>برای ماندگاری اعضا، دکمه‌ها و پیام‌های سفارشی، یک فضای ذخیره‌سازی پایدار برای پوشهٔ "
            "<code>data/</code> متصل کنید.</p>"
        )

    status = "ربات فعال است ✅" if started and bot_task and not bot_task.done() else "تنظیمات ثبت شده؛ ربات در حال شروع است."
    error = f"<div class='err'>{html.escape(bot_error)}</div>" if bot_error else ""
    return page(
        "<h1>🤖 ربات سفارشی</h1>"
        f"<div class='ok'>{html.escape(status)}</div>"
        + error
        + "<p class='muted'>این پروژه پیام شروع و دکمه‌های پاسخ‌دار/لینکی، جوین اجباری و ارسال همگانی چندپیامه دارد.</p>"
        "<p class='muted'>وضعیت سلامت سرویس: <a style='color:#93c5fd' href='/health'>/health</a></p>"
    )


async def setup_info(request):
    return await index(request)


async def health(request):
    return web.json_response(
        {
            "ok": True,
            "configured": not bool(missing_config()),
            "bot_running": bool(bot_task and not bot_task.done()),
            "bot_error": bot_error,
        }
    )


async def on_startup(app):
    await start_bot_if_ready()


async def on_cleanup(app):
    if bot_task and not bot_task.done():
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)


def create_app():
    application = web.Application()
    application.router.add_get("/", index)
    application.router.add_get("/setup", setup_info)
    application.router.add_get("/health", health)
    application.on_startup.append(on_startup)
    application.on_cleanup.append(on_cleanup)
    return application


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"Status web server running on port {port}", flush=True)
    web.run_app(create_app(), host="0.0.0.0", port=port)

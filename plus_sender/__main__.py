"""Точка входу: запускає бот і фоновий моніторинг тривоги в одному asyncio-процесі.

Запуск:
    python -m plus_sender
"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from .alarm import AlarmMonitor
from .bot.handlers import register as register_handlers
from .config import PROJECT_ROOT, Settings, ensure_runtime_dirs
from .sender import broadcast_for_all_users

LOGS_DIR = PROJECT_ROOT / "logs"


def _configure_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Консоль ──
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    # ── Файл: всі INFO+ (ротація 5 МБ, зберігаємо 7 файлів) ──
    file_all = RotatingFileHandler(
        LOGS_DIR / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    file_all.setFormatter(fmt)

    # ── Файл: тільки ERROR+ (окремий для швидкого пошуку проблем) ──
    file_err = RotatingFileHandler(
        LOGS_DIR / "errors.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_err.setLevel(logging.ERROR)
    file_err.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_all)
    root.addHandler(file_err)

    # Trim noisy libs
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    # ── Прибираємо шум від інтернет-сканерів ──
    # Порт-сканери, шукачі вразливостей і HTTP/2-проби постійно тицяють
    # у відкритий webhook-порт. aiohttp коректно відхиляє їх з 400, але
    # це засмічує errors.log. Лишаємо тільки справжні серверні помилки.
    class _DropScannerNoise(logging.Filter):
        _NOISE_KEYWORDS = (
            "Pause on PRI/Upgrade",          # HTTP/2 проба
            "BadHttpMessage",                # сміттєвий HTTP
            "InvalidURLError",               # «GET stager64 HTTP/1.1» тощо
            "LineTooLong",                   # довгі заголовки-проби
            "BadStatusLine",                 # криві response/request
            "Got more than",                 # «more than 8190 bytes when reading»
            "Unexpected char in url",        # невалідний URL
        )

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            for kw in self._NOISE_KEYWORDS:
                if kw in msg:
                    return False
            # Те саме, але якщо все сховалось у exc_info
            if record.exc_info:
                exc_text = str(record.exc_info[1]) if record.exc_info[1] else ""
                for kw in self._NOISE_KEYWORDS:
                    if kw in exc_text:
                        return False
            return True

    logging.getLogger("aiohttp.server").addFilter(_DropScannerNoise())
    logging.getLogger("aiohttp.web").addFilter(_DropScannerNoise())


async def _on_alarm_change(mode: str) -> None:
    """Колбек, який викликає alarm-monitor при зміні стану."""
    log = logging.getLogger("alarm-callback")
    if mode == "alert":
        log.info("⚠️ УВАГА! Почалася повітряна тривога — запускаю розсилку (alert).")
    else:
        log.info("✅ Тривогу скасовано — запускаю розсилку (clear).")
    try:
        await broadcast_for_all_users(mode)
    except Exception:
        log.exception("Помилка під час розсилки")


async def _run_mono_server(bot, settings: Settings, log) -> None:
    """Запускає aiohttp-сервер для Monobank webhook (якщо MONO_TOKEN задано)."""
    if not settings.mono_token:
        return

    from aiohttp import web
    from .mono_webhook import build_app, register_webhook

    app = build_app(bot, settings.mono_jar_id)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.mono_webhook_port)
    await site.start()
    log.info("🌐 Monobank webhook сервер запущено на порту %d", settings.mono_webhook_port)

    # Автореєстрація webhook якщо задано MONO_WEBHOOK_URL
    import os
    webhook_url = (os.getenv("MONO_WEBHOOK_URL") or "").strip()
    if webhook_url:
        await register_webhook(settings.mono_token, webhook_url)


async def main() -> None:
    _configure_logging()
    log = logging.getLogger("plus_sender")

    settings = Settings.load()
    ensure_runtime_dirs()

    # ── Проксі (необов'язково) ──
    session: AiohttpSession | None = None
    if settings.telegram_proxy:
        proxy_url = settings.telegram_proxy
        if proxy_url.startswith("socks"):
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy_url)
                session = AiohttpSession(connector=connector)
                log.info("🔀 Telegram через SOCKS-проксі: %s", proxy_url.split("@")[-1])
            except ImportError:
                log.warning("aiohttp-socks не встановлено — ігноруємо SOCKS-проксі. "
                            "Запустіть: pip install aiohttp-socks")
        else:
            session = AiohttpSession(proxy=proxy_url)
            log.info("🔀 Telegram через HTTP-проксі: %s", proxy_url.split("@")[-1])

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        **({"session": session} if session else {}),
    )
    dp = Dispatcher(storage=MemoryStorage())
    register_handlers(dp)

    monitor = AlarmMonitor(settings, on_change=_on_alarm_change)
    monitor.start()

    # ── Monobank webhook сервер (якщо налаштовано) ──
    await _run_mono_server(bot, settings, log)

    log.info("✅ Plus Sender 2.0 запущено")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("⏹ Зупинка…")
        await monitor.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

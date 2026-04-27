"""Точка входу: запускає бот і фоновий моніторинг тривоги в одному asyncio-процесі.

Запуск:
    python -m plus_sender
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from .alarm import AlarmMonitor
from .bot.handlers import register as register_handlers
from .config import Settings, ensure_runtime_dirs
from .sender import broadcast_for_all_users


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Trim noisy libs
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


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

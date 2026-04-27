"""Моніторинг повітряної тривоги через ukrainealarm.com API.

Async-варіант: працює як фонова asyncio-задача в одному процесі з ботом.
Замість subprocess викликає sender.broadcast_for_all_users напряму.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import aiohttp

from .config import Settings

log = logging.getLogger(__name__)

AlertCallback = Callable[[str], Awaitable[None]]  # mode: "alert" | "clear"


class AlarmMonitor:
    """Опитує API ukrainealarm.com і викликає callback на зміну стану."""

    def __init__(self, settings: Settings, on_change: AlertCallback) -> None:
        self.settings = settings
        self.on_change = on_change
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_state: Optional[bool] = None
        self.url = (
            f"https://api.ukrainealarm.com/api/v3/alerts/{settings.alarm_region_id}"
        )
        self.headers = {"Authorization": settings.alarm_api_key}

    async def _fetch_active(self, session: aiohttp.ClientSession) -> Optional[bool]:
        try:
            async with session.get(self.url, headers=self.headers, timeout=10) as resp:
                if resp.status != 200:
                    log.warning("ukrainealarm API повернув %s", resp.status)
                    return None
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    log.warning("Невалідний JSON у відповіді")
                    return None
                if not payload or not isinstance(payload, list):
                    return None
                alerts = payload[0].get("activeAlerts") or []
                return len(alerts) > 0
        except asyncio.TimeoutError:
            log.warning("Таймаут запиту до ukrainealarm API")
            return None
        except Exception as e:
            log.warning("Помилка запиту: %s: %s", type(e).__name__, e)
            return None

    async def _loop(self) -> None:
        log.info(
            "🚨 Моніторинг тривоги стартував (region=%s, interval=%ds)",
            self.settings.alarm_region_id,
            self.settings.alarm_poll_interval,
        )
        async with aiohttp.ClientSession() as session:
            while not self._stop.is_set():
                state = await self._fetch_active(session)

                if state is None:
                    log.info("📡 Перевірка тривоги → ⚠️  немає відповіді від API")
                elif state != self._last_state:
                    if self._last_state is None:
                        # перший достовірний стан — без розсилки
                        status_str = "🚨 ТРИВОГА" if state else "🟢 СПОКІЙ"
                        log.info("📡 Перевірка тривоги → %s  (початковий стан)", status_str)
                    else:
                        mode = "alert" if state else "clear"
                        status_str = "🚨 ТРИВОГА" if state else "🟢 СПОКІЙ"
                        log.info("📡 Перевірка тривоги → %s  ⚡ ЗМІНА СТАНУ, запускаю розсилку…", status_str)
                        try:
                            await self.on_change(mode)
                        except Exception:
                            log.exception("Помилка в callback alarm")
                    self._last_state = state
                else:
                    status_str = "🚨 ТРИВОГА" if state else "🟢 СПОКІЙ"
                    log.info("📡 Перевірка тривоги → %s  (без змін)", status_str)

                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.settings.alarm_poll_interval,
                    )
                except asyncio.TimeoutError:
                    pass

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="alarm-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

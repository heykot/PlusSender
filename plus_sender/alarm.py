"""Моніторинг повітряної тривоги через ukrainealarm.com API.

Async-варіант: працює як фонова asyncio-задача в одному процесі з ботом.
Замість subprocess викликає sender.broadcast_for_all_users напряму.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

import aiohttp

from .config import Settings

log = logging.getLogger(__name__)

AlertCallback = Callable[[str], Awaitable[None]]  # mode: "alert" | "clear"
OutageCallback = Callable[[int], Awaitable[None]]  # хвилин без відповіді API

API_DOWN_ALERT_AFTER = timedelta(minutes=10)

# Запущений монітор — щоб адмін-панель могла показати поточний стан
CURRENT: Optional["AlarmMonitor"] = None


class AlarmMonitor:
    """Опитує API ukrainealarm.com і викликає callback на зміну стану."""

    def __init__(
        self,
        settings: Settings,
        on_change: AlertCallback,
        on_api_down: Optional[OutageCallback] = None,
        on_api_up: Optional[OutageCallback] = None,
    ) -> None:
        self.settings = settings
        self.on_change = on_change
        self.on_api_down = on_api_down
        self.on_api_up = on_api_up
        self._fail_since: Optional[datetime] = None
        self._down_alerted = False
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._last_state: Optional[bool] = None
        self.last_check: Optional[datetime] = None      # остання успішна відповідь API
        self.last_change: Optional[datetime] = None     # коли востаннє змінився стан
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
                if state is not None:
                    self.last_check = datetime.now()
                await self._track_outage(state is not None)

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
                    self.last_change = datetime.now()
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

    async def _track_outage(self, ok: bool) -> None:
        """Після API_DOWN_ALERT_AFTER без відповіді — один раз кличемо on_api_down,
        після відновлення — on_api_up."""
        now = datetime.now()
        if not ok:
            self._fail_since = self._fail_since or now
            if not self._down_alerted and now - self._fail_since >= API_DOWN_ALERT_AFTER:
                self._down_alerted = True
                log.error("API тривог не відповідає вже %s", now - self._fail_since)
                if self.on_api_down:
                    try:
                        await self.on_api_down(int((now - self._fail_since).total_seconds() // 60))
                    except Exception:
                        log.exception("on_api_down")
            return
        if self._down_alerted and self.on_api_up:
            try:
                await self.on_api_up(int((now - self._fail_since).total_seconds() // 60))
            except Exception:
                log.exception("on_api_up")
        self._fail_since = None
        self._down_alerted = False

    @property
    def is_alert(self) -> Optional[bool]:
        return self._last_state

    def start(self) -> None:
        global CURRENT
        CURRENT = self
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

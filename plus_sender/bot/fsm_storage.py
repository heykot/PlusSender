"""FSM-сховище в JSON-файлі: крок майстра переживає рестарт бота.

MemoryStorage губив стан при кожному рестарті — людина надсилала текст,
а бот мовчав, бо вже «не памʼятав», що чекає на нього.

Файл невеликий (лише активні кроки), тож перезаписуємо його цілком
атомарно при кожній зміні. Права 0600: під час підключення там лежать
номер телефону й phone_code_hash. Записи, не чіпані STALE_AFTER, відкидаються.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StateType, StorageKey

from ..storage import _atomic_write_json

log = logging.getLogger(__name__)

STALE_AFTER = 7 * 24 * 3600


class JsonFileStorage(BaseStorage):
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._keys = DefaultKeyBuilder(with_destiny=True, with_business_connection_id=True)
        self._items: dict[str, dict[str, Any]] = self._load()

    # ── файл ──
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("FSM-стан не прочитано (%s) — починаємо з чистого", exc)
            return {}
        now = time.time()
        return {
            k: v for k, v in raw.items()
            if isinstance(v, dict) and now - float(v.get("ts", 0)) < STALE_AFTER
        }

    def _save(self) -> None:
        # default=str — щоб випадкове не-JSON значення не зламало збереження
        _atomic_write_json(str(self.path), self._items, indent=None, default=str)

    def _touch(self, key: StorageKey, **fields) -> None:
        k = self._keys.build(key)
        item = {**self._items.get(k, {}), **fields, "ts": time.time(),
                "chat": key.chat_id, "user": key.user_id}
        if item.get("state") is None and not item.get("data"):
            self._items.pop(k, None)
        else:
            self._items[k] = item
        self._save()

    # ── BaseStorage ──
    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        self._touch(key, state=state.state if isinstance(state, State) else state)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return self._items.get(self._keys.build(key), {}).get("state")

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        self._touch(key, data=dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict(self._items.get(self._keys.build(key), {}).get("data") or {})

    async def close(self) -> None:
        pass

    # ── для старту бота ──
    def active(self) -> Iterator[tuple[int, int, str]]:
        """(chat_id, user_id, state) усіх незавершених кроків."""
        for v in list(self._items.values()):
            if v.get("state") and "chat" in v and "user" in v:
                yield int(v["chat"]), int(v["user"]), v["state"]

    def drop(self, chat_id: int, user_id: int) -> None:
        for k in [k for k, v in self._items.items() if (v.get("chat"), v.get("user")) == (chat_id, user_id)]:
            del self._items[k]
        self._save()

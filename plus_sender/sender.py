"""In-process розсилка через Telethon-сесії всіх активних користувачів."""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime
from glob import glob
from typing import Optional

from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.errors import RPCError
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeVideo,
)

from .config import SESSIONS_DIR, USERS_DIR
from .storage import (
    delay_for_target,
    get_default_media,
    get_target_forward_mode,
    get_target_forward_source,
    get_target_forward_used,
    get_target_media,
    get_target_messages,
    get_target_type,
    get_targets,
    is_in_schedule,
    load_user_json,
    mark_target_forward_used,
    message_for_target,
    save_user_json,
)

log = logging.getLogger(__name__)


# ─────────────────────── Визначення типу медіа ───────────────────────

def _msg_kind(msg) -> str:
    """Визначає тип медіа з Telethon-повідомлення."""
    doc = getattr(getattr(msg, "media", None), "document", None)
    if doc:
        for attr in getattr(doc, "attributes", []):
            if isinstance(attr, DocumentAttributeVideo) and getattr(attr, "round_message", False):
                return "video_note"
            if isinstance(attr, DocumentAttributeAudio) and getattr(attr, "voice", False):
                return "voice"
    if hasattr(getattr(msg, "media", None), "photo"):
        return "photo"
    return "document"


async def _send_media_no_fwd(client: TelegramClient, entity, msg) -> None:
    """Надсилає медіа з Telethon-повідомлення БЕЗ тегу 'Переслано від'."""
    kind = _msg_kind(msg)
    kw: dict = {}
    if kind == "video_note":
        kw["video_note"] = True
    elif kind == "voice":
        kw["voice_note"] = True
    await client.send_file(entity, msg.media, **kw)


# ─────────────────────── Отримання повідомлень з чату-джерела ───────────────────────

def _is_video_note_msg(msg) -> bool:
    """Перевіряє чи повідомлення є відео-кружком."""
    from telethon.tl.types import DocumentAttributeVideo
    doc = getattr(getattr(msg, "media", None), "document", None)
    if not doc:
        return False
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeVideo) and getattr(attr, "round_message", False):
            return True
    return False


async def _fetch_source_msgs(client: TelegramClient, chat_id: int) -> list:
    """Повертає список відео-кружків з вказаного чату (відсортованих за msg_id)."""
    try:
        try:
            real_id, peer_cls = tl_utils.resolve_id(chat_id)
            src_entity = await client.get_input_entity(peer_cls(real_id))
        except (ValueError, KeyError):
            src_entity = await client.get_entity(chat_id)

        msgs = []
        total = 0
        async for sm in client.iter_messages(src_entity, limit=200):
            total += 1
            if _is_video_note_msg(sm):
                msgs.append(sm)

        # Сортуємо за ID щоб порядок був детермінованим після рестарту
        msgs.sort(key=lambda m: m.id)

        log.info("src-fetch chat_id=%d: всього=%d  кружків=%d", chat_id, total, len(msgs))
        return msgs
    except Exception as exc:
        log.warning("src-fetch chat_id=%d ПОМИЛКА: %s", chat_id, exc)
        return []


def _pick_round_robin(msgs: list, used_ids: list[int]) -> Optional[object]:
    """Вибирає наступний невідправлений кружок по черзі (sequential, не random).

    Список відсортований за msg_id — тому після рестарту порядок однаковий.
    Якщо всі відправлено — починає нове коло з найменшого ID.
    """
    used_set = set(used_ids)
    avail = [m for m in msgs if m.id not in used_set]
    if not avail:
        # Всі відправлено — починаємо нове коло
        avail = msgs
    return avail[0] if avail else None


# ─────────────────────── Основна логіка розсилки ───────────────────────

async def _send_for_user(json_path: str, mode: str) -> tuple[str, bool, Optional[str]]:
    """Робить розсилку для одного користувача. Повертає (username, success, err)."""
    username_base = os.path.splitext(os.path.basename(json_path))[0]
    try:
        data = load_user_json(json_path)
    except Exception as e:
        return username_base, False, f"bad json: {e}"

    if not data.get("status", False):
        return username_base, False, "status off"
    if not (data.get("api_id") and data.get("api_hash")):
        return username_base, False, "missing api credentials"

    targets = get_targets(data)
    if not targets:
        return username_base, False, "no targets"

    # Перевірка терміну доступу
    access_until = data.get("access_until")
    if not access_until:
        return username_base, False, "no access"
    try:
        if datetime.now() > datetime.strptime(str(access_until), "%Y-%m-%d"):
            return username_base, False, f"expired ({access_until})"
    except ValueError:
        return username_base, False, f"bad access_until: {access_until}"

    # Перевірка розкладу роботи
    if not is_in_schedule(data):
        from_time = data.get("schedule_from", "?")
        to_time   = data.get("schedule_to",   "?")
        return username_base, False, f"out of schedule ({from_time}–{to_time})"

    session_path = os.path.join(SESSIONS_DIR, username_base)
    api_id = int(data["api_id"])
    api_hash = str(data["api_hash"])

    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return username_base, False, "session not authorized"

        # ── Глобальний медіа-дефолт (файл, завантажений вручну) ──
        global_media = get_default_media(data, mode)
        global_media_path: Optional[str] = None
        if global_media and os.path.isfile(str(global_media.get("path") or "")):
            global_media_path = str(global_media["path"])

        # ── Кеш чатів-джерел щоб не робити iter_messages для кожного таргету повторно ──
        source_cache: dict[int, list] = {}

        # ── Готуємо список (delay, pid) ──
        jobs: list[tuple[int, int]] = []
        disabled = 0
        for pid in targets:
            target_type = get_target_type(data, pid, mode)
            if target_type == "none":
                disabled += 1
                continue
            # Якщо є хоч якась конфігурація — включаємо
            has_content = False
            if target_type == "forward":
                has_content = get_target_forward_source(data, pid, mode) is not None
            elif target_type == "text":
                tms = get_target_messages(data).get(pid) or {}
                has_content = bool(tms.get(mode)) or bool(get_target_media(data, pid, mode))
            else:
                # Не задано per-target — перевіряємо глобальний дефолт
                text = message_for_target(data, pid, mode)
                has_content = bool(text) or bool(global_media_path) or bool(
                    get_target_media(data, pid, mode)
                )
            if not has_content:
                disabled += 1
                continue
            jobs.append((delay_for_target(data, pid, mode), pid))
        jobs.sort(key=lambda item: item[0])

        if not jobs:
            return username_base, True, f"all disabled ({disabled})"

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        sent = 0
        failures: list[str] = []

        for delay_s, pid in jobs:
            wait_for = delay_s - (loop.time() - started_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            try:
                # ── Розв'язання entity ──────────────────────────────────
                try:
                    real_id, peer_cls = tl_utils.resolve_id(pid)
                    entity = await client.get_input_entity(peer_cls(real_id))
                except (ValueError, KeyError):
                    entity = await client.get_entity(pid)

                target_type = get_target_type(data, pid, mode)

                # ══ Рівень 1: Per-target пересилання з чату-джерела ══════
                if target_type == "forward":
                    src = get_target_forward_source(data, pid, mode)
                    if src:
                        src_chat_id = int(src["chat_id"])
                        fwd_mode = get_target_forward_mode(data, pid, mode)

                        if fwd_mode == "delete":
                            # ── Режим "відправив → видалив" ──
                            # Завжди завантажуємо свіжий список (не кешуємо)
                            fresh_msgs = await _fetch_source_msgs(client, src_chat_id)
                            if fresh_msgs:
                                chosen = fresh_msgs[0]  # перший доступний
                                await _send_media_no_fwd(client, entity, chosen)
                                # Видаляємо з джерела
                                try:
                                    await client.delete_messages(src_chat_id, [chosen.id])
                                    log.info("[%s] fwd-delete → pid=%d  src=%s  msg_id=%d видалено",
                                             username_base, pid, src.get("title", src_chat_id), chosen.id)
                                except Exception as del_exc:
                                    log.warning("[%s] fwd-delete: не вдалося видалити msg_id=%d: %s",
                                                username_base, chosen.id, del_exc)
                                sent += 1
                            else:
                                failures.append(f"{pid}:src_empty")
                        else:
                            # ── Режим "по колу" (round-robin) ──
                            if src_chat_id not in source_cache:
                                source_cache[src_chat_id] = await _fetch_source_msgs(client, src_chat_id)
                            msgs = source_cache[src_chat_id]
                            if msgs:
                                used = get_target_forward_used(data, pid, mode)
                                chosen = _pick_round_robin(msgs, used)
                                if chosen:
                                    await _send_media_no_fwd(client, entity, chosen)
                                    mark_target_forward_used(data, pid, mode, chosen.id, len(msgs))
                                    save_user_json(json_path, data)
                                    log.info("[%s] fwd-roundrobin → pid=%d  src=%s  msg_id=%d",
                                             username_base, pid, src.get("title", src_chat_id), chosen.id)
                                    sent += 1
                                else:
                                    failures.append(f"{pid}:no_media_in_src")
                            else:
                                failures.append(f"{pid}:src_empty")
                    else:
                        failures.append(f"{pid}:no_src")

                # ══ Рівень 2: Per-target медіа-файл (завантажений вручну) ══
                elif target_type == "text":
                    target_media = get_target_media(data, pid, mode)
                    if target_media and os.path.isfile(str(target_media.get("path") or "")):
                        kind = str(target_media.get("kind") or "")
                        path = str(target_media["path"])
                        kw: dict = {}
                        if kind == "video_note":
                            kw["video_note"] = True
                        elif kind == "voice":
                            kw["voice_note"] = True
                        elif target_media.get("caption"):
                            kw["caption"] = target_media["caption"]
                        await client.send_file(entity, path, **kw)
                        log.info("[%s] media → pid=%d  kind=%s", username_base, pid, kind)
                        sent += 1
                    else:
                        # Надсилаємо текст
                        tms = get_target_messages(data).get(pid) or {}
                        text = tms.get(mode)
                        if text:
                            await client.send_message(entity, str(text))
                            sent += 1
                        else:
                            failures.append(f"{pid}:empty_text")

                # ══ Рівень 3: Глобальний дефолт (якщо немає per-target) ══
                else:
                    target_media = get_target_media(data, pid, mode)
                    if target_media and os.path.isfile(str(target_media.get("path") or "")):
                        kind = str(target_media.get("kind") or "")
                        kw = {}
                        if kind == "video_note":
                            kw["video_note"] = True
                        elif kind == "voice":
                            kw["voice_note"] = True
                        await client.send_file(entity, str(target_media["path"]), **kw)
                        sent += 1
                    elif global_media_path:
                        dm = global_media
                        dm_kind = str(dm.get("kind") or "")
                        dm_kw: dict = {}
                        if dm_kind == "video_note":
                            dm_kw["video_note"] = True
                        elif dm_kind == "voice":
                            dm_kw["voice_note"] = True
                        elif dm.get("caption"):
                            dm_kw["caption"] = dm["caption"]
                        await client.send_file(entity, global_media_path, **dm_kw)
                        sent += 1
                    else:
                        text = message_for_target(data, pid, mode)
                        if text:
                            await client.send_message(entity, text)
                            sent += 1
                        else:
                            failures.append(f"{pid}:no_content")

                await asyncio.sleep(random.uniform(0.7, 2.2))

            except RPCError as e:
                failures.append(f"{pid}:{e.__class__.__name__}")
                log.warning("[%s] RPCError pid=%d: %s", username_base, pid, e)
            except Exception as e:
                failures.append(f"{pid}:{type(e).__name__}")
                log.warning("[%s] Error pid=%d: %s", username_base, pid, e)

        if sent == 0:
            return username_base, False, f"none sent ({', '.join(failures[:3]) or 'unknown'})"
        if failures:
            return username_base, True, f"partial {sent}/{len(jobs)}; fails: {', '.join(failures[:3])}"
        if disabled:
            return username_base, True, f"sent {sent}; disabled {disabled}"
        return username_base, True, None

    except RPCError as e:
        return username_base, False, f"RPCError {e.__class__.__name__}: {e}"
    except Exception as e:
        return username_base, False, f"{type(e).__name__}: {e}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def broadcast_for_all_users(mode: str) -> tuple[int, int]:
    """Запускає _send_for_user паралельно для всіх профілів. Повертає (ok, total)."""
    json_files = sorted(glob(os.path.join(USERS_DIR, "*.json")))
    if not json_files:
        log.info("Немає профілів — нічого надсилати.")
        return 0, 0

    log.info("▶️ Розсилка mode=%s для %d користувачів", mode, len(json_files))

    results = await asyncio.gather(
        *[_send_for_user(jp, mode) for jp in json_files],
        return_exceptions=False,
    )

    ok = 0
    for username, success, err in results:
        if success:
            ok += 1
            log.info("[OK]   %s -> %s%s", username, mode, f" ({err})" if err else "")
        else:
            log.info("[SKIP] %s -> %s", username, err)

    log.info("✅ Готово: %d/%d", ok, len(results))
    return ok, len(results)

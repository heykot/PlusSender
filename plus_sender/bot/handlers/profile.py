"""Профіль користувача та статус."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from aiogram import F, Router, types
from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.tl.types import DocumentAttributeVideo

from ...config import BTN_PROFILE, EMO, HR, SESSIONS_DIR
from ...storage import (
    delay_for_target,
    get_schedule,
    get_target_forward_source,
    get_target_messages,
    get_target_type,
    get_targets,
    get_targets_meta,
    load_user,
    message_for_target,
)
from ...utils import (
    access_status_line,
    card,
    default_delay_seconds,
    default_message_text,
    h,
    preview_message,
    safe_username_from,
    section,
    status_badge,
)
from ..keyboards import main_menu_kb

log = logging.getLogger(__name__)
router = Router(name="profile")


# ─────────────────────── Підрахунок кружків через Telethon ───────────────────────

def _is_video_note(msg) -> bool:
    doc = getattr(getattr(msg, "media", None), "document", None)
    if not doc:
        return False
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeVideo) and getattr(attr, "round_message", False):
            return True
    return False


async def _count_video_notes(data: dict, username: str) -> dict[int, int]:
    """Підключається до Telethon і рахує кружки в кожному чаті-джерелі.
    Повертає {chat_id: count}. При помилці — пустий dict."""
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    if not (api_id and api_hash):
        return {}

    # Збираємо всі унікальні chat_id з усіх чатів/режимів
    targets = get_targets(data)
    source_ids: set[int] = set()
    for pid in targets:
        for mode in ("alert", "clear"):
            if get_target_type(data, pid, mode) == "forward":
                src = get_target_forward_source(data, pid, mode)
                if src:
                    source_ids.add(int(src["chat_id"]))

    if not source_ids:
        return {}

    session_path = os.path.join(SESSIONS_DIR, username)
    client = TelegramClient(session_path, int(api_id), str(api_hash))
    counts: dict[int, int] = {}
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {}

        for chat_id in source_ids:
            try:
                try:
                    real_id, peer_cls = tl_utils.resolve_id(chat_id)
                    entity = await client.get_input_entity(peer_cls(real_id))
                except (ValueError, KeyError):
                    entity = await client.get_entity(chat_id)

                cnt = 0
                async for msg in client.iter_messages(entity, limit=200):
                    if _is_video_note(msg):
                        cnt += 1
                counts[chat_id] = cnt
                log.debug("profile: chat_id=%d → %d кружків", chat_id, cnt)
            except Exception as exc:
                log.debug("profile: не вдалося рахувати chat_id=%d: %s", chat_id, exc)
                counts[chat_id] = -1   # -1 = помилка

    except Exception as exc:
        log.debug("profile: Telethon помилка: %s", exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return counts


# ─────────────────────── Рядок одного режиму ───────────────────────

def _mode_line(data: dict, pid: int, mode: str,
               fwd_counts: dict[int, int],
               branch: str = "├") -> str:
    """Повертає рядок опису налаштування одного режиму (тривога або відбій)."""
    t = get_target_type(data, pid, mode)
    delay = delay_for_target(data, pid, mode)
    icon = "🚨" if mode == "alert" else "🟢"

    if t == "none":
        return f"  {branch} {icon} 🚫 не надсилати"

    if t == "forward":
        src = get_target_forward_source(data, pid, mode)
        src_name = h(src["title"]) if src else "?"
        count_str = ""
        if src:
            cid = int(src["chat_id"])
            if cid in fwd_counts:
                n = fwd_counts[cid]
                count_str = f"  · <b>{n} кружків</b>" if n >= 0 else "  · <i>недоступно</i>"
        return f"  {branch} {icon} 📦 <b>{src_name}</b>{count_str}  · {delay}с"

    if t == "text":
        tms = get_target_messages(data).get(pid) or {}
        raw = tms.get(mode)
        if raw:
            preview = h(preview_message(str(raw), 28))
            return f"  {branch} {icon} ✍️ <code>{preview}</code>  · {delay}с"
        return f"  {branch} {icon} ✍️ 📎 медіа  · {delay}с"

    # None/default — глобальний дефолт
    txt = h(preview_message(message_for_target(data, pid, mode), 28))
    return f"  {branch} {icon} ↩️ <code>{txt}</code>  · {delay}с"


# ─────────────────────── Секція чатів ───────────────────────

def _chats_section(data: dict, fwd_counts: dict[int, int], max_items: int = 8) -> str:
    targets = get_targets(data)
    if not targets:
        return "<i>не обрано — перейдіть у «🎛 Налаштування»</i>"

    meta = get_targets_meta(data)
    lines: list[str] = []

    for pid in targets[:max_items]:
        item = meta.get(pid, {}) or {}
        title = h(str(item.get("title") or "—"))
        uname = item.get("username")
        upart = f" @{h(uname)}" if uname else ""
        alert_line = _mode_line(data, pid, "alert", fwd_counts, branch="├")
        clear_line  = _mode_line(data, pid, "clear", fwd_counts, branch="└")
        lines.append(f"<b>{title}</b>{upart}\n{alert_line}\n{clear_line}")

    if len(targets) > max_items:
        lines.append(f"<i>…ще {len(targets) - max_items} чатів</i>")

    return "\n\n".join(lines)


# ─────────────────────── Хендлер профілю ───────────────────────

@router.message(F.text == BTN_PROFILE)
async def show_profile(msg: types.Message) -> None:
    user = msg.from_user
    username = safe_username_from(user)
    sess_file = os.path.join(SESSIONS_DIR, f"{username}.session")
    sess_exists = os.path.isfile(sess_file)

    data = load_user(user)
    active = bool(data.get("status", False))
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")

    # Підраховуємо кружки через Telethon (тільки якщо є сесія і є forward-джерела)
    fwd_counts: dict[int, int] = {}
    if sess_exists and api_id and api_hash:
        fwd_counts = await _count_video_notes(data, username)

    alert_default = h(preview_message(default_message_text(data, "alert"), 80))
    clear_default = h(preview_message(default_message_text(data, "clear"), 80))
    alert_delay = default_delay_seconds(data, "alert")
    clear_delay = default_delay_seconds(data, "clear")

    # ── Акаунт ──
    user_display = f"@{h(user.username)}" if user.username else f"ID {user.id}"
    access_line = access_status_line(data.get("access_until"))
    account_body = (
        f"Користувач:  <b>{user_display}</b>  (<code>{user.id}</code>)\n"
        f"Режим:       {status_badge(active)}\n"
        f"Доступ:      <b>{access_line}</b>"
    )

    # ── Сесія Telethon ──
    if sess_exists:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(sess_file))
            sess_line = f"<b>є</b>  <i>({mtime:%d.%m.%Y %H:%M})</i>"
        except OSError:
            sess_line = "<b>є</b>"
    else:
        sess_line = "<i>немає — натисніть «🔌 Підключити»</i>"

    cred_parts: list[str] = []
    if api_id:
        cred_parts.append(f"api_id <code>{api_id}</code>")
    if api_hash and isinstance(api_hash, str):
        cred_parts.append(f"api_hash <code>{h(api_hash[:6])}…</code>")
    cred_line = "  ·  ".join(cred_parts) if cred_parts else "<i>не налаштовано</i>"

    session_body = (
        f"Стан:    {sess_line}\n"
        f"Ключі:   {cred_line}"
    )

    # ── Розсилка — статистика ──
    targets = get_targets(data)
    n_targets = len(targets)
    if n_targets == 0:
        chats_label = "не обрано"
    elif n_targets == 1:
        chats_label = "1 чат"
    elif 2 <= n_targets <= 4:
        chats_label = f"{n_targets} чати"
    else:
        chats_label = f"{n_targets} чатів"

    fwd_a = sum(1 for p in targets if get_target_type(data, p, "alert") == "forward")
    fwd_c = sum(1 for p in targets if get_target_type(data, p, "clear") == "forward")
    txt_a = sum(1 for p in targets if get_target_type(data, p, "alert") == "text")
    txt_c = sum(1 for p in targets if get_target_type(data, p, "clear") == "text")

    def _stat(fwd: int, txt: int, total: int) -> str:
        parts = []
        if fwd:
            parts.append(f"📦 {fwd}")
        if txt:
            parts.append(f"✍️ {txt}")
        def_cnt = total - fwd - txt
        if def_cnt > 0:
            parts.append(f"↩️ {def_cnt}")
        return "  ".join(parts) if parts else "↩️ дефолт"

    # Розклад
    sched = get_schedule(data)
    if sched["enabled"]:
        sched_line = f"⏰ <b>{sched['from_time']} – {sched['to_time']}</b>"
    else:
        sched_line = "⏰ без обмежень"

    settings_body = (
        f"Обрано чатів:  <b>{chats_label}</b>\n"
        f"🚨 Тривога:    {_stat(fwd_a, txt_a, n_targets)}\n"
        f"🟢 Відбій:     {_stat(fwd_c, txt_c, n_targets)}\n"
        f"Розклад:       {sched_line}"
    )

    # ── Детально по чатах ──
    chats_detail = _chats_section(data, fwd_counts)

    text = card(
        title="Профіль",
        emoji=EMO["user"],
        sections=[
            ("Акаунт", account_body),
            ("Сесія Telethon", session_body),
            ("Розсилка", settings_body),
            ("Тексти по чатах", chats_detail),
        ],
    )

    await msg.answer(text, reply_markup=main_menu_kb(user))

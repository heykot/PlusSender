"""Авто-збір медіа з source-чатів: бот перехоплює кружечки/голосові/фото
і складає їх у локальну бібліотеку користувача."""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import F, Router, types
from aiogram.enums import ChatMemberStatus, ChatType

from ...config import MEDIA_DIR
from ...storage import (
    add_source_lib_file,
    get_media_source,
    iter_user_files,
    load_user_json,
    save_user_json,
    source_lib_count,
)

log = logging.getLogger(__name__)
router = Router(name="source_chat")

# Розширення за типом медіа
_EXT = {
    "video_note": "mp4",
    "voice": "ogg",
    "photo": "jpg",
    "video": "mp4",
    "animation": "mp4",
}


def _extract(msg: types.Message) -> tuple[str, str] | None:
    """Повертає (file_id, kind) або None."""
    if msg.video_note:
        return msg.video_note.file_id, "video_note"
    if msg.voice:
        return msg.voice.file_id, "voice"
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if msg.video:
        return msg.video.file_id, "video"
    if msg.animation:
        return msg.animation.file_id, "animation"
    return None


def _find_owners(chat_id: int) -> list[tuple[str, str, dict]]:
    """Шукає всіх юзерів, у яких цей chat_id — медіа-джерело.
    Повертає [(json_path, mode, data), ...]."""
    results: list[tuple[str, str, dict]] = []
    for path in iter_user_files():
        data = load_user_json(path)
        for mode in ("alert", "clear"):
            src = get_media_source(data, mode)
            if src and int(src.get("chat_id", 0)) == chat_id:
                results.append((path, mode, data))
    return results


# ── Автозбір медіа з груп/каналів ─────────────────────────────────────────
@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL})
)
async def harvest_media(msg: types.Message) -> None:
    """Якщо повідомлення прийшло з source-чату — скачуємо медіа в бібліотеку."""
    media = _extract(msg)
    if not media:
        return

    chat_id = msg.chat.id
    owners = _find_owners(chat_id)
    if not owners:
        return

    file_id, kind = media
    ext = _EXT.get(kind, "bin")
    filename = f"src_{abs(chat_id)}_{msg.message_id}.{ext}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = str(MEDIA_DIR / filename)

    # Скачуємо один раз, додаємо всім власникам
    try:
        await msg.bot.download(file_id, destination=dest)
    except Exception as exc:
        log.warning("harvest_media: download failed (chat %d, msg %d): %s",
                    chat_id, msg.message_id, exc)
        return

    for json_path, mode, data in owners:
        add_source_lib_file(data, mode, dest, kind)
        save_user_json(json_path, data)
        total = source_lib_count(data, mode)
        log.info("harvest_media: +1 %s→mode=%s (total=%d)", json_path, mode, total)


# ── Бот доданий до чату: підказуємо ──────────────────────────────────────
@router.my_chat_member(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL})
)
async def on_bot_added(update: types.ChatMemberUpdated) -> None:
    """Коли бота додають до групи/каналу — надсилаємо підказку в приватному чаті."""
    new_status = update.new_chat_member.status
    if new_status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return

    user = update.from_user
    chat = update.chat
    chat_id = chat.id
    title = chat.title or str(chat_id)

    try:
        await update.bot.send_message(
            user.id,
            f"📦  <b>Бота додано до «{title}»</b>\n\n"
            f"Тепер бот автоматично збирає кружечки / голосові / фото з цього чату "
            f"у вашу бібліотеку медіа.\n\n"
            f"Щоб прив'язати цей чат як <b>джерело для тривоги або відбою</b> — "
            f"перейдіть до <b>🎛 Налаштування → 🚨 Повідомлення</b> "
            f"та виберіть <b>📦 Чат-джерело</b>, потім перешліть звідси будь-яке повідомлення.",
            parse_mode="HTML",
        )
    except Exception:
        pass   # юзер міг заблокувати бота в приваті

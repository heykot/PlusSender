"""Сповіщення користувачам і адмінам.

  • після розсилки — користувачу, якщо щось не надіслалось і він може це виправити;
  • щодня — нагадування про закінчення доступу (за 3 дні, в останній день, наступного дня);
  • щодня — перевірка, чи сесії Telegram ще живі;
  • адмінам — якщо API тривог довго не відповідає.

Позначки «вже нагадали» зберігаються в профілі, тож рестарт не спричиняє дублів.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from html import escape as h
from typing import Optional

from aiogram import types
from telethon import TelegramClient

from .config import BTN_BROADCAST
from .storage import (
    access_days_left,
    get_access_until,
    get_target_forward_source,
    get_targets,
    get_targets_meta,
    iter_user_files,
    load_admins,
    load_user_json,
    save_user_json,
    session_file_path_for_id,
    session_path_for_id,
    user_file_path_for_id,
)

log = logging.getLogger(__name__)

REMIND_FROM_HOUR, REMIND_TO_HOUR = 10, 20   # не будимо людей уночі
SESSION_CHECK_HOUR = 12
PROBLEM_REPEAT_AFTER = timedelta(hours=20)  # однакову проблему повторюємо не частіше
LOOP_INTERVAL = 30 * 60

I = types.InlineKeyboardButton
PAY_KB = types.InlineKeyboardMarkup(inline_keyboard=[[I(text="💳 Продовжити доступ", callback_data="more:pay")]])
RECONNECT_KB = types.InlineKeyboardMarkup(inline_keyboard=[[I(text="🔌 Перепідключити Telegram", callback_data="more:connect")]])


def _update_profile(uid: int, **fields) -> None:
    """Перечитує профіль і дописує поля — не затираючи паралельних змін."""
    path = user_file_path_for_id(uid)
    data = load_user_json(path)
    if data:
        data.update(fields)
        save_user_json(path, data)


async def _send(bot, uid: int, text: str, kb: Optional[types.InlineKeyboardMarkup] = None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=kb)
        return True
    except Exception as exc:  # заблокував бота, видалив акаунт тощо
        log.info("notify uid=%d не доставлено: %s", uid, exc)
        return False


async def notify_admins(bot, text: str) -> None:
    for uid in load_admins():
        await _send(bot, uid, text)


# ─────────────────────── Проблеми розсилки ───────────────────────

# Коди, коли бот втратив доступ до акаунта — користувач має перепідключитись
SESSION_CODES = {
    "session", "AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError",
    "UserDeactivatedError", "UserDeactivatedBanError", "AuthKeyDuplicatedError",
}
NO_RIGHTS = {
    "ChatWriteForbiddenError", "ChatAdminRequiredError", "UserBannedInChannelError",
    "ChatRestrictedError", "ChatSendMediaForbiddenError", "ChatSendPlainForbiddenError",
    "ChatSendPhotosForbiddenError", "ChatSendVideosForbiddenError",
    "ChatSendVoicesForbiddenError", "ChatSendRoundvideosForbiddenError",
}
UNREACHABLE = {
    "ChannelPrivateError", "ValueError", "PeerIdInvalidError", "ChatIdInvalidError",
    "InputUserDeactivatedError", "UserIsBlockedError", "YouBlockedUserError", "ChannelInvalidError",
}
TEMPORARY = {"FloodWaitError", "TimeoutError", "ConnectionError", "ServerError", "RpcCallFailError"}


def _problem_text(data: dict, pid: int, code: str, mode: str) -> Optional[str]:
    """Людський опис проблеми з конкретним чатом; None — не варто турбувати користувача."""
    title = str((get_targets_meta(data).get(pid) or {}).get("title") or pid)
    chat = f"«{h(title)}»"
    if code in TEMPORARY:
        return None
    if code in NO_RIGHTS:
        return f"{chat} — немає права писати (або надсилати медіа) в цей чат"
    if code in UNREACHABLE:
        return f"{chat} — чат недоступний: вас видалили з нього або його видалено"
    if code in ("src_empty", "no_media_in_src"):
        src = get_target_forward_source(data, pid, mode)
        where = f" «{h(src['title'])}»" if src else ""
        return f"{chat} — у чаті-джерелі кружків{where} немає кружків (або він недоступний)"
    if code == "no_src":
        return f"{chat} — не обрано, звідки брати кружки"
    if code == "SlowModeWaitError":
        return f"{chat} — у чаті ввімкнено повільний режим"
    return f"{chat} — не вдалося надіслати ({h(code)})"


_last_notified: dict[int, tuple[tuple, datetime]] = {}


async def notify_broadcast_problems(bot, mode: str, problems: dict[str, list[tuple[Optional[int], str]]]) -> None:
    """Після розсилки пише користувачам, у кого щось не надіслалось з їхньої вини/налаштувань."""
    event = "тривоги" if mode == "alert" else "відбою"
    for name, items in problems.items():
        if not name.isdigit() or not items:
            continue
        uid = int(name)
        data = load_user_json(user_file_path_for_id(uid))
        if any(pid is None and code in SESSION_CODES for pid, code in items):
            text = (
                f"⚠️  <b>Під час {event} повідомлення НЕ надіслано</b>\n\n"
                f"Бот більше не має доступу до вашого Telegram — сесію завершено "
                f"(в «Пристроях» або після виходу з акаунта).\n\n"
                f"Перепідключіть Telegram — це хвилина."
            )
            kb = RECONNECT_KB
        else:
            lines = [t for pid, code in items if pid is not None and (t := _problem_text(data, pid, code, mode))]
            if not lines:
                continue
            text = (
                f"⚠️  <b>Під час {event} не все надіслалось</b>\n\n"
                + "\n".join(f"• {x}" for x in lines)
                + f"\n\n<i>Перевірте «{BTN_BROADCAST}».</i>"
            )
            kb = None

        signature = tuple(sorted((str(p), c) for p, c in items))
        prev = _last_notified.get(uid)
        if prev and prev[0] == signature and datetime.now() - prev[1] < PROBLEM_REPEAT_AFTER:
            continue
        if await _send(bot, uid, text, kb):
            _last_notified[uid] = (signature, datetime.now())
            log.info("notify uid=%d: проблеми розсилки %s", uid, signature)


# ─────────────────────── Нагадування про доступ ───────────────────────

def _reminder_stage(data: dict) -> Optional[str]:
    """Яке нагадування зараз доречне: "3" | "0" | "expired" | None."""
    until = get_access_until(data)
    if not until:
        return None
    left = access_days_left(data)
    if left is not None:
        if left == 0:
            return "0"
        if left <= 3:
            return "3"
        return None
    days_ago = (datetime.now().date() - until.date()).days
    return "expired" if 1 <= days_ago <= 3 else None


REMINDER_TEXTS = {
    "3": ("⏰  <b>Доступ закінчується через {left} дн.</b> ({until})\n\n"
          "Продовжте заздалегідь, щоб бот не пропустив жодної тривоги."),
    "0": ("⏰  <b>Сьогодні останній день доступу</b>\n\n"
          "З завтрашнього дня бот перестане надсилати повідомлення при тривозі й відбої."),
    "expired": ("🔕  <b>Доступ закінчився</b> ({until})\n\n"
                "Бот зараз <b>не надсилає</b> повідомлення при тривозі. "
                "Продовжте доступ — налаштування збережені, все запрацює одразу."),
}


async def send_access_reminders(bot) -> None:
    for path in iter_user_files():
        data = load_user_json(path)
        uid = data.get("user_id")
        stage = _reminder_stage(data)
        if not uid or not stage:
            continue
        until = str(data.get("access_until"))
        sent = data.get("access_reminders") or {}
        # Позначки прив'язані до конкретної дати: після продовження нагадуємо знову
        if sent.get("until") != until:
            sent = {"until": until, "stages": []}
        if stage in sent["stages"]:
            continue
        text = REMINDER_TEXTS[stage].format(left=access_days_left(data), until=f"{get_access_until(data):%d.%m.%Y}")
        if await _send(bot, int(uid), text, PAY_KB):
            log.info("reminder uid=%s stage=%s until=%s", uid, stage, until)
        # Позначаємо навіть якщо не доставлено (бот заблоковано) — не пробуємо щогодини
        sent["stages"].append(stage)
        _update_profile(int(uid), access_reminders=sent)


# ─────────────────────── Перевірка сесій ───────────────────────

async def _session_alive(uid: int, data: dict) -> Optional[bool]:
    """True/False — жива чи ні; None — не вдалося перевірити (мережа тощо)."""
    client = TelegramClient(session_path_for_id(uid), int(data["api_id"]), str(data["api_hash"]))
    try:
        await client.connect()
        return await client.is_user_authorized()
    except Exception as exc:
        log.info("session check uid=%d: %s", uid, exc)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def check_sessions(bot) -> None:
    """Раз на день: у кого розсилка має працювати, а сесію завершено — пишемо."""
    today = datetime.now().strftime("%Y-%m-%d")
    for path in iter_user_files():
        data = load_user_json(path)
        uid = data.get("user_id")
        if not (uid and data.get("status") and data.get("api_id") and get_targets(data)
                and access_days_left(data) is not None and os.path.isfile(session_file_path_for_id(uid))):
            continue
        if data.get("session_lost_notified") == today:
            continue
        if await _session_alive(int(uid), data) is False:
            await _send(bot, int(uid), (
                "⚠️  <b>Бот втратив доступ до вашого Telegram</b>\n\n"
                "Сесію завершено — в «Пристроях» або після виходу з акаунта. "
                "Поки ви не перепідключитесь, бот <b>не надсилатиме</b> повідомлення при тривозі."
            ), RECONNECT_KB)
            _update_profile(int(uid), session_lost_notified=today)
            log.info("session lost uid=%s — сповіщено", uid)
        await asyncio.sleep(1)  # не навантажуємо Telegram


# ─────────────────────── Фоновий цикл ───────────────────────

async def run_daily_jobs(bot) -> None:
    checked_on: Optional[str] = None
    while True:
        now = datetime.now()
        try:
            if REMIND_FROM_HOUR <= now.hour < REMIND_TO_HOUR:
                await send_access_reminders(bot)
            if now.hour >= SESSION_CHECK_HOUR and checked_on != now.strftime("%Y-%m-%d"):
                checked_on = now.strftime("%Y-%m-%d")
                await check_sessions(bot)
        except Exception:
            log.exception("daily jobs")
        await asyncio.sleep(LOOP_INTERVAL)

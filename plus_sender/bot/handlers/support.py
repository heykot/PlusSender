"""Підтримка: користувач пише повідомлення → адмін отримує → відповідає → юзер отримує відповідь."""
from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from ...config import BTN_CANCEL, BTN_SUPPORT, CANCEL_TEXTS, DIV, EMO
from ...storage import load_admins, load_user
from ...utils import h
from ..keyboards import cancel_kb, main_menu_kb
from ..states import SupportStates

log = logging.getLogger(__name__)
router = Router(name="support")

# Префікс у повідомленні адміну — щоб бот розумів що це тікет підтримки
_TAG = "🆘support"


# ─────────────────────── Відкрити підтримку ───────────────────────

@router.message(F.text == BTN_SUPPORT)
async def open_support(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    admins = load_admins()
    if not admins:
        await msg.answer(
            f"{EMO['warn']}  <b>Підтримка недоступна</b>\n"
            f"Адміністратор не налаштований.",
            reply_markup=main_menu_kb(msg.from_user),
        )
        return

    await state.set_state(SupportStates.waiting_message)
    await msg.answer(
        f"🆘  <b>Підтримка</b>\n"
        f"{DIV}\n"
        f"Напишіть ваше питання або проблему — і ми відповімо якнайшвидше.\n\n"
        f"<i>Надсилайте текст, фото, відео або голосове повідомлення.</i>",
        reply_markup=cancel_kb(),
    )


# ─────────────────────── Отримати повідомлення від юзера ───────────────────────

@router.message(SupportStates.waiting_message)
async def receive_support_message(msg: types.Message, state: FSMContext) -> None:
    if msg.text and msg.text in CANCEL_TEXTS:
        await state.clear()
        await msg.answer("Скасовано.", reply_markup=main_menu_kb(msg.from_user))
        return

    admins = load_admins()
    if not admins:
        await state.clear()
        await msg.answer(
            f"{EMO['err']} Підтримка недоступна.",
            reply_markup=main_menu_kb(msg.from_user),
        )
        return

    user = msg.from_user
    data = load_user(user)
    name = h(user.full_name)
    uname = f" @{h(user.username)}" if user.username else ""

    # Формуємо заголовок для адміна
    header = (
        f"{_TAG}:{user.id}\n"
        f"👤 <b>{name}</b>{uname}  <code>{user.id}</code>\n"
        f"{DIV}\n"
    )

    # Пересилаємо всім адмінам
    sent_ok = False
    for admin_id in admins:
        try:
            # Спочатку надсилаємо заголовок
            header_msg = await msg.bot.send_message(admin_id, header)
            # Потім пересилаємо оригінальне повідомлення (зі збереженням медіа)
            await msg.forward(admin_id)
            sent_ok = True
        except Exception as exc:
            log.warning("support: не вдалося надіслати адміну %d: %s", admin_id, exc)

    await state.clear()

    if sent_ok:
        await msg.answer(
            f"✅  <b>Повідомлення надіслано!</b>\n"
            f"<i>Ми відповімо вам якнайшвидше.</i>",
            reply_markup=main_menu_kb(user),
        )
    else:
        await msg.answer(
            f"{EMO['err']} Не вдалося надіслати повідомлення. Спробуйте пізніше.",
            reply_markup=main_menu_kb(user),
        )


# ─────────────────────── Відповідь адміна ───────────────────────
# Адмін відповідає (reply) на повідомлення з тегом _TAG → бот пересилає юзеру

@router.message(F.reply_to_message)
async def admin_reply_to_user(msg: types.Message) -> None:
    admins = load_admins()
    if msg.from_user.id not in admins:
        return  # не адмін — ігноруємо

    replied = msg.reply_to_message
    if not replied or not replied.text:
        return

    # Шукаємо тег підтримки в повідомленні-заголовку
    if not replied.text.startswith(_TAG):
        return

    # Витягуємо user_id
    try:
        tag_line = replied.text.split("\n")[0]  # "🆘support:123456789"
        uid = int(tag_line.split(":")[1])
    except (IndexError, ValueError):
        return

    # Пересилаємо відповідь адміна юзеру
    try:
        await msg.bot.send_message(
            uid,
            f"💬  <b>Відповідь підтримки:</b>\n"
            f"{DIV}\n"
            f"{h(msg.text or '')}",
        )
        # Якщо є медіа — пересилаємо окремо
        if not msg.text:
            await msg.forward(uid)
        await msg.react([types.ReactionTypeEmoji(emoji="✅")])
    except Exception as exc:
        log.warning("support reply: не вдалося надіслати юзеру %d: %s", uid, exc)
        await msg.answer(f"{EMO['err']} Не вдалося надіслати відповідь користувачу <code>{uid}</code>.")

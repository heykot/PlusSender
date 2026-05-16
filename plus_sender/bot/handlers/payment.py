"""Оплата доступу через Monobank банку."""
from __future__ import annotations

import logging
import os

from aiogram import Router, types
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ...config import DIV, EMO
from ...storage import load_user
from ...utils import access_status_line
from ..keyboards import main_menu_kb

log = logging.getLogger(__name__)
router = Router(name="payment")

# ─────────────────────── Тарифи ───────────────────────
PLANS_UAH: list[tuple[int, int, str]] = [
    (30,  300,  "30 днів"),
    (90,  800,  "90 днів"),
    (180, 1500, "180 днів"),
    (365, 2800, "365 днів"),
]


# ─────────────────────── Відображення сторінки оплати ───────────────────────

async def show_payment(msg: types.Message) -> None:
    data = load_user(msg.from_user)
    access = data.get("access_until")
    uid = msg.from_user.id

    mono_jar_send_id = (os.getenv("MONO_JAR_SEND_ID") or "").strip()

    plans_text = "\n".join(
        f"  • {label} — <b>{uah} грн</b>"
        for _, uah, label in PLANS_UAH
    )

    text = (
        f"{EMO['card']}  <b>Оплата доступу</b>\n"
        f"{DIV}\n"
        f"📅  Поточний доступ:  <b>{access_status_line(access)}</b>\n\n"
        f"💳  <b>Тарифи (Monobank):</b>\n"
        f"{plans_text}\n\n"
        f"<b>Як оплатити — 3 кроки:</b>\n"
        f"  ①  Натисніть «📋 Копіювати мій ID»\n"
        f"  ②  Натисніть «💳 Перейти до банки»\n"
        f"  ③  У коментарі до платежу <b>вставте свій ID</b>\n\n"
        f"<i>Ваш ID:</i>  <code>{uid}</code>\n\n"
        f"<i>Після оплати доступ продовжується автоматично — "
        f"вам прийде повідомлення в Telegram.</i>"
    )

    # ── Inline-кнопки: копіювання ID та посилання на банку ──
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📋  Копіювати мій ID  ({uid})",
                copy_text=CopyTextButton(text=str(uid)),
            ),
        ],
    ]
    if mono_jar_send_id:
        rows.append([
            InlineKeyboardButton(
                text="💳  Перейти до банки Monobank",
                url=f"https://send.monobank.ua/{mono_jar_send_id}",
            ),
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await msg.answer(text, reply_markup=kb, disable_web_page_preview=True)

"""Оплата доступу через Monobank банку."""
from __future__ import annotations

import logging
import os

from aiogram import Router, types

from ...config import DIV, EMO
from ...storage import load_user
from ...utils import access_status_line
from ..keyboards import main_menu_kb

log = logging.getLogger(__name__)
router = Router(name="payment")

# ─────────────────────── Тарифи ───────────────────────
PLANS_UAH: list[tuple[int, int, str]] = [
    (30,  25,   "30 днів (тест)"),
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

    jar_link = (
        f"\n\n<a href='https://send.monobank.ua/{mono_jar_send_id}'>👉 Перейти до банки</a>"
        if mono_jar_send_id else ""
    )

    text = (
        f"{EMO['card']}  <b>Оплата доступу</b>\n"
        f"{DIV}\n"
        f"📅 Поточний доступ: <b>{access_status_line(access)}</b>\n\n"
        f"💳 <b>Тарифи (Monobank):</b>\n"
        f"{plans_text}\n\n"
        f"У коментарі до платежу обов'язково вкажіть ваш ID:\n"
        f"<code>{uid}</code>\n\n"
        f"Після оплати доступ <b>продовжується автоматично</b>."
        f"{jar_link}"
    )
    await msg.answer(text, reply_markup=main_menu_kb(msg.from_user), disable_web_page_preview=True)

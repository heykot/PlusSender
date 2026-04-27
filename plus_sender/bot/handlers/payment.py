"""Оплата доступу через Telegram Stars (XTR)."""
from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.types import LabeledPrice

from ...config import DIV, EMO
from ...storage import (
    extend_access_days,
    grant_access_days,
    load_admins,
    load_user,
    toggle_user_status,
)
from ...utils import access_status_line, h
from ..keyboards import main_menu_kb, payment_plans_kb

log = logging.getLogger(__name__)
router = Router(name="payment")

# ─────────────────────── Тарифи ───────────────────────
# (days, stars, label)
PLANS: list[tuple[int, int, str]] = [
    (30,  1,    "30 днів"),
    (90,  700,  "90 днів"),
    (180, 1350, "180 днів"),
    (365, 2600, "365 днів"),
]


# ─────────────────────── Відображення сторінки оплати ───────────────────────

async def show_payment(msg: types.Message) -> None:
    import os
    data = load_user(msg.from_user)
    access = data.get("access_until")
    uid = msg.from_user.id

    plans_text = "\n".join(
        f"  • {label} — <b>{stars} ⭐</b>"
        for _, stars, label in PLANS
    )

    # Monobank банка (показуємо тільки якщо налаштована)
    mono_jar_send_id = (os.getenv("MONO_JAR_SEND_ID") or "").strip()
    mono_block = ""
    if mono_jar_send_id:
        mono_plans = "\n".join([
            "  • 30 днів — <b>50 грн</b>",
            "  • 90 днів — <b>130 грн</b>",
            "  • 180 днів — <b>250 грн</b>",
            "  • 365 днів — <b>500 грн</b>",
        ])
        mono_block = (
            f"\n\n💳 <b>Monobank (банка):</b>\n"
            f"{mono_plans}\n\n"
            f"У коментарі до платежу обов'язково вкажіть ваш ID:\n"
            f"<code>{uid}</code>\n"
            f"<a href='https://send.monobank.ua/{mono_jar_send_id}'>👉 Перейти до банки</a>"
        )

    text = (
        f"{EMO['card']}  <b>Оплата доступу</b>\n"
        f"{DIV}\n"
        f"📅 Поточний доступ: <b>{access_status_line(access)}</b>\n\n"
        f"⭐ <b>Тарифи (Telegram Stars):</b>\n"
        f"{plans_text}\n\n"
        f"Оплата Stars відбувається прямо в Telegram — безпечно та миттєво.\n"
        f"Після успішної оплати доступ <b>продовжується автоматично</b>."
        f"{mono_block}"
    )
    await msg.answer(text, reply_markup=payment_plans_kb(), disable_web_page_preview=True)


# ─────────────────────── Вибір тарифу → invoice ───────────────────────

@router.callback_query(F.data.startswith("pay:buy:"))
async def cb_buy_plan(call: types.CallbackQuery) -> None:
    await call.answer()
    try:
        _, _, days_str, stars_str = call.data.split(":")
        days = int(days_str)
        stars = int(stars_str)
    except (ValueError, IndexError):
        await call.message.answer("❌ Невірний тариф.")
        return

    # Знаходимо label плану
    label = next((lbl for d, s, lbl in PLANS if d == days and s == stars), f"{days} днів")
    user = call.from_user
    payload = f"stars:{days}:{user.id}"

    await call.message.answer_invoice(
        title=f"Доступ Plus Sender — {label}",
        description=(
            f"Продовження доступу до Plus Sender на {label}.\n"
            f"Після оплати доступ буде активовано автоматично."
        ),
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=label, amount=stars)],
    )


# ─────────────────────── Pre-checkout (завжди підтверджуємо) ───────────────────────

@router.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery) -> None:
    # Валідуємо payload
    if query.invoice_payload.startswith("stars:"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Невідомий платіж.")


# ─────────────────────── Успішна оплата ───────────────────────

@router.message(F.successful_payment)
async def successful_payment_handler(msg: types.Message) -> None:
    sp = msg.successful_payment
    payload = sp.invoice_payload
    stars = sp.total_amount   # XTR: amount == кількість зірок

    if not payload.startswith("stars:"):
        log.warning("Невідомий payload після оплати: %s", payload)
        return

    try:
        _, days_str, uid_str = payload.split(":")
        days = int(days_str)
        paid_uid = int(uid_str)
    except (ValueError, IndexError):
        log.error("Не вдалося розпарсити payment payload: %s", payload)
        await msg.answer("⚠️ Помилка обробки платежу. Зверніться до адміністратора.")
        return

    user = msg.from_user

    # Безпека: uid із payload має збігатися з реальним відправником
    if paid_uid != user.id:
        log.warning(
            "payment uid mismatch: payload_uid=%d  actual_uid=%d", paid_uid, user.id
        )

    # Продовжуємо або нараховуємо доступ
    new_until = extend_access_days(user.id, days)
    if new_until is None:
        # Профілю не існує — нараховуємо з сьогодні
        new_until = grant_access_days(user.id, days) or "невідомо"

    # Активуємо статус якщо був вимкнений
    toggle_user_status(user.id, True)

    log.info(
        "✅ Stars payment: user=%d (@%s) days=%d stars=%d  → access_until=%s",
        user.id, user.username or "—", days, stars, new_until,
    )

    # Повідомляємо користувача
    label = next((lbl for d, _, lbl in PLANS if d == days), f"{days} днів")
    await msg.answer(
        f"✅  <b>Оплату отримано!</b>\n"
        f"{DIV}\n"
        f"📦 Тариф: <b>{label}</b> ({stars} ⭐)\n"
        f"📅 Доступ продовжено до: <b>{new_until}</b>\n\n"
        f"Розсилка активована. Приємного користування! 🚀",
        reply_markup=main_menu_kb(user),
    )

    # Сповіщаємо всіх адмінів
    admins = load_admins()
    if admins:
        name = h(user.full_name)
        uname = f" (@{h(user.username)})" if user.username else ""
        notify = (
            f"💳  <b>Нова оплата Stars</b>\n"
            f"{DIV}\n"
            f"👤 {name}{uname}  (<code>{user.id}</code>)\n"
            f"📦 {label} — <b>{stars} ⭐</b>\n"
            f"📅 Доступ до: <b>{new_until}</b>"
        )
        for admin_id in admins:
            try:
                await msg.bot.send_message(admin_id, notify)
            except Exception as exc:
                log.debug("Не вдалося надіслати сповіщення адміну %d: %s", admin_id, exc)

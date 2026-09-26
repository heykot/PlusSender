"""Загальні handlers: /start, /help, /cancel, перемикач увімк./вимк., меню «Ще».

Цей роутер реєструється ПЕРШИМ — щоб менеджмент-команди (cancel, статус, меню)
могли перервати будь-який FSM-майстер.
"""
from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext

from ...config import (
    BRAND,
    BTN_BROADCAST,
    BTN_CHOOSE_CHATS,
    BTN_CONNECT,
    BTN_HELP,
    BTN_MORE,
    BTN_PAYMENT,
    BTN_PROFILE,
    BTN_REFERRAL,
    BTN_START,
    BTN_STATUS_PREFIX,
    BTN_STOP,
    BTN_SUPPORT,
    BTN_TURN_OFF,
    BTN_TURN_ON,
    CANCEL_TEXTS,
    DIV,
    DIV_THIN,
    EMO,
    REFERRAL_BONUS_DAYS,
    REFERRAL_PAYLOAD_PREFIX,
    TAGLINE,
)
from ...storage import (
    access_days_left,
    get_access_until,
    get_targets,
    has_access,
    has_session,
    load_user,
    refresh_user_meta,
    set_referrer,
    set_status,
)
from ...utils import (
    card,
    h,
    next_hint,
    status_label,
    warm_greeting,
)
from ..keyboards import main_menu_kb, more_menu_kb

router = Router(name="common")


async def _notify_referrer_joined(bot, referrer_id: int) -> None:
    """Повідомляє запрошувача, що за його посиланням прийшов новий користувач."""
    try:
        await bot.send_message(
            referrer_id,
            f"🎉  <b>За вашим посиланням приєднався друг!</b>\n"
            f"<i>Щойно він оплатить тариф — ви отримаєте +{REFERRAL_BONUS_DAYS} днів доступу.</i>",
        )
    except Exception:
        pass  # запрошувач міг заблокувати бота


def _parse_referral_payload(payload: str) -> int | None:
    """Розбирає `ref_<digits>` → user_id, або None."""
    if not payload:
        return None
    payload = payload.strip()
    if not payload.startswith(REFERRAL_PAYLOAD_PREFIX):
        return None
    try:
        return int(payload[len(REFERRAL_PAYLOAD_PREFIX):])
    except ValueError:
        return None


@router.message(Command("start", "menu"))
async def cmd_start(
    msg: types.Message,
    state: FSMContext,
    command: CommandObject | None = None,
) -> None:
    await state.clear()
    user = msg.from_user

    # ── Реферальна програма: якщо /start ref_<uid> — зберігаємо запрошувача ──
    just_referred = False
    if command is not None:
        ref_id = _parse_referral_payload(command.args or "")
        if ref_id:
            just_referred = set_referrer(user, ref_id)
            if just_referred:
                await _notify_referrer_joined(msg.bot, ref_id)

    refresh_user_meta(user)
    data = load_user(user)
    active = bool(data.get("status", False))
    targets_count = len(get_targets(data))
    connected = has_session(user)
    paid = has_access(user)

    greeting = warm_greeting(user.first_name)
    ref_badge = (
        f"\n🎁  <i>Вас запросив друг — коли купите тариф, "
        f"він отримає +{REFERRAL_BONUS_DAYS} днів доступу.</i>"
        if just_referred else ""
    )

    # ── Усе налаштовано: короткий статус замість інструкції ──
    if connected and targets_count and paid and active:
        until = get_access_until(data)
        left = access_days_left(data)
        tail = "сьогодні останній день" if left == 0 else f"ще {left} дн."
        await msg.answer(
            f"{greeting}{ref_badge}\n\n"
            f"✅  <b>Все працює</b> — бот стежить за тривогою.\n"
            f"💬 Чатів: <b>{targets_count}</b> · 📅 доступ до <b>{until:%d.%m.%Y}</b> ({tail})\n\n"
            f"<i>Що надсилається — у «{BTN_BROADCAST}».</i>",
            reply_markup=main_menu_kb(user),
        )
        return

    # ── Новачок / не все зроблено: що це і які кроки лишились ──
    def _step(done: bool, text: str) -> str:
        return f"{'✅' if done else '▫️'}  {text}"

    steps = "\n".join([
        _step(connected, "① Підключити Telegram"),
        _step(targets_count > 0, "② Обрати чати"),
        _step(paid, "③ Оплатити доступ"),
        _step(active, "④ Увімкнути"),
    ])
    text = (
        f"{greeting}{ref_badge}\n"
        f"🤖  <b>{BRAND}</b>  <i>· {TAGLINE}</i>\n{DIV}\n"
        f"Коли в Києві <b>починається</b> або <b>закінчується</b> повітряна тривога, "
        f"я сам надсилаю від вашого імені повідомлення у ваші чати.\n\n"
        f"{steps}\n\n"
        f"{_next_step_hint(connected, targets_count, paid, active)}"
    )
    await msg.answer(text, reply_markup=main_menu_kb(user))


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    # Короткий FAQ — щоб користувач отримав відповіді одразу в чаті
    faq = card(
        title="Швидка довідка",
        emoji=EMO["info"],
        sections=[
            (
                "Що це за бот",
                "Я надсилаю повідомлення у ваші чати, коли в Києві вмикається "
                "або вимикається повітряна тривога. Все автоматично — ви лише "
                "налаштовуєте, що саме надсилати.",
            ),
            (
                "Як почати",
                "①  🔌  <b>Підключити</b> — вхід у ваш Telegram (номер і код)\n"
                "②  🎯  <b>Обрати чати</b> — куди й що надсилати\n"
                "③  💳  <b>Оплата</b> — активний доступ\n"
                "④  ▶️  <b>Увімкнути</b> — і бот працює сам",
            ),
            (
                "Часті питання",
                "<b>Чи безпечно?</b>\n"
                "<i>Сесія зберігається лише на сервері бота й використовується тільки "
                "для розсилки. Пароль 2FA не зберігається — повідомлення з ним одразу "
                "видаляється. Завершити сесію можна будь-коли: Telegram → Налаштування → Пристрої.</i>\n\n"
                "<b>Чому не надсилається?</b>\n"
                "<i>Перевірте: активна сесія, обрано чати, оплачений доступ і бот "
                "увімкнений (кнопка «🟢 Працює»). Усе це видно в «👤 Профіль».</i>",
            ),
            (
                "Команди",
                "/start — головне меню\n"
                "/connect — майстер підключення\n"
                "/on, /off — увімкнути / вимкнути\n"
                "/cancel — скасувати поточний крок",
            ),
        ],
    )

    await msg.answer(faq, reply_markup=main_menu_kb(msg.from_user))


@router.message(Command("cancel"))
@router.message(F.text.in_(CANCEL_TEXTS))
async def cancel_any(msg: types.Message, state: FSMContext) -> None:
    # Якщо є активний Telethon-клієнт у wizard'і підключення — вимикаємо
    from .connect import _disconnect_active

    await _disconnect_active(msg.from_user.id)
    await state.clear()
    await msg.answer(
        "❎  <b>Скасовано</b>\n<i>Повертаюся у головне меню.</i>",
        reply_markup=main_menu_kb(msg.from_user),
    )


def _next_step_hint(connected: bool, targets_count: int, paid: bool, active: bool) -> str:
    if not connected:
        return next_hint(f"натисніть «{BTN_CONNECT}» — це найдовший крок, далі простіше.")
    if targets_count == 0:
        return next_hint(f"натисніть «{BTN_CHOOSE_CHATS}» — куди надсилати сповіщення.")
    if not paid:
        return next_hint(f"оплатіть доступ у «{BTN_PAYMENT}» — і повертайтесь сюди.")
    if not active:
        return next_hint(f"натисніть «{BTN_TURN_ON}» — і бот почне реагувати на наступну тривогу.")
    return (
        f"{EMO['star']}  <b>Все готово!</b>\n"
        f"<i>Бот уже стежить за тривогою. Можна закривати чат — він працює сам.</i>"
    )


# ===================== Увімкнути / вимкнути =====================
@router.message(Command("on"))
@router.message(F.text.in_({BTN_TURN_ON, BTN_START}))
async def turn_on(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    # Перериваємо активний Telethon-клієнт wizard'у, якщо він є
    from .connect import _disconnect_active
    await _disconnect_active(msg.from_user.id)

    user = msg.from_user
    if not has_session(user):
        await msg.answer(
            f"{EMO['warn']}  <b>Спершу підключіть Telegram</b>\n"
            f"<i>Натисніть «{BTN_CONNECT}».</i>",
            reply_markup=main_menu_kb(user),
        )
        return
    if not get_targets(load_user(user)):
        await msg.answer(
            f"{EMO['warn']}  <b>Спершу оберіть чати</b>\n"
            f"<i>Натисніть «{BTN_CHOOSE_CHATS}».</i>",
            reply_markup=main_menu_kb(user),
        )
        return
    if not has_access(user):
        await msg.answer(
            f"{EMO['warn']}  <b>Немає активного доступу</b>\n"
            f"<i>Оплатіть тариф нижче — і поверніться до «{BTN_TURN_ON}».</i>",
            reply_markup=main_menu_kb(user),
        )
        from .payment import show_payment
        await show_payment(msg)
        return
    refresh_user_meta(user)
    set_status(user, True)
    await msg.answer(
        f"{EMO['active']}  <b>Увімкнено</b>\n"
        f"<i>Бот реагуватиме на наступну тривогу та відбій.</i>",
        reply_markup=main_menu_kb(user),
    )


@router.message(Command("off"))
@router.message(F.text.in_({BTN_TURN_OFF, BTN_STOP}))
async def turn_off(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    from .connect import _disconnect_active
    await _disconnect_active(msg.from_user.id)

    set_status(msg.from_user, False)
    await msg.answer(
        f"{EMO['inactive']}  <b>Вимкнено</b>\n"
        f"<i>Авто-розсилка призупинена. Налаштування збережено.</i>",
        reply_markup=main_menu_kb(msg.from_user),
    )


# Статус-кнопка зі старого меню («📊 Стан: …», ще раніше «📊 Статус…») — відкриває профіль
@router.message(F.text.startswith(BTN_STATUS_PREFIX) | F.text.startswith("📊 Статус"))
async def status_button(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    from .profile import show_profile
    await show_profile(msg)


# ===================== Перехоплення меню-кнопок під час wizard'у =====================
# Ці handlers ловлять кліки по кнопках навігації навіть коли активний FSM.
# Зрозумілий принцип: завжди очищаємо стан і вимикаємо Telethon-клієнт wizard'у,
# далі делегуємо роботу до спеціалізованого роутера через прямий виклик.

async def _interrupt(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    from .connect import _disconnect_active
    await _disconnect_active(msg.from_user.id)


@router.message(F.text == BTN_CONNECT)
async def menu_connect(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .connect import start_connection
    await start_connection(msg, state)


@router.message(F.text == BTN_PROFILE)
async def menu_profile(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .profile import show_profile
    await show_profile(msg)


@router.message(F.text.in_({BTN_BROADCAST, BTN_CHOOSE_CHATS}))
async def menu_broadcast(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .broadcast import show_broadcast_settings
    await show_broadcast_settings(msg, state=state)


@router.message(F.text == BTN_PAYMENT)
async def menu_payment(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .payment import show_payment
    await show_payment(msg)


@router.message(F.text == BTN_REFERRAL)
async def menu_referral(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .referral import show_referral
    await show_referral(msg)


@router.message(F.text == BTN_SUPPORT)
async def menu_support(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .support import open_support
    await open_support(msg, state)


# ===================== Меню «☰ Ще» =====================
@router.message(F.text == BTN_MORE)
async def menu_more(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    await msg.answer(
        "☰  <b>Ще</b>",
        reply_markup=more_menu_kb(connected=has_session(msg.from_user)),
    )


@router.callback_query(F.data.startswith("more:"))
async def more_action(call: types.CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    # Хендлери розділів приймають Message — підставляємо автора натискання
    msg = call.message.model_copy(update={"from_user": call.from_user})
    action = call.data.split(":", 1)[1]
    if action == "referral":
        from .referral import show_referral
        await show_referral(msg)
    elif action == "pay":
        from .payment import show_payment
        await show_payment(msg)
    elif action == "support":
        from .support import open_support
        await open_support(msg, state)
    elif action == "help":
        await cmd_help(msg, state)
    elif action == "connect":
        from .connect import start_connection
        await start_connection(msg, state)

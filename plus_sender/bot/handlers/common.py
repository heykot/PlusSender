"""Загальні handlers: /start, /help, /cancel, Старт/Стоп, статус-кнопка.

Цей роутер реєструється ПЕРШИМ — щоб менеджмент-команди (cancel, статус, меню)
могли перервати будь-який FSM-майстер.
"""
from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from ...config import (
    BRAND,
    BTN_BROADCAST,
    BTN_CANCEL,
    BTN_CONNECT,
    BTN_HELP,
    BTN_PAYMENT,
    BTN_PROFILE,
    BTN_START,
    BTN_STOP,
    BTN_SUPPORT,
    CANCEL_TEXTS,
    DIV,
    EMO,
    TAGLINE,
)
from ...storage import (
    get_access_until,
    get_status,
    get_targets,
    has_access,
    load_user,
    refresh_user_meta,
    set_status,
)
from ...utils import (
    access_status_line,
    card,
    h,
    section,
    status_badge,
    status_label,
)
from ..keyboards import main_menu_kb

router = Router(name="common")


@router.message(Command("start", "menu"))
async def cmd_start(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    user = msg.from_user
    refresh_user_meta(user)
    data = load_user(user)
    active = bool(data.get("status", False))
    targets_count = len(get_targets(data))
    has_session = bool(data.get("api_id") and data.get("api_hash"))

    # Заголовок з брендингом
    header = (
        f"🤖  <b>{BRAND}</b>  <i>· {TAGLINE}</i>\n"
        f"{DIV}"
    )

    intro = (
        "Привіт! Я автоматично надсилаю повідомлення з вашого "
        "Telegram у вибрані чати, коли в Києві <b>починається</b> або "
        "<b>закінчується</b> повітряна тривога."
    )

    # Швидкий старт — три кроки із чек-боксами поточного стану
    step1 = "✅" if has_session else "▫️"
    step2 = "✅" if targets_count > 0 else "▫️"
    step3 = "✅" if active else "▫️"

    quick = section(
        "Швидкий старт",
        f"{step1}  ① 🔌 <b>Підключення</b> — створюємо Telethon-сесію\n"
        f"{step2}  ② 🎯 <b>Налаштування</b> — обираємо чати та тексти\n"
        f"{step3}  ③ ▶️ <b>Старт</b> — вмикаємо авто-розсилку",
    )

    # Стан-картка
    state_lines = [
        f"Режим:    {status_badge(active)}",
        f"Чатів:    <b>{targets_count}</b>",
        f"Доступ:   <b>{access_status_line(data.get('access_until'))}</b>",
    ]
    state_block = section("Поточний стан", "\n".join(state_lines))

    text = f"{header}\n{intro}\n\n{quick}\n\n{state_block}"
    await msg.answer(text, reply_markup=main_menu_kb(user))


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    from ...config import PROJECT_ROOT
    instruction_path = PROJECT_ROOT / "ІНСТРУКЦІЯ.html"
    if instruction_path.is_file():
        doc = types.FSInputFile(str(instruction_path), filename="Plus_Sender_Інструкція.html")
        await msg.answer_document(
            doc,
            caption=(
                f"{EMO['info']}  <b>Інструкція Plus Sender</b>\n"
                f"Збережіть файл і відкрийте у браузері — повний посібник від А до Я."
            ),
            reply_markup=main_menu_kb(msg.from_user),
        )
    else:
        await msg.answer(
            f"{EMO['info']}  <b>Інструкція не знайдена</b>\n"
            f"Файл <code>ІНСТРУКЦІЯ.html</code> відсутній у папці бота.",
            reply_markup=main_menu_kb(msg.from_user),
        )


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


# ===================== Старт / Стоп =====================
@router.message(Command("on"))
@router.message(F.text == BTN_START)
async def turn_on(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    # Перериваємо активний Telethon-клієнт wizard'у, якщо він є
    from .connect import _disconnect_active
    await _disconnect_active(msg.from_user.id)

    if not has_access(msg.from_user):
        await msg.answer(
            f"{EMO['warn']}  <b>Немає активного доступу</b>\n"
            f"Перевірте розділ <b>💳 Оплата</b> або зверніться до адміністратора."
        )
        return
    refresh_user_meta(msg.from_user)
    set_status(msg.from_user, True)
    await msg.answer(
        f"{EMO['active']}  <b>Режим увімкнено</b>\n"
        f"<i>Бот реагуватиме на наступну тривогу та відбій.</i>",
        reply_markup=main_menu_kb(msg.from_user),
    )


@router.message(Command("off"))
@router.message(F.text == BTN_STOP)
async def turn_off(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    from .connect import _disconnect_active
    await _disconnect_active(msg.from_user.id)

    set_status(msg.from_user, False)
    await msg.answer(
        f"{EMO['inactive']}  <b>Режим вимкнено</b>\n"
        f"<i>Авто-розсилка призупинена. Налаштування збережено.</i>",
        reply_markup=main_menu_kb(msg.from_user),
    )


# Кнопка-індикатор статусу — теж відкриває профіль
@router.message(F.text.startswith("📊 Статус"))
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


@router.message(F.text == BTN_BROADCAST)
async def menu_broadcast(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .broadcast import show_broadcast_settings
    await show_broadcast_settings(msg, state=state)


@router.message(F.text == BTN_PAYMENT)
async def menu_payment(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .payment import show_payment
    await show_payment(msg)


@router.message(F.text == BTN_SUPPORT)
async def menu_support(msg: types.Message, state: FSMContext) -> None:
    await _interrupt(msg, state)
    from .support import open_support
    await open_support(msg, state)

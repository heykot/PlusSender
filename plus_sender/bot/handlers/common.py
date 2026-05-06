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
    DIV_THIN,
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
    next_hint,
    section,
    status_badge,
    status_label,
    warm_greeting,
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

    # ── Тепле привітання ──
    greeting = warm_greeting(user.first_name)
    header = (
        f"{greeting}\n"
        f"🤖  <b>{BRAND}</b>  <i>· {TAGLINE}</i>\n"
        f"{DIV}"
    )

    intro = (
        "Я допомагаю вам <b>автоматично</b> надсилати повідомлення у ваші чати, "
        "коли в Києві <b>починається</b> або <b>закінчується</b> повітряна тривога.\n"
        "<i>Ви налаштовуєте — далі бот працює сам.</i>"
    )

    # ── Прогрес: 3 кроки з великими маркерами ──
    def _mark(done: bool) -> str:
        return "✅" if done else "▫️"

    quick_body = (
        f"{_mark(has_session)}  <b>① Підключення</b>  🔌\n"
        f"      <i>прив'язуємо ваш Telegram-акаунт</i>\n\n"
        f"{_mark(targets_count > 0)}  <b>② Налаштування</b>  🎯\n"
        f"      <i>обираємо чати та повідомлення</i>\n\n"
        f"{_mark(active)}  <b>③ Старт</b>  ▶️\n"
        f"      <i>вмикаємо авто-розсилку</i>"
    )
    quick = section("Як почати — 3 простих кроки", quick_body)

    # ── Стан ──
    state_lines = [
        f"Режим:    {status_badge(active)}",
        f"Чатів:    <b>{targets_count}</b>",
        f"Доступ:   <b>{access_status_line(data.get('access_until'))}</b>",
    ]
    state_block = section("Ваш поточний стан", "\n".join(state_lines))

    # ── Динамічна підказка наступної дії ──
    if not has_access(user):
        hint = next_hint(
            "оплатіть доступ у розділі «💳 Оплата» — і повертайтесь сюди."
        )
    elif not has_session:
        hint = next_hint(
            "натисніть «🔌 Підключити» — це найдовший крок, далі простіше."
        )
    elif targets_count == 0:
        hint = next_hint(
            "натисніть «🎛 Налаштування» — оберіть чати, у які буде надсилатися сповіщення."
        )
    elif not active:
        hint = next_hint(
            "натисніть «▶️ Старт» — і бот почне реагувати на наступну тривогу."
        )
    else:
        hint = (
            f"{EMO['star']}  <b>Все готово!</b>\n"
            f"<i>Бот уже стежить за тривогою. Можна закривати чат — він працює сам.</i>"
        )

    text = f"{header}\n{intro}\n\n{quick}\n\n{state_block}\n\n{hint}"
    await msg.answer(text, reply_markup=main_menu_kb(user))


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    from ...config import PROJECT_ROOT
    instruction_path = PROJECT_ROOT / "ІНСТРУКЦІЯ.html"

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
                "Як почати — за 3 кроки",
                "①  🔌  <b>Підключити</b> — прив'язуємо ваш акаунт через Telethon\n"
                "②  🎛  <b>Налаштування</b> — обираємо чати, тексти, кружечки\n"
                "③  ▶️  <b>Старт</b> — вмикаємо авто-роботу",
            ),
            (
                "Часті питання",
                "<b>Що таке api_id / api_hash?</b>\n"
                "<i>Ключі вашого Telegram-акаунта. Беруться на "
                "my.telegram.org → API Development Tools.</i>\n\n"
                "<b>Чи безпечно?</b>\n"
                "<i>Так. Ключі зберігаються лише у вашому профілі на сервері бота. "
                "Жодних паролів я не бачу й не передаю далі.</i>\n\n"
                "<b>Чому не надсилається?</b>\n"
                "<i>Перевірте: оплачений доступ, активна сесія, обрано чати, "
                "і кнопка «▶️ Старт» натиснута. Усе це видно в «👤 Профіль».</i>",
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

    if instruction_path.is_file():
        doc = types.FSInputFile(str(instruction_path), filename="Plus_Sender_Інструкція.html")
        await msg.answer(faq, reply_markup=main_menu_kb(msg.from_user))
        await msg.answer_document(
            doc,
            caption=(
                f"📖  <b>Повна інструкція в HTML</b>\n"
                f"<i>Збережіть і відкрийте у браузері — там є все від А до Я зі скріншотами.</i>"
            ),
        )
    else:
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

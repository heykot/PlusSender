"""Адмін-панель: одне повідомлення, яке перемальовується на місці.

  ad:home                        зведення + меню
  ad:users:<filter>:<page>       список (all | active | expiring | noaccess | problems)
  ad:find                        пошук за ID / @username / імʼям
  ad:u:<uid>                     картка користувача
  ad:add:<uid>:<days>            продовжити доступ
  ad:date:<uid>                  точна дата доступу
  ad:toggle:<uid>                увімкнути / вимкнути авто-розсилку
  ad:revoke:<uid> → ad:revokeok  забрати доступ
  ad:test:<uid>:<mode> → testok  тест розсилки ЛИШЕ для цього користувача
  ad:msg:<uid>                   написати користувачу
  ad:del:<uid> → ad:delok        видалити профіль і сесію
  ad:pay                         оплати
  ad:last                        остання розсилка
  ad:bc → ad:bc:<aud> → ad:bcgo  повідомлення користувачам (з попереднім переглядом)
  ad:admins / ad:admdel / ad:admadd

Права перевіряє фільтр на весь роутер — окремий хендлер «забути» не може.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext

from ... import alarm, sender
from ...config import HR, PROJECT_ROOT, REFERRAL_BONUS_DAYS
from ...storage import (
    access_days_left,
    count_referrals_for,
    delete_user_profile,
    extend_access_days,
    get_access_until,
    get_referrer,
    get_targets,
    is_admin,
    load_admins,
    load_all_users,
    revoke_access,
    save_admins,
    session_file_path_for_id,
    set_access_for_user_id,
    toggle_user_status,
    user_file_path_for_id,
)
from ...utils import h, truncate
from ..states import AdminStates
from .broadcast import _describe, _is_unset, _target_title

log = logging.getLogger(__name__)
router = Router(name="admin")

PAGE_SIZE = 8
EXPIRING_DAYS = 7
PAYMENTS_LOG = PROJECT_ROOT / "logs" / "payments.log"

I = types.InlineKeyboardButton


class IsAdmin(BaseFilter):
    async def __call__(self, event: types.TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and is_admin(user.id))


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _kb(rows: list[list[types.InlineKeyboardButton]]) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _back(cb: str = "ad:home", text: str = "‹ Назад") -> list[types.InlineKeyboardButton]:
    return [I(text=text, callback_data=cb)]


async def _render(call: types.CallbackQuery, text: str, kb: types.InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc):
            return
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


def _audit(admin: types.User, action: str, uid: Optional[int] = None, **extra) -> None:
    parts = " ".join(f"{k}={v}" for k, v in extra.items())
    log.info("admin %d: %s%s %s", admin.id, action, f" uid={uid}" if uid else "", parts)


# ─────────────────────── Дані про користувача ───────────────────────

def _name(uid: int, data: dict) -> str:
    """Найкраще людське імʼя: @username → імʼя → ID."""
    uname = data.get("username") or data.get("user_name")
    if uname:
        return f"@{uname}"
    first = " ".join(x for x in (data.get("first_name"), data.get("last_name")) if x)
    return first or f"id{uid}"


_days_left = access_days_left


def _access_line(data: dict) -> str:
    until = get_access_until(data)
    if not until:
        return "🚫 немає"
    left = _days_left(data)
    if left is None:
        return f"⏰ закінчився {until:%d.%m.%Y}"
    return f"✅ до {until:%d.%m.%Y} (ще {left} дн.)"


def _has_session(uid: int) -> bool:
    return os.path.isfile(session_file_path_for_id(uid))


def _issues(uid: int, data: dict) -> list[str]:
    """Чому в користувача може не працювати розсилка."""
    out: list[str] = []
    if not _has_session(uid):
        out.append("❌ Telegram не підключено")
    targets = get_targets(data)
    if not targets:
        out.append("❌ Не обрано жодного чату")
    for pid in targets:
        for mode in ("alert", "clear"):
            if _is_unset(data, pid, mode):
                out.append(f"⚠️ «{_target_title(data, pid)}» не налаштовано")
                break
    if _days_left(data) is None:
        out.append("❌ Немає активного доступу")
    if not data.get("status"):
        out.append("⏸ Авто-розсилку вимкнено")
    return out


def _is_problem(uid: int, data: dict) -> bool:
    """Хоче працювати (увімкнено або є доступ), але щось заважає."""
    wants = bool(data.get("status")) or _days_left(data) is not None
    return wants and bool([x for x in _issues(uid, data) if not x.startswith("⏸")])


def _expiring(uid: int, data: dict) -> bool:
    left = _days_left(data)
    return left is not None and left <= EXPIRING_DAYS


FILTERS: dict[str, tuple[str, callable]] = {
    "all":      ("Всі",            lambda uid, d: True),
    "active":   ("Працюють",       lambda uid, d: bool(d.get("status")) and _days_left(d) is not None),
    "expiring": ("Спливає доступ", _expiring),
    "noaccess": ("Без доступу",    lambda uid, d: _days_left(d) is None),
    "problems": ("Проблеми",       _is_problem),
}


def _sorted_users(users: dict[int, dict]) -> list[tuple[int, dict]]:
    """Спершу ті, в кого доступ діє, — за датою закінчення; потім решта."""
    def key(item):
        uid, d = item
        left = _days_left(d)
        return (left is None, left if left is not None else 0, _name(uid, d).lower())
    return sorted(users.items(), key=key)


def _user_button(uid: int, data: dict) -> types.InlineKeyboardButton:
    dot = "🟢" if data.get("status") and _days_left(data) is not None else "⚪️"
    left = _days_left(data)
    tail = f"{left} дн." if left is not None else "—"
    warn = " ⚠️" if _is_problem(uid, data) else ""
    return I(text=f"{dot} {truncate(_name(uid, data), 22)} · {tail}{warn}", callback_data=f"ad:u:{uid}")


# ─────────────────────── Оплати ───────────────────────

_PAY_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \| \[(\w+)\]\s+(.*)$")


def _payments() -> list[dict]:
    """Записи з logs/payments.log (старі й нові), від нових до старих."""
    try:
        lines = PAYMENTS_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        m = _PAY_RE.match(line)
        if not m:
            continue
        fields = dict(re.findall(r"(\w+)=(\S+)", m.group(3)))
        out.append({"at": datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), "status": m.group(2), **fields})
    return list(reversed(out))


def _uah(p: dict) -> int:
    try:
        return int(float(p.get("amount_uah", 0)))
    except ValueError:
        return 0


# ─────────────────────── Головний екран ───────────────────────

def _home() -> tuple[str, types.InlineKeyboardMarkup]:
    users = load_all_users()
    total = len(users)
    with_access = sum(1 for d in users.values() if _days_left(d) is not None)
    working = sum(1 for uid, d in users.items() if FILTERS["active"][1](uid, d))
    expiring = sum(1 for uid, d in users.items() if FILTERS["expiring"][1](uid, d))
    problems = sum(1 for uid, d in users.items() if _is_problem(uid, d))

    month_ago = datetime.now() - timedelta(days=30)
    pays = [p for p in _payments() if p["status"] == "SUCCESS"]
    month = [p for p in pays if p["at"] >= month_ago]
    attention = [p for p in _payments() if p["status"] in ("NO_ID", "UNVERIFIED") and p["at"] >= month_ago]

    mon = alarm.CURRENT
    if mon is None or mon.is_alert is None:
        alarm_line = "❔ невідомо (API ще не відповів)"
    else:
        state = "🚨 <b>ТРИВОГА</b>" if mon.is_alert else "🟢 спокій"
        checked = f", перевірено {mon.last_check:%H:%M}" if mon.last_check else ""
        stale = " ⚠️ <b>API не відповідає</b>" if mon.last_check and datetime.now() - mon.last_check > timedelta(minutes=5) else ""
        alarm_line = f"{state}{checked}{stale}"

    run = sender.LAST_RUN
    if run:
        ok = sum(1 for s, _ in run["results"].values() if s)
        run_line = (f"{'🚨 тривога' if run['mode'] == 'alert' else '✅ відбій'} "
                    f"{run['at']:%d.%m %H:%M} — надіслано {ok} з {len(run['results'])}")
    else:
        run_line = "ще не було (з моменту запуску бота)"

    text = (
        f"🔐  <b>Адмін-панель</b>\n{HR}\n\n"
        f"👥 Користувачів: <b>{total}</b> · працюють: <b>{working}</b>\n"
        f"✅ З доступом: <b>{with_access}</b> · спливає за {EXPIRING_DAYS} дн.: <b>{expiring}</b>\n"
        f"⚠️ З проблемами: <b>{problems}</b>\n\n"
        f"💳 Оплат за 30 дн.: <b>{len(month)}</b> на <b>{sum(map(_uah, month))} грн</b>"
        + (f"\n❗ Потребують уваги: <b>{len(attention)}</b>" if attention else "")
        + f"\n\n📡 Тривога зараз: {alarm_line}\n"
        f"📨 Остання розсилка: {run_line}"
    )
    rows = [
        [I(text="👥 Користувачі", callback_data="ad:users:all:0"), I(text="🔍 Знайти", callback_data="ad:find")],
        [I(text=f"⚠️ Проблеми ({problems})", callback_data="ad:users:problems:0"),
         I(text=f"⏰ Спливає ({expiring})", callback_data="ad:users:expiring:0")],
        [I(text="💳 Оплати", callback_data="ad:pay"), I(text="📡 Остання розсилка", callback_data="ad:last")],
        [I(text="📨 Написати користувачам", callback_data="ad:bc")],
        [I(text="👮 Адміни", callback_data="ad:admins"), I(text="🔄 Оновити", callback_data="ad:home")],
    ]
    return text, _kb(rows)


@router.message(Command("admin"))
async def admin_menu(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = _home()
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data == "ad:home")
async def cb_home(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _render(call, *_home())


# ─────────────────────── Список і пошук ───────────────────────

def _users_screen(flt: str, page: int) -> tuple[str, types.InlineKeyboardMarkup]:
    users = load_all_users()
    label, pred = FILTERS.get(flt, FILTERS["all"])
    items = [(uid, d) for uid, d in _sorted_users(users) if pred(uid, d)]
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    text = (
        f"👥  <b>Користувачі · {label}</b>  ({len(items)})\n{HR}\n\n"
        f"🟢 працює · ⚪️ ні · <i>N дн.</i> — лишилось доступу · ⚠️ є проблема"
        + ("\n\n<i>Нікого немає.</i>" if not items else "")
    )
    rows = [[_user_button(uid, d)] for uid, d in chunk]
    nav = []
    if page > 0:
        nav.append(I(text="◀️", callback_data=f"ad:users:{flt}:{page - 1}"))
    if pages > 1:
        nav.append(I(text=f"{page + 1}/{pages}", callback_data="ad:noop"))
    if page < pages - 1:
        nav.append(I(text="▶️", callback_data=f"ad:users:{flt}:{page + 1}"))
    if nav:
        rows.append(nav)
    tabs = [I(text=("• " if key == flt else "") + name, callback_data=f"ad:users:{key}:0")
            for key, (name, _) in FILTERS.items()]
    rows += [tabs[:3], tabs[3:]]
    rows.append(_back())
    return text, _kb(rows)


@router.callback_query(F.data.startswith("ad:users:"))
async def cb_users(call: types.CallbackQuery) -> None:
    _, _, flt, page = (call.data.split(":") + ["0"])[:4]
    await call.answer()
    await _render(call, *_users_screen(flt, int(page) if page.isdigit() else 0))


@router.callback_query(F.data == "ad:find")
async def cb_find(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_user_search)
    await call.answer()
    await _render(call, (
        "🔍  <b>Знайти користувача</b>\n\n"
        "Напишіть Telegram ID, @username або частину імені."
    ), _kb([_back()]))


@router.message(AdminStates.waiting_user_search)
async def find_input(msg: types.Message, state: FSMContext) -> None:
    q = (msg.text or "").strip().lstrip("@").lower()
    await state.clear()
    users = load_all_users()
    found = [
        (uid, d) for uid, d in _sorted_users(users)
        if q and (q == str(uid) or q in _name(uid, d).lower() or q in str(d.get("first_name") or "").lower())
    ]
    if len(found) == 1:
        text, kb = _card(*found[0])
    else:
        text = f"🔍  Знайдено: <b>{len(found)}</b> за «{h(q)}»" + ("" if found else "\n<i>Спробуйте інший запит.</i>")
        kb = _kb([[_user_button(uid, d)] for uid, d in found[:15]] + [[I(text="🔍 Ще раз", callback_data="ad:find")], _back()])
    await msg.answer(text, reply_markup=kb)


# ─────────────────────── Картка користувача ───────────────────────

def _card(uid: int, data: dict) -> tuple[str, types.InlineKeyboardMarkup]:
    users = load_all_users()
    lines = [
        f"👤  <b>{h(_name(uid, data))}</b>  <code>{uid}</code>",
        f"<i>{h(' '.join(x for x in (data.get('first_name'), data.get('last_name')) if x))}</i>" if data.get("first_name") else "",
        HR,
        f"Авто-розсилка: {'🟢 увімкнена' if data.get('status') else '⚪️ вимкнена'}",
        f"Доступ: {_access_line(data)}",
        f"Telegram: {'✅ підключено' if _has_session(uid) else '❌ не підключено'}",
    ]

    ref = get_referrer(data)
    if ref:
        rname = _name(ref, users.get(ref, {}))
        bonus = " (бонус видано)" if data.get("referral_rewarded") else ""
        lines.append(f"Запросив: {h(rname)} <code>{ref}</code>{bonus}")
    invited, paid = count_referrals_for(uid)
    if invited:
        lines.append(f"Запросив друзів: <b>{invited}</b>, купили: <b>{paid}</b> (+{paid * REFERRAL_BONUS_DAYS} дн.)")

    pays = [p for p in _payments() if p.get("uid") == str(uid) and p["status"] == "SUCCESS"]
    if pays:
        lines.append(f"Оплат: <b>{len(pays)}</b> на <b>{sum(map(_uah, pays))} грн</b>, остання {pays[0]['at']:%d.%m.%Y}")

    targets = get_targets(data)
    lines.append(f"\n💬 <b>Чати ({len(targets)}):</b>" if targets else "\n💬 Чатів не обрано")
    for pid in targets:
        lines.append(
            f"• {h(truncate(_target_title(data, pid), 28))}\n"
            f"   🚨 {h(truncate(_describe(data, pid, 'alert'), 34))}\n"
            f"   ✅ {h(truncate(_describe(data, pid, 'clear'), 34))}"
        )

    issues = _issues(uid, data)
    lines.append("\n🩺 <b>Стан:</b> " + ("все гаразд, розсилка працюватиме" if not issues else ""))
    lines += [f"   {h(x)}" for x in issues]

    run = sender.LAST_RUN.get("results", {}).get(str(uid)) if sender.LAST_RUN else None
    if run:
        ok, reason = run
        when = sender.LAST_RUN["at"]
        lines.append(f"\n📨 Остання розсилка ({when:%d.%m %H:%M}): "
                     + ("✅ надіслано" if ok else "❌ не надіслано") + (f" — <code>{h(reason)}</code>" if reason else ""))

    toggle = "⏸ Вимкнути" if data.get("status") else "▶️ Увімкнути"
    rows = [
        [I(text="+30 дн.", callback_data=f"ad:add:{uid}:30"),
         I(text="+90 дн.", callback_data=f"ad:add:{uid}:90"),
         I(text="+365 дн.", callback_data=f"ad:add:{uid}:365")],
        [I(text="📅 Дата", callback_data=f"ad:date:{uid}"), I(text="🚫 Забрати доступ", callback_data=f"ad:revoke:{uid}")],
        [I(text=toggle, callback_data=f"ad:toggle:{uid}"), I(text="📨 Написати", callback_data=f"ad:msg:{uid}")],
        [I(text="🧪 Тест тривоги", callback_data=f"ad:test:{uid}:alert"),
         I(text="🧪 Тест відбою", callback_data=f"ad:test:{uid}:clear")],
        [I(text="🗑 Видалити", callback_data=f"ad:del:{uid}"), I(text="🔄", callback_data=f"ad:u:{uid}")],
        [I(text="‹ До списку", callback_data="ad:users:all:0"), I(text="🏠", callback_data="ad:home")],
    ]
    return "\n".join(x for x in lines if x != ""), _kb(rows)


def _uid(call: types.CallbackQuery) -> Optional[int]:
    try:
        return int(call.data.split(":")[2])
    except (IndexError, ValueError):
        return None


async def _show_card(call: types.CallbackQuery, uid: int, toast: Optional[str] = None, alert: bool = False) -> None:
    data = load_all_users().get(uid)
    if not data:
        await call.answer("Користувача не знайдено.", show_alert=True)
        await _render(call, *_home())
        return
    await call.answer(toast, show_alert=alert)
    await _render(call, *_card(uid, data))


@router.callback_query(F.data.startswith("ad:u:"))
async def cb_card(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    uid = _uid(call)
    if uid is not None:
        await _show_card(call, uid)


async def _notify(bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass  # користувач міг заблокувати бота


@router.callback_query(F.data.startswith("ad:add:"))
async def cb_add_days(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    days = int(call.data.split(":")[3])
    until = extend_access_days(uid, days)
    if not until:
        await call.answer("Користувача не знайдено.", show_alert=True)
        return
    _audit(call.from_user, "extend", uid, days=days, until=until)
    await _notify(call.bot, uid, f"🗝 Ваш доступ продовжено до <b>{until}</b>!")
    await _show_card(call, uid, f"✅ +{days} дн. → до {until}")


@router.callback_query(F.data.startswith("ad:date:"))
async def cb_set_date(call: types.CallbackQuery, state: FSMContext) -> None:
    uid = _uid(call)
    await state.set_state(AdminStates.waiting_access_date)
    await state.update_data(target_uid=uid)
    await call.answer()
    await _render(call, (
        f"📅  Дата, до якої діє доступ для <code>{uid}</code>\n\n"
        f"Формат: <b>РРРР-ММ-ДД</b> або <b>ДД.ММ.РРРР</b>"
    ), _kb([_back(f"ad:u:{uid}", "‹ Скасувати")]))


@router.message(AdminStates.waiting_access_date)
async def set_date_input(msg: types.Message, state: FSMContext) -> None:
    uid = int((await state.get_data()).get("target_uid", 0))
    raw = (msg.text or "").strip()
    date = None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            date = datetime.strptime(raw, fmt)
            break
        except ValueError:
            pass
    if not date:
        await msg.answer("⚠️ Не розпізнав дату. Приклад: <code>2027-01-01</code> або <code>01.01.2027</code>")
        return
    await state.clear()
    value = date.strftime("%Y-%m-%d")
    if set_access_for_user_id(uid, value):
        _audit(msg.from_user, "set_date", uid, until=value)
        await _notify(msg.bot, uid, f"🗝 Ваш доступ встановлено до <b>{value}</b>!")
    data = load_all_users().get(uid)
    if data:
        text, kb = _card(uid, data)
        await msg.answer(f"✅ Доступ до <b>{value}</b>.\n\n{text}", reply_markup=kb)
    else:
        await msg.answer("❌ Користувача не знайдено.", reply_markup=_kb([_back()]))


@router.callback_query(F.data.startswith("ad:toggle:"))
async def cb_toggle(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    data = load_all_users().get(uid) or {}
    new = not data.get("status")
    toggle_user_status(uid, new)
    _audit(call.from_user, "toggle", uid, status=new)
    await _show_card(call, uid, "▶️ Увімкнено" if new else "⏸ Вимкнено")


@router.callback_query(F.data.startswith("ad:revoke:"))
async def cb_revoke(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    await call.answer()
    await _render(call, f"🚫  Забрати доступ у <code>{uid}</code>?\n<i>Користувач отримає повідомлення.</i>", _kb([[
        I(text="🚫 Так, забрати", callback_data=f"ad:revokeok:{uid}"), I(text="‹ Ні", callback_data=f"ad:u:{uid}"),
    ]]))


@router.callback_query(F.data.startswith("ad:revokeok:"))
async def cb_revoke_ok(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    if revoke_access(uid):
        _audit(call.from_user, "revoke", uid)
        await _notify(call.bot, uid, "🚫 Ваш доступ скасовано адміністратором.")
    await _show_card(call, uid, "🚫 Доступ забрано")


@router.callback_query(F.data.startswith("ad:test:"))
async def cb_test(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    mode = call.data.split(":")[3]
    data = load_all_users().get(uid) or {}
    label = "тривоги" if mode == "alert" else "відбою"
    chats = "\n".join(f"• {h(_target_title(data, pid))}: {h(_describe(data, pid, mode))}" for pid in get_targets(data))
    await call.answer()
    await _render(call, (
        f"🧪  <b>Тест {label} для {h(_name(uid, data))}</b>\n{HR}\n\n"
        f"Бот <b>по-справжньому</b> надішле від імені користувача:\n{chats or '<i>чатів немає</i>'}\n\n"
        f"<i>Затримки враховуються. Кружки в режимі «відправив → видалив» будуть видалені з джерела.</i>"
    ), _kb([[I(text="🧪 Так, надіслати", callback_data=f"ad:testok:{uid}:{mode}"),
             I(text="‹ Ні", callback_data=f"ad:u:{uid}")]]))


@router.callback_query(F.data.startswith("ad:testok:"))
async def cb_test_ok(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    mode = call.data.split(":")[3]
    await call.answer("Надсилаю…")
    _audit(call.from_user, "test", uid, mode=mode)
    _, ok, reason = await sender._send_for_user(user_file_path_for_id(uid), mode)
    result = "✅ Надіслано" + (f" ({reason})" if reason else "") if ok else f"❌ Не надіслано: {reason}"
    data = load_all_users().get(uid)
    if data:
        text, kb = _card(uid, data)
        await _render(call, f"🧪 <b>Тест:</b> {h(result)}\n\n{text}", kb)


@router.callback_query(F.data.startswith("ad:msg:"))
async def cb_msg(call: types.CallbackQuery, state: FSMContext) -> None:
    uid = _uid(call)
    await state.set_state(AdminStates.waiting_user_message)
    await state.update_data(target_uid=uid)
    await call.answer()
    await _render(call, (
        f"📨  Надішліть повідомлення для <code>{uid}</code>\n"
        f"<i>Текст, фото, відео — буде надіслано як є, від імені бота.</i>"
    ), _kb([_back(f"ad:u:{uid}", "‹ Скасувати")]))


@router.message(AdminStates.waiting_user_message)
async def msg_input(msg: types.Message, state: FSMContext) -> None:
    uid = int((await state.get_data()).get("target_uid", 0))
    await state.clear()
    try:
        await msg.copy_to(uid)
        _audit(msg.from_user, "message", uid)
        result = f"✅ Надіслано <code>{uid}</code>"
    except Exception as e:
        result = f"❌ Не вдалося: {h(str(e))}"
    await msg.answer(result, reply_markup=_kb([_back(f"ad:u:{uid}", "‹ До картки")]))


@router.callback_query(F.data.startswith("ad:del:"))
async def cb_delete(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    await call.answer()
    await _render(call, (
        f"🗑  <b>Видалити профіль <code>{uid}</code>?</b>\n\n"
        f"Буде видалено налаштування, доступ і сесію Telegram. Дію не можна скасувати."
    ), _kb([[I(text="🗑 Так, видалити", callback_data=f"ad:delok:{uid}"), I(text="‹ Ні", callback_data=f"ad:u:{uid}")]]))


@router.callback_query(F.data.startswith("ad:delok:"))
async def cb_delete_ok(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    ok = delete_user_profile(uid)
    if ok:
        _audit(call.from_user, "delete", uid)
    await call.answer("🗑 Видалено" if ok else "Не знайдено")
    await _render(call, *_users_screen("all", 0))


# ─────────────────────── Оплати ───────────────────────

@router.callback_query(F.data == "ad:pay")
async def cb_payments(call: types.CallbackQuery) -> None:
    pays = _payments()
    users = load_all_users()
    ok = [p for p in pays if p["status"] == "SUCCESS"]
    month = [p for p in ok if p["at"] >= datetime.now() - timedelta(days=30)]
    attention = [p for p in pays if p["status"] in ("NO_ID", "UNVERIFIED", "LOW_AMOUNT")][:5]

    def who(p: dict) -> str:
        uid = p.get("uid")
        return h(_name(int(uid), users.get(int(uid), {}))) if uid and uid.isdigit() else "—"

    lines = [
        f"💳  <b>Оплати</b>\n{HR}\n",
        f"За 30 днів: <b>{len(month)}</b> на <b>{sum(map(_uah, month))} грн</b>",
        f"Всього: <b>{len(ok)}</b> на <b>{sum(map(_uah, ok))} грн</b>",
        "\n<b>Останні:</b>" if ok else "\n<i>Оплат ще не було.</i>",
    ]
    lines += [f"• {p['at']:%d.%m %H:%M} · {_uah(p)} грн · {who(p)} · +{p.get('days', '?')} дн." for p in ok[:10]]
    if attention:
        labels = {"NO_ID": "без ID у коментарі", "UNVERIFIED": "не вдалося перевірити", "LOW_AMOUNT": "сума замала"}
        lines.append("\n❗ <b>Потребують уваги</b> <i>(видайте доступ вручну в картці):</i>")
        lines += [f"• {p['at']:%d.%m %H:%M} · {_uah(p) or '?'} грн · {labels[p['status']]}"
                  + (f" · {who(p)}" if p.get("uid") else "") for p in attention]
    await call.answer()
    await _render(call, "\n".join(lines), _kb([_back()]))


# ─────────────────────── Остання розсилка ───────────────────────

@router.callback_query(F.data == "ad:last")
async def cb_last_run(call: types.CallbackQuery) -> None:
    run = sender.LAST_RUN
    await call.answer()
    if not run:
        await _render(call, "📡  Розсилок ще не було з моменту запуску бота.", _kb([_back()]))
        return
    users = load_all_users()
    results = run["results"]
    ok = [(n, r) for n, (s, r) in results.items() if s]
    fail = [(n, r) for n, (s, r) in results.items() if not s]

    def who(n: str) -> str:
        return h(_name(int(n), users.get(int(n), {}))) if n.isdigit() else h(n)

    lines = [
        f"📡  <b>Остання розсилка</b>: {'🚨 тривога' if run['mode'] == 'alert' else '✅ відбій'} "
        f"{run['at']:%d.%m %H:%M}\n{HR}\n",
        f"✅ Надіслано: <b>{len(ok)}</b>" + "".join(f"\n• {who(n)}" + (f" — {h(r)}" if r else "") for n, r in ok[:15]),
        f"\n⏭ Не надіслано: <b>{len(fail)}</b>" + "".join(f"\n• {who(n)} — <code>{h(r or '?')}</code>" for n, r in fail[:15]),
    ]
    rows = [[_user_button(int(n), users[int(n)])] for n, _ in fail if n.isdigit() and int(n) in users and _is_problem(int(n), users[int(n)])][:6]
    await _render(call, "\n".join(lines), _kb(rows + [_back()]))


# ─────────────────────── Повідомлення користувачам ───────────────────────

AUDIENCES = {
    "all":      ("всім", lambda uid, d: True),
    "access":   ("з активним доступом", lambda uid, d: _days_left(d) is not None),
    "noaccess": ("без доступу", lambda uid, d: _days_left(d) is None),
}


def _audience(key: str) -> list[int]:
    _, pred = AUDIENCES[key]
    return [uid for uid, d in load_all_users().items() if pred(uid, d)]


@router.callback_query(F.data == "ad:bc")
async def cb_bc(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    rows = [[I(text=f"{name.capitalize()} ({len(_audience(key))})", callback_data=f"ad:bc:{key}")]
            for key, (name, _) in AUDIENCES.items()]
    await _render(call, "📨  <b>Повідомлення від бота</b>\n\nКому надіслати?", _kb(rows + [_back()]))


@router.callback_query(F.data.startswith("ad:bc:"))
async def cb_bc_audience(call: types.CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":")[2]
    if key not in AUDIENCES:
        await call.answer()
        return
    await state.set_state(AdminStates.waiting_broadcast_text)
    await state.update_data(bc_audience=key)
    await call.answer()
    await _render(call, (
        f"📨  Надішліть повідомлення для користувачів <b>{AUDIENCES[key][0]}</b> "
        f"({len(_audience(key))}).\n<i>Текст, фото, відео — перед відправкою покажу, як воно виглядатиме.</i>"
    ), _kb([_back("ad:bc", "‹ Скасувати")]))


@router.message(AdminStates.waiting_broadcast_text)
async def bc_input(msg: types.Message, state: FSMContext) -> None:
    key = (await state.get_data()).get("bc_audience", "all")
    await state.update_data(bc_chat=msg.chat.id, bc_msg=msg.message_id)
    await state.set_state(None)
    count = len(_audience(key))
    await msg.copy_to(msg.chat.id)  # попередній перегляд — так побачать користувачі
    await msg.answer(
        f"👆 Так виглядатиме повідомлення.\nНадіслати <b>{count}</b> користувачам ({AUDIENCES[key][0]})?",
        reply_markup=_kb([[I(text=f"✅ Надіслати ({count})", callback_data="ad:bcgo"),
                           I(text="‹ Скасувати", callback_data="ad:home")]]),
    )


@router.callback_query(F.data == "ad:bcgo")
async def cb_bc_go(call: types.CallbackQuery, state: FSMContext) -> None:
    fsm = await state.get_data()
    await state.clear()
    if "bc_msg" not in fsm:
        await call.answer("Повідомлення вже надіслано або застаріло.", show_alert=True)
        return
    uids = _audience(fsm.get("bc_audience", "all"))
    await call.answer("Надсилаю…")
    await _render(call, f"📨 Надсилаю {len(uids)} користувачам…", _kb([]))
    ok = fail = 0
    for uid in uids:
        try:
            await call.bot.copy_message(uid, fsm["bc_chat"], fsm["bc_msg"])
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # ліміт Telegram ~30 повідомлень/с
    _audit(call.from_user, "broadcast", None, audience=fsm.get("bc_audience"), ok=ok, fail=fail)
    await _render(call, f"📨 <b>Готово.</b>\n✅ Доставлено: <b>{ok}</b>\n❌ Не доставлено: <b>{fail}</b> "
                        f"<i>(заблокували бота)</i>", _kb([_back()]))


# ─────────────────────── Адміни ───────────────────────

def _admins_screen() -> tuple[str, types.InlineKeyboardMarkup]:
    admins = load_admins()
    users = load_all_users()
    rows = [[I(text=f"👮 {truncate(_name(uid, users.get(uid, {})), 24)} · 🗑", callback_data=f"ad:admdel:{uid}")]
            for uid in admins]
    rows += [[I(text="➕ Додати адміна", callback_data="ad:admadd")], _back()]
    return f"👮  <b>Адміністратори</b> ({len(admins)})\n{HR}\n\nНатисніть на адміна, щоб прибрати.", _kb(rows)


@router.callback_query(F.data == "ad:admins")
async def cb_admins(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await _render(call, *_admins_screen())


@router.callback_query(F.data.startswith("ad:admdel:"))
async def cb_admin_del(call: types.CallbackQuery) -> None:
    uid = _uid(call)
    if uid == call.from_user.id:
        await call.answer("Не можна прибрати себе.", show_alert=True)
        return
    admins = load_admins()
    if admins.pop(uid, None) is not None:
        save_admins(admins)
        _audit(call.from_user, "admin_del", uid)
    await call.answer("Прибрано")
    await _render(call, *_admins_screen())


@router.callback_query(F.data == "ad:admadd")
async def cb_admin_add(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_new_admin_id)
    await call.answer()
    await _render(call, "👮  Напишіть <b>Telegram ID</b> нового адміна (число).\n"
                        "<i>Його видно в картці користувача.</i>", _kb([_back("ad:admins", "‹ Скасувати")]))


@router.message(AdminStates.waiting_new_admin_id)
async def admin_add_input(msg: types.Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        await msg.answer("⚠️ Потрібен числовий ID, наприклад <code>123456789</code>")
        return
    await state.clear()
    new_id = int(raw)
    admins = load_admins()
    data = load_all_users().get(new_id, {})
    admins[new_id] = data.get("username") or data.get("user_name") or "—"
    save_admins(admins)
    _audit(msg.from_user, "admin_add", new_id)
    text, kb = _admins_screen()
    await msg.answer(f"✅ Адміна <code>{new_id}</code> додано.\n\n{text}", reply_markup=kb)


# ─────────────────────── Команди (швидкий доступ) ───────────────────────

@router.message(Command("access"))
async def cmd_access(msg: types.Message) -> None:
    parts = (msg.text or "").split()
    try:
        uid = int(parts[1])
        value = datetime.strptime(parts[2], "%Y-%m-%d").strftime("%Y-%m-%d")
    except (IndexError, ValueError):
        await msg.answer("⚙️ Формат: <code>/access 123456789 2027-01-01</code>")
        return
    if set_access_for_user_id(uid, value):
        _audit(msg.from_user, "set_date", uid, until=value)
        await msg.answer(f"✅ <code>{uid}</code> — доступ до <b>{value}</b>")
    else:
        await msg.answer("❌ Користувача не знайдено.")


@router.callback_query(F.data == "ad:noop")
async def cb_noop(call: types.CallbackQuery) -> None:
    await call.answer()


# Кнопки старої адмінки, що лишились у чатах
@router.callback_query(F.data.regexp(r"^(admin|admu|adma|grant|revoke):"))
async def cb_stale(call: types.CallbackQuery) -> None:
    await call.answer("Адмін-панель оновилась — надішліть /admin.", show_alert=True)

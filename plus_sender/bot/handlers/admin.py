"""Адмін-панель: управління користувачами, доступом, статистика, розсилка."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from ...config import EMO, HR, SESSIONS_DIR
from ...sender import broadcast_for_all_users
from ...storage import (
    delete_user_profile,
    extend_access_days,
    get_targets,
    grant_access_days,
    is_admin,
    load_admins,
    load_all_users,
    revoke_access,
    save_admins,
    set_access_for_user_id,
    toggle_user_status,
)
from ...utils import access_status_line, h
from ..keyboards import (
    admin_admins_kb,
    admin_root_kb,
    admin_user_detail_kb,
    admin_user_list_kb,
)
from ..states import AdminStates

log = logging.getLogger(__name__)
router = Router(name="admin")

_PAGE_SIZE = 10


# ─────────────────────── Перевірка прав ───────────────────────

def _guard(uid: int) -> bool:
    return is_admin(uid)


# ─────────────────────── Головне меню ───────────────────────

@router.message(Command("admin"))
async def admin_menu(msg: types.Message) -> None:
    if not _guard(msg.from_user.id):
        await msg.answer("❌ Немає доступу.")
        return
    await msg.answer(f"🔐 <b>Адмін-панель</b>\n{HR}", reply_markup=admin_root_kb())


@router.callback_query(F.data == "admin:back")
async def admin_back(call: types.CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    await state.clear()
    await call.answer()
    try:
        await call.message.edit_text(f"🔐 <b>Адмін-панель</b>\n{HR}", reply_markup=admin_root_kb())
    except Exception:
        await call.message.answer(f"🔐 <b>Адмін-панель</b>\n{HR}", reply_markup=admin_root_kb())


# ─────────────────────── Статистика ───────────────────────

@router.callback_query(F.data == "admin:stats")
async def cb_stats(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return

    users = load_all_users()
    total = len(users)
    active = sum(1 for d in users.values() if d.get("status"))
    now = datetime.now()

    valid_access = 0
    expired_access = 0
    no_access = 0
    has_session = 0
    has_targets = 0

    for uid, d in users.items():
        acc = d.get("access_until")
        if acc:
            try:
                if datetime.strptime(str(acc), "%Y-%m-%d") >= now:
                    valid_access += 1
                else:
                    expired_access += 1
            except ValueError:
                no_access += 1
        else:
            no_access += 1

        uname = d.get("user_name") or str(uid)
        sess = os.path.join(SESSIONS_DIR, f"{uname}.session")
        if os.path.isfile(sess):
            has_session += 1

        if get_targets(d):
            has_targets += 1

    text = (
        f"📊  <b>Статистика</b>\n{HR}\n\n"
        f"👥 Всього профілів:     <b>{total}</b>\n"
        f"🟢 Активних (status):  <b>{active}</b>\n"
        f"🔴 Неактивних:         <b>{total - active}</b>\n\n"
        f"✅ Доступ дійсний:     <b>{valid_access}</b>\n"
        f"⏰ Доступ прострочено: <b>{expired_access}</b>\n"
        f"🚫 Без доступу:        <b>{no_access}</b>\n\n"
        f"📱 Є Telethon-сесія:   <b>{has_session}</b>\n"
        f"🎯 Є цільові чати:     <b>{has_targets}</b>"
    )
    await call.answer()
    try:
        await call.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="↩️ Назад", callback_data="admin:back")]
            ]),
        )
    except Exception:
        await call.message.answer(text, reply_markup=admin_root_kb())


# ─────────────────────── Список користувачів ───────────────────────

@router.callback_query(F.data == "admin:users")
async def cb_users(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    users = load_all_users()
    await call.answer()
    if not users:
        try:
            await call.message.edit_text("❌ Немає користувачів.", reply_markup=admin_root_kb())
        except Exception:
            await call.message.answer("❌ Немає користувачів.", reply_markup=admin_root_kb())
        return

    text = f"👥  <b>Користувачі</b>  ({len(users)})\n{HR}"
    markup = admin_user_list_kb(users, page=0, page_size=_PAGE_SIZE)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admu:page:"))
async def cb_users_page(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    try:
        page = int(call.data.split(":")[-1])
    except ValueError:
        await call.answer()
        return
    users = load_all_users()
    await call.answer()
    try:
        await call.message.edit_reply_markup(
            reply_markup=admin_user_list_kb(users, page=page, page_size=_PAGE_SIZE)
        )
    except Exception:
        pass


# ─────────────────────── Деталі користувача ───────────────────────

def _user_detail_text(uid: int, data: dict) -> str:
    uname = data.get("user_name") or "—"
    active = bool(data.get("status"))
    access = access_status_line(data.get("access_until"))
    api_id = data.get("api_id") or "—"
    api_hash = str(data.get("api_hash") or "")
    api_hash_disp = f"{api_hash[:6]}…" if len(api_hash) > 6 else api_hash or "—"

    sess_file = os.path.join(SESSIONS_DIR, f"{uname}.session")
    sess = "є ✅" if os.path.isfile(sess_file) else "немає ❌"

    targets = get_targets(data)
    n_chats = len(targets)

    status_line = "🟢 <b>Активний</b>" if active else "🔴 <b>Неактивний</b>"
    return (
        f"👤  <b>@{h(uname)}</b>  (<code>{uid}</code>)\n{HR}\n\n"
        f"Статус:    {status_line}\n"
        f"Доступ:    <b>{access}</b>\n"
        f"Сесія:     {sess}\n"
        f"api_id:    <code>{api_id}</code>\n"
        f"api_hash:  <code>{api_hash_disp}</code>\n"
        f"Чатів:     <b>{n_chats}</b>"
    )


@router.callback_query(F.data.startswith("admu:view:"))
async def cb_user_view(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    try:
        uid = int(call.data.split(":")[-1])
    except ValueError:
        await call.answer()
        return

    users = load_all_users()
    data = users.get(uid)
    if not data:
        await call.answer("Користувача не знайдено.", show_alert=True)
        return

    await call.answer()
    text = _user_detail_text(uid, data)
    markup = admin_user_detail_kb(uid, bool(data.get("status")))
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)


# ─────────────────────── Вмикання / вимикання ───────────────────────

@router.callback_query(F.data.startswith("admu:enable:"))
@router.callback_query(F.data.startswith("admu:disable:"))
async def cb_user_toggle(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    parts = call.data.split(":")
    action, uid_str = parts[1], parts[2]
    try:
        uid = int(uid_str)
    except ValueError:
        await call.answer()
        return

    new_status = action == "enable"
    toggle_user_status(uid, new_status)
    label = "увімкнено ✅" if new_status else "вимкнено ❌"
    await call.answer(f"Користувача {label}", show_alert=False)

    # Оновлюємо картку
    users = load_all_users()
    data = users.get(uid, {})
    try:
        await call.message.edit_text(
            _user_detail_text(uid, data),
            reply_markup=admin_user_detail_kb(uid, new_status),
        )
    except Exception:
        pass


# ─────────────────────── Швидке додавання днів ───────────────────────

@router.callback_query(F.data.regexp(r"^admu:add(30|90|365):\d+$"))
async def cb_user_add_days(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    parts = call.data.split(":")
    days = int(parts[1][3:])   # "add30" → 30
    uid = int(parts[2])

    until = extend_access_days(uid, days)
    if until:
        await call.answer(f"✅ Доступ до {until}", show_alert=False)
        try:
            await call.bot.send_message(uid, f"🗝 Ваш доступ продовжено до <b>{until}</b>!")
        except Exception:
            pass
    else:
        await call.answer("❌ Не знайдено", show_alert=True)
        return

    users = load_all_users()
    data = users.get(uid, {})
    try:
        await call.message.edit_text(
            _user_detail_text(uid, data),
            reply_markup=admin_user_detail_kb(uid, bool(data.get("status"))),
        )
    except Exception:
        pass


# ─────────────────────── Встановити точну дату ───────────────────────

@router.callback_query(F.data.startswith("admu:setdate:"))
async def cb_user_setdate(call: types.CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    uid = int(call.data.split(":")[-1])
    await state.set_state(AdminStates.waiting_access_date)
    await state.update_data(target_uid=uid)
    await call.answer()
    await call.message.answer(
        f"📅  Введіть дату доступу для <code>{uid}</code>\n"
        f"Формат: <b>РРРР-ММ-ДД</b>  (наприклад: <code>2027-01-01</code>)",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="↩️ Скасувати", callback_data=f"admu:view:{uid}")]
        ]),
    )


@router.message(AdminStates.waiting_access_date)
async def admin_set_date(msg: types.Message, state: FSMContext) -> None:
    if not _guard(msg.from_user.id):
        return
    fsm = await state.get_data()
    uid = fsm.get("target_uid")
    raw = (msg.text or "").strip()
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        await msg.answer("⚠️ Невірний формат. Введіть дату як <code>2027-01-01</code>")
        return

    if set_access_for_user_id(int(uid), raw):
        await msg.answer(f"✅ Доступ до <b>{raw}</b> встановлено для <code>{uid}</code>")
        try:
            await msg.bot.send_message(int(uid), f"🗝 Ваш доступ встановлено до <b>{raw}</b>!")
        except Exception:
            pass
    else:
        await msg.answer("❌ Користувача не знайдено.")
    await state.clear()


# ─────────────────────── Забрати доступ ───────────────────────

@router.callback_query(F.data.startswith("admu:revoke:"))
async def cb_user_revoke(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    uid = int(call.data.split(":")[-1])
    if revoke_access(uid):
        await call.answer("🚫 Доступ забрано", show_alert=False)
        try:
            await call.bot.send_message(uid, "🚫 Ваш доступ скасовано адміністратором.")
        except Exception:
            pass
    else:
        await call.answer("❌ Не знайдено", show_alert=True)
        return

    users = load_all_users()
    data = users.get(uid, {})
    try:
        await call.message.edit_text(
            _user_detail_text(uid, data),
            reply_markup=admin_user_detail_kb(uid, bool(data.get("status"))),
        )
    except Exception:
        pass


# ─────────────────────── Видалити профіль ───────────────────────

@router.callback_query(F.data.startswith("admu:delete:"))
async def cb_user_delete(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    uid = int(call.data.split(":")[-1])

    # Підтвердження через окрему кнопку
    await call.answer()
    await call.message.answer(
        f"⚠️  <b>Видалити профіль <code>{uid}</code>?</b>\n\n"
        f"Буде видалено JSON-профіль і Telethon-сесію. Дію не можна скасувати.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(text="🗑 Так, видалити", callback_data=f"admu:delconfirm:{uid}"),
                types.InlineKeyboardButton(text="↩️ Ні", callback_data=f"admu:view:{uid}"),
            ]
        ]),
    )


@router.callback_query(F.data.startswith("admu:delconfirm:"))
async def cb_user_delete_confirm(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    uid = int(call.data.split(":")[-1])
    ok = delete_user_profile(uid)
    await call.answer("🗑 Видалено" if ok else "❌ Не знайдено", show_alert=False)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    users = load_all_users()
    await call.message.answer(
        f"{'✅ Профіль видалено.' if ok else '❌ Профіль не знайдено.'}\n"
        f"Залишилось профілів: <b>{len(users)}</b>",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="↩️ До списку", callback_data="admin:users")]
        ]),
    )


# ─────────────────────── Написати конкретному юзеру ───────────────────────

@router.callback_query(F.data.startswith("admu:msg:"))
async def cb_user_msg(call: types.CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    uid = int(call.data.split(":")[-1])
    await state.set_state(AdminStates.waiting_user_message)
    await state.update_data(target_uid=uid)
    await call.answer()
    await call.message.answer(
        f"📨  Надішліть повідомлення для <code>{uid}</code>:\n"
        f"<i>(текст, фото, відео — буде переслано без змін)</i>",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="↩️ Скасувати", callback_data=f"admu:view:{uid}")]
        ]),
    )


@router.message(AdminStates.waiting_user_message)
async def admin_send_user_msg(msg: types.Message, state: FSMContext) -> None:
    if not _guard(msg.from_user.id):
        return
    fsm = await state.get_data()
    uid = int(fsm.get("target_uid", 0))
    try:
        await msg.copy_to(uid)
        await msg.answer(f"✅ Повідомлення надіслано <code>{uid}</code>")
    except Exception as e:
        await msg.answer(f"❌ Не вдалося: {h(str(e))}")
    await state.clear()


# ─────────────────────── Розсилка адміна (всім юзерам) ───────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast_start(call: types.CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_broadcast_text)
    await call.answer()
    await call.message.answer(
        "📨  Надішліть повідомлення для розсилки <b>всім користувачам</b>:\n"
        "<i>(текст, фото, відео, кружечок)</i>",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="↩️ Скасувати", callback_data="admin:back")]
        ]),
    )


@router.message(AdminStates.waiting_broadcast_text)
async def admin_do_broadcast(msg: types.Message, state: FSMContext) -> None:
    if not _guard(msg.from_user.id):
        return
    users = load_all_users()
    ok = fail = 0
    for uid in users.keys():
        try:
            await msg.copy_to(uid)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await state.clear()
    await msg.answer(
        f"✅ Розіслано: <b>{ok}</b>\n❌ Помилок: <b>{fail}</b>",
        reply_markup=admin_root_kb(),
    )


# ─────────────────────── Управління адмінами ───────────────────────

@router.callback_query(F.data == "admin:admins")
async def cb_admins(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    admins = load_admins()
    await call.answer()
    text = f"👮  <b>Адміністратори</b>  ({len(admins)})\n{HR}\n\nНатисніть 🗑 щоб видалити адміна."
    try:
        await call.message.edit_text(text, reply_markup=admin_admins_kb(admins))
    except Exception:
        await call.message.answer(text, reply_markup=admin_admins_kb(admins))


@router.callback_query(F.data.startswith("adma:del:"))
async def cb_admin_del(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    try:
        uid = int(call.data.split(":")[-1])
    except ValueError:
        await call.answer()
        return
    if uid == call.from_user.id:
        await call.answer("❌ Не можна видалити себе.", show_alert=True)
        return
    admins = load_admins()
    if uid in admins:
        del admins[uid]
        save_admins(admins)
        await call.answer(f"🗑 Адміна {uid} видалено")
    else:
        await call.answer("Не знайдено.", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=admin_admins_kb(admins))
    except Exception:
        pass


@router.callback_query(F.data == "adma:add")
async def cb_admin_add(call: types.CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_new_admin_id)
    await call.answer()
    await call.message.answer(
        "👮  Введіть <b>Telegram ID</b> нового адміна (числовий):",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="↩️ Скасувати", callback_data="admin:admins")]
        ]),
    )


@router.message(AdminStates.waiting_new_admin_id)
async def admin_add_id(msg: types.Message, state: FSMContext) -> None:
    if not _guard(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    if not raw.isdigit():
        await msg.answer("⚠️ Введіть числовий ID (наприклад: <code>123456789</code>)")
        return
    new_id = int(raw)
    admins = load_admins()
    admins[new_id] = "—"
    save_admins(admins)
    await state.clear()
    await msg.answer(
        f"✅ Адміна <code>{new_id}</code> додано.",
        reply_markup=admin_admins_kb(admins),
    )


# ─────────────────────── Надати / забрати доступ (старий flow через список) ───────────────────────

@router.callback_query(F.data == "admin:access")
async def cb_grant_list(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    # Редіректимо до нового списку юзерів
    await cb_users(call)


@router.callback_query(F.data == "admin:revoke")
async def cb_revoke_list(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    await cb_users(call)


# Старий flow grant:/revoke: (з клавіатури надання доступу) — залишаємо для сумісності
@router.callback_query(F.data.startswith("grant:"))
async def cb_grant_pick(call: types.CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    uid = int(call.data.split(":")[1])
    # Редіректимо до нової картки юзера
    call.data = f"admu:view:{uid}"
    await cb_user_view(call)


@router.callback_query(F.data.startswith("revoke:"))
async def cb_revoke_pick(call: types.CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    uid = int(call.data.split(":")[1])
    call.data = f"admu:revoke:{uid}"
    await cb_user_revoke(call)


@router.message(AdminStates.waiting_access_days)
async def admin_set_days(msg: types.Message, state: FSMContext) -> None:
    if not _guard(msg.from_user.id):
        return
    fsm = await state.get_data()
    uid = fsm.get("target_uid")
    try:
        days = int((msg.text or "").strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("⚠️ Введіть кількість днів (наприклад: 30)")
        return
    until = grant_access_days(int(uid), days)
    if until:
        await msg.answer(f"✅ Доступ до <b>{until}</b> для <code>{uid}</code>")
        try:
            await msg.bot.send_message(int(uid), f"🗝 Вам надано доступ до {until}!")
        except Exception:
            pass
    else:
        await msg.answer("❌ Не знайдено.")
    await state.clear()


# ─────────────────────── Тестова тривога / відбій ───────────────────────

async def _run_test(call: types.CallbackQuery, mode: str) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Немає прав.", show_alert=True)
        return
    label = "🚨 тривога" if mode == "alert" else "✅ відбій"
    await call.answer(f"Запускаю тест ({label})…")
    await call.message.answer(
        f"🧪  <b>Тест: {label}</b>\n<i>Запускаю розсилку…</i>"
    )
    try:
        ok, total = await broadcast_for_all_users(mode)
    except Exception as e:
        await call.message.answer(f"❌  <b>Помилка:</b>\n<code>{h(str(e))}</code>")
        return
    await call.message.answer(
        f"✅  <b>Тест завершено</b>\nНадіслано: <b>{ok}</b> з <b>{total}</b>.",
        reply_markup=admin_root_kb(),
    )


@router.callback_query(F.data == "admin:test_alert")
async def cb_test_alert(call: types.CallbackQuery) -> None:
    await _run_test(call, "alert")


@router.callback_query(F.data == "admin:test_clear")
async def cb_test_clear(call: types.CallbackQuery) -> None:
    await _run_test(call, "clear")


# ─────────────────────── Команди ───────────────────────

@router.message(Command("access"))
async def cmd_access(msg: types.Message) -> None:
    if not _guard(msg.from_user.id):
        await msg.answer("❌ Немає прав.")
        return
    parts = (msg.text or "").strip().split()
    if len(parts) != 3:
        await msg.answer("⚙️ Формат: /access &lt;user_id&gt; &lt;YYYY-MM-DD&gt;")
        return
    try:
        uid = int(parts[1])
        date_obj = datetime.strptime(parts[2], "%Y-%m-%d")
    except Exception:
        await msg.answer("⚠️ Приклад: /access 123456789 2025-12-31")
        return
    if set_access_for_user_id(uid, date_obj.strftime("%Y-%m-%d")):
        await msg.answer(f"✅ <code>{uid}</code> — доступ до <b>{date_obj:%Y-%m-%d}</b>")
    else:
        await msg.answer("❌ Користувача не знайдено.")


@router.message(Command("admin_add"))
async def cmd_admin_add(msg: types.Message) -> None:
    admins = load_admins()
    if msg.from_user.id not in admins:
        await msg.answer("❌ Немає прав.")
        return
    parts = (msg.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("⚙️ Використай: <code>/admin_add 123456789</code>")
        return
    new_id = int(parts[1])
    admins[new_id] = "—"
    save_admins(admins)
    await msg.answer(f"✅ Додано адміна <code>{new_id}</code>")


@router.message(Command("admin_del"))
async def cmd_admin_del(msg: types.Message) -> None:
    admins = load_admins()
    if msg.from_user.id not in admins:
        await msg.answer("❌ Немає прав.")
        return
    parts = (msg.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("⚙️ Використай: <code>/admin_del 123456789</code>")
        return
    target = int(parts[1])
    if target in admins:
        del admins[target]
        save_admins(admins)
        await msg.answer(f"🗑 Видалено адміна <code>{target}</code>")
    else:
        await msg.answer("⚠️ Такого адміна немає.")


@router.message(Command("admin_list"))
async def cmd_admin_list(msg: types.Message) -> None:
    admins = load_admins()
    if msg.from_user.id not in admins:
        await msg.answer("❌ Немає доступу.")
        return
    if not admins:
        await msg.answer("👮 Адмінів немає.")
        return
    lines = [f"• @{uname or '—'} (<code>{uid}</code>)" for uid, uname in admins.items()]
    await msg.answer("👮 <b>Адміни</b>:\n" + "\n".join(lines))


@router.message(Command("test_alert"))
async def cmd_test_alert(msg: types.Message) -> None:
    if not _guard(msg.from_user.id):
        await msg.answer("❌ Немає прав.")
        return
    await msg.answer("🧪  <b>Тест тривоги…</b>")
    ok, total = await broadcast_for_all_users("alert")
    await msg.answer(f"✅  Надіслано: <b>{ok}/{total}</b>", reply_markup=admin_root_kb())


@router.message(Command("test_clear"))
async def cmd_test_clear(msg: types.Message) -> None:
    if not _guard(msg.from_user.id):
        await msg.answer("❌ Немає прав.")
        return
    await msg.answer("🧪  <b>Тест відбою…</b>")
    ok, total = await broadcast_for_all_users("clear")
    await msg.answer(f"✅  Надіслано: <b>{ok}/{total}</b>", reply_markup=admin_root_kb())

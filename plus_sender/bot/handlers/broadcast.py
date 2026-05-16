"""Налаштування розсилки: вибір чатів, per-chat конфіг (текст або пересилання)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from telethon import TelegramClient
from telethon import utils as tl_utils

from ...config import (
    BTN_BROADCAST,
    BTN_CANCEL,
    BTN_DISABLE_TEXT,
    CANCEL_TEXTS,
    BULLET,
    EMO,
    HR,
    MEDIA_DIR,
)
from ...storage import (
    clear_target_media,
    delay_for_target,
    get_schedule,
    get_target_forward_mode,
    get_target_forward_source,
    get_target_messages,
    get_target_type,
    get_targets,
    get_targets_meta,
    load_user,
    reset_target_config,
    save_user,
    session_path,
    set_schedule,
    set_target_forward_mode,
    set_target_forward_source,
    set_target_messages,
    set_target_type,
    sync_targets,
)
from ...utils import (
    default_delay_seconds,
    default_message_text,
    example_block,
    h,
    next_hint,
    parse_text_input,
    preview_message,
    section,
    soft_error,
    tip,
    truncate,
)
from ..keyboards import (
    broadcast_settings_kb,
    cancel_kb,
    forward_mode_kb,
    main_menu_kb,
    schedule_kb,
    source_chat_select_kb,
    target_chat_kb,
    target_list_kb,
    target_mode_type_kb,
    text_input_kb,
)
from ..states import BroadcastStates

# ─────────────────────── Хелпер: показати список діалогів для вибору джерела ───────────────────────

async def _show_src_dialog_list(
    msg_or_call,
    state: FSMContext,
    user: types.User,
    pid: int,
    mode: str,
    query: Optional[str] = None,
) -> None:
    """Завантажує Telethon-діалоги і показує список для вибору чату-джерела."""
    # Визначаємо метод надсилання: завжди надсилаємо ПОВІДОМЛЕННЯ в чат,
    # а не popup (CallbackQuery.answer — це лише toast-сповіщення).
    if isinstance(msg_or_call, types.CallbackQuery):
        send = msg_or_call.message.answer
    else:
        send = msg_or_call.answer

    items, err = await _fetch_dialogs(user, query=query)
    if err:
        await send(
            f"❌ Не вдалося завантажити діалоги:\n<code>{h(str(err))}</code>\n\n"
            f"Переконайтесь що сесія авторизована (виконайте «🔌 Підключення»).",
            reply_markup=cancel_kb(),
        )
        return

    if not items:
        await send(
            "⚠️ Нічого не знайдено. Спробуйте інший запит або введіть числовий ID чату.",
            reply_markup=cancel_kb(),
        )
        return

    mapping: dict[str, int] = {str(i): int(it["pid"]) for i, it in enumerate(items[:20])}
    await state.update_data(src_dialog_map=mapping, src_dialog_items=items[:20])

    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"
    if query:
        header = (
            f"🎥  <b>Джерело кружків для {mode_label}</b>\n{HR}\n\n"
            f"Результати пошуку «{h(query)}»:\n"
        )
    else:
        header = (
            f"🎥  <b>Джерело кружків для {mode_label}</b>\n{HR}\n\n"
            f"Оберіть чат зі списку:\n"
        )

    markup = source_chat_select_kb(items[:20], mapping)
    await send(header, reply_markup=markup)

log = logging.getLogger(__name__)
router = Router(name="broadcast")


# ─────────────────────── Медіа-хелпери ───────────────────────

_MEDIA_LABELS: dict[str, str] = {
    "video_note": "🎥 кружечок",
    "voice":      "🎙 голосове",
    "photo":      "🖼 фото",
    "video":      "📹 відео",
    "animation":  "🎞 gif",
}


def _extract_media(msg: types.Message) -> Optional[tuple[str, str, Optional[str]]]:
    if msg.video_note:
        return "video_note", msg.video_note.file_id, None
    if msg.voice:
        return "voice", msg.voice.file_id, None
    if msg.photo:
        return "photo", msg.photo[-1].file_id, msg.caption
    if msg.video:
        return "video", msg.video.file_id, msg.caption
    if msg.animation:
        return "animation", msg.animation.file_id, msg.caption
    return None


async def _download_media(bot, file_id: str, username: str, scope: str, kind: str) -> str:
    ext_map = {"video_note": "mp4", "voice": "ogg", "photo": "jpg",
               "video": "mp4", "animation": "mp4"}
    ext = ext_map.get(kind, "bin")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = str(MEDIA_DIR / f"{username}_{scope}.{ext}")
    await bot.download(file_id, destination=path)
    return path


def _media_display(media: Optional[dict]) -> Optional[str]:
    if not media:
        return None
    path = str(media.get("path") or "")
    if not path or not Path(path).is_file():
        return None
    kind = media.get("kind", "")
    label = _MEDIA_LABELS.get(kind, f"📎 {kind}")
    caption = media.get("caption")
    if caption:
        label += f" · «{preview_message(caption, 28)}»"
    return label


# ─────────────────────── Telethon: діалоги ───────────────────────

async def _telethon_client(user: types.User) -> tuple[Optional[TelegramClient], Optional[str]]:
    data = load_user(user)
    if not (data.get("api_id") and data.get("api_hash")):
        return None, f"{EMO['err']} Спочатку виконайте «🔌 Підключення»."
    client = TelegramClient(session_path(user), int(data["api_id"]), str(data["api_hash"]))
    return client, None


async def _fetch_dialogs(
    user: types.User, query: Optional[str] = None
) -> tuple[list[dict], Optional[str]]:
    client, err = await _telethon_client(user)
    if err:
        return [], err
    items: list[dict] = []
    try:
        async with client:
            if not await client.is_user_authorized():
                return [], f"{EMO['err']} Сесія не авторизована. Виконайте «🔌 Підключення»."
            if query is None:
                async for d in client.iter_dialogs(limit=30):
                    ent = d.entity
                    items.append({
                        "title": d.name or "—",
                        "pid": tl_utils.get_peer_id(ent),
                        "username": getattr(ent, "username", None),
                        "kind": ent.__class__.__name__,
                    })
            else:
                q = query.strip().lower()
                as_id: Optional[int] = None
                if q.lstrip("-").isdigit():
                    try:
                        as_id = int(q)
                    except ValueError:
                        pass
                async for d in client.iter_dialogs(limit=None):
                    ent = d.entity
                    title = (d.name or "").lower()
                    uname = (getattr(ent, "username", "") or "").lower()
                    pid = tl_utils.get_peer_id(ent)
                    if q in title or (uname and q in uname) or (as_id is not None and pid == as_id):
                        items.append({
                            "title": d.name or "—",
                            "pid": pid,
                            "username": getattr(ent, "username", None),
                            "kind": ent.__class__.__name__,
                        })
                        if len(items) >= 60:
                            break
        return items, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


# ─────────────────────── Підрахунок кружків ───────────────────────

from telethon.tl.types import DocumentAttributeVideo as _DAV


async def _count_fwd_video_notes(
    user: types.User, data: dict
) -> dict[int, int]:
    """Рахує відео-кружки (до 200 повідомлень) в кожному унікальному чаті-джерелі.
    Повертає {chat_id: count}; -1 якщо доступ до чату закритий."""
    targets = get_targets(data)
    source_ids: set[int] = set()
    for pid in targets:
        for mode in ("alert", "clear"):
            if get_target_type(data, pid, mode) == "forward":
                src = get_target_forward_source(data, pid, mode)
                if src:
                    source_ids.add(int(src["chat_id"]))

    if not source_ids:
        return {}

    client, err = await _telethon_client(user)
    if err:
        return {}

    counts: dict[int, int] = {}
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {}

        for chat_id in source_ids:
            try:
                try:
                    real_id, peer_cls = tl_utils.resolve_id(chat_id)
                    entity = await client.get_input_entity(peer_cls(real_id))
                except (ValueError, KeyError):
                    entity = await client.get_entity(chat_id)

                cnt = 0
                async for msg in client.iter_messages(entity, limit=200):
                    doc = getattr(getattr(msg, "media", None), "document", None)
                    if doc:
                        for attr in getattr(doc, "attributes", []):
                            if isinstance(attr, _DAV) and getattr(attr, "round_message", False):
                                cnt += 1
                                break
                counts[chat_id] = cnt
            except Exception as exc:
                log.debug("bset count circles: chat_id=%d err=%s", chat_id, exc)
                counts[chat_id] = -1
    except Exception as exc:
        log.debug("bset count circles: telethon err=%s", exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return counts


# ─────────────────────── Допоміжне ───────────────────────

def _target_title(data: dict, pid: int) -> str:
    meta = get_targets_meta(data).get(pid, {}) or {}
    title = str(meta.get("title") or f"chat_{pid}")
    username = str(meta.get("username") or "").strip()
    return f"{title} @{username}" if username else title


def _items_to_pretty(items: list[dict], selected: list[int]) -> str:
    selected_set = set(selected)
    lines: list[str] = []
    for it in items[:30]:
        pid = int(it["pid"])
        title = h(str(it.get("title") or "—"))
        uname = h(str(it.get("username") or ""))
        mark = "✅" if pid in selected_set else "⬜"
        upart = f"  @{uname}" if uname else ""
        lines.append(f"  {mark}  <b>{title}</b>{upart}")
    return "\n".join(lines)


def _type_label(type_str: Optional[str]) -> str:
    return {"text": "✍️ текст", "forward": "🎥 кружок з чату", "none": "🚫 вимкнено"}.get(
        type_str or "", "↩️ дефолт"
    )


def _type_label_short(type_str: Optional[str]) -> str:
    """Без провідного emoji — для summary де ліворуч вже є 🚨/🟢."""
    return {"text": "текст", "forward": "🎥 кружок", "none": "вимкнено"}.get(
        type_str or "", "дефолт"
    )


def _settings_summary(data: dict, fwd_counts: dict[int, int] | None = None) -> str:
    fwd_counts = fwd_counts or {}
    targets = get_targets(data)
    n = len(targets)
    if not targets:
        chats_body = "<i>не обрано — натисніть «🔍 Пошук чату» або оберіть з останніх</i>"
    else:
        lines: list[str] = []
        for pid in targets[:12]:
            item = get_targets_meta(data).get(pid, {}) or {}
            title = h(str(item.get("title") or "—"))
            uname = item.get("username")
            upart = f" @{h(uname)}" if uname else ""
            a_type = get_target_type(data, pid, "alert")
            c_type = get_target_type(data, pid, "clear")
            a_src = get_target_forward_source(data, pid, "alert")
            c_src = get_target_forward_source(data, pid, "clear")
            a_delay = delay_for_target(data, pid, "alert")
            c_delay = delay_for_target(data, pid, "clear")

            def _lbl(t, src):
                lbl = _type_label_short(t)
                if t == "forward" and src:
                    lbl += f" «{truncate(src['title'], 14)}»"
                    cid = int(src["chat_id"])
                    if cid in fwd_counts:
                        cnt = fwd_counts[cid]
                        lbl += f"  · <b>{cnt} 🎥</b>" if cnt >= 0 else "  · <i>?</i>"
                return lbl

            lines.append(
                f"<b>{title}</b>{upart}\n"
                f"  ├ 🚨 {_lbl(a_type, a_src)}  ·  {a_delay}с\n"
                f"  └ 🟢 {_lbl(c_type, c_src)}  ·  {c_delay}с"
            )
        if n > 12:
            lines.append(f"<i>…ще {n - 12} чатів</i>")
        chats_body = "\n\n".join(lines)

    count_badge = f" ({n})" if n else ""
    return "\n\n".join([
        f"⚙️  <b>Налаштування розсилки</b>\n{HR}",
        section(f"Обрані чати{count_badge}", chats_body),
    ])


async def _send_long(msg: types.Message, text: str, chunk: int = 3800) -> None:
    for i in range(0, len(text), chunk):
        await msg.answer(text[i: i + chunk])


def _build_chat_kb(data: dict, pid: int) -> types.InlineKeyboardMarkup:
    """Збирає клавіатуру налаштувань конкретного чату."""
    from ...storage import get_target_media

    a_type = get_target_type(data, pid, "alert")
    c_type = get_target_type(data, pid, "clear")
    a_src = get_target_forward_source(data, pid, "alert")
    c_src = get_target_forward_source(data, pid, "clear")
    a_delay = delay_for_target(data, pid, "alert")
    c_delay = delay_for_target(data, pid, "clear")

    # Підказки
    a_hint: Optional[str] = None
    c_hint: Optional[str] = None
    if a_type == "forward" and a_src:
        a_hint = a_src["title"]
    elif a_type == "text":
        tms = get_target_messages(data).get(pid) or {}
        raw = tms.get("alert")
        if raw:
            a_hint = f"«{preview_message(str(raw), 18)}»"

    if c_type == "forward" and c_src:
        c_hint = c_src["title"]
    elif c_type == "text":
        tms = get_target_messages(data).get(pid) or {}
        raw = tms.get("clear")
        if raw:
            c_hint = f"«{preview_message(str(raw), 18)}»"

    return target_chat_kb(
        alert_type=a_type,
        alert_hint=a_hint,
        alert_delay=a_delay,
        clear_type=c_type,
        clear_hint=c_hint,
        clear_delay=c_delay,
    )


def _build_target_list_kb(
    data: dict, mapping: dict[str, int]
) -> types.InlineKeyboardMarkup:
    """Перебудовує клавіатуру списку чатів."""
    targets = get_targets(data)
    titles = {pid: _target_title(data, pid) for pid in targets}
    configs = {
        pid: {
            "alert_type": get_target_type(data, pid, "alert"),
            "clear_type": get_target_type(data, pid, "clear"),
        }
        for pid in targets
    }
    kb, _ = target_list_kb(targets, titles, configs)
    return kb


# ─────────────────────── Точка входу ───────────────────────

@router.message(F.text == BTN_BROADCAST)
async def open_broadcast_settings(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    await show_broadcast_settings(msg, state=state)


async def show_broadcast_settings(
    msg: types.Message, query: Optional[str] = None, state: Optional[FSMContext] = None
) -> None:
    user = msg.from_user
    items, err = await _fetch_dialogs(user, query=query)
    if err:
        await msg.answer(err, reply_markup=main_menu_kb(user))
        return

    data = load_user(user)
    selected = get_targets(data)

    if query is None:
        header = (
            "💬  <b>Ваші останні чати</b>  <i>(до 30)</i>\n"
            f"{HR}\n"
            "<i>Це ваші чати з Telegram. Тицьніть кнопку щоб додати чат у розсилку — "
            "поруч з'явиться галочка ✅.</i>"
        )
    else:
        if not items:
            await msg.answer(
                f"{EMO['info']}  <b>Нічого не знайдено</b> для <code>{h(query)}</code>.\n"
                f"<i>Спробуйте іншу назву або введіть числовий ID чату.</i>",
                reply_markup=main_menu_kb(user),
            )
            return
        header = (
            f"🔍  <b>Результати пошуку</b> «{h(query)}»  <i>(до 60)</i>\n"
            f"{HR}"
        )

    pretty = _items_to_pretty(items, selected)
    await _send_long(msg, f"{header}\n\n{pretty}")

    if state is not None:
        await state.update_data(
            pending_targets={
                str(int(it["pid"])): {
                    "title": it.get("title"),
                    "username": it.get("username"),
                    "kind": it.get("kind"),
                }
                for it in items
            }
        )

    hint = (
        "<i>👇 Тицяйте по чатах щоб обрати їх для розсилки. "
        "Максимум — 4 чати.</i>\n"
        "<i>Далі — «⚙️ Налаштування кожного чату»: задати, що саме надсилати в кожен.</i>"
    )

    await msg.answer(
        f"{_settings_summary(data)}\n\n{hint}",
        reply_markup=broadcast_settings_kb(items, selected),
    )


# ─────────────────────── Toggle чату ───────────────────────

@router.callback_query(F.data.startswith("bset:toggle:"))
async def cb_toggle(call: types.CallbackQuery, state: FSMContext) -> None:
    try:
        pid = int(call.data.split(":")[-1])
    except (ValueError, IndexError):
        await call.answer("Невірний ID.", show_alert=True)
        return

    user = call.from_user
    data = load_user(user)
    targets = get_targets(data)
    meta = get_targets_meta(data)

    fsm_data = await state.get_data()
    pending = fsm_data.get("pending_targets") or {}

    MAX_TARGETS = 4

    if pid in targets:
        targets = [x for x in targets if x != pid]
        meta.pop(pid, None)
        status = "Прибрано"
    else:
        if len(targets) >= MAX_TARGETS:
            await call.answer(f"Максимум {MAX_TARGETS} чати", show_alert=True)
            return
        targets.append(pid)
        m = pending.get(str(pid))
        if m:
            meta[pid] = m
        # Не наслідуємо глобальний дефолт — ставимо явно "не надсилати"
        # для обох режимів. Користувач сам обере, що саме надсилати,
        # через «⚙️ Налаштування кожного чату».
        set_target_type(data, pid, "alert", "none")
        set_target_type(data, pid, "clear", "none")
        status = "Додано — налаштуйте у ⚙️"

    sync_targets(data, targets, meta)
    save_user(user, data)
    await call.answer(status)

    items = [{"pid": int(p), **(v or {})} for p, v in pending.items()]
    if items:
        try:
            await call.message.edit_reply_markup(
                reply_markup=broadcast_settings_kb(items, get_targets(data))
            )
        except Exception:
            pass


# ─────────────────────── Пошук ───────────────────────

@router.callback_query(F.data == "bset:search")
async def cb_search(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_search_query)
    await call.answer()
    await call.message.answer(
        f"{EMO['magn']}  <b>Пошук чату</b>\n{HR}\n\n"
        f"Не бачите потрібний чат у списку? Введіть його назву, @username або числовий ID.\n\n"
        f"{example_block('Робочий чат', '@username_chat', '-1001234567890')}\n\n"
        f"{tip('пошук — нечутливий до регістру, шукає по всьому списку ваших діалогів.')}",
        reply_markup=cancel_kb(),
    )


@router.message(BroadcastStates.waiting_search_query)
async def search_query(msg: types.Message, state: FSMContext) -> None:
    query = (msg.text or "").strip()
    if not query or query in CANCEL_TEXTS:
        await state.clear()
        await msg.answer("Скасовано.", reply_markup=main_menu_kb(msg.from_user))
        return
    await state.clear()
    await show_broadcast_settings(msg, query=query, state=state)


# ─────────────────────── Очистити / показати ───────────────────────

@router.callback_query(F.data == "bset:clear")
async def cb_clear(call: types.CallbackQuery, state: FSMContext) -> None:
    user = call.from_user
    data = load_user(user)
    sync_targets(data, [], {})
    save_user(user, data)
    await call.answer("Список чатів очищено")
    fsm_data = await state.get_data()
    pending = fsm_data.get("pending_targets") or {}
    items = [{"pid": int(p), **(v or {})} for p, v in pending.items()]
    if items:
        try:
            await call.message.edit_reply_markup(reply_markup=broadcast_settings_kb(items, []))
        except Exception:
            pass


@router.callback_query(F.data == "bset:show")
async def cb_show(call: types.CallbackQuery) -> None:
    data = load_user(call.from_user)
    await call.answer()
    fwd_counts = await _count_fwd_video_notes(call.from_user, data)
    await _send_long(call.message, _settings_summary(data, fwd_counts))


@router.callback_query(F.data == "bset:done")
async def cb_done(call: types.CallbackQuery) -> None:
    data = load_user(call.from_user)
    n = len(get_targets(data))
    await call.answer("Збережено ✅")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    fwd_counts = await _count_fwd_video_notes(call.from_user, data)
    badge = f"{n} чат" + ("и" if 2 <= n <= 4 else "ів" if n != 1 else "")
    await _send_long(
        call.message,
        f"✅  <b>Налаштування збережено</b>\n"
        f"<i>Обрано {badge} для авторозсилки.</i>\n\n"
        f"{_settings_summary(data, fwd_counts)}",
    )
    await call.message.answer(
        "⬆️ Перевірте налаштування вище",
        reply_markup=main_menu_kb(call.from_user),
    )


# ─────────────────────── Список чатів для налаштувань ───────────────────────

@router.callback_query(F.data == "bset:chatsettings")
async def cb_chat_settings(call: types.CallbackQuery, state: FSMContext) -> None:
    user = call.from_user
    data = load_user(user)
    targets = get_targets(data)
    await call.answer()

    if not targets:
        await call.message.answer(
            "⚠️ Спочатку оберіть хоча б один чат у списку вище.",
            reply_markup=main_menu_kb(user),
        )
        return

    titles = {pid: _target_title(data, pid) for pid in targets}
    configs = {
        pid: {
            "alert_type": get_target_type(data, pid, "alert"),
            "clear_type": get_target_type(data, pid, "clear"),
        }
        for pid in targets
    }

    markup, mapping = target_list_kb(targets, titles, configs)
    await state.update_data(text_targets_map=mapping)

    await call.message.answer(
        f"⚙️  <b>Налаштування кожного чату</b>\n{HR}\n\n"
        f"Тут ви для кожного чату задаєте, що саме надсилати "
        f"при <b>🚨 тривозі</b> та при <b>✅ відбої</b>.\n\n"
        f"<b>Іконки поруч з чатом</b> — це підказка стану:\n"
        f"  ⚙️  — все налаштовано\n"
        f"  📝  — налаштовано тільки одне (тривога або відбій)\n"
        f"  ↩️  — поки що використовується глобальний дефолт\n\n"
        f"<i>👇 Натисніть будь-який чат, щоб увійти в його налаштування.</i>",
        reply_markup=markup,
    )


@router.callback_query(F.data == "bset:textdone")
async def cb_text_done(call: types.CallbackQuery, state: FSMContext) -> None:
    fsm_data = await state.get_data()
    fsm_data.pop("text_targets_map", None)
    fsm_data.pop("target_pid", None)
    await state.set_data(fsm_data)
    await call.answer("Готово")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ─────────────────────── Відкрити конфіг конкретного чату ───────────────────────

@router.callback_query(F.data.startswith("bset:textchat:"))
async def cb_textchat_start(call: types.CallbackQuery, state: FSMContext) -> None:
    payload = call.data[len("bset:textchat:"):]
    fsm_data = await state.get_data()
    mapping: dict[str, int] = fsm_data.get("text_targets_map") or {}
    pid_raw = mapping.get(payload)
    try:
        pid = int(pid_raw if pid_raw is not None else payload)
    except (TypeError, ValueError):
        await call.answer("Клавіатура застаріла. Відкрийте «⚙️ Налаштування кожного чату» ще раз.", show_alert=True)
        return

    data = load_user(call.from_user)
    if pid not in get_targets(data):
        await call.answer("Цей чат не вибрано.", show_alert=True)
        return

    await state.update_data(target_pid=pid)
    await call.answer()
    title = h(_target_title(data, pid))
    await call.message.answer(
        f"⚙️  <b>Налаштування для:</b>  {title}\n{HR}\n\n"
        f"Тут ви обираєте, що саме надсилатиметься в цей чат "
        f"при <b>🚨 тривозі</b> і при <b>✅ відбої</b>.\n\n"
        f"<b>Натисніть на «🚨 Тривога» або «✅ Відбій»</b> — і виберіть один із трьох варіантів:\n"
        f"  ✍️  <b>Текст</b> — звичайне повідомлення (можна додати фото / голос / відео)\n"
        f"  🎥  <b>Кружок з чату</b> — бот надішле відео-кружечок з іншого чату <i>без позначки «переслано»</i>\n"
        f"  🚫  <b>Не надсилати</b> — у цьому чаті ця подія пропускається\n\n"
        f"{tip('іконка ↩️ означає що використовується глобальний дефолт.')}",
        reply_markup=_build_chat_kb(data, pid),
    )


@router.callback_query(F.data == "bset:tc_back")
async def cb_tc_back(call: types.CallbackQuery, state: FSMContext) -> None:
    """Повернення від конфігу чату до списку."""
    user = call.from_user
    data = load_user(user)
    targets = get_targets(data)
    await call.answer()

    titles = {pid: _target_title(data, pid) for pid in targets}
    configs = {
        pid: {
            "alert_type": get_target_type(data, pid, "alert"),
            "clear_type": get_target_type(data, pid, "clear"),
        }
        for pid in targets
    }
    markup, mapping = target_list_kb(targets, titles, configs)
    await state.update_data(text_targets_map=mapping)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(
        f"⚙️  <b>Налаштування по чатах</b>\n{HR}\n\nОберіть чат:",
        reply_markup=markup,
    )


# ─────────────────────── Вибір режиму (alert / clear) ───────────────────────

async def _show_mode_menu(call: types.CallbackQuery, state: FSMContext, mode: str) -> None:
    fsm_data = await state.get_data()
    pid = fsm_data.get("target_pid")
    if pid is None:
        await call.answer("Сесія застаріла. Відкрийте «⚙️ Налаштування кожного чату» ще раз.", show_alert=True)
        return

    data = load_user(call.from_user)
    current_type = get_target_type(data, pid, mode)
    src = get_target_forward_source(data, pid, mode)
    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"

    await state.update_data(target_mode=mode)
    await call.answer()

    chat_title = h(_target_title(data, pid))
    info = ""
    if current_type == "forward" and src:
        info = f"\n\nПоточне джерело: <b>{h(src['title'])}</b>"
    elif current_type == "text":
        tms = get_target_messages(data).get(pid) or {}
        raw = tms.get(mode)
        if raw:
            info = f"\n\nПоточний текст: <code>{h(preview_message(str(raw), 80))}</code>"

    await call.message.answer(
        f"{'🚨' if mode == 'alert' else '✅'}  <b>{chat_title} — {mode_label}</b>\n{HR}"
        f"{info}\n\n"
        f"Оберіть що надсилати при {'тривозі' if mode == 'alert' else 'відбої'}:",
        reply_markup=target_mode_type_kb(mode, current_type, src["title"] if src else None),
    )


@router.callback_query(F.data == "bset:tc_a")
async def cb_tc_alert(call: types.CallbackQuery, state: FSMContext) -> None:
    await _show_mode_menu(call, state, "alert")


@router.callback_query(F.data == "bset:tc_c")
async def cb_tc_clear(call: types.CallbackQuery, state: FSMContext) -> None:
    await _show_mode_menu(call, state, "clear")


@router.callback_query(F.data == "bset:tc_modeback")
async def cb_tc_modeback(call: types.CallbackQuery, state: FSMContext) -> None:
    """Повернення від вибору типу до конфігу чату."""
    fsm_data = await state.get_data()
    pid = fsm_data.get("target_pid")
    if pid is None:
        await call.answer("Сесія застаріла.", show_alert=True)
        return
    data = load_user(call.from_user)
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(
        f"⚙️  <b>{h(_target_title(data, pid))}</b>\n{HR}",
        reply_markup=_build_chat_kb(data, pid),
    )


@router.callback_query(F.data == "bset:tc_reset")
async def cb_tc_reset(call: types.CallbackQuery, state: FSMContext) -> None:
    fsm_data = await state.get_data()
    pid = fsm_data.get("target_pid")
    if pid is None:
        await call.answer("Сесія застаріла.", show_alert=True)
        return
    data = load_user(call.from_user)
    reset_target_config(data, pid)
    save_user(call.from_user, data)
    await call.answer("🗑 Скинуто до дефолту")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(
        f"✅  Налаштування «{h(_target_title(data, pid))}» скинуто.\n"
        f"Буде використовуватись глобальний дефолт.",
        reply_markup=_build_chat_kb(data, pid),
    )


# ─────────────────────── Тип: 🚫 Не надсилати ───────────────────────

@router.callback_query(F.data.regexp(r"^bset:tc_type:(alert|clear):none$"))
async def cb_tc_type_none(call: types.CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    mode = parts[2]
    fsm_data = await state.get_data()
    pid = fsm_data.get("target_pid")
    if pid is None:
        await call.answer("Сесія застаріла.", show_alert=True)
        return

    data = load_user(call.from_user)
    set_target_type(data, pid, mode, "none")
    save_user(call.from_user, data)

    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"
    await call.answer(f"🚫 Вимкнено для {mode_label}")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await call.message.answer(
        f"🚫 Надсилання для {mode_label} вимкнено для цього чату.",
        reply_markup=_build_chat_kb(data, pid),
    )


# ─────────────────────── Тип: ✍️ Текст ───────────────────────

@router.callback_query(F.data.regexp(r"^bset:tc_type:(alert|clear):text$"))
async def cb_tc_type_text(call: types.CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    mode = parts[2]
    fsm_data = await state.get_data()
    pid = fsm_data.get("target_pid")
    if pid is None:
        await call.answer("Сесія застаріла.", show_alert=True)
        return

    data = load_user(call.from_user)
    tms = get_target_messages(data).get(pid) or {}
    current_text = tms.get(mode, "")
    cur_delay = delay_for_target(data, pid, mode)
    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"

    await state.update_data(target_mode=mode)
    await state.set_state(BroadcastStates.waiting_target_mode_text)
    await call.answer()
    await call.message.answer(
        f"{'🚨' if mode == 'alert' else '✅'}  <b>Що надсилати під час {mode_label}</b>\n{HR}\n\n"
        f"<b>Зараз:</b>  <code>{h(preview_message(str(current_text), 120))}</code>\n"
        f"<b>Затримка:</b>  ⏱ <b>{cur_delay} с</b>\n\n"
        f"<b>Надішліть мені:</b>\n"
        f"  ✍️  звичайний текст (можна з emoji)\n"
        f"  🖼  фото, відео, кружечок або голосове\n"
        f"  🚫  або кнопку «{h(BTN_DISABLE_TEXT)}» — щоб у цей чат нічого не йшло\n\n"
        f"{example_block('+', '✅ Відбій тривоги', 'УВАГА! Усі в укриття!')}",
        reply_markup=text_input_kb(),
    )


@router.message(BroadcastStates.waiting_target_mode_text)
async def target_mode_text_input(msg: types.Message, state: FSMContext) -> None:
    from ...utils import safe_username_from

    fsm_data = await state.get_data()
    pid_raw = fsm_data.get("target_pid")
    mode = fsm_data.get("target_mode")
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        await state.clear()
        await msg.answer("⚠️ Стан втрачено.", reply_markup=main_menu_kb(msg.from_user))
        return

    data = load_user(msg.from_user)
    cur_delay = delay_for_target(data, pid, mode)
    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"

    # ── Медіа ──
    media_info = _extract_media(msg)
    if media_info:
        kind, file_id, caption = media_info
        username = safe_username_from(msg.from_user)
        try:
            path = await _download_media(msg.bot, file_id, username, f"pid{pid}_{mode}", kind)
        except Exception as e:
            await msg.answer(f"❌ Не вдалося завантажити: {h(str(e))}")
            return
        await state.update_data(
            pending_text=None,
            pending_media={"kind": kind, "path": path, "caption": caption},
        )
        await state.set_state(BroadcastStates.waiting_target_text_delay)
        await msg.answer(
            f"✅  {_MEDIA_LABELS.get(kind, kind)} для {mode_label} отримано.\n\n"
            f"⏱  Затримка в секундах (поточна: <b>{cur_delay} с</b>):",
            reply_markup=cancel_kb(),
        )
        return

    # ── Текст ──
    if not msg.text:
        await msg.answer(f"⚠️ Надішліть текст, медіа або «{h(BTN_DISABLE_TEXT)}».")
        return

    if msg.text in CANCEL_TEXTS:
        await state.clear()
        await msg.answer("Скасовано.", reply_markup=_build_chat_kb(data, pid))
        return

    text = parse_text_input(msg.text)
    if text is None:
        await msg.answer(f"⚠️ Надішліть текст або «{h(BTN_DISABLE_TEXT)}».")
        return

    await state.update_data(pending_text=text, pending_media=None)
    await state.set_state(BroadcastStates.waiting_target_text_delay)

    if text == "":
        await msg.answer(
            f"🚫 Надсилання для {mode_label} вимкнено.\n\n⏱ Затримка (0):",
            reply_markup=cancel_kb(),
        )
    else:
        await msg.answer(
            f"✅  Текст прийнято:\n<code>{h(preview_message(text, 160))}</code>\n\n"
            f"⏱  Затримка в секундах (поточна: <b>{cur_delay} с</b>):",
            reply_markup=cancel_kb(),
        )


@router.message(BroadcastStates.waiting_target_text_delay)
async def target_text_delay_input(msg: types.Message, state: FSMContext) -> None:
    from ...storage import set_target_media

    raw = (msg.text or "").strip()
    if raw in CANCEL_TEXTS:
        await state.clear()
        data = load_user(msg.from_user)
        fsm = await state.get_data()
        pid = int(fsm.get("target_pid", 0))
        await msg.answer("Скасовано.", reply_markup=_build_chat_kb(data, pid))
        return

    try:
        delay = int(raw)
        if delay < 0:
            raise ValueError
    except ValueError:
        await msg.answer("⚠️ Введіть ціле число секунд (наприклад: 0, 15, 60).")
        return

    fsm_data = await state.get_data()
    pid_raw = fsm_data.get("target_pid")
    mode = fsm_data.get("target_mode")
    pending_text = fsm_data.get("pending_text")
    pending_media = fsm_data.get("pending_media")

    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        await state.clear()
        await msg.answer("⚠️ Стан втрачено.", reply_markup=main_menu_kb(msg.from_user))
        return
    if pending_text is None and pending_media is None:
        await state.clear()
        await msg.answer("⚠️ Стан втрачено.", reply_markup=main_menu_kb(msg.from_user))
        return

    data = load_user(msg.from_user)
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})

    if pending_media:
        set_target_media(data, pid, mode, **pending_media)
        item.pop(mode, None)
        item[f"{mode}_type"] = "text"   # медіа — це теж тип "text" (ручне)
    else:
        from ...storage import clear_target_media as _ctm
        _ctm(data, pid, mode)
        if pending_text == "":
            item[f"{mode}_type"] = "none"
            item.pop(mode, None)
        else:
            item[f"{mode}_type"] = "text"
            item[mode] = pending_text.strip()

    item[f"{mode}_delay_seconds"] = delay
    tms[pid] = item
    set_target_messages(data, tms)
    save_user(msg.from_user, data)
    await state.set_state(None)

    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"
    what = _MEDIA_LABELS.get(pending_media["kind"], "медіа") if pending_media else "текст"
    await msg.answer(
        f"✅  <b>{mode_label} для «{h(_target_title(data, pid))}» збережено</b>\n"
        f"Тип: <b>{what}</b>   ⏱ <b>{delay} с</b>",
        reply_markup=_build_chat_kb(data, pid),
    )


# ─────────────────────── Тип: 📦 Переслати з чату — вибір через Telethon-діалоги ───────────────────────

@router.callback_query(F.data.regexp(r"^bset:tc_type:(alert|clear):forward$"))
async def cb_tc_type_forward(call: types.CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    mode = parts[2]
    fsm_data = await state.get_data()
    pid = fsm_data.get("target_pid")
    if pid is None:
        await call.answer("Сесія застаріла.", show_alert=True)
        return

    await state.update_data(target_mode=mode)
    await call.answer("Завантаження діалогів…")

    # _show_src_dialog_list сама надішле список (або помилку) в чат
    await _show_src_dialog_list(call, state, call.from_user, pid, mode)


@router.callback_query(F.data == "bset:tc_src_search")
async def cb_tc_src_search(call: types.CallbackQuery, state: FSMContext) -> None:
    """Відкриває ввід пошукового запиту для чату-джерела."""
    await state.set_state(BroadcastStates.waiting_target_src_search)
    await call.answer()
    await call.message.answer(
        "🔍  Введіть назву чату, @username або числовий ID:",
        reply_markup=cancel_kb(),
    )


@router.message(BroadcastStates.waiting_target_src_search)
async def target_src_search_input(msg: types.Message, state: FSMContext) -> None:
    query = (msg.text or "").strip()
    if query in CANCEL_TEXTS:
        await state.set_state(None)
        fsm = await state.get_data()
        pid = fsm.get("target_pid")
        mode = fsm.get("target_mode", "alert")
        data = load_user(msg.from_user)
        await msg.answer("Скасовано.", reply_markup=_build_chat_kb(data, pid))
        return
    if not query:
        await msg.answer("⚠️ Введіть непорожній запит.")
        return

    await state.set_state(None)
    fsm = await state.get_data()
    pid = fsm.get("target_pid")
    mode = fsm.get("target_mode", "alert")
    await _show_src_dialog_list(msg, state, msg.from_user, pid, mode, query=query)


@router.callback_query(F.data.startswith("bset:tc_src:"))
async def cb_tc_src_select(call: types.CallbackQuery, state: FSMContext) -> None:
    """Користувач обрав чат-джерело зі списку."""
    key = call.data[len("bset:tc_src:"):]
    fsm_data = await state.get_data()
    mapping: dict[str, int] = fsm_data.get("src_dialog_map") or {}
    items: list[dict] = fsm_data.get("src_dialog_items") or []

    pid_raw = fsm_data.get("target_pid")
    mode = fsm_data.get("target_mode", "alert")

    src_pid = mapping.get(key)
    if src_pid is None:
        await call.answer("Клавіатура застаріла. Відкрийте «🎥 Кружок з чату» ще раз.", show_alert=True)
        return

    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        await call.answer("Сесія застаріла.", show_alert=True)
        return

    item = next((it for it in items if int(it["pid"]) == src_pid), None)
    title = str(item.get("title") or src_pid) if item else str(src_pid)

    data = load_user(call.from_user)
    set_target_forward_source(data, pid, mode, src_pid, title)
    save_user(call.from_user, data)

    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"
    current_fwd_mode = get_target_forward_mode(data, pid, mode)

    await state.set_state(BroadcastStates.waiting_forward_mode)
    await call.answer("✅ Збережено")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.message.answer(
        f"✅  Чат-джерело для {mode_label} збережено:\n"
        f"<b>{h(title)}</b>  (ID: <code>{src_pid}</code>)\n\n"
        f"Оберіть <b>режим надсилання кружків</b>:\n\n"
        f"🔄 <b>По колу</b> — кружки надсилаються по черзі, кожен наступний після попереднього. "
        f"Після останнього починається спочатку.\n\n"
        f"🗑 <b>Відправив → видалив</b> — завжди береться перший кружок зі списку і після надсилання видаляється з чату-джерела.",
        reply_markup=forward_mode_kb(mode, current_fwd_mode),
    )


@router.callback_query(F.data.regexp(r"^bset:tc_fwd_mode:(alert|clear):(roundrobin|delete)$"))
async def cb_tc_fwd_mode(call: types.CallbackQuery, state: FSMContext) -> None:
    """Користувач обрав режим відправки кружків."""
    parts = call.data.split(":")
    mode = parts[2]
    fwd_mode = parts[3]

    fsm_data = await state.get_data()
    pid_raw = fsm_data.get("target_pid")
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        await call.answer("Сесія застаріла.", show_alert=True)
        return

    data = load_user(call.from_user)
    set_target_forward_mode(data, pid, mode, fwd_mode)
    save_user(call.from_user, data)

    cur_delay = delay_for_target(data, pid, mode)
    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"
    fwd_label = "🔄 По колу" if fwd_mode == "roundrobin" else "🗑 Відправив → видалив"

    await state.set_state(BroadcastStates.waiting_target_forward_delay)
    await call.answer(f"✅ {fwd_label}")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.message.answer(
        f"✅  Режим: <b>{fwd_label}</b>\n\n"
        f"⏱  Вкажіть <b>затримку в секундах</b> для {mode_label} (поточна: <b>{cur_delay} с</b>, 0 = одразу):",
        reply_markup=cancel_kb(),
    )


@router.message(BroadcastStates.waiting_target_forward_delay)
async def target_forward_delay_input(msg: types.Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    if raw in CANCEL_TEXTS:
        await state.clear()
        fsm = await state.get_data()
        pid = int(fsm.get("target_pid", 0))
        data = load_user(msg.from_user)
        await msg.answer("Скасовано.", reply_markup=_build_chat_kb(data, pid))
        return

    try:
        delay = int(raw)
        if delay < 0:
            raise ValueError
    except ValueError:
        await msg.answer("⚠️ Введіть ціле число секунд (0, 15, 60…).")
        return

    fsm_data = await state.get_data()
    pid_raw = fsm_data.get("target_pid")
    mode = fsm_data.get("target_mode", "alert")
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        await state.clear()
        await msg.answer("⚠️ Стан втрачено.", reply_markup=main_menu_kb(msg.from_user))
        return

    data = load_user(msg.from_user)
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    item[f"{mode}_delay_seconds"] = delay
    tms[pid] = item
    set_target_messages(data, tms)
    save_user(msg.from_user, data)
    await state.set_state(None)

    src = get_target_forward_source(data, pid, mode)
    mode_label = "тривоги 🚨" if mode == "alert" else "відбою ✅"
    src_name = h(src["title"]) if src else "?"
    await msg.answer(
        f"✅  <b>Налаштування для {mode_label} збережено</b>\n"
        f"Чат-джерело: <b>{src_name}</b>   ⏱ <b>{delay} с</b>\n\n"
        f"При {'тривозі' if mode == 'alert' else 'відбої'} Telethon надішле відео-кружок "
        f"з цього чату без тегу «Переслано від».",
        reply_markup=_build_chat_kb(data, pid),
    )


# ─────────────────────── Розклад роботи ───────────────────────

def _schedule_text(data: dict) -> str:
    sched = get_schedule(data)
    if sched["enabled"]:
        status = (
            f"🟢  <b>Розклад увімкнено</b>\n"
            f"   Бот працюватиме лише з <b>{sched['from_time']}</b> до <b>{sched['to_time']}</b>"
        )
        if sched["from_time"] > sched["to_time"]:
            status += "\n   <i>(нічний діапазон — переходить через північ)</i>"
    else:
        status = (
            "🔴  <b>Розклад вимкнено</b>\n"
            "   Бот працює <b>цілодобово</b>"
        )
    return (
        f"⏰  <b>Розклад роботи</b>\n{HR}\n\n"
        f"{status}\n\n"
        f"<i>💡 Поза цим часом тривога/відбій просто пропускаються — повідомлення не надсилаються.</i>\n"
        f"<i>🕐 Час серверний — Київ (UTC+2 / UTC+3 влітку).</i>"
    )


@router.callback_query(F.data == "bset:schedule")
async def cb_schedule_open(call: types.CallbackQuery) -> None:
    data = load_user(call.from_user)
    sched = get_schedule(data)
    await call.answer()
    await call.message.answer(
        _schedule_text(data),
        reply_markup=schedule_kb(sched["enabled"], sched["from_time"], sched["to_time"]),
    )


@router.callback_query(F.data == "bset:sched_back")
async def cb_sched_back(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Повертаємося до головного меню налаштувань
    user = call.from_user
    items, err = await _fetch_dialogs(user)
    if err:
        await call.message.answer(err, reply_markup=main_menu_kb(user))
        return
    data = load_user(user)
    selected = get_targets(data)
    await call.message.answer(
        f"{_settings_summary(data)}\n\n<i>Розклад збережено.</i>",
        reply_markup=broadcast_settings_kb(items, selected),
    )


@router.callback_query(F.data == "sched:disable")
async def cb_sched_disable(call: types.CallbackQuery) -> None:
    data = load_user(call.from_user)
    sched = get_schedule(data)
    set_schedule(data, False, sched["from_time"], sched["to_time"])
    save_user(call.from_user, data)
    sched = get_schedule(data)
    await call.answer("🔴 Розклад вимкнено")
    try:
        await call.message.edit_text(
            _schedule_text(data),
            reply_markup=schedule_kb(sched["enabled"], sched["from_time"], sched["to_time"]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "sched:edit")
async def cb_sched_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    data = load_user(call.from_user)
    sched = get_schedule(data)
    await state.set_state(BroadcastStates.waiting_schedule_from)
    await call.answer()
    await call.message.answer(
        f"⏰  <b>Час, з якого починаємо працювати</b>\n{HR}\n\n"
        f"Поточний: <b>{sched['from_time']}</b>\n\n"
        f"Введіть час у форматі <b>ГГ:ХХ</b>:\n\n"
        f"{example_block('08:00', '22:30', '00:00')}\n\n"
        f"{tip('двокрапка обовʼязкова. Однозначний формат «8:00» теж приймається.')}",
        reply_markup=cancel_kb(),
    )


@router.message(BroadcastStates.waiting_schedule_from)
async def sched_from_input(msg: types.Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    if raw in CANCEL_TEXTS:
        await state.clear()
        data = load_user(msg.from_user)
        sched = get_schedule(data)
        await msg.answer(
            _schedule_text(data),
            reply_markup=schedule_kb(sched["enabled"], sched["from_time"], sched["to_time"]),
        )
        return

    if not _valid_time(raw):
        await msg.answer("⚠️ Невірний формат. Введіть час як <code>08:00</code> або <code>22:30</code>")
        return

    await state.update_data(pending_from=raw)
    await state.set_state(BroadcastStates.waiting_schedule_to)
    data = load_user(msg.from_user)
    sched = get_schedule(data)
    await msg.answer(
        f"✅  Початок: <b>{raw}</b>\n\n"
        f"⏰  <b>Тепер — час, до якого працюємо</b>\n\n"
        f"Поточний: <b>{sched['to_time']}</b>\n\n"
        f"{example_block('22:00', '06:00', '23:59')}\n\n"
        f"{tip('якщо кінець менший за початок — вважатиму, що це нічний діапазон через північ. Наприклад «22:00 → 06:00» = всю ніч.')}",
        reply_markup=cancel_kb(),
    )


@router.message(BroadcastStates.waiting_schedule_to)
async def sched_to_input(msg: types.Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    if raw in CANCEL_TEXTS:
        await state.clear()
        data = load_user(msg.from_user)
        sched = get_schedule(data)
        await msg.answer(
            _schedule_text(data),
            reply_markup=schedule_kb(sched["enabled"], sched["from_time"], sched["to_time"]),
        )
        return

    if not _valid_time(raw):
        await msg.answer("⚠️ Невірний формат. Введіть час як <code>22:00</code>")
        return

    fsm = await state.get_data()
    from_time = fsm.get("pending_from", "00:00")
    to_time = raw

    data = load_user(msg.from_user)
    set_schedule(data, True, from_time, to_time)
    save_user(msg.from_user, data)
    await state.clear()

    sched = get_schedule(data)
    night = "  <i>(нічний діапазон)</i>" if from_time > to_time else ""
    await msg.answer(
        f"✅  <b>Розклад збережено</b>\n\n"
        f"🟢 Розсилка активна: <b>{from_time}</b> — <b>{to_time}</b>{night}\n\n"
        f"Поза цим часом повідомлення надсилатися не будуть.",
        reply_markup=schedule_kb(sched["enabled"], sched["from_time"], sched["to_time"]),
    )


def _valid_time(s: str) -> bool:
    """Перевіряє формат ГГ:ХХ."""
    try:
        parts = s.split(":")
        if len(parts) != 2:
            return False
        h_val, m_val = int(parts[0]), int(parts[1])
        return 0 <= h_val <= 23 and 0 <= m_val <= 59
    except (ValueError, AttributeError):
        return False


@router.callback_query(F.data == "sched:noop")
async def cb_sched_noop(call: types.CallbackQuery) -> None:
    await call.answer()


# ─────────────────────── Noop ───────────────────────

@router.callback_query(F.data == "noop")
async def cb_noop(call: types.CallbackQuery) -> None:
    await call.answer()

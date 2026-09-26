"""Налаштування розсилки: один екран, який перемальовується на місці.

Навігація — inline-кнопки з pid у callback-даних (не залежить від FSM,
тож переживає рестарт бота):

  st:home                         головний екран: обрані чати + час роботи
  st:add                          вибір нового чату з діалогів Telegram
  st:pick:<pid>                   додати чат (або відкрити, якщо вже доданий)
  st:search                       пошук чату за назвою / @username / ID
  st:chat:<pid>                   екран чату: що йде при тривозі та відбої
  st:mode:<pid>:<mode>            екран події (mode = alert | clear)
  st:quick | st:text | st:fwd | st:none   вибір, що надсилати: <pid>:<mode>
  st:fwdmode:<pid>:<mode>         перемкнути режим кружків (по колу / видаляти)
  st:delay:<pid>:<mode>[:<sec>]   затримка
  st:rm:<pid> / st:rmok:<pid>     прибрати чат (з підтвердженням)
  st:close                        завершити

Текст/медіа, пошук і власна затримка вводяться повідомленням (FSM).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from telethon import TelegramClient
from telethon import utils as tl_utils
from telethon.tl.types import DocumentAttributeVideo

from ...config import (
    BTN_BROADCAST,
    CANCEL_TEXTS,
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
    get_target_media,
    get_target_messages,
    get_target_type,
    get_targets,
    get_targets_meta,
    load_user,
    message_for_target,
    save_user,
    session_path,
    set_schedule,
    set_target_forward_mode,
    set_target_forward_source,
    set_target_media,
    set_target_messages,
    set_target_type,
    sync_targets,
)
from ...utils import (
    example_block,
    h,
    parse_text_input,
    preview_message,
    soft_error,
    tip,
    truncate,
)
from ..keyboards import (
    cancel_kb,
    main_menu_kb,
    schedule_kb,
    source_chat_select_kb,
)
from ..states import BroadcastStates

log = logging.getLogger(__name__)
router = Router(name="broadcast")

MAX_TARGETS = 4
DELAY_PRESETS = (0, 15, 30, 60, 300)
MODES = ("alert", "clear")
# Швидкий варіант, який пропонуємо першою кнопкою для обох подій.
# (DEFAULT_ALERT_TEXT / DEFAULT_CLEAR_TEXT у config — інше: запасний текст для
# старих профілів без явного типу, його не чіпаємо.)
QUICK_TEXT = "+"

I = types.InlineKeyboardButton


def _kb(rows: list[list[types.InlineKeyboardButton]]) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


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


async def _download_media(bot, file_id: str, owner: str, scope: str, kind: str) -> str:
    ext_map = {"video_note": "mp4", "voice": "ogg", "photo": "jpg",
               "video": "mp4", "animation": "mp4"}
    ext = ext_map.get(kind, "bin")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = str(MEDIA_DIR / f"{owner}_{scope}.{ext}")
    await bot.download(file_id, destination=path)
    return path


def _valid_media(media: Optional[dict]) -> Optional[dict]:
    if media and Path(str(media.get("path") or "")).is_file():
        return media
    return None


# ─────────────────────── Telethon ───────────────────────

async def _telethon_client(user: types.User) -> tuple[Optional[TelegramClient], Optional[str]]:
    data = load_user(user)
    if not (data.get("api_id") and data.get("api_hash")):
        return None, f"{EMO['err']} Спочатку підключіть Telegram («🔌 Підключити»)."
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
        # ВАЖЛИВО: connect(), а не `async with client:` — контекст-менеджер Telethon
        # викликає client.start(), який на неавторизованій сесії намагається
        # читати номер телефону зі stdin → EOFError/зависання на сервері.
        await client.connect()
        if not await client.is_user_authorized():
            return [], f"{EMO['err']} Сесія не авторизована. Перепідключіть Telegram."
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
            q = query.strip().lower().lstrip("@")
            as_id: Optional[int] = None
            if q.lstrip("-").isdigit():
                as_id = int(q)
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
                    if len(items) >= 20:
                        break
        return items, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _count_circles(user: types.User, chat_id: int) -> Optional[int]:
    """Скільки відео-кружків серед останніх 200 повідомлень чату-джерела.
    None — якщо чат недоступний."""
    client, err = await _telethon_client(user)
    if err:
        return None
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        try:
            real_id, peer_cls = tl_utils.resolve_id(chat_id)
            entity = await client.get_input_entity(peer_cls(real_id))
        except (ValueError, KeyError):
            entity = await client.get_entity(chat_id)
        count = 0
        async for msg in client.iter_messages(entity, limit=200):
            doc = getattr(getattr(msg, "media", None), "document", None)
            if doc and any(
                isinstance(a, DocumentAttributeVideo) and getattr(a, "round_message", False)
                for a in getattr(doc, "attributes", [])
            ):
                count += 1
        return count
    except Exception as exc:
        log.debug("count circles chat_id=%d: %s", chat_id, exc)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ─────────────────────── Опис налаштувань ───────────────────────

def _mode_icon(mode: str) -> str:
    return "🚨" if mode == "alert" else "✅"


def _mode_name(mode: str) -> str:
    return "Тривога" if mode == "alert" else "Відбій"


def _on_event(mode: str) -> str:
    return "при тривозі" if mode == "alert" else "при відбої"


def _delay_text(sec: int) -> str:
    if sec <= 0:
        return "одразу"
    if sec % 60 == 0:
        return f"через {sec // 60} хв"
    return f"через {sec} с"


def _target_title(data: dict, pid: int) -> str:
    meta = get_targets_meta(data).get(pid, {}) or {}
    return str(meta.get("title") or f"chat {pid}")


def _describe(data: dict, pid: int, mode: str) -> str:
    """Що саме піде в чат — коротко, без HTML."""
    if _is_unset(data, pid, mode):
        return "⚠️ не налаштовано"
    t = get_target_type(data, pid, mode)
    if t == "none":
        return "🚫 не надсилати"
    if t == "forward":
        src = get_target_forward_source(data, pid, mode)
        return f"🎥 кружок з «{src['title']}»" if src else "🎥 кружок (джерело не обрано)"
    media = _valid_media(get_target_media(data, pid, mode))
    if media:
        return _MEDIA_LABELS.get(str(media.get("kind")), "📎 медіа")
    if t == "text":
        text = (get_target_messages(data).get(pid) or {}).get(mode)
        return f"«{preview_message(str(text), 40)}»" if text else "🚫 не надсилати"
    # Старі профілі без явного типу — глобальний дефолт
    text = message_for_target(data, pid, mode)
    return f"«{preview_message(text, 40)}»" if text else "🚫 не надсилати"


def _event_line(data: dict, pid: int, mode: str) -> str:
    desc = _describe(data, pid, mode)
    if desc.startswith(("🚫", "⚠️")):
        return f"{_mode_icon(mode)} {h(desc)}"
    return f"{_mode_icon(mode)} {h(desc)} · {_delay_text(delay_for_target(data, pid, mode))}"


# ─────────────────────── Збереження ───────────────────────

def _save_item(data: dict, pid: int, **fields) -> None:
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    item.update(fields)
    tms[pid] = item
    set_target_messages(data, tms)


def _is_unset(data: dict, pid: int, mode: str) -> bool:
    """Подію нового чату ще не обрано — у чат нічого не йде, поки не оберуть."""
    return bool((get_target_messages(data).get(pid) or {}).get(f"{mode}_unset"))


def _mark_set(data: dict, pid: int, mode: str) -> None:
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    if item.pop(f"{mode}_unset", None):
        tms[pid] = item
        set_target_messages(data, tms)


def _save_text(data: dict, pid: int, mode: str, text: str) -> None:
    clear_target_media(data, pid, mode)
    _save_item(data, pid, **{mode: text, f"{mode}_type": "text"})
    _mark_set(data, pid, mode)


def _save_media(data: dict, pid: int, mode: str, media: dict) -> None:
    # set_target_media прибирає текст цього режиму; тип «text» = ручний вміст
    set_target_media(data, pid, mode, **media)
    set_target_type(data, pid, mode, "text")
    _mark_set(data, pid, mode)


def _save_none(data: dict, pid: int, mode: str) -> None:
    set_target_type(data, pid, mode, "none")
    _mark_set(data, pid, mode)


def _add_target(data: dict, pid: int, meta: dict) -> None:
    """Додає чат без вмісту: тип «none» (розсильник пропускає) + позначка
    «не налаштовано». Що надсилати, користувач обирає одразу після додавання."""
    targets = get_targets(data)
    all_meta = get_targets_meta(data)
    targets.append(pid)
    all_meta[pid] = meta
    sync_targets(data, targets, all_meta)
    for mode in MODES:
        _save_item(data, pid, **{f"{mode}_type": "none", f"{mode}_unset": True})


def _remove_target(data: dict, pid: int) -> None:
    targets = [p for p in get_targets(data) if p != pid]
    all_meta = get_targets_meta(data)
    all_meta.pop(pid, None)
    sync_targets(data, targets, all_meta)


# ─────────────────────── Екрани ───────────────────────

def _home_screen(data: dict) -> tuple[str, types.InlineKeyboardMarkup]:
    targets = get_targets(data)
    sched = get_schedule(data)
    sched_text = f"{sched['from_time']}–{sched['to_time']}" if sched["enabled"] else "цілодобово"

    if targets:
        blocks = [
            f"💬 <b>{h(_target_title(data, pid))}</b>\n"
            f"    {_event_line(data, pid, 'alert')}\n"
            f"    {_event_line(data, pid, 'clear')}"
            for pid in targets
        ]
        body = "\n\n".join(blocks) + "\n\n<i>Натисніть чат, щоб змінити, що туди надсилається.</i>"
    else:
        body = "<i>Ще не обрано жодного чату. Натисніть «➕ Додати чат».</i>"

    text = (
        f"🎛  <b>Налаштування розсилки</b>\n{HR}\n\n"
        f"{body}\n\n"
        f"⏰ Час роботи: <b>{sched_text}</b>"
    )
    rows = [[I(text=f"💬 {truncate(_target_title(data, pid), 30)}", callback_data=f"st:chat:{pid}")]
            for pid in targets]
    if len(targets) < MAX_TARGETS:
        rows.append([I(text="➕ Додати чат", callback_data="st:add")])
    rows.append([I(text=f"⏰ Час роботи: {sched_text}", callback_data="bset:schedule")])
    rows.append([I(text="✅ Готово", callback_data="st:close")])
    return text, _kb(rows)


def _add_screen(
    items: list[dict], selected: list[int], query: Optional[str] = None
) -> tuple[str, types.InlineKeyboardMarkup]:
    selected_set = set(selected)
    if query:
        head = f"🔍  <b>Пошук «{h(query)}»</b>"
        hint = "Натисніть чат, щоб додати його." if items else "Нічого не знайдено. Спробуйте іншу назву або ID."
    else:
        head = "➕  <b>Додати чат</b>"
        hint = "Натисніть чат, щоб додати його. Не бачите потрібного — «🔍 Пошук»."
    text = f"{head}\n{HR}\n\n{hint}\n<i>Можна обрати до {MAX_TARGETS} чатів.</i>"

    rows: list[list[types.InlineKeyboardButton]] = []
    row: list[types.InlineKeyboardButton] = []
    for it in items[:20]:
        pid = int(it["pid"])
        mark = "✅ " if pid in selected_set else ""
        row.append(I(text=f"{mark}{truncate(it.get('title') or '—', 22)}", callback_data=f"st:pick:{pid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([I(text="🔍 Пошук", callback_data="st:search"), I(text="‹ Назад", callback_data="st:home")])
    return text, _kb(rows)


def _chat_screen(data: dict, pid: int) -> tuple[str, types.InlineKeyboardMarkup]:
    text = (
        f"💬  <b>{h(_target_title(data, pid))}</b>\n{HR}\n\n"
        f"{_event_line(data, pid, 'alert')}\n"
        f"{_event_line(data, pid, 'clear')}\n\n"
        f"<i>Натисніть подію, щоб змінити, що надсилати.</i>"
    )
    rows = [
        [I(text=f"🚨 Тривога: {truncate(_describe(data, pid, 'alert'), 30)}", callback_data=f"st:mode:{pid}:alert")],
        [I(text=f"✅ Відбій: {truncate(_describe(data, pid, 'clear'), 30)}", callback_data=f"st:mode:{pid}:clear")],
        [I(text="🗑 Прибрати чат", callback_data=f"st:rm:{pid}"), I(text="‹ Назад", callback_data="st:home")],
    ]
    return text, _kb(rows)


def _mode_screen(
    data: dict, pid: int, mode: str, circles: Optional[int] = None
) -> tuple[str, types.InlineKeyboardMarkup]:
    t = get_target_type(data, pid, mode)
    delay = delay_for_target(data, pid, mode)
    unset = _is_unset(data, pid, mode)
    if unset:
        step = "1" if mode == "alert" else "2"
        lines = [
            f"{_mode_icon(mode)}  <b>{h(_target_title(data, pid))}</b> · крок {step} з 2\n{HR}\n",
            f"Що надсилати в цей чат <b>{_on_event(mode)}</b>?",
        ]
    else:
        lines = [
            f"{_mode_icon(mode)}  <b>{h(_target_title(data, pid))} — {_mode_name(mode).lower()}</b>\n{HR}\n",
            f"Зараз: <b>{h(_describe(data, pid, mode))}</b>",
        ]
    if t != "none":
        lines.append(f"Коли: <b>{_delay_text(delay)}</b> після початку {'тривоги' if mode == 'alert' else 'відбою'}")
    if t == "forward" and circles is not None:
        if circles == 0:
            lines.append("\n⚠️ <b>У джерелі поки немає кружків</b> — запишіть їх туди, інакше нічого не піде.")
        else:
            lines.append(f"Кружків у джерелі: <b>{circles}</b>")
    if not unset:
        lines.append(f"\nЩо надсилати {_on_event(mode)}?")

    item = get_target_messages(data).get(pid) or {}
    is_quick = t == "text" and item.get(mode) == QUICK_TEXT and not _valid_media(get_target_media(data, pid, mode))

    def mark(kind: str) -> str:
        if unset:
            return ""
        is_text = (t == "text" or (t is None and message_for_target(data, pid, mode))) and not is_quick
        current = {"quick": is_quick, "text": is_text, "forward": t == "forward", "none": t == "none"}[kind]
        return "✓ " if current else ""

    key = f"{pid}:{mode}"
    rows = [
        [I(text=f"{mark('quick')}➕ «{QUICK_TEXT}»", callback_data=f"st:quick:{key}")],
        [I(text=f"{mark('text')}✍️ Свій текст або медіа", callback_data=f"st:text:{key}")],
        [I(text=f"{mark('forward')}🎥 Кружок з чату", callback_data=f"st:fwd:{key}")],
        [I(text=f"{mark('none')}🚫 Не надсилати", callback_data=f"st:none:{key}")],
    ]
    if t == "forward" and get_target_forward_source(data, pid, mode):
        fwd = get_target_forward_mode(data, pid, mode)
        label = "🔄 Кружки: по колу" if fwd == "roundrobin" else "🗑 Кружки: відправив → видалив"
        rows.append([I(text=label, callback_data=f"st:fwdmode:{key}")])
    if t != "none":
        rows.append([I(text=f"⏱ Затримка: {_delay_text(delay)}", callback_data=f"st:delay:{key}")])
    rows.append([I(text="‹ Назад", callback_data=f"st:chat:{pid}")])
    return "\n".join(lines), _kb(rows)


def _delay_screen(data: dict, pid: int, mode: str) -> tuple[str, types.InlineKeyboardMarkup]:
    cur = delay_for_target(data, pid, mode)
    text = (
        f"⏱  <b>Затримка — {_mode_name(mode).lower()}</b>\n{HR}\n\n"
        f"Через скільки після початку {'тривоги' if mode == 'alert' else 'відбою'} "
        f"надсилати в «{h(_target_title(data, pid))}»?\n"
        f"Зараз: <b>{_delay_text(cur)}</b>"
    )
    labels = {0: "Одразу", 15: "15 с", 30: "30 с", 60: "1 хв", 300: "5 хв"}
    key = f"{pid}:{mode}"
    presets = [
        I(text=("✓ " if cur == s else "") + labels[s], callback_data=f"st:delay:{key}:{s}")
        for s in DELAY_PRESETS
    ]
    rows = [presets[:3], presets[3:], [
        I(text="✏️ Інше число", callback_data=f"st:delayin:{key}"),
        I(text="‹ Назад", callback_data=f"st:mode:{key}"),
    ]]
    return text, _kb(rows)


def _after_choice(data: dict, pid: int) -> tuple[str, types.InlineKeyboardMarkup]:
    """Після вибору для події: якщо в чата лишилась не налаштована подія —
    одразу питаємо про неї, інакше — екран чату."""
    for mode in MODES:
        if _is_unset(data, pid, mode):
            return _mode_screen(data, pid, mode)
    return _chat_screen(data, pid)


async def _render(call: types.CallbackQuery, screen: tuple[str, types.InlineKeyboardMarkup]) -> None:
    """Перемальовує поточне повідомлення; якщо не можна — надсилає нове."""
    text, kb = screen
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "not modified" in str(exc):
            return
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


def _parse(call: types.CallbackQuery) -> tuple[Optional[int], Optional[str], list[str]]:
    """st:<action>:<pid>[:<mode>[:extra…]] → (pid, mode, extra)."""
    parts = call.data.split(":")
    try:
        pid = int(parts[2])
    except (IndexError, ValueError):
        return None, None, []
    mode = parts[3] if len(parts) > 3 and parts[3] in MODES else None
    return pid, mode, parts[4:]


async def _load_target(call: types.CallbackQuery) -> tuple[Optional[dict], Optional[int], Optional[str], list[str]]:
    """Дані користувача + pid/mode з кнопки; якщо чат уже прибрано — повертає на головний."""
    pid, mode, extra = _parse(call)
    data = load_user(call.from_user)
    if pid is None or pid not in get_targets(data):
        await call.answer("Цей чат уже прибрано з розсилки.")
        await _render(call, _home_screen(data))
        return None, None, None, []
    return data, pid, mode, extra


# ─────────────────────── Точки входу ───────────────────────

@router.message(F.text == BTN_BROADCAST)
async def open_broadcast_settings(msg: types.Message, state: FSMContext) -> None:
    await state.clear()
    await show_broadcast_settings(msg, state=state)


async def show_broadcast_settings(
    msg: types.Message, query: Optional[str] = None, state: Optional[FSMContext] = None
) -> None:
    """Головний екран; якщо чатів ще немає — одразу вибір чату."""
    data = load_user(msg.from_user)
    if not get_targets(data) or query is not None:
        items, err = await _fetch_dialogs(msg.from_user, query=query)
        if err:
            await msg.answer(err, reply_markup=main_menu_kb(msg.from_user))
            return
        if state is not None:
            await _remember_items(state, items)
        text, kb = _add_screen(items, get_targets(data), query)
    else:
        text, kb = _home_screen(data)
    await msg.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def _remember_items(state: FSMContext, items: list[dict]) -> None:
    """Назви знайдених чатів — щоб зберегти їх у профіль при додаванні."""
    await state.update_data(pending_targets={
        str(int(it["pid"])): {"title": it.get("title"), "username": it.get("username"), "kind": it.get("kind")}
        for it in items
    })


@router.callback_query(F.data == "st:home")
async def cb_home(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await call.answer()
    await _render(call, _home_screen(load_user(call.from_user)))


@router.callback_query(F.data == "st:close")
async def cb_close(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Збережено ✅")
    data = load_user(call.from_user)
    text, _ = _home_screen(data)
    try:
        await call.message.edit_text(text.replace("Натисніть чат, щоб змінити, що туди надсилається.", ""),
                                     reply_markup=None)
    except TelegramBadRequest:
        pass
    # Нове повідомлення — щоб оновити головне меню (етап міг змінитись)
    await call.message.answer("✅  <b>Готово.</b> Налаштування збережено.", reply_markup=main_menu_kb(call.from_user))


# ─────────────────────── Додати чат ───────────────────────

@router.callback_query(F.data == "st:add")
async def cb_add(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    data = load_user(call.from_user)
    if len(get_targets(data)) >= MAX_TARGETS:
        await call.answer(f"Максимум {MAX_TARGETS} чати. Спершу приберіть один.", show_alert=True)
        return
    await call.answer("Завантажую ваші чати…")
    items, err = await _fetch_dialogs(call.from_user)
    if err:
        await call.message.answer(err)
        return
    await _remember_items(state, items)
    await _render(call, _add_screen(items, get_targets(data)))


@router.callback_query(F.data.startswith("st:pick:"))
async def cb_pick(call: types.CallbackQuery, state: FSMContext) -> None:
    pid, _, _ = _parse(call)
    if pid is None:
        await call.answer()
        return
    data = load_user(call.from_user)
    if pid not in get_targets(data):
        if len(get_targets(data)) >= MAX_TARGETS:
            await call.answer(f"Максимум {MAX_TARGETS} чати. Спершу приберіть один.", show_alert=True)
            return
        pending = (await state.get_data()).get("pending_targets") or {}
        meta = pending.get(str(pid)) or {"title": str(pid)}
        _add_target(data, pid, meta)
        save_user(call.from_user, data)
        await call.answer("✅ Чат додано")
        await _render(call, _mode_screen(data, pid, "alert"))
        return
    await call.answer()
    await _render(call, _after_choice(data, pid))


@router.callback_query(F.data == "st:search")
async def cb_search(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_search_query)
    await call.answer()
    await _render(call, (
        f"🔍  <b>Пошук чату</b>\n{HR}\n\n"
        f"Напишіть назву чату, @username або числовий ID.\n\n"
        f"{example_block('Робочий чат', '@username_chat', '-1001234567890')}",
        _kb([[I(text="‹ Назад", callback_data="st:add")]]),
    ))


@router.message(BroadcastStates.waiting_search_query)
async def search_query(msg: types.Message, state: FSMContext) -> None:
    query = (msg.text or "").strip()
    await state.set_state(None)
    if not query or query in CANCEL_TEXTS:
        await show_broadcast_settings(msg, state=state)
        return
    await show_broadcast_settings(msg, query=query, state=state)


# ─────────────────────── Чат / подія ───────────────────────

@router.callback_query(F.data.startswith("st:chat:"))
async def cb_chat(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    data, pid, _, _ = await _load_target(call)
    if data is None:
        return
    await call.answer()
    await _render(call, _chat_screen(data, pid))


async def _render_mode(call: types.CallbackQuery, data: dict, pid: int, mode: str) -> None:
    circles = None
    src = get_target_forward_source(data, pid, mode)
    if get_target_type(data, pid, mode) == "forward" and src:
        circles = await _count_circles(call.from_user, int(src["chat_id"]))
    await _render(call, _mode_screen(data, pid, mode, circles))


@router.callback_query(F.data.startswith("st:mode:"))
async def cb_mode(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    data, pid, mode, _ = await _load_target(call)
    if data is None or mode is None:
        return
    await call.answer()
    await _render_mode(call, data, pid, mode)


@router.callback_query(F.data.startswith("st:none:"))
async def cb_none(call: types.CallbackQuery) -> None:
    data, pid, mode, _ = await _load_target(call)
    if data is None or mode is None:
        return
    _save_none(data, pid, mode)
    save_user(call.from_user, data)
    await call.answer(f"🚫 {_mode_name(mode)}: не надсилати")
    await _render(call, _after_choice(data, pid))


@router.callback_query(F.data.startswith("st:quick:"))
async def cb_quick(call: types.CallbackQuery) -> None:
    data, pid, mode, _ = await _load_target(call)
    if data is None or mode is None:
        return
    _save_text(data, pid, mode, QUICK_TEXT)
    save_user(call.from_user, data)
    await call.answer(f"✅ {_mode_name(mode)}: «{QUICK_TEXT}»")
    await _render(call, _after_choice(data, pid))


@router.callback_query(F.data.startswith("st:rm:"))
async def cb_remove(call: types.CallbackQuery) -> None:
    data, pid, _, _ = await _load_target(call)
    if data is None:
        return
    await call.answer()
    await _render(call, (
        f"🗑  Прибрати «{h(_target_title(data, pid))}» з розсилки?\n"
        f"<i>Налаштування цього чату буде видалено.</i>",
        _kb([[I(text="🗑 Так, прибрати", callback_data=f"st:rmok:{pid}"),
              I(text="‹ Ні", callback_data=f"st:chat:{pid}")]]),
    ))


@router.callback_query(F.data.startswith("st:rmok:"))
async def cb_remove_ok(call: types.CallbackQuery) -> None:
    data, pid, _, _ = await _load_target(call)
    if data is None:
        return
    _remove_target(data, pid)
    save_user(call.from_user, data)
    await call.answer("Чат прибрано")
    await _render(call, _home_screen(data))


# ─────────────────────── Текст або медіа ───────────────────────

@router.callback_query(F.data.startswith("st:text:"))
async def cb_text(call: types.CallbackQuery, state: FSMContext) -> None:
    data, pid, mode, _ = await _load_target(call)
    if data is None or mode is None:
        return
    await state.set_state(BroadcastStates.waiting_target_mode_text)
    await state.update_data(target_pid=pid, target_mode=mode, prompt_msg_id=call.message.message_id)
    await call.answer()
    await _render(call, (
        f"✍️  <b>{h(_target_title(data, pid))} — {_mode_name(mode).lower()}</b>\n{HR}\n\n"
        f"Надішліть сюди, що відправляти {_on_event(mode)}:\n"
        f"  • текст (можна з emoji)\n"
        f"  • або фото, відео, кружок, голосове\n\n"
        f"Зараз: <b>{h(_describe(data, pid, mode))}</b>\n\n"
        f"{example_block('+', '✅ Відбій тривоги', 'УВАГА! Усі в укриття!')}",
        _kb([[I(text="‹ Скасувати", callback_data=f"st:mode:{pid}:{mode}")]]),
    ))


@router.message(BroadcastStates.waiting_target_mode_text)
async def text_input(msg: types.Message, state: FSMContext) -> None:
    fsm = await state.get_data()
    mode = fsm.get("target_mode")
    try:
        pid = int(fsm.get("target_pid"))
    except (TypeError, ValueError):
        await state.set_state(None)
        await show_broadcast_settings(msg, state=state)
        return
    data = load_user(msg.from_user)
    if pid not in get_targets(data) or mode not in MODES:
        await state.set_state(None)
        await show_broadcast_settings(msg, state=state)
        return

    media_info = _extract_media(msg)
    if media_info:
        kind, file_id, caption = media_info
        try:
            path = await _download_media(msg.bot, file_id, str(msg.from_user.id), f"pid{pid}_{mode}", kind)
        except Exception as e:
            await msg.answer(f"❌ Не вдалося завантажити: {h(str(e))}")
            return
        _save_media(data, pid, mode, {"kind": kind, "path": path, "caption": caption})
    else:
        text = parse_text_input(msg.text)
        if text is None:
            await msg.answer("⚠️ Надішліть текст або фото / відео / кружок / голосове.")
            return
        if text == "":
            _save_none(data, pid, mode)
        else:
            _save_text(data, pid, mode, text)
    save_user(msg.from_user, data)
    await state.set_state(None)

    # Прибираємо кнопки з підказки вище — актуальний екран буде нижче
    if fsm.get("prompt_msg_id"):
        try:
            await msg.bot.edit_message_reply_markup(
                chat_id=msg.chat.id, message_id=fsm["prompt_msg_id"], reply_markup=None,
            )
        except TelegramBadRequest:
            pass
    text, kb = _after_choice(data, pid)
    await msg.answer(f"✅ <b>Збережено.</b>\n\n{text}", reply_markup=kb)


# ─────────────────────── Затримка ───────────────────────

@router.callback_query(F.data.startswith("st:delay:"))
async def cb_delay(call: types.CallbackQuery) -> None:
    data, pid, mode, extra = await _load_target(call)
    if data is None or mode is None:
        return
    if extra:
        try:
            sec = max(0, int(extra[0]))
        except ValueError:
            sec = 0
        _save_item(data, pid, **{f"{mode}_delay_seconds": sec})
        save_user(call.from_user, data)
        await call.answer(f"⏱ {_delay_text(sec).capitalize()}")
        await _render_mode(call, data, pid, mode)
        return
    await call.answer()
    await _render(call, _delay_screen(data, pid, mode))


@router.callback_query(F.data.startswith("st:delayin:"))
async def cb_delay_input(call: types.CallbackQuery, state: FSMContext) -> None:
    data, pid, mode, _ = await _load_target(call)
    if data is None or mode is None:
        return
    await state.set_state(BroadcastStates.waiting_target_text_delay)
    await state.update_data(target_pid=pid, target_mode=mode, prompt_msg_id=call.message.message_id)
    await call.answer()
    await _render(call, (
        "⏱  Напишіть затримку в <b>секундах</b> (наприклад <code>45</code> або <code>120</code>).",
        _kb([[I(text="‹ Скасувати", callback_data=f"st:delay:{pid}:{mode}")]]),
    ))


@router.message(BroadcastStates.waiting_target_text_delay)
async def delay_input(msg: types.Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    fsm = await state.get_data()
    try:
        pid = int(fsm.get("target_pid"))
        mode = fsm["target_mode"]
    except (TypeError, ValueError, KeyError):
        await state.set_state(None)
        await show_broadcast_settings(msg, state=state)
        return
    if raw in CANCEL_TEXTS:
        await state.set_state(None)
        await show_broadcast_settings(msg, state=state)
        return
    try:
        sec = int(raw)
        if not 0 <= sec <= 86400:
            raise ValueError
    except ValueError:
        await msg.answer("⚠️ Потрібне ціле число секунд від 0 до 86400.")
        return

    data = load_user(msg.from_user)
    if pid not in get_targets(data):
        await state.set_state(None)
        await show_broadcast_settings(msg, state=state)
        return
    _save_item(data, pid, **{f"{mode}_delay_seconds": sec})
    save_user(msg.from_user, data)
    await state.set_state(None)
    if fsm.get("prompt_msg_id"):
        try:
            await msg.bot.edit_message_reply_markup(
                chat_id=msg.chat.id, message_id=fsm["prompt_msg_id"], reply_markup=None,
            )
        except TelegramBadRequest:
            pass
    text, kb = _chat_screen(data, pid)
    await msg.answer(f"✅ <b>Затримку збережено:</b> {_delay_text(sec)}.\n\n{text}", reply_markup=kb)


# ─────────────────────── Кружок з чату ───────────────────────

@router.callback_query(F.data.startswith("st:fwd:"))
async def cb_forward(call: types.CallbackQuery, state: FSMContext) -> None:
    data, pid, mode, _ = await _load_target(call)
    if data is None or mode is None:
        return
    # Хендлери вибору джерела (bset:tc_src*) беруть pid/mode з FSM
    await state.update_data(target_pid=pid, target_mode=mode)
    await call.answer("Завантажую ваші чати…")
    await _show_src_dialog_list(call, state, pid, mode)


@router.callback_query(F.data.startswith("st:fwdmode:"))
async def cb_forward_mode(call: types.CallbackQuery) -> None:
    data, pid, mode, _ = await _load_target(call)
    if data is None or mode is None:
        return
    new = "delete" if get_target_forward_mode(data, pid, mode) == "roundrobin" else "roundrobin"
    set_target_forward_mode(data, pid, mode, new)
    save_user(call.from_user, data)
    await call.answer(
        "🔄 По колу: кружки йдуть по черзі, після останнього — знову з першого"
        if new == "roundrobin" else
        "🗑 Відправив → видалив: кружок видаляється з джерела після надсилання",
        show_alert=True,
    )
    await _render_mode(call, data, pid, mode)


async def _show_src_dialog_list(
    msg_or_call, state: FSMContext, pid: int, mode: str, query: Optional[str] = None,
) -> None:
    """Список діалогів для вибору чату-джерела кружків."""
    user = msg_or_call.from_user
    items, err = await _fetch_dialogs(user, query=query)
    back = f"st:mode:{pid}:{mode}"
    if err:
        text, kb = f"❌ Не вдалося завантажити чати:\n<code>{h(str(err))}</code>", _kb([[I(text="‹ Назад", callback_data=back)]])
    else:
        mapping: dict[str, int] = {str(i): int(it["pid"]) for i, it in enumerate(items[:20])}
        await state.update_data(src_dialog_map=mapping, src_dialog_items=items[:20])
        found = f"Результати пошуку «{h(query)}»:" if query else "Оберіть чат, з якого брати кружки:"
        if query and not items:
            found = f"За «{h(query)}» нічого не знайдено."
        text = (
            f"🎥  <b>Звідки брати кружки — {_mode_name(mode).lower()}</b>\n{HR}\n\n"
            f"{found}\n<i>Або створіть окремий чат — бот зробить його сам.</i>"
        )
        kb = source_chat_select_kb(items[:20], mapping, back_cb=back)

    if isinstance(msg_or_call, types.CallbackQuery):
        await _render(msg_or_call, (text, kb))
    else:
        await msg_or_call.answer(text, reply_markup=kb)


@router.callback_query(F.data == "bset:tc_src_search")
async def cb_tc_src_search(call: types.CallbackQuery, state: FSMContext) -> None:
    fsm = await state.get_data()
    await state.set_state(BroadcastStates.waiting_target_src_search)
    await call.answer()
    await _render(call, (
        "🔍  Напишіть назву чату, @username або числовий ID:",
        _kb([[I(text="‹ Назад", callback_data=f"st:fwd:{fsm.get('target_pid')}:{fsm.get('target_mode', 'alert')}")]]),
    ))


@router.message(BroadcastStates.waiting_target_src_search)
async def target_src_search_input(msg: types.Message, state: FSMContext) -> None:
    query = (msg.text or "").strip()
    await state.set_state(None)
    fsm = await state.get_data()
    try:
        pid = int(fsm.get("target_pid"))
    except (TypeError, ValueError):
        await show_broadcast_settings(msg, state=state)
        return
    mode = fsm.get("target_mode", "alert")
    if not query or query in CANCEL_TEXTS:
        await show_broadcast_settings(msg, state=state)
        return
    await _show_src_dialog_list(msg, state, pid, mode, query=query)


@router.callback_query(F.data.startswith("bset:tc_src:"))
async def cb_tc_src_select(call: types.CallbackQuery, state: FSMContext) -> None:
    """Користувач обрав чат-джерело зі списку."""
    key = call.data[len("bset:tc_src:"):]
    fsm = await state.get_data()
    mapping: dict[str, int] = fsm.get("src_dialog_map") or {}
    items: list[dict] = fsm.get("src_dialog_items") or []
    mode = fsm.get("target_mode", "alert")
    src_pid = mapping.get(key)
    try:
        pid = int(fsm.get("target_pid"))
    except (TypeError, ValueError):
        pid = None
    if src_pid is None or pid is None:
        await call.answer("Список застарів — відкрийте «🎥 Кружок з чату» ще раз.", show_alert=True)
        return

    item = next((it for it in items if int(it["pid"]) == src_pid), None)
    title = str(item.get("title") or src_pid) if item else str(src_pid)
    await call.answer("✅ Джерело збережено")
    await _source_selected(call, pid, mode, src_pid, title, edit=True)


async def _source_selected(
    call: types.CallbackQuery, pid: int, mode: str, src_pid: int, title: str, edit: bool
) -> None:
    """Зберігає чат-джерело (режим — «по колу») і показує екран події."""
    data = load_user(call.from_user)
    set_target_forward_source(data, pid, mode, src_pid, title)
    if not (get_target_messages(data).get(pid) or {}).get(f"{mode}_forward_mode"):
        set_target_forward_mode(data, pid, mode, "roundrobin")
    _mark_set(data, pid, mode)
    save_user(call.from_user, data)

    if any(_is_unset(data, pid, m) for m in MODES):
        # Новий чат: одразу питаємо про другу подію
        text, kb = _after_choice(data, pid)
        text = f"✅ {_mode_name(mode)}: кружок з «{h(title)}».\n\n{text}"
    else:
        circles = await _count_circles(call.from_user, src_pid)
        text, kb = _mode_screen(data, pid, mode, circles)
    if edit:
        await _render(call, (text, kb))
    else:
        await call.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "bset:tc_src_new")
async def cb_tc_src_new(call: types.CallbackQuery, state: FSMContext) -> None:
    """Створює в акаунті користувача приватний канал для кружків і робить його джерелом."""
    fsm_data = await state.get_data()
    mode = fsm_data.get("target_mode", "alert")
    try:
        pid = int(fsm_data.get("target_pid"))
    except (TypeError, ValueError):
        await call.answer("Список застарів — відкрийте «🎥 Кружок з чату» ще раз.", show_alert=True)
        return

    await call.answer("Створюю чат…")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    mode_word = "тривога" if mode == "alert" else "відбій"
    title = f"🎥 Кружки — {mode_word}"
    try:
        channel_id, peer_id = await _create_circles_channel(call.from_user, title, mode)
    except Exception as exc:
        log.warning("create circles channel failed uid=%d: %s", call.from_user.id, exc)
        await call.message.answer(
            soft_error(
                "Не вдалося створити чат",
                body=f"<code>{h(str(exc))}</code>\n\n"
                     f"<i>Можна створити канал вручну в Telegram і обрати його зі списку.</i>",
                retry=False,
            ),
            reply_markup=_kb([[I(text="‹ Назад", callback_data=f"st:mode:{pid}:{mode}")]]),
        )
        return

    # Посилання t.me/c/<id>/1 відкриває приватний канал для його власника
    open_kb = _kb([[I(text="📂 Відкрити чат", url=f"https://t.me/c/{channel_id}/1")]])
    await call.message.answer(
        f"🎥  <b>Чат «{h(title)}» створено</b>\n{HR}\n"
        f"Це приватний канал у вашому Telegram — його бачите лише ви.\n\n"
        f"<b>Що далі:</b> відкрийте його і запишіть кілька кружків "
        f"<i>(затисніть кнопку мікрофона — вона перемкнеться на камеру)</i>. "
        f"{_on_event(mode).capitalize()} бот надсилатиме їх звідти.",
        reply_markup=open_kb,
    )
    await _source_selected(call, pid, mode, peer_id, title, edit=False)


async def _create_circles_channel(user: types.User, title: str, mode: str) -> tuple[int, int]:
    """Створює приватний канал через Telethon-сесію. Повертає (channel_id, peer_id)."""
    from telethon.tl.functions.channels import CreateChannelRequest

    client, err = await _telethon_client(user)
    if err:
        raise RuntimeError("Telegram не підключено")
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Сесія не авторизована — перепідключіть Telegram")
        about = (
            "Запишіть сюди відео-кружки — Plus Sender надсилатиме їх у ваші чати "
            f"{_on_event(mode)}."
        )
        result = await client(CreateChannelRequest(title=title, about=about, broadcast=True))
        channel = result.chats[0]
        return channel.id, tl_utils.get_peer_id(channel)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ─────────────────────── Час роботи ───────────────────────

def _schedule_text(data: dict) -> str:
    sched = get_schedule(data)
    if sched["enabled"]:
        status = (
            f"🟢  <b>Час роботи увімкнено</b>\n"
            f"   Бот працюватиме лише з <b>{sched['from_time']}</b> до <b>{sched['to_time']}</b>"
        )
        if sched["from_time"] > sched["to_time"]:
            status += "\n   <i>(нічний діапазон — переходить через північ)</i>"
    else:
        status = (
            "🔴  <b>Час роботи вимкнено</b>\n"
            "   Бот працює <b>цілодобово</b>"
        )
    return (
        f"⏰  <b>Час роботи</b>\n{HR}\n\n"
        f"{status}\n\n"
        f"<i>💡 Поза цим часом тривога/відбій просто пропускаються — повідомлення не надсилаються.</i>\n"
        f"<i>🕐 Час — київський.</i>"
    )


def _schedule_screen(data: dict) -> tuple[str, types.InlineKeyboardMarkup]:
    sched = get_schedule(data)
    return _schedule_text(data), schedule_kb(sched["enabled"], sched["from_time"], sched["to_time"])


@router.callback_query(F.data == "bset:schedule")
async def cb_schedule_open(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await call.answer()
    await _render(call, _schedule_screen(load_user(call.from_user)))


@router.callback_query(F.data == "sched:disable")
async def cb_sched_disable(call: types.CallbackQuery) -> None:
    data = load_user(call.from_user)
    sched = get_schedule(data)
    set_schedule(data, False, sched["from_time"], sched["to_time"])
    save_user(call.from_user, data)
    await call.answer("🔴 Час роботи вимкнено")
    await _render(call, _schedule_screen(data))


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
        text, kb = _schedule_screen(load_user(msg.from_user))
        await msg.answer(text, reply_markup=kb)
        return

    if not _valid_time(raw):
        await msg.answer("⚠️ Невірний формат. Введіть час як <code>08:00</code> або <code>22:30</code>")
        return

    await state.update_data(pending_from=raw)
    await state.set_state(BroadcastStates.waiting_schedule_to)
    sched = get_schedule(load_user(msg.from_user))
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
        text, kb = _schedule_screen(load_user(msg.from_user))
        await msg.answer(text, reply_markup=kb)
        return

    if not _valid_time(raw):
        await msg.answer("⚠️ Невірний формат. Введіть час як <code>22:00</code>")
        return

    fsm = await state.get_data()
    from_time = fsm.get("pending_from", "00:00")
    data = load_user(msg.from_user)
    set_schedule(data, True, from_time, raw)
    save_user(msg.from_user, data)
    await state.clear()

    night = "  <i>(нічний діапазон)</i>" if from_time > raw else ""
    # Прибираємо «Скасувати» з reply-клавіатури, далі — екран часу роботи
    await msg.answer(
        f"✅  <b>Час роботи збережено:</b> {from_time} — {raw}{night}",
        reply_markup=main_menu_kb(msg.from_user),
    )
    text, kb = _schedule_screen(data)
    await msg.answer(text, reply_markup=kb)


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


@router.callback_query(F.data.in_({"sched:noop", "noop"}))
async def cb_noop(call: types.CallbackQuery) -> None:
    await call.answer()


# ─────────────────────── Старі кнопки ───────────────────────
# Повідомлення зі старим меню налаштувань лишаються в чатах користувачів —
# без цього хендлера їхні кнопки «крутилися» б без відповіді.

@router.callback_query(F.data.startswith("bset:"))
async def cb_stale(call: types.CallbackQuery) -> None:
    await call.answer(
        f"Меню налаштувань оновилось. Відкрийте «{BTN_BROADCAST}» ще раз.", show_alert=True,
    )

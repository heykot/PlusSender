"""JSON-сховище: профілі користувачів, адміни, цілі розсилки."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from aiogram import types

from .config import ADMINS_FILE, PAYMENTS_FILE, USERS_DIR, SESSIONS_DIR
from .utils import (
    normalize_optional_text,
    normalize_delay_seconds,
)

log = logging.getLogger(__name__)


# ===================== Шляхи до файлів =====================
# Профіль і сесія прив'язані до незмінного Telegram user_id, а не до @username:
# username можна змінити або звільнити, і тоді новий власник отримав би
# чужий профіль та чужу Telethon-сесію.
def user_file_path_for_id(user_id: int) -> str:
    return os.path.join(USERS_DIR, f"{int(user_id)}.json")


def session_path_for_id(user_id: int) -> str:
    return os.path.join(SESSIONS_DIR, str(int(user_id)))


def session_file_path_for_id(user_id: int) -> str:
    return f"{session_path_for_id(user_id)}.session"


def user_file_path(user: types.User) -> str:
    return user_file_path_for_id(user.id)


def session_path(user: types.User) -> str:
    return session_path_for_id(user.id)


def session_file_path(user: types.User) -> str:
    return session_file_path_for_id(user.id)


def has_session(user: types.User) -> bool:
    return os.path.isfile(session_file_path(user))


_SESSION_SUFFIXES = (".session", ".session-journal", ".session-wal", ".session-shm")


def _move_session_files(old_stem: str, new_dir: Path, new_stem: str) -> None:
    for suffix in _SESSION_SUFFIXES:
        src = Path(SESSIONS_DIR) / f"{old_stem}{suffix}"
        if src.exists():
            new_dir.mkdir(parents=True, exist_ok=True)
            os.replace(src, new_dir / f"{new_stem}{suffix}")


def migrate_profiles_to_user_id() -> None:
    """Одноразова міграція `user_data/<username>.json` → `user_data/<user_id>.json`
    (і відповідних .session-файлів).

    Якщо в одного user_id кілька профілів (користувач міняв username), основним
    стає той, де є сесія (далі — найсвіжіший); з інших підтягуються найпізніший
    access_until і реферальні поля, а самі файли переносяться в `_legacy/`.
    """
    users_dir = Path(USERS_DIR)
    if not users_dir.is_dir():
        return

    groups: dict[int, list[Path]] = {}
    for path in users_dir.glob("*.json"):
        data = load_user_json(str(path))
        raw_uid = data.get("user_id")
        if not raw_uid and path.stem.startswith("user_"):
            raw_uid = path.stem[len("user_"):]
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            log.warning("migrate: %s без user_id — пропускаю", path.name)
            continue
        groups.setdefault(uid, []).append(path)

    legacy_users = users_dir / "_legacy"
    legacy_sessions = Path(SESSIONS_DIR) / "_legacy"
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")

    for uid, paths in groups.items():
        target = users_dir / f"{uid}.json"
        if paths == [target]:
            continue

        def _rank(p: Path) -> tuple[bool, float]:
            has_sess = (Path(SESSIONS_DIR) / f"{p.stem}.session").is_file()
            return has_sess, p.stat().st_mtime

        primary = max(paths, key=_rank)
        merged = load_user_json(str(primary))
        merged["user_id"] = uid

        for other in paths:
            if other == primary:
                continue
            od = load_user_json(str(other))
            if (get_access_until(od) or datetime.min) > (get_access_until(merged) or datetime.min):
                merged["access_until"] = od["access_until"]
            for key in ("referrer_id", "referral_rewarded"):
                if od.get(key) and not merged.get(key):
                    merged[key] = od[key]
            legacy_users.mkdir(parents=True, exist_ok=True)
            os.replace(other, legacy_users / f"{other.stem}.{stamp}.json")
            _move_session_files(other.stem, legacy_sessions, f"{other.stem}.{stamp}")
            log.warning("migrate: uid=%d дублікат %s → _legacy/", uid, other.name)

        save_user_json(str(target), merged)
        if primary != target:
            primary.unlink(missing_ok=True)
            _move_session_files(primary.stem, Path(SESSIONS_DIR), str(uid))
            log.info("migrate: %s → %s", primary.name, target.name)


# ===================== Базове R/W =====================
def load_user_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _atomic_write_json(path: str, data, indent: Optional[int], default=None) -> None:
    """Пише JSON через тимчасовий файл + os.replace, щоб падіння посеред
    запису не лишило битий файл."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json.part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent, default=default)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_user_json(path: str, data: dict) -> None:
    _atomic_write_json(path, data, indent=4)


def load_user(user: types.User) -> dict:
    return load_user_json(user_file_path(user))


def save_user(user: types.User, data: dict) -> None:
    save_user_json(user_file_path(user), data)


def update_user(user: types.User, **fields) -> dict:
    data = load_user(user)
    data.update(fields)
    save_user(user, data)
    return data


def refresh_user_meta(user: types.User) -> None:
    """Оновлює user_id, username, first_name, last_name у профілі."""
    data = load_user(user)
    data["user_id"]    = user.id
    data["username"]   = user.username or ""
    data["first_name"] = user.first_name or ""
    data["last_name"]  = user.last_name or ""
    save_user(user, data)


# ===================== Статус активності =====================
def get_status(user: types.User) -> bool:
    return bool(load_user(user).get("status", False))


def set_status(user: types.User, value: bool) -> None:
    data = load_user(user)
    data["status"] = bool(value)
    save_user(user, data)


# ===================== Цілі розсилки =====================
def get_targets(data: dict) -> list[int]:
    raw = data.get("targets")
    targets: list[int] = []
    if isinstance(raw, list):
        for value in raw:
            try:
                pid = int(value)
            except (TypeError, ValueError):
                continue
            if pid not in targets:
                targets.append(pid)
    if not targets and data.get("target") is not None:
        try:
            targets = [int(data["target"])]
        except (TypeError, ValueError):
            targets = []
    return targets


def get_targets_meta(data: dict) -> dict[int, dict]:
    raw = data.get("targets_meta")
    out: dict[int, dict] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                pid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, dict):
                out[pid] = {
                    "title": v.get("title"),
                    "username": v.get("username"),
                    "kind": v.get("kind"),
                }
    return out


def get_target_messages(data: dict) -> dict[int, dict[str, object]]:
    raw = data.get("target_messages")
    result: dict[int, dict[str, object]] = {}
    if not isinstance(raw, dict):
        return result

    for key, value in raw.items():
        try:
            pid = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value:
            result[pid] = dict(value)
    return result


def set_target_messages(data: dict, messages: dict[int, dict[str, object]]) -> None:
    data["target_messages"] = {
        str(pid): value
        for pid, value in messages.items()
        if isinstance(value, dict) and value
    }


# ===================== Медіа-повідомлення (глобальний дефолт) =====================
def get_default_media(data: dict, mode: str) -> Optional[dict]:
    """Повертає {kind, path, caption} або None."""
    return data.get(f"{mode}_media") or None


def set_default_media(
    data: dict, mode: str, kind: str, path: str, caption: Optional[str] = None
) -> None:
    """Зберігає медіа і очищає текстовий дефолт для цього режиму."""
    data[f"{mode}_media"] = {"kind": kind, "path": str(path), "caption": caption}
    # Текст ↔ медіа є взаємовиключними для дефолту
    if mode == "alert":
        data.pop("message_text_alert", None)
        data.pop("message_text", None)
    elif mode == "clear":
        data.pop("message_text_clear", None)


def clear_default_media(data: dict, mode: str) -> None:
    data.pop(f"{mode}_media", None)


# ===================== Медіа для конкретного чату =====================
def get_target_media(data: dict, pid: int, mode: str) -> Optional[dict]:
    """Повертає медіа-конфіг {kind, path, caption} для конкретного чату або None."""
    target_messages = get_target_messages(data)
    item = target_messages.get(pid) or {}
    return item.get(f"{mode}_media") or None


def set_target_media(
    data: dict, pid: int, mode: str, kind: str, path: str, caption: Optional[str] = None
) -> None:
    target_messages = get_target_messages(data)
    item = dict(target_messages.get(pid) or {})
    item[f"{mode}_media"] = {"kind": kind, "path": str(path), "caption": caption}
    item.pop(mode, None)  # прибираємо текст для цього режиму
    target_messages[pid] = item
    set_target_messages(data, target_messages)


def clear_target_media(data: dict, pid: int, mode: str) -> None:
    target_messages = get_target_messages(data)
    item = dict(target_messages.get(pid) or {})
    item.pop(f"{mode}_media", None)
    target_messages[pid] = item
    set_target_messages(data, target_messages)


# ===================== Медіа-чат-джерело =====================
def get_media_source(data: dict, mode: str) -> Optional[dict]:
    """Повертає {chat_id, title} або None."""
    return data.get(f"{mode}_media_chat") or None


def set_media_source(data: dict, mode: str, chat_id: int, title: str) -> None:
    data[f"{mode}_media_chat"] = {"chat_id": chat_id, "title": title}
    data.pop(f"{mode}_media_chat_used", None)   # скидаємо лічильник при зміні джерела


def clear_media_source(data: dict, mode: str) -> None:
    data.pop(f"{mode}_media_chat", None)
    data.pop(f"{mode}_media_chat_used", None)


def get_used_media_ids(data: dict, mode: str) -> list[int]:
    raw = data.get(f"{mode}_media_chat_used")
    if isinstance(raw, list):
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError):
            return []
    return []


def mark_media_used(data: dict, mode: str, msg_id: int, total: int) -> None:
    """Позначає повідомлення використаним. Скидає список коли всі пройдені."""
    used = get_used_media_ids(data, mode)
    if msg_id not in used:
        used.append(msg_id)
    if len(used) >= total:
        used = []           # всі показані — починаємо нове коло
    data[f"{mode}_media_chat_used"] = used


def media_source_progress(data: dict, mode: str) -> tuple[int, int]:
    """Повертає (показано_у_поточному_колі, всього) або (0, 0) якщо немає джерела."""
    if not get_media_source(data, mode):
        return 0, 0
    used = get_used_media_ids(data, mode)
    return len(used), 0     # total невідомий без Telethon — підставляється ззовні


# ===================== Локальна бібліотека (файли, завантажені ботом) =====================
def get_source_lib(data: dict, mode: str) -> list[dict]:
    """Повертає [{path, kind}, ...] — файли, завантажені ботом із source-чату."""
    raw = data.get(f"{mode}_source_lib")
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def add_source_lib_file(data: dict, mode: str, path: str, kind: str) -> None:
    lib = get_source_lib(data, mode)
    if not any(e.get("path") == path for e in lib):
        lib.append({"path": path, "kind": kind})
    data[f"{mode}_source_lib"] = lib
    # скидаємо "використані" індекси при новому файлі — щоб він міг потрапити в наступний цикл
    used_raw = data.get(f"{mode}_source_lib_used")
    if isinstance(used_raw, list) and len(used_raw) >= len(lib):
        data[f"{mode}_source_lib_used"] = []


def clear_source_lib(data: dict, mode: str) -> None:
    """Видаляє всі файли бібліотеки з диску і очищає JSON-запис."""
    from pathlib import Path as _Path
    for e in get_source_lib(data, mode):
        try:
            _Path(str(e.get("path", ""))).unlink(missing_ok=True)
        except Exception:
            pass
    data.pop(f"{mode}_source_lib", None)
    data.pop(f"{mode}_source_lib_used", None)


def source_lib_count(data: dict, mode: str) -> int:
    from pathlib import Path as _Path
    return sum(1 for e in get_source_lib(data, mode) if _Path(str(e.get("path", ""))).is_file())


def pick_source_lib_file(data: dict, mode: str) -> Optional[dict]:
    """Вибирає наступний файл по колу (без повторень). Змінює data in-place. Повертає {path,kind} або None."""
    import random as _random
    from pathlib import Path as _Path

    lib = get_source_lib(data, mode)
    valid = [i for i, e in enumerate(lib) if _Path(str(e.get("path", ""))).is_file()]
    if not valid:
        return None

    used_raw = data.get(f"{mode}_source_lib_used")
    used: set[int] = set()
    if isinstance(used_raw, list):
        try:
            used = {int(x) for x in used_raw}
        except (TypeError, ValueError):
            used = set()

    avail = [i for i in valid if i not in used]
    if not avail:
        used = set()          # всі показані — починаємо нове коло
        avail = valid

    idx = _random.choice(avail)
    used.add(idx)
    if used >= set(valid):
        used = set()           # всі пройдені — скидаємо

    data[f"{mode}_source_lib_used"] = list(used)
    return lib[idx]


def sync_targets(data: dict, targets: list[int], meta: dict[int, dict]) -> None:
    """Перезаписує targets/targets_meta/target_messages у data, прибираючи лишнє."""
    uniq: list[int] = []
    for pid in targets:
        if pid not in uniq:
            uniq.append(pid)

    data["targets"] = uniq
    data["targets_meta"] = {
        str(pid): meta[pid]
        for pid in uniq
        if isinstance(meta.get(pid), dict)
    }

    current = get_target_messages(data)
    set_target_messages(data, {pid: current[pid] for pid in uniq if pid in current})

    if uniq:
        first = uniq[0]
        data["target"] = first
        first_meta = meta.get(first, {}) or {}
        if first_meta.get("title"):
            data["target_name"] = first_meta["title"]
        if first_meta.get("username"):
            data["target_username"] = first_meta["username"]
        if first_meta.get("kind"):
            data["target_kind"] = first_meta["kind"]
    else:
        for k in ("target", "target_name", "target_username", "target_kind"):
            data.pop(k, None)


# ===================== Медіа-джерело для конкретного чату =====================
def get_target_media_source(data: dict, pid: int, mode: str) -> Optional[dict]:
    """Повертає {chat_id, title} або None для конкретного цільового чату."""
    item = get_target_messages(data).get(pid) or {}
    return item.get(f"{mode}_media_chat") or None


def set_target_media_source(data: dict, pid: int, mode: str, chat_id: int, title: str) -> None:
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    item[f"{mode}_media_chat"] = {"chat_id": chat_id, "title": title}
    item.pop(f"{mode}_media_chat_used", None)   # скидаємо лічильник
    tms[pid] = item
    set_target_messages(data, tms)


def clear_target_media_source(data: dict, pid: int, mode: str) -> None:
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    item.pop(f"{mode}_media_chat", None)
    item.pop(f"{mode}_media_chat_used", None)
    tms[pid] = item
    set_target_messages(data, tms)


def get_target_chat_used_ids(data: dict, pid: int, mode: str) -> list[int]:
    item = get_target_messages(data).get(pid) or {}
    raw = item.get(f"{mode}_media_chat_used")
    if isinstance(raw, list):
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError):
            return []
    return []


def mark_target_chat_media_used(data: dict, pid: int, mode: str, msg_id: int, total: int) -> None:
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    raw = item.get(f"{mode}_media_chat_used")
    used: list[int] = []
    if isinstance(raw, list):
        try:
            used = [int(x) for x in raw]
        except (TypeError, ValueError):
            used = []
    if msg_id not in used:
        used.append(msg_id)
    if len(used) >= total:
        used = []
    item[f"{mode}_media_chat_used"] = used
    tms[pid] = item
    set_target_messages(data, tms)


# ===================== Тексти/затримки на чат =====================
def message_for_target(data: dict, pid: int, mode: str) -> Optional[str]:
    """Повертає текст для конкретного чату або дефолтний; None — не надсилати."""
    from .utils import default_message_text  # уникаємо циклічного імпорту

    target_messages = get_target_messages(data)
    item = target_messages.get(pid, {})
    if mode in item:
        explicit = normalize_optional_text(item.get(mode))
        return explicit if explicit else None
    return default_message_text(data, mode)


def delay_for_target(data: dict, pid: int, mode: str) -> int:
    from .utils import default_delay_seconds

    key = f"{mode}_delay_seconds"
    target_messages = get_target_messages(data)
    value = target_messages.get(pid, {}).get(key)
    if value is not None:
        return normalize_delay_seconds(value, default_delay_seconds(data, mode))
    return default_delay_seconds(data, mode)


# ===================== Доступ =====================
def get_access_until(data: dict) -> Optional[datetime]:
    val = data.get("access_until")
    if not val:
        return None
    try:
        return datetime.strptime(str(val), "%Y-%m-%d")
    except ValueError:
        return None


def access_days_left(data: dict) -> Optional[int]:
    """Скільки днів доступу лишилось: 0 — сьогодні останній день; None — доступу немає.

    Доступ діє ВКЛЮЧНО з датою access_until: «до 26.10» означає весь день 26.10.
    """
    until = get_access_until(data)
    if not until:
        return None
    left = (until.date() - datetime.now().date()).days
    return left if left >= 0 else None


def access_active(data: dict) -> bool:
    return access_days_left(data) is not None


def has_access(user: types.User) -> bool:
    return access_active(load_user(user))


def set_access_for_user_id(user_id: int, date_str: str) -> bool:
    for path in iter_user_files():
        data = load_user_json(path)
        if data.get("user_id") == user_id:
            data["access_until"] = date_str
            save_user_json(path, data)
            return True
    return False


def grant_access_days(user_id: int, days: int) -> Optional[str]:
    until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    return until if set_access_for_user_id(user_id, until) else None


def revoke_access(user_id: int) -> bool:
    for path in iter_user_files():
        data = load_user_json(path)
        if data.get("user_id") == user_id:
            data["access_until"] = None
            save_user_json(path, data)
            return True
    return False


# ===================== Усі користувачі =====================
def iter_user_files() -> Iterator[str]:
    if not os.path.isdir(USERS_DIR):
        return iter([])
    return (
        os.path.join(USERS_DIR, f)
        for f in os.listdir(USERS_DIR)
        if f.endswith(".json")
    )


def delete_user_profile(user_id: int) -> bool:
    """Видаляє JSON-профіль і .session файл користувача. Повертає True якщо знайдено."""
    import glob as _glob
    from pathlib import Path as _Path

    found = False
    for path in iter_user_files():
        data = load_user_json(path)
        if data.get("user_id") == user_id:
            username = _Path(path).stem
            # Видаляємо JSON
            try:
                _Path(path).unlink(missing_ok=True)
            except Exception:
                pass
            # Видаляємо .session
            sess = _Path(SESSIONS_DIR) / f"{username}.session"
            try:
                sess.unlink(missing_ok=True)
            except Exception:
                pass
            # Додаткові файли сесії (-journal, -wal, -shm)
            for ext in ("-journal", "-wal", "-shm"):
                try:
                    (_Path(SESSIONS_DIR) / f"{username}.session{ext}").unlink(missing_ok=True)
                except Exception:
                    pass
            found = True
            break
    return found


def toggle_user_status(user_id: int, value: bool) -> bool:
    """Встановлює статус активності для користувача за ID. Повертає True якщо знайдено."""
    for path in iter_user_files():
        data = load_user_json(path)
        if data.get("user_id") == user_id:
            data["status"] = bool(value)
            save_user_json(path, data)
            return True
    return False


def extend_access_days(user_id: int, days: int) -> Optional[str]:
    """Додає N днів до поточного access_until (або від сьогодні якщо прострочено)."""
    for path in iter_user_files():
        data = load_user_json(path)
        if data.get("user_id") == user_id:
            current = data.get("access_until")
            try:
                base = datetime.strptime(str(current), "%Y-%m-%d")
                # Доступ діє включно з датою: прострочений — рахуємо від сьогодні
                if base.date() < datetime.now().date():
                    base = datetime.now()
            except (ValueError, TypeError):
                base = datetime.now()
            until = (base + timedelta(days=days)).strftime("%Y-%m-%d")
            data["access_until"] = until
            save_user_json(path, data)
            return until
    return None


def ensure_profile_for_id(user_id: int) -> None:
    """Створює мінімальний профіль, якщо користувач ще не натискав /start
    (наприклад, оплатив раніше), щоб видане право доступу не загубилось."""
    path = user_file_path_for_id(user_id)
    if not os.path.isfile(path):
        save_user_json(path, {"user_id": int(user_id)})


def load_all_users() -> dict[int, dict]:
    users: dict[int, dict] = {}
    for path in iter_user_files():
        data = load_user_json(path)
        uid = data.get("user_id")
        if uid:
            users[int(uid)] = data
    return users


# ===================== Розклад роботи =====================
def get_schedule(data: dict) -> dict:
    """Повертає {enabled, from_time, to_time}."""
    return {
        "enabled":   bool(data.get("schedule_enabled", False)),
        "from_time": str(data.get("schedule_from") or "00:00"),
        "to_time":   str(data.get("schedule_to")   or "23:59"),
    }


def set_schedule(data: dict, enabled: bool, from_time: str, to_time: str) -> None:
    data["schedule_enabled"] = bool(enabled)
    data["schedule_from"] = from_time
    data["schedule_to"]   = to_time


def is_in_schedule(data: dict) -> bool:
    """True — надсилати зараз. Підтримує нічний діапазон (22:00–06:00)."""
    if not data.get("schedule_enabled"):
        return True

    from_str = str(data.get("schedule_from") or "00:00")
    to_str   = str(data.get("schedule_to")   or "23:59")

    now = datetime.now()
    now_m = now.hour * 60 + now.minute

    try:
        fh, fm = map(int, from_str.split(":"))
        th, tm = map(int, to_str.split(":"))
    except (ValueError, AttributeError):
        return True

    f_m = fh * 60 + fm
    t_m = th * 60 + tm

    if f_m <= t_m:
        return f_m <= now_m <= t_m          # звичайний діапазон
    else:
        return now_m >= f_m or now_m <= t_m  # нічний (22:00–06:00)


# ===================== Per-target тип розсилки ("text" | "forward" | "none") =====================
def get_target_type(data: dict, pid: int, mode: str) -> Optional[str]:
    """Повертає "text", "forward", "none" або None (використовувати глобальний дефолт)."""
    item = get_target_messages(data).get(pid) or {}
    val = item.get(f"{mode}_type")
    return str(val) if val in {"text", "forward", "none"} else None


def set_target_type(data: dict, pid: int, mode: str, type_str: str) -> None:
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    item[f"{mode}_type"] = type_str
    tms[pid] = item
    set_target_messages(data, tms)


# ===================== Per-target чат-джерело для пересилання =====================
def get_target_forward_source(data: dict, pid: int, mode: str) -> Optional[dict]:
    """Повертає {chat_id, title} або None."""
    item = get_target_messages(data).get(pid) or {}
    chat_id = item.get(f"{mode}_forward_chat_id")
    title = item.get(f"{mode}_forward_chat_title")
    if chat_id:
        return {"chat_id": int(chat_id), "title": str(title or chat_id)}
    return None


def set_target_forward_source(data: dict, pid: int, mode: str, chat_id: int, title: str) -> None:
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    item[f"{mode}_forward_chat_id"] = chat_id
    item[f"{mode}_forward_chat_title"] = title
    item.pop(f"{mode}_forward_used", None)   # скидаємо round-robin
    item[f"{mode}_type"] = "forward"
    tms[pid] = item
    set_target_messages(data, tms)


def get_target_forward_mode(data: dict, pid: int, mode: str) -> str:
    """Повертає режим відправки кружків: 'roundrobin' або 'delete'. Default: 'roundrobin'."""
    item = get_target_messages(data).get(pid) or {}
    return str(item.get(f"{mode}_forward_mode") or "roundrobin")


def set_target_forward_mode(data: dict, pid: int, mode: str, fwd_mode: str) -> None:
    """Встановлює режим відправки кружків: 'roundrobin' або 'delete'."""
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    item[f"{mode}_forward_mode"] = fwd_mode
    item.pop(f"{mode}_forward_used", None)  # скидаємо лічильник при зміні режиму
    tms[pid] = item
    set_target_messages(data, tms)


def clear_target_forward_source(data: dict, pid: int, mode: str) -> None:
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    item.pop(f"{mode}_forward_chat_id", None)
    item.pop(f"{mode}_forward_chat_title", None)
    item.pop(f"{mode}_forward_used", None)
    if item.get(f"{mode}_type") == "forward":
        item.pop(f"{mode}_type", None)
    tms[pid] = item
    set_target_messages(data, tms)


def get_target_forward_used(data: dict, pid: int, mode: str) -> list[int]:
    item = get_target_messages(data).get(pid) or {}
    raw = item.get(f"{mode}_forward_used")
    if isinstance(raw, list):
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError):
            return []
    return []


def mark_target_forward_used(data: dict, pid: int, mode: str, msg_id: int, total: int) -> None:
    """Зберігає ID відправленого кружка. Скидає список коли всі відправлено."""
    tms = get_target_messages(data)
    item = dict(tms.get(pid) or {})
    raw = item.get(f"{mode}_forward_used")
    used: list[int] = []
    if isinstance(raw, list):
        try:
            used = [int(x) for x in raw]
        except (TypeError, ValueError):
            used = []
    if msg_id not in used:
        used.append(msg_id)
    # Скидаємо тільки коли відправили всі доступні кружки
    if total > 0 and len(used) >= total:
        used = []
    item[f"{mode}_forward_used"] = used
    tms[pid] = item
    set_target_messages(data, tms)


def record_forward_used(json_path: str, pid: int, mode: str, msg_id: int, total: int) -> None:
    """Зберігає відправлений кружок, перечитавши профіль з диска.

    Розсилка тримає копію профілю від свого старту і може чекати затримку
    хвилинами — якщо записати ту копію, пропадуть зміни, які користувач
    зробив у боті за цей час. Тому оновлюємо лише лічильник у свіжих даних.
    """
    fresh = load_user_json(json_path)
    if not fresh:
        return
    mark_target_forward_used(fresh, pid, mode, msg_id, total)
    save_user_json(json_path, fresh)


def reset_target_config(data: dict, pid: int) -> None:
    """Повністю скидає індивідуальні налаштування чату."""
    tms = get_target_messages(data)
    tms.pop(pid, None)
    set_target_messages(data, tms)
    # Також медіа
    clear_target_media(data, pid, "alert")
    clear_target_media(data, pid, "clear")


# ===================== Реферальна програма =====================
def get_referrer(data: dict) -> Optional[int]:
    """Повертає user_id того, хто запросив цього користувача, або None."""
    raw = data.get("referrer_id")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def set_referrer(user: types.User, referrer_id: int) -> bool:
    """Зберігає referrer_id у профілі.

    Правила:
      • Тільки для нового користувача: профілю ще немає або доступ ще жодного
        разу не видавався. Інакше вже платний клієнт міг би «прив'язатися» до
        друга, і той отримав би бонус за продовження, а не за першу покупку.
      • Заборонено self-referral.
      • Якщо referrer_id вже встановлений — не перезаписуємо.

    Повертає True якщо зберегли, False якщо проігнорували.
    """
    if not referrer_id or referrer_id == user.id:
        return False

    path = user_file_path(user)
    if os.path.isfile(path):
        # Профіль уже існує — не дозволяємо змінювати реферера
        data = load_user_json(path)
        if data.get("referrer_id") or data.get("access_until"):
            return False
        data["referrer_id"] = int(referrer_id)
        save_user_json(path, data)
        return True

    # Новий користувач — створюємо профіль з referrer_id
    data = {
        "user_id": user.id,
        "user_name": user.username,
        "referrer_id": int(referrer_id),
    }
    save_user_json(path, data)
    return True


def is_referral_rewarded(data: dict) -> bool:
    """Чи отримав запрошувач уже нагороду за цього юзера?"""
    return bool(data.get("referral_rewarded"))


def mark_referral_rewarded(user_id: int) -> bool:
    """Позначає у профілі запрошеного, що нагорода реферу вже видана.
    Повертає True якщо знайшли і позначили."""
    for path in iter_user_files():
        data = load_user_json(path)
        if data.get("user_id") == user_id:
            data["referral_rewarded"] = True
            save_user_json(path, data)
            return True
    return False


def count_referrals_for(referrer_id: int) -> tuple[int, int]:
    """Повертає (всього_запрошено, з_них_купили_тариф) для конкретного запрошувача."""
    total = 0
    paid = 0
    for path in iter_user_files():
        data = load_user_json(path)
        if data.get("referrer_id") == referrer_id:
            total += 1
            if data.get("referral_rewarded"):
                paid += 1
    return total, paid


# ===================== Оброблені платежі =====================
def _load_processed_payments() -> dict[str, dict]:
    try:
        with open(PAYMENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def is_payment_processed(item_id: str) -> bool:
    return str(item_id) in _load_processed_payments()


def mark_payment_processed(item_id: str, **info) -> None:
    payments = _load_processed_payments()
    payments[str(item_id)] = {"at": datetime.now().isoformat(timespec="seconds"), **info}
    _atomic_write_json(str(PAYMENTS_FILE), payments, indent=2)


# ===================== Адміни =====================
def load_admins() -> dict[int, str]:
    if not os.path.isfile(ADMINS_FILE):
        os.makedirs(os.path.dirname(ADMINS_FILE) or ".", exist_ok=True)
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump({"admins": []}, f, ensure_ascii=False, indent=2)
        return {}

    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        result: dict[int, str] = {}
        for a in data.get("admins", []):
            if isinstance(a, dict) and "id" in a:
                result[int(a["id"])] = a.get("username", "—")
        return result
    except Exception:
        return {}


def save_admins(admins: dict[int, str]) -> None:
    payload = {"admins": [{"id": uid, "username": uname} for uid, uname in admins.items()]}
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def is_admin(uid: int) -> bool:
    return uid in load_admins()

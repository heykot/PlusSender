"""FSM-стани aiogram для майстрів (підключення, налаштування, адмінка)."""
from aiogram.fsm.state import State, StatesGroup


class ConnectStates(StatesGroup):
    """Майстер підключення Telegram-сесії для нового користувача."""
    waiting_credentials = State()   # api_id + api_hash в одному кроці (або через :)
    waiting_phone = State()         # +380...
    waiting_code = State()          # код з Telegram (підтримка пробілів/тире)
    waiting_password = State()      # 2FA-пароль


class BroadcastStates(StatesGroup):
    """Майстер налаштування розсилки."""
    waiting_search_query = State()

    # Per-target: введення тексту повідомлення
    waiting_target_mode_text = State()     # FSM: target_pid, target_mode
    waiting_target_text_delay = State()    # FSM: target_pid, target_mode, pending_text/media

    # Розклад роботи
    waiting_schedule_from = State()   # FSM: — юзер вводить час початку
    waiting_schedule_to   = State()   # FSM: pending_from — юзер вводить час кінця

    # Per-target: вибір чату-джерела через список діалогів Telethon
    waiting_target_src_search = State()    # FSM: target_pid, target_mode — юзер вводить пошук
    waiting_forward_mode = State()         # FSM: target_pid, target_mode (source вже збережено, обирається режим)
    waiting_target_forward_delay = State() # FSM: target_pid, target_mode (source + mode вже збережено)


class SupportStates(StatesGroup):
    waiting_message = State()   # юзер пише повідомлення в підтримку


class AdminStates(StatesGroup):
    waiting_broadcast_text = State()
    waiting_access_days = State()
    waiting_access_date = State()      # встановити точну дату доступу
    waiting_user_message = State()     # написати конкретному юзеру
    waiting_new_admin_id = State()     # додати адміна через UI

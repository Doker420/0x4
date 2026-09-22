from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💼 Портфель", callback_data="portfolio"))
    b.row(
        InlineKeyboardButton(text="👛 Кошельки", callback_data="wallets"),
        InlineKeyboardButton(text="🔑 xpub", callback_data="xpubs"),
    )
    b.row(InlineKeyboardButton(text="🔔 Мониторинг", callback_data="watches"))
    b.row(
        InlineKeyboardButton(text="📊 История", callback_data="history"),
        InlineKeyboardButton(text="📤 CSV", callback_data="export_csv"),
    )
    b.row(
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        InlineKeyboardButton(text="💎 Тариф", callback_data="tariffs"),
    )
    return b.as_markup()


def back(to: str = "back_main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=to))
    return b.as_markup()


def wallets_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить адрес", callback_data="wallet_add"))
    b.row(InlineKeyboardButton(text="📋 Список", callback_data="wallet_list"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main"))
    return b.as_markup()


def xpubs_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить xpub", callback_data="xpub_add"))
    b.row(InlineKeyboardButton(text="📋 Мои xpub", callback_data="xpub_list"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main"))
    return b.as_markup()


def watches_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить", callback_data="watch_add"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main"))
    return b.as_markup()


def chain_picker(chains: list[str], prefix: str, back_to: str,
                 per_row: int = 3) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for c in chains:
        row.append(InlineKeyboardButton(text=c, callback_data=f"{prefix}:{c}"))
        if len(row) == per_row:
            b.row(*row)
            row = []
    if row:
        b.row(*row)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_to))
    return b.as_markup()


def direction_picker() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="⬇️ Входящие", callback_data="dir:in"),
        InlineKeyboardButton(text="⬆️ Исходящие", callback_data="dir:out"),
        InlineKeyboardButton(text="↕️ Оба", callback_data="dir:both"),
    )
    return b.as_markup()


def settings_menu(notify_on: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    state = "включены ✅" if notify_on else "выключены ❌"
    b.row(InlineKeyboardButton(
        text=f"🔔 Уведомления: {state}", callback_data="toggle_notify"))
    b.row(InlineKeyboardButton(text="🔄 Обновить API-ключ", callback_data="rotate_key"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main"))
    return b.as_markup()

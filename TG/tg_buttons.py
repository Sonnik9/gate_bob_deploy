# TG/tg_interface.py
import asyncio
import copy
from typing import *

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from a_config import *
from b_context import BotContext
from c_log import ErrorHandler
from .tg_notifier import TelegramNotifier


# ============================================================
#  VALIDATION & FORMATTING
# ============================================================

def validate_user_config(user_cfg: dict) -> bool:
    """
    Проверяет корректность конфигурации пользователя.
    """
    config = user_cfg.setdefault("config", {})
    fin = config.setdefault("fin_settings", {})

    gate = config.get("GATE", {})
    if not gate.get("api_key") or not gate.get("api_secret"):
        return False

    # Проверка веток soft / trading pair
    for section in ("soft", "trading pair"):
        branch = fin.get(section, {})
        if not branch:
            return False

        required_fields = {
            "margin_size": (float, int),
            "margin_mode": int,
            "order_type": int,
            "trigger_order_type": int,
            "order_timeout": int,
            # "leverage": int,
        }
        for field, allowed_types in required_fields.items():
            val = branch.get(field)
            if val in (None, "", 0):
                return False
            if not isinstance(val, allowed_types):
                return False
    return True


def format_config(cfg: dict, indent: int = 0) -> str:
    lines = []
    pad = "  " * indent

    for k, v in cfg.items():
        # Если вложенный словарь — рекурсивно
        if isinstance(v, dict):
            lines.append(f"{pad}• {k}:")
            lines.append(format_config(v, indent + 1))
            continue

        # Специальная логика для конкретных ключей
        display_value = v
        if k == "dop_tp":
            display_value = v if v is not None else "не задан, по умолчанию 100"
        elif k == "margin_mode":
            display_value = "Изолированная" if v == 1 else "Кросс" if v == 2 else v
        elif k == "trigger_order_type":
            display_value = "Лимитный" if v == 1 else "Рыночный" if v == 2 else v
        elif k == "order_type":
            display_value = "Лимитный" if v == 1 else "Рыночный" if v == 2 else v
        elif k == "leverage":
            display_value = f"{v}x"
        elif k == "order_timeout":
            display_value = f"{v} сек"
        elif isinstance(v, str) and k.lower() == "api_secret":
            display_value = f"{v[:5]}•••••••••••"  # скрываем ключи

        lines.append(f"{pad}• {k}: {display_value}")

    return "\n".join(lines)


# ============================================================
#  TELEGRAM UI CLASS
# ============================================================

class TelegramUserInterface:
    """
    UI-слой. ВАЖНО: ВЕЗДЕ используем chat_id как единый ключ.
    Это исключает рассинхрон с TelegramNotifier / order_buttons_handler / context.*.
    """

    def __init__(
        self,
        bot: Bot,
        dp: Dispatcher,
        context: BotContext,
        info_handler: ErrorHandler,
        notifier: TelegramNotifier
    ):
        self.bot = bot
        self.dp = dp
        self.context = context
        self.info_handler = info_handler
        self.notifier = notifier

        self._polling_task: asyncio.Task | None = None
        self._stop_flag = False
        self.bot_iteration_lock = asyncio.Lock()

        # ===== Главное меню =====
        self.context.main_menu = types.ReplyKeyboardMarkup(
            keyboard=[
                [
                    types.KeyboardButton(text="🛠 Настройки"),
                    types.KeyboardButton(text="📊 Статус"),
                ],
                [
                    types.KeyboardButton(text="▶️ Старт"),
                    types.KeyboardButton(text="⏹ Стоп"),
                ],
            ],
            resize_keyboard=True,
            input_field_placeholder="Выберите действие…"
        )

    # ============================================================
    #  HANDLERS REGISTRATION
    # ============================================================

    def register_handlers(self):
        dp = self.dp

        # --- Команды ---
        dp.message.register(self.start_handler, Command("start"))

        # --- Кнопки и текстовые вводы ---
        dp.message.register(self.settings_cmd, self._text_contains(["настройки"]))
        dp.message.register(self.status_cmd, self._text_contains(["статус"]))
        dp.message.register(self.start_cmd, self._text_contains(["старт"]))
        dp.message.register(self.stop_cmd, self._text_contains(["стоп"]))

        # --- Текстовый ввод для ожидаемых полей ---
        dp.message.register(
            self.text_message_handler,
            lambda m: self._awaiting_input(m) and m.chat.type == "private"
        )

        # --- CALLBACK-кнопки (всё пространство UI:) ---
        dp.callback_query.register(self.settings_handler, F.data == "UI:SETTINGS")
        dp.callback_query.register(self.gate_settings_handler, F.data == "UI:SET_GATE")
        dp.callback_query.register(self.fin_settings_handler, F.data == "UI:SET_FIN")

        dp.callback_query.register(self.fin_soft_menu, F.data == "UI:SET_FIN_SOFT")
        dp.callback_query.register(self.fin_tpair_menu, F.data == "UI:SET_FIN_TPAIR")

        for section in ("soft", "trading pair"):
            upper = section.upper().replace(" ", "_")
            dp.callback_query.register(self._make_field_input(section, "margin_size"), F.data == f"UI:SET_{upper}_MARGIN")
            dp.callback_query.register(self._make_field_input(section, "margin_mode"), F.data == f"UI:SET_{upper}_MARGIN_MODE")
            dp.callback_query.register(self._make_field_input(section, "leverage"), F.data == f"UI:SET_{upper}_LEVERAGE")
            dp.callback_query.register(self._make_field_input(section, "order_type"), F.data == f"UI:SET_{upper}_ORDER_TYPE")
            dp.callback_query.register(self._make_field_input(section, "trigger_order_type"), F.data == f"UI:SET_{upper}_TRIGGER_ORDER_TYPE")
            dp.callback_query.register(self._make_field_input(section, "order_timeout"), F.data == f"UI:SET_{upper}_ORDER_TIMEOUT")
            dp.callback_query.register(self._make_field_input(section, "dop_tp"), F.data == f"UI:SET_{upper}_DOP_TP")

        dp.callback_query.register(self.api_key_input, F.data == "UI:SET_API_KEY")
        dp.callback_query.register(self.secret_key_input, F.data == "UI:SET_SECRET_KEY")


    # ============================================================
    #  BASIC COMMANDS
    # ============================================================

    async def start_handler(self, message: types.Message):
        chat_id = message.chat.id
        self.ensure_user_config(chat_id)
        await message.answer("Добро пожаловать! Главное меню снизу 👇", reply_markup=self.context.main_menu)

    async def settings_cmd(self, message: types.Message):
        chat_id = message.chat.id
        self.ensure_user_config(chat_id)
        await message.answer("Выберите раздел настроек:", reply_markup=self._settings_keyboard())

    async def status_cmd(self, message: types.Message):
        chat_id = message.chat.id
        self.ensure_user_config(chat_id)
        cfg = self.context.users_configs[chat_id]
        status = "В работе" if getattr(self.context, "start_bot_iteration", False) else "Не активен"
        pretty_cfg = format_config(cfg.get("config", {}))
        await message.answer(
            f"📊 Текущий статус: {status}\n\n⚙ Настройки:\n{pretty_cfg}",
            reply_markup=self.context.main_menu
        )

    async def start_cmd(self, message: types.Message):
        chat_id = message.chat.id
        self.ensure_user_config(chat_id)

        async with self.bot_iteration_lock:
            # Уже активен или есть открытые позиции
            if self.context.start_bot_iteration or any(
                pos.get("in_position", False)
                for symbol_data in self.context.position_vars.values()
                for side, pos in symbol_data.items()
                if side != "spec"
            ):
                await message.answer("Бот уже активен или имеет открытые позиции.", reply_markup=self.context.main_menu)
                return

            cfg = self.context.users_configs[chat_id]
            if validate_user_config(cfg):
                self.context.start_bot_iteration = True
                self.context.stop_bot_iteration = False
                await message.answer("✅ Бот ожидает сигнал.", reply_markup=self.context.main_menu)
            else:
                self.context.start_bot_iteration = False
                await message.answer("❗ Сначала настройте конфиг полностью", reply_markup=self.context.main_menu)

    async def stop_cmd(self, message: types.Message):
        chat_id = message.chat.id
        self.ensure_user_config(chat_id)

        async with self.bot_iteration_lock:
            # Проверяем, есть ли открытые позиции
            has_open_positions = any(
                pos.get("in_position", False)
                for symbol_data in self.context.position_vars.values()
                for side, pos in symbol_data.items()
                if side != "spec"
            )

            if has_open_positions:
                await message.answer(
                    "❗ Сначала закройте все открытые позиции.",
                    reply_markup=self.context.main_menu
                )
                return

            # Если бот запущен — останавливаем
            if self.context.start_bot_iteration:
                self.context.start_bot_iteration = False
                self.context.stop_bot_iteration = True
                await message.answer(
                    "⛔ Бот остановлен.",
                    reply_markup=self.context.main_menu
                )
            else:
                await message.answer(
                    "⚙️ Бот не запущен — остановка не требуется.",
                    reply_markup=self.context.main_menu
                )

    # ============================================================
    #  UTILS
    # ============================================================

    def _text_contains(self, keys: list[str]):
        def _f(message: types.Message) -> bool:
            return bool(message.text and any(k in message.text.lower() for k in keys))
        return _f

    def _awaiting_input(self, message: types.Message) -> bool:
        chat_id = message.chat.id
        cfg = self.context.users_configs.get(chat_id)
        return bool(cfg and cfg.get("_await_field"))

    def ensure_user_config(self, chat_id: int):
        """Создаёт структуру пользователя по chat_id, если её нет."""
        if chat_id not in self.context.users_configs:
            self.context.users_configs[chat_id] = copy.deepcopy(INIT_USER_CONFIG)
            self.context.queues_msg[chat_id] = []

    # ============================================================
    #  KEYBOARDS
    # ============================================================

    def _settings_keyboard(self):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 GATE", callback_data="UI:SET_GATE")],
            [InlineKeyboardButton(text="💰 FIN SETTINGS", callback_data="UI:SET_FIN")]
        ])

    def _gate_keyboard(self):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="API Key", callback_data="UI:SET_API_KEY")],
            [InlineKeyboardButton(text="Secret Key", callback_data="UI:SET_SECRET_KEY")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="UI:SETTINGS")]
        ])

    def _fin_keyboard(self):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Trading Pair", callback_data="UI:SET_FIN_TPAIR")],
            [InlineKeyboardButton(text="Soft", callback_data="UI:SET_FIN_SOFT")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="UI:SETTINGS")]
        ])

    def _fin_branch_keyboard(self, section: str):
        prefix = section.upper().replace(" ", "_")
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="margin size", callback_data=f"UI:SET_{prefix}_MARGIN")],
            [InlineKeyboardButton(text="margin mode", callback_data=f"UI:SET_{prefix}_MARGIN_MODE")],
            [InlineKeyboardButton(text="leverage", callback_data=f"UI:SET_{prefix}_LEVERAGE")],
            [InlineKeyboardButton(text="order type", callback_data=f"UI:SET_{prefix}_ORDER_TYPE")],
            [InlineKeyboardButton(text="trigger order type", callback_data=f"UI:SET_{prefix}_TRIGGER_ORDER_TYPE")],
            [InlineKeyboardButton(text="order timeout", callback_data=f"UI:SET_{prefix}_ORDER_TIMEOUT")],
            [InlineKeyboardButton(text="dop tp", callback_data=f"UI:SET_{prefix}_DOP_TP")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="UI:SET_FIN")]
        ])

    # ============================================================
    #  CALLBACK HANDLERS (ВСЕГДА chat_id!)
    # ============================================================

    async def settings_handler(self, callback: types.CallbackQuery):
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)
        await callback.message.edit_text("Выберите раздел настроек:", reply_markup=self._settings_keyboard())

    async def gate_settings_handler(self, callback: types.CallbackQuery):
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)
        await callback.message.edit_text("Настройки GATE:", reply_markup=self._gate_keyboard())

    async def fin_settings_handler(self, callback: types.CallbackQuery):
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)
        await callback.message.edit_text("FIN SETTINGS:", reply_markup=self._fin_keyboard())

    async def fin_soft_menu(self, callback: types.CallbackQuery):
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)
        await callback.message.edit_text("Настройки FIN SETTINGS / Soft:", reply_markup=self._fin_branch_keyboard("soft"))

    async def fin_tpair_menu(self, callback: types.CallbackQuery):
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)
        await callback.message.edit_text("Настройки FIN SETTINGS / Trading Pair:", reply_markup=self._fin_branch_keyboard("trading pair"))

    # ============================================================
    #  DYNAMIC FIELD INPUT
    # ============================================================

    def _make_field_input(self, section: str, field: str):
        async def handler(callback: types.CallbackQuery):
            if not callback.data.startswith("UI:"):
                return  # игнорируем чужие колбэки
            
        async def handler(callback: types.CallbackQuery):
            await callback.answer()
            chat_id = callback.message.chat.id
            self.ensure_user_config(chat_id)

            # ожидаем ввод в чате chat_id
            self.context.users_configs[chat_id]["_await_field"] = {
                "section": f"fin_settings.{section}",
                "field": field
            }

            # подсказки
            tips = {
                "order_type": "Тип ордера: 1 — лимитный, 2 — по маркету.",
                "trigger_order_type": "Тип триггерного ордера: 1 — лимитный, 2 — по маркету.",
                "margin_mode": "Режим маржи: 1 — изолированная, 2 — кросс.",
            }
            hint = tips.get(field, "Введите значение:")
            if field == "dop_tp":
                hint += " (1 - 100+)"
            await callback.message.answer(f"{hint}\nВведите значение для {field} ({section}):")
        return handler

    # ============================================================
    #  TEXT INPUT HANDLER (ожидание значения)
    # ============================================================

    async def text_message_handler(self, message: types.Message):
        chat_id = message.chat.id
        self.ensure_user_config(chat_id)
        cfg = self.context.users_configs[chat_id]
        field_info = cfg.get("_await_field")
        if not field_info:
            return  # нет ожидания

        section = field_info["section"]
        field = field_info["field"]
        raw = (message.text or "").strip()

        if section.startswith("fin_settings."):
            _, sub = section.split(".", 1)
            fs = cfg["config"].setdefault("fin_settings", {}).setdefault(sub, {})
        else:
            fs = cfg["config"].setdefault(section, {})

        try:
            if field in ("leverage", "order_timeout", "order_type", "trigger_order_type", "margin_mode"):
                fs[field] = int(raw)
            elif field in ("margin_size", "dop_tp"):
                fs[field] = float(raw.replace(",", "."))
            else:
                fs[field] = raw
        except Exception as e:
            self.info_handler.debug_error_notes(f"[text_message_handler] Error parsing {field}: {e}")
            await message.answer(f"❗ Ошибка ввода: {str(e)}")
            return

        cfg["_await_field"] = None  # очистили ожидание

        # информативные ответы
        if field == "order_type":
            msg = "✅ Активирован лимитный тип входа" if fs[field] == 1 else "✅ Активирован рыночный тип входа"
        elif field == "trigger_order_type":
            msg = "✅ Активированы лимитные триггер-ордера" if fs[field] == 1 else "✅ Активированы рыночные триггер-ордера"
        elif field == "margin_mode":
            msg = "✅ Установлен режим маржи: изолированный" if fs[field] == 1 else "✅ Установлен режим маржи: кросс"
        else:
            msg = f"✅ {field} для {section} сохранено!"

        await message.answer(msg, reply_markup=self.context.main_menu)

        if validate_user_config(cfg):
            await message.answer("✅ Конфиг полностью заполнен!", reply_markup=self.context.main_menu)
        else:
            self.context.start_bot_iteration = False

    # ============================================================
    #  GATE INPUT
    # ============================================================

    async def api_key_input(self, callback: types.CallbackQuery):
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)

        if getattr(self.context, "start_bot_iteration", False):
            await callback.message.answer(
                "❗ Сначала остановите бота (команда СТОП), чтобы заменить API Key.",
                reply_markup=self.context.main_menu
            )
            return

        self.context.users_configs[chat_id]["_await_field"] = {"section": "GATE", "field": "api_key"}
        await callback.message.answer("Введите новый API Key:")

    async def secret_key_input(self, callback: types.CallbackQuery):
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)

        if getattr(self.context, "start_bot_iteration", False):
            await callback.message.answer(
                "❗ Сначала остановите бота (команда СТОП), чтобы заменить Secret Key.",
                reply_markup=self.context.main_menu
            )
            return

        self.context.users_configs[chat_id]["_await_field"] = {"section": "GATE", "field": "api_secret"}
        await callback.message.answer("Введите новый Secret Key:")


    # ============================================================
    #  INLINE START / STOP BUTTONS
    # ============================================================

    async def start_button(self, callback: types.CallbackQuery):
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)

        cfg = self.context.users_configs[chat_id]
        if validate_user_config(cfg):
            self.context.start_bot_iteration = True
            self.context.stop_bot_iteration = False
            await callback.message.answer(
                "✅ Бот запущен и готов к работе!",
                reply_markup=self.context.main_menu
            )
        else:
            self.context.start_bot_iteration = False
            await callback.message.answer(
                "❗ Конфиг не заполнен полностью. Сначала настройте все параметры.",
                reply_markup=self.context.main_menu
            )

    async def stop_button(self, callback: types.CallbackQuery):
        
        await callback.answer()
        chat_id = callback.message.chat.id
        self.ensure_user_config(chat_id)

        # есть открытые позиции?
        if any(
            pos.get("in_position", False)
            for symbol_data in self.context.position_vars.values()
            for side, pos in symbol_data.items()
            if side != "spec"
        ):
            await callback.message.answer("Сперва закройте все позиции.", reply_markup=self.context.main_menu)
            return

        if self.context.start_bot_iteration:
            self.context.start_bot_iteration = False
            self.context.stop_bot_iteration = True
            await callback.message.answer("⛔ Бот остановлен", reply_markup=self.context.main_menu)
        else:
            await callback.message.answer("Данная опция недоступна, поскольку торговля еще не начата.", reply_markup=self.context.main_menu)

    # ============================================================
    #  RUN / STOP
    # ============================================================

    async def run(self):
        self._polling_task = asyncio.create_task(
            self.dp.start_polling(self.bot, stop_signal=lambda: self._stop_flag)
        )
        # Небольшая задержка для старта loop
        await asyncio.sleep(0.1)

    async def stop(self):
        self._stop_flag = True
        if self._polling_task:
            try:
                await asyncio.wait_for(self._polling_task, timeout=2.0)
            except Exception:
                pass

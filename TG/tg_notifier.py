import asyncio
import hashlib
import time
import traceback
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.exceptions import TelegramAPIError

from b_context import BotContext
from c_log import ErrorHandler
from c_utils import to_human_digit

INLINE_MESSAGE_TIMEOUT = 30


# ============================================================
#  TEXT FORMATTER
# ============================================================

def build_status_text(body: dict) -> str:
    """Форматирует тело ордера в красивый Telegram-текст."""
    if not body:
        return "⚠️ Нет данных для отображения."

    def fmt_val(v):
        if v is None or v == "":
            return "—"
        if isinstance(v, (int, float)):
            return to_human_digit(v)
        if isinstance(v, (tuple, list)) and len(v) == 2:
            p, pct = v
            return f"{to_human_digit(p)} ({pct}%)"
        return str(v)

    s = body.get("symbol", "?")
    ps = body.get("pos_side", "?")
    lev = f"X{body.get('leverage', '')}" if body.get("leverage") else ""
    order_type = body.get("order_type", "?")
    entry_status = body.get("entry_status", "unknown")

    lines = [
        f"{s} | {ps} {lev}",
        f"Order Type: {order_type}",
        f"Entry price: {fmt_val(body.get('entry_price'))} | Status: {entry_status}",
        f"Take profit 1: {fmt_val(body.get('tp1'))} | Status: {body.get('tp1_status', '—')}",
    ]

    if body.get("tp2") is not None:
        lines.append(f"Take profit 2: {fmt_val(body.get('tp2'))} | Status: {body.get('tp2_status', '—')}")

    lines.append(f"Stop-loss: {fmt_val(body.get('sl'))} | Status: {body.get('sl_status', '—')}")
    lines.append("")
    lines.append(body.get("pnl_text") or "PNL: сделка не завершена")

    return "\n".join(lines).strip()


def text_hash(text: str) -> str:
    """Возвращает md5-хеш строки (для сравнения изменений)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ============================================================
#  TELEGRAM NOTIFIER CLASS
# ============================================================

class TelegramNotifier:
    """
    Управляет инлайн-кнопками сделок, вводом TP/SL, закрытием позиций,
    обновлением сообщений и хранением состояния в order_status_book.
    """

    def __init__(self, bot: Bot, dp: Dispatcher, context: BotContext, info_handler: ErrorHandler):
        self.bot = bot
        self.dp = dp
        self.context = context
        self.info_handler = info_handler

        self.modify_stop_loss = None
        self.modify_take_profits = None
        self.force_position_close = None

        # предотвращаем коллизии в параллельных сообщениях
        self._await_lock = asyncio.Lock()

    # ============================================================
    #  BINDING METHODS
    # ============================================================

    def bind_templates(self, modify_sl, modify_tp, force_close):
        """Привязка внешних методов."""
        self.modify_stop_loss = modify_sl
        self.modify_take_profits = modify_tp
        self.force_position_close = force_close
        self.info_handler.debug_info_notes("[Notifier] Templates successfully bound")

    def register_handlers(self):
        """Регистрация обработчиков aiogram 3.x."""
        patterns = ("close:", "change:", "tp1:", "tp2:", "sl:", "close_type:", "close_confirm:")
        self.dp.callback_query.register(self.handle_callback, F.data.startswith(patterns))
        self.dp.message.register(self._on_message, lambda m: self.context.awaiting_input.get(m.chat.id))

    # ============================================================
    #  HELPERS
    # ============================================================

    async def _send_temp_message(self, chat_id: int, text: str, delay: int = INLINE_MESSAGE_TIMEOUT, reply_markup=None):
        """Отправляет сообщение с опциональным автоудалением."""
        try:
            msg = await self.bot.send_message(chat_id, text, reply_markup=reply_markup)
            if not reply_markup:
                await asyncio.sleep(delay)
                await self.bot.delete_message(chat_id, msg.message_id)
        except Exception as e:
            self.info_handler.debug_error_notes(f"[_send_temp_message] {e}")

    async def _safe_callback_answer(self, callback: CallbackQuery, text: Optional[str] = None, show_alert=False):
        """Безопасный ответ на callback, чтобы избежать ошибок Telegram."""
        try:
            await callback.answer(text=text, show_alert=show_alert)
        except TelegramAPIError as e:
            if "query is too old" not in str(e).lower():
                self.info_handler.debug_error_notes(f"[_safe_callback_answer] {e}")

    # ============================================================
    #  BUTTON BUILDERS
    # ============================================================

    def _build_status_buttons(self, key):
        s, ps = key
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Закрыть", callback_data=f"close:{s}:{ps}"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"change:{s}:{ps}")
        ]])

    def _build_change_buttons(self, s, ps):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="TP1", callback_data=f"tp1:{s}:{ps}"),
            InlineKeyboardButton(text="TP2", callback_data=f"tp2:{s}:{ps}"),
            InlineKeyboardButton(text="SL", callback_data=f"sl:{s}:{ps}")
        ]])

    def _build_close_type_buttons(self, s, ps):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💰 Market", callback_data=f"close_type:{s}:{ps}:market"),
            InlineKeyboardButton(text="📉 Limit", callback_data=f"close_type:{s}:{ps}:limit")
        ]])

    def _build_confirm_buttons(self, s, ps, order_type):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"close_confirm:{s}:{ps}:{order_type}:yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"close_confirm:{s}:{ps}:{order_type}:no")
        ]])

    # ============================================================
    #  MESSAGE ANCHOR SYSTEM
    # ============================================================

    async def update_anchor_state(self, chat_id: int, symbol: str, pos_side: str, body: dict,
                                  *, force_message_id: Optional[int] = None, buttons_state: int = 2):
        """
        Создает или обновляет якорное сообщение.
        Гарантирует, что при первой инициализации создается клавиатура.
        """
        key = (symbol, pos_side)
        chat_book = self.context.order_status_book.setdefault(chat_id, {})
        current_anchor = chat_book.get(key)

        if current_anchor is None:
            current_anchor = {"message_id": None, "hash": None, "last_data": None}
            chat_book[key] = current_anchor

        record = body or {}
        entry_status = str(record.get("entry_status") or "").lower()
        closed = entry_status in ("closed manually", "finished")

        # if buttons_state == 1:
        #     markup = self._build_status_buttons(key)
        # elif buttons_state == 3:
        #     markup = InlineKeyboardMarkup(inline_keyboard=[[
        #         InlineKeyboardButton(text="✅ Позиция закрыта", callback_data="noop")
        #     ]])
        # else:
        #     markup = current_anchor.get("buttons")

        if buttons_state == 1:
            markup = self._build_status_buttons(key)
        elif buttons_state == 3:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Позиция закрыта", callback_data="noop")
            ]])
        else:
            # fallback если кнопок нет
            markup = current_anchor.get("buttons") or self._build_status_buttons(key)

        new_text = build_status_text(record)
        new_hash = text_hash(new_text)
        old_hash = current_anchor.get("hash")
        old_msg_id = force_message_id or current_anchor.get("message_id")
        new_msg_id = old_msg_id

        if old_hash == new_hash and old_msg_id:
            async with self.context.queues_msg_lock:
                current_anchor.update({
                    "last_data": record,
                    "hash": new_hash,
                    "closed": closed,
                    "last_update": time.time(),
                    "buttons": markup,
                })
            return

        try:
            if old_msg_id:
                await self.bot.edit_message_text(chat_id=chat_id, message_id=old_msg_id, text=new_text, reply_markup=markup)
            else:
                msg = await self.bot.send_message(chat_id, new_text, reply_markup=markup)
                new_msg_id = msg.message_id
        except TelegramAPIError as e:
            if "message is not modified" not in str(e).lower():
                self.info_handler.debug_error_notes(f"[update_anchor_state][edit_fail] {key}: {e}")
        except Exception as e:
            self.info_handler.debug_error_notes(f"[update_anchor_state][unexpected] {key}: {e}")

        async with self.context.queues_msg_lock:
            chat_book[key] = {
                "message_id": new_msg_id,
                "last_data": record,
                "hash": new_hash,
                "closed": closed,
                "last_update": time.time(),
                "buttons": markup,
            }

    # ============================================================
    #  MESSAGE HANDLER (TP / SL INPUT)
    # ============================================================

    async def _on_message(self, message: Message):
        """Обрабатывает ввод TP/SL из чата."""
        async with self._await_lock:
            chat_id = message.chat.id
            try:
                info = self.context.awaiting_input.get(chat_id)
                if not info:
                    return

                field = info["field"]
                symbol, pos_side = info["key"]
                key = (symbol, pos_side)
                self.context.awaiting_input.pop(chat_id, None)

                pos = self.context.position_vars.get(symbol, {}).get(pos_side, {})
                if not pos.get("in_position"):
                    await self.bot.send_message(chat_id, f"⚠️ Позиция {symbol} ({pos_side}) уже неактивна.", reply_markup=self.context.main_menu)
                    return

                current_anchor = self.context.order_status_book.get(chat_id, {}).get(key)
                if not current_anchor or not current_anchor.get("last_data"):
                    await self.bot.send_message(chat_id, "⚠️ Сообщение не найдено.", reply_markup=self.context.main_menu)
                    return

                record = current_anchor["last_data"]
                raw = message.text.strip()

                # === Валидация и парсинг ===
                try:
                    if field == "sl":
                        value = float(raw.replace(",", "."))
                    else:
                        parts = raw.split()
                        if len(parts) != 2:
                            await self.bot.send_message(chat_id, "❌ Формат: <цена> <процент>", reply_markup=self.context.main_menu)
                            return
                        price = float(parts[0].replace(",", "."))
                        percent = float(parts[1].replace(",", "."))
                        if not (0 < percent <= 100):
                            await self.bot.send_message(chat_id, "❌ Процент должен быть в диапазоне (0, 100].", reply_markup=self.context.main_menu)
                            return
                        value = (price, percent)
                except Exception as e:
                    await self.bot.send_message(chat_id, f"❌ Ошибка парсинга: {e}.", reply_markup=self.context.main_menu)
                    return

                # === Обновление ордера ===
                session = self.context.session
                fin_settings_root = self.context.users_configs[chat_id]["config"]["fin_settings"]
                settings_tag = pos.get("settings_tag")
                fin_settings = fin_settings_root.get(settings_tag, {})
                symbol_data = self.context.position_vars.get(symbol, {})

                if field == "sl":
                    success = await self.modify_stop_loss(session=session, record=current_anchor, chat_id=chat_id,
                                                          symbol=symbol, pos_side=pos_side,
                                                          fin_settings=fin_settings, symbol_data=symbol_data,
                                                          pos_data=pos, new_sl=value)
                else:
                    tp_index = 1 if field == "tp1" else 2
                    success = await self.modify_take_profits(session=session, record=current_anchor, chat_id=chat_id,
                                                             symbol=symbol, pos_side=pos_side,
                                                             fin_settings=fin_settings, symbol_data=symbol_data,
                                                             pos_data=pos, new_tp=value, tp_index=tp_index)

                msg = f"✅ {field.upper()} обновлён: {value}" if success else f"⚠️ Ошибка при изменении {field.upper()}"
                await self.bot.send_message(chat_id, msg, reply_markup=self.context.main_menu)

                if success:
                    await self.update_anchor_state(chat_id=chat_id, symbol=symbol, pos_side=pos_side,
                                                   body=record, force_message_id=current_anchor.get("message_id"))

            except Exception as e:
                self.info_handler.debug_error_notes(f"[_on_message] {e}\n{traceback.format_exc()}")
                await self.bot.send_message(chat_id, "⚠️ Ошибка обработки.", reply_markup=self.context.main_menu)

    # ============================================================
    #  INLINE CALLBACK HANDLER
    # ============================================================

    async def handle_callback(self, callback: CallbackQuery):
        """Главный обработчик inline-кнопок."""
        try:
            self.info_handler.debug_info_notes(f"[Notifier callback] data={callback.data}")
            await self._safe_callback_answer(callback)

            data = callback.data.strip()
            chat_id = callback.message.chat.id
            parts = data.split(":")
            if len(parts) < 3:
                return

            s, ps = parts[1], parts[2]
            key = (s, ps)

            current_anchor = self.context.order_status_book.get(chat_id, {}).get(key)
            if not current_anchor:
                await self._safe_callback_answer(callback, "⚠️ Сообщение устарело.", show_alert=True)
                return

            pos = self.context.position_vars.get(s, {}).get(ps, {})
            record = current_anchor.get("last_data") or {}

            if not pos.get("in_position"):
                await self._safe_callback_answer(callback, "⚠️ Позиция не активна.", show_alert=True)
                return

            # === Основные ветки ===
            if data.startswith("change:"):
                await self._send_temp_message(chat_id, f"Выберите параметр для изменения {s} ({ps}):",
                                              reply_markup=self._build_change_buttons(s, ps))
            elif data.startswith(("tp1:", "tp2:", "sl:")):
                field = parts[0]
                hint = "Введите: <цена> <процент>" if "tp" in field else "Введите: <цена>"
                self.context.awaiting_input[chat_id] = {"field": field, "key": (s, ps)}
                await self.bot.send_message(chat_id, f"✏️ {field.upper()} {s} ({ps}). {hint}",
                                            reply_markup=self.context.main_menu)
            elif data.startswith("close:"):
                await self._send_temp_message(chat_id, f"Выберите тип закрытия {s} ({ps}):",
                                              reply_markup=self._build_close_type_buttons(s, ps))
            elif data.startswith("close_type:"):
                ot = parts[3]
                await self._send_temp_message(chat_id, f"Подтвердите закрытие {s} ({ps}) по {ot.upper()}:",
                                              reply_markup=self._build_confirm_buttons(s, ps, ot))
            elif data.startswith("close_confirm:"):
                ot, conf = parts[3], parts[4]
                if conf != "yes":
                    await self._send_temp_message(chat_id, f"❌ Закрытие {s} ({ps}) отменено.")
                    return

                fin_settings_root = self.context.users_configs[chat_id]["config"]["fin_settings"]
                settings_tag = pos.get("settings_tag")
                fin_settings = fin_settings_root.get(settings_tag, {})

                success = await self.force_position_close(session=self.context.session, chat_id=chat_id,
                                                          key=key, fin_settings=fin_settings,
                                                          close_type=ot)

                msg = f"✅ Закрытие {s} ({ps}) по {ot.upper()} выполнено." if success else f"⚠️ Ошибка при закрытии {s} ({ps})"
                await self._send_temp_message(chat_id, msg)

        except Exception as e:
            self.info_handler.debug_error_notes(f"[handle_callback] {e}\n{traceback.format_exc()}")
            await self.bot.send_message(chat_id, "⚠️ Ошибка обработки.", reply_markup=self.context.main_menu)

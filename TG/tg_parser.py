from a_config import *
from b_context import BotContext
from c_log import ErrorHandler, log_time
from typing import *
import re
from typing import Optional, Tuple, Set
from aiogram import Dispatcher, types, F


# Базовый словарь: пара символов (латиница, кириллица)
CHAR_PAIRS = {
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "y": "у", "x": "х",
    "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н", "O": "О",
    "P": "Р", "C": "С", "T": "Т", "X": "Х",
}

LATIN_TO_CYR = CHAR_PAIRS
CYR_TO_LATIN = {v: k for k, v in CHAR_PAIRS.items()}


class TgParser:
    def __init__(self, info_handler: ErrorHandler):    
        info_handler.wrap_foreign_methods(self)
        self.info_handler = info_handler

    @staticmethod
    def clean_whitespace(text: str) -> str:
        """Очищает текст от лишних пробелов, нормализуя его."""
        if not text:
            return ""
        words = [word.strip() for word in text.split()]
        return " ".join(words)

    @staticmethod
    def cyr_to_latin(text: str) -> str:
        """Преобразует кириллические символы в соответствующие латинские."""
        if not text:
            return ""
        return "".join(CYR_TO_LATIN.get(ch, ch) for ch in text)

    @staticmethod
    def latin_to_cyr(text: str) -> str:
        """Преобразует похожие латинские буквы в кириллические, убирает шум."""
        if not text:
            return ""
        text = "".join(LATIN_TO_CYR.get(ch, ch) for ch in text)
        text = text.lower().replace(",", ".")
        return text

    @staticmethod
    def clean_number(num_str: str) -> float:
        """
        Преобразует строку с любыми разделителями в float.
        Берёт последнюю точку как разделитель дробной части.
        """
        num_str = num_str.replace(",", ".")
        cleaned = re.sub(r"[^\d.]", "", num_str)
        if "." in cleaned:
            last_dot = cleaned.rfind(".")
            int_part = re.sub(r"[^\d]", "", cleaned[:last_dot])
            frac_part = re.sub(r"[^\d]", "", cleaned[last_dot + 1:])
            normalized = f"{int_part}.{frac_part}" if frac_part else int_part
        else:
            normalized = re.sub(r"[^\d]", "", cleaned)

        return float(normalized) if normalized else 0.0

    def parse_tg_message(self, message: str, tag: str = "") -> Tuple[dict, bool]:
        # === Очистка и нормализация ===
        message = re.sub(r"[^\w\s.,:/\-#+]", " ", message)  # убираем ⏺️, смайлы, прочий мусор
        message = self.clean_whitespace(message.strip())

        # выбираем направление конверсии
        if tag == "#soft" or not tag:
            text = self.latin_to_cyr(message)
        else:
            text = self.cyr_to_latin(message)

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        text_joined = " ".join(lines)

        # === Результирующий шаблон ===
        result = {
            "symbol": "",
            "pos_side": None,
            "entry_price": None,
            "stop_loss": None,
            "take_profit1": None,
            "take_profit2": None,
            "leverage": None,
            "force_limit": False,
            "half_margin": False
        }

        # ====== #SOFT (СТАРАЯ ЛОГИКА) ======
        if tag == "#soft" or not tag:
            # символ
            if lines:
                m_symbol = re.search(r"\$([а-яa-z0-9]+)", lines[0], re.IGNORECASE)
                if m_symbol:
                    result["symbol"] = m_symbol.group(1).upper()

            # позиция
            for line in lines:
                if "лонг" in line:
                    result["pos_side"] = "LONG"
                elif "шорт" in line:
                    result["pos_side"] = "SHORT"

            # основные параметры
            patterns = {
                "entry_price": r"вход\s*[-–—:]?\s*([\d\s.,]+)",
                "stop_loss": r"стоп\s*[-–—:]?\s*([\d\s.,]+)",
                "take_profit1": r"тейк\s*[-–—:]?\s*([\d\s.,]+)",
                "leverage": r"плечо\s*[-–—:]?\s*[хx]?\s*(\d+)"
            }

            for key, pattern in patterns.items():
                m = re.search(pattern, text_joined)
                if m:
                    if key == "leverage":
                        try:
                            result[key] = int(m.group(1))
                        except ValueError:
                            pass
                    else:
                        result[key] = self.clean_number(m.group(1))

        # ====== TRADING PAIR (НОВЫЙ ФОРМАТ) ======
        elif tag == "trading pair":
            # символ
            m_symbol = re.search(r"\btrading[_\- :]*pair[_\- :]*([a-z0-9]+)\s*(?:/ ?usdt)?", text_joined, re.IGNORECASE)
            if m_symbol:
                result["symbol"] = m_symbol.group(1).upper()

            # позиция + плечо
            m_pos = re.search(r"\b(long|short)\b\s*[xхXХ]\s*(\d+)", text_joined, re.IGNORECASE)
            if m_pos:
                result["pos_side"] = m_pos.group(1).upper()
                try:
                    result["leverage"] = int(m_pos.group(2))
                except ValueError:
                    pass

            # entry
            m_entry = re.search(r"\bentry[_\- :]*price[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
            if m_entry:
                result["entry_price"] = self.clean_number(m_entry.group(1))

            # stop loss
            m_sl = re.search(r"\bstop[_\- :]*loss[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
            if m_sl:
                result["stop_loss"] = self.clean_number(m_sl.group(1))

            # take profit1
            m_tp1 = re.search(r"\btake[_\- :]*profit1?[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
            if m_tp1:
                result["take_profit1"] = self.clean_number(m_tp1.group(1))

            # take profit2 (новое)
            m_tp2 = re.search(r"\btake[_\- :]*profit2[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
            if m_tp2:
                result["take_profit2"] = self.clean_number(m_tp2.group(1))

        # ====== ДОПОЛНИТЕЛЬНЫЕ ФЛАГИ ======
        text_lower = text_joined.lower()
        result["force_limit"] = "#limit" in text_lower
        result["half_margin"] = any(
            x in text_lower for x in [
                "1/2 size", "0.5% size", "1\\2 size", "0.5 size", "half size", "половина позиции", "половина маржи"
            ]
        )

        # ====== ФИНАЛЬНАЯ ОБРАБОТКА ======
        base_symbol = self.cyr_to_latin(result["symbol"]).upper()
        if not base_symbol:
            return {}, False

        result["symbol"] = base_symbol.replace("USDT", "").replace("_", "").replace("-", "") + "_USDT"

        # обязательные ключи без флагов и второго тейка
        mandatory_keys = ["symbol", "pos_side", "entry_price", "stop_loss", "take_profit1", "leverage"]
        all_present = all(result[k] for k in mandatory_keys)

        return result, all_present


class TgBotWatcherAiogram(TgParser):
    """
    Отслеживает сообщения из Telegram-канала через aiogram-хендлеры.
    Фильтрует по тегам из self.tags_set.
    """

    def __init__(
        self,
        dp: Dispatcher,
        channel_id: int,
        tags_set: Set[str],
        context: BotContext,
        info_handler: ErrorHandler,
        max_cache: int = 20
    ):
        super().__init__(info_handler)
        self.dp = dp
        self.channel_id = channel_id
        self.tags_set: Set[str] = {x.lower().strip() for x in tags_set if x}
        self.message_cache = context.message_cache
        self.stop_bot = context.stop_bot
        self._seen_messages: Set[int] = set()
        self.max_cache: int = max_cache

    def register_handlers(self):
        """
        Регистрирует все channel_post обработчики через Dispatcher.
        """
        @self.dp.channel_post()
        # @self.dp.channel_post(F.chat.id == self.channel_id)
        async def channel_post_handler(message: types.Message):
            try:
                if not message.text:
                    print(f"Нет сообщений для парсинга либо права доступа ограничены. {log_time()}")
                    return

                msg_text = message.text.lower()
                matched_tag = next((tag for tag in self.tags_set if tag in msg_text), None)
                if not matched_tag:
                    return

                ts_ms = int(message.date.timestamp() * 1000)
                if ts_ms in self._seen_messages:
                    return

                self._seen_messages.add(ts_ms)
                self.message_cache.append((matched_tag, message.text, ts_ms))

                if len(self.message_cache) > self.max_cache:
                    self.message_cache = self.message_cache[-self.max_cache:]
                    self._seen_messages.clear()

            except Exception as e:
                self.info_handler.debug_error_notes(f"[watch_channel error] {e}", is_print=True)

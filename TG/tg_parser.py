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
        return " ".join(word.strip() for word in text.split())

    @staticmethod
    def cyr_to_latin(text: str) -> str:
        return "".join(CYR_TO_LATIN.get(ch, ch) for ch in text)

    @staticmethod
    def latin_to_cyr(text: str) -> str:
        text = "".join(LATIN_TO_CYR.get(ch, ch) for ch in text)
        return text.lower().replace(",", ".")

    @staticmethod
    def clean_number(num_str: str) -> float:
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
        # Нормализация текста
        message = re.sub(r"[^\w\s.,:/\-#+]", " ", message)
        message = self.clean_whitespace(message.strip())

        # Выбор направления транслита
        text = self.cyr_to_latin(message) if tag != "#soft" else self.latin_to_cyr(message)

        # Разбивка по строкам
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        text_joined = " ".join(lines)

        # Базовый результат
        result = {
            "symbol": "",
            "pos_side": None,
            "entry_price": None,
            "stop_loss": None,
            "take_profit1": None,
            "take_profit2": None,
            "leverage": None,
            "force_limit": False,
            "half_margin": False,
        }

        # === ВЕТКА SOFT (русские сигналы) ===
        if tag == "#soft" or not tag:

            # Символ через $BNB
            if lines:
                m_symbol = re.search(r"\$([а-яa-z0-9]+)", lines[0], re.IGNORECASE)
                if m_symbol:
                    result["symbol"] = m_symbol.group(1).upper()

            # Позиция лонг/шорт
            for line in lines:
                if "лонг" in line:
                    result["pos_side"] = "LONG"
                elif "шорт" in line:
                    result["pos_side"] = "SHORT"

            # Паттерны для soft
            patterns = {
                "entry_price": r"вход\s*[-–—:]?\s*([\d\s.,]+)",
                "stop_loss": r"стоп\s*[-–—:]?\s*([\d\s.,]+)",
                "take_profit1": r"тейк\s*[-–—:]?\s*([\d\s.,]+)",
                "leverage": r"плечо\s*[-–—:]?\s*[хx]?\s*(\d+)",
            }

            for key, pattern in patterns.items():
                m = re.search(pattern, text_joined, re.IGNORECASE)
                if m:
                    if key == "leverage":
                        try:
                            result[key] = int(m.group(1))
                        except ValueError:
                            pass
                    else:
                        result[key] = self.clean_number(m.group(1))

        # === ВЕТКА TRADING PAIR (английские сигналы) ===
        elif tag == "trading pair":
            # --- символ ---
            m_pair = re.search(r"trading\s*pair\s*[:\- ]*\s*([a-zA-Zа-яА-Я0-9]+)", text_joined, re.IGNORECASE)
            if m_pair:
                result["symbol"] = m_pair.group(1)

            if not result["symbol"]:  # fallback BTC/USDT -> BTC
                m_symbol = re.search(r"\b([A-Za-zА-Яа-я0-9]{2,6})\s*/\s*USDT\b", text_joined, re.IGNORECASE)
                if m_symbol:
                    result["symbol"] = m_symbol.group(1)

            # --- POS_SIDE (универсально) ---
            if re.search(r"\blong\b", text_joined, re.IGNORECASE):
                result["pos_side"] = "LONG"
            elif re.search(r"\bshort\b", text_joined, re.IGNORECASE):
                result["pos_side"] = "SHORT"

            # --- LEVERAGE (универсально) ---
            # x10 / X10 / х10
            m_x = re.search(r"[xхXХ]\s*(\d{1,3})", text_joined)
            if m_x:
                result["leverage"] = int(m_x.group(1))
            # 10x / 20X / 5х
            m_rev = re.search(r"(\d{1,3})\s*[xхXХ]", text_joined)
            if not result["leverage"] and m_rev:
                result["leverage"] = int(m_rev.group(1))

            # --- Entry ---
            m_entry = re.search(r"\bentry[_\s\-:]*price\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
            if m_entry:
                result["entry_price"] = self.clean_number(m_entry.group(1))

            # --- Stop ---
            m_sl = re.search(r"\bstop[_\s\-:]*loss\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
            if m_sl:
                result["stop_loss"] = self.clean_number(m_sl.group(1))

            # --- TP1 ---
            m_tp1 = re.search(r"\b(?:tp|take\s*profit)\s*1\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
            if m_tp1:
                result["take_profit1"] = self.clean_number(m_tp1.group(1))

            # fallback "Take profit:"
            if result["take_profit1"] is None:
                m_tpf = re.search(r"take\s*profit\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
                if m_tpf:
                    result["take_profit1"] = self.clean_number(m_tpf.group(1))

            # --- TP2 ---
            m_tp2 = re.search(r"\b(?:tp|take\s*profit)\s*2\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
            if m_tp2:
                result["take_profit2"] = self.clean_number(m_tp2.group(1))

        # Флаги
        text_lower = text_joined.lower()
        result["force_limit"] = "#limit" in text_lower
        result["half_margin"] = any(x in text_lower for x in [
            "1/2 size", "1\\2 size", "0.5 size", "half size",
            "половина позиции", "половина маржи"
        ])

        # Финальная нормализация символа
        base_symbol = self.cyr_to_latin(result["symbol"]).upper()
        if not base_symbol:
            return {}, False
        result["symbol"] = base_symbol.replace("USDT", "").replace("_", "").replace("-", "") + "_USDT"

        # Проверка обязательных полей
        mandatory = ["symbol", "pos_side", "entry_price", "stop_loss", "take_profit1", "leverage"]
        ok = all(result[k] for k in mandatory)

        return result, ok


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

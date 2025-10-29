import re
from typing import Tuple, Set


# ==== вставляем сокращённый класс TgParser без зависимостей ====
CHAR_PAIRS = {
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "y": "у", "x": "х",
    "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н", "O": "О",
    "P": "Р", "C": "С", "T": "Т", "X": "Х",
}
LATIN_TO_CYR = CHAR_PAIRS
CYR_TO_LATIN = {v: k for k, v in CHAR_PAIRS.items()}

class TgParser:
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

    # def parse_tg_message(self, message: str, tag: str = "") -> Tuple[dict, bool]:
    #     # 1) очистка
    #     message = re.sub(r"[^\w\s.,:/\-\+=#–—]", " ", message)
    #     message = self.clean_whitespace(message.strip())

    #     # 2) раскладка
    #     text = self.latin_to_cyr(message) if tag == "#soft" else self.cyr_to_latin(message)

    #     # 3) единая строка
    #     lines = [l.strip() for l in text.split("\n") if l.strip()]
    #     text_joined = " ".join(lines)

    #     result = {
    #         "symbol": "", "pos_side": None, "entry_price": None,
    #         "stop_loss": None, "take_profit1": None, "take_profit2": None,
    #         "leverage": None, "force_limit": False, "half_margin": False
    #     }

    #     # --- символ ---
    #     m_pair = re.search(r"trading\s*pair\s*[:\- ]*\s*([a-zA-Zа-яА-Я0-9]+)", text_joined, re.IGNORECASE)
    #     if m_pair:
    #         result["symbol"] = m_pair.group(1)

    #     if not result["symbol"]:  # fallback BTC/USDT -> BTC
    #         m_symbol = re.search(r"\b([A-Za-zА-Яа-я0-9]{2,6})\s*/\s*USDT\b", text_joined, re.IGNORECASE)
    #         if m_symbol:
    #             result["symbol"] = m_symbol.group(1)

    #     # --- POS_SIDE (универсально) ---
    #     if re.search(r"\blong\b", text_joined, re.IGNORECASE):
    #         result["pos_side"] = "LONG"
    #     elif re.search(r"\bshort\b", text_joined, re.IGNORECASE):
    #         result["pos_side"] = "SHORT"

    #     # --- LEVERAGE (универсально) ---
    #     # x10 / X10 / х10
    #     m_x = re.search(r"[xхXХ]\s*(\d{1,3})", text_joined)
    #     if m_x:
    #         result["leverage"] = int(m_x.group(1))
    #     # 10x / 20X / 5х
    #     m_rev = re.search(r"(\d{1,3})\s*[xхXХ]", text_joined)
    #     if not result["leverage"] and m_rev:
    #         result["leverage"] = int(m_rev.group(1))

    #     # --- Entry ---
    #     m_entry = re.search(r"\bentry[_\s\-:]*price\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
    #     if m_entry:
    #         result["entry_price"] = self.clean_number(m_entry.group(1))

    #     # --- Stop ---
    #     m_sl = re.search(r"\bstop[_\s\-:]*loss\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
    #     if m_sl:
    #         result["stop_loss"] = self.clean_number(m_sl.group(1))

    #     # --- TP1 ---
    #     m_tp1 = re.search(r"\b(?:tp|take\s*profit)\s*1\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
    #     if m_tp1:
    #         result["take_profit1"] = self.clean_number(m_tp1.group(1))

    #     # fallback "Take profit:"
    #     if result["take_profit1"] is None:
    #         m_tpf = re.search(r"take\s*profit\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
    #         if m_tpf:
    #             result["take_profit1"] = self.clean_number(m_tpf.group(1))

    #     # --- TP2 ---
    #     m_tp2 = re.search(r"\b(?:tp|take\s*profit)\s*2\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
    #     if m_tp2:
    #         result["take_profit2"] = self.clean_number(m_tp2.group(1))

    #     # флаги
    #     text_lower = text_joined.lower()
    #     result["force_limit"] = "#limit" in text_lower
    #     if re.search(r"\b1\s*[/\\]\s*2\b", text_lower) or re.search(r"\b0[.,]?5\b", text_lower) or "half" in text_lower:
    #         result["half_margin"] = True

    #     # нормализуем символ
    #     base_symbol = self.cyr_to_latin(result["symbol"]).upper()
    #     if not base_symbol:
    #         return {}, False

    #     result["symbol"] = base_symbol.replace("USDT", "").replace("_", "").replace("-", "") + "_USDT"

    #     mandatory = ["symbol", "pos_side", "entry_price", "stop_loss", "take_profit1", "leverage"]
    #     ok = all(result.get(k) is not None for k in mandatory)

    #     return result, ok



    # def parse_tg_message(self, message: str, tag: str = "") -> Tuple[dict, bool]:
    #     # убираем неалфанум кроме нужных
    #     message = re.sub(r"[^\w\s.,:/\-#+=–—]", " ", message)
    #     message = self.clean_whitespace(message.strip())

    #     # преобразуем раскладку
    #     text = self.cyr_to_latin(message) if tag != "#soft" else self.latin_to_cyr(message)

    #     # приводим к одной строке
    #     lines = [l.strip() for l in text.split("\n") if l.strip()]
    #     text_joined = " ".join(lines)

    #     result = {
    #         "symbol": "", "pos_side": None, "entry_price": None,
    #         "stop_loss": None, "take_profit1": None, "take_profit2": None,
    #         "leverage": None, "force_limit": False, "half_margin": False
    #     }

    #     if tag == "trading pair":
    #         # символ
    #         m_symbol = re.search(
    #             r"\btrading[_\s\-:]*pair[_\s\-:]*([a-z0-9]+)\s*(?:/ ?usdt)?",
    #             text_joined, re.IGNORECASE
    #         )
    #         if m_symbol:
    #             result["symbol"] = m_symbol.group(1).upper()

    #         # long/short + x leverage
    #         m_pos = re.search(r"\b(long|short)\b\s*[xхXХ]\s*(\d+)", text_joined, re.IGNORECASE)
    #         if m_pos:
    #             result["pos_side"] = m_pos.group(1).upper()
    #             result["leverage"] = int(m_pos.group(2))

    #         # entry price
    #         m_entry = re.search(
    #             r"\bentry[_\s\-:]*price\b[\s:\-–—=]+([\d.,]+)",
    #             text_joined, re.IGNORECASE
    #         )
    #         if m_entry:
    #             result["entry_price"] = self.clean_number(m_entry.group(1))

    #         # stop loss
    #         m_sl = re.search(
    #             r"\bstop[_\s\-:]*loss\b[\s:\-–—=]+([\d.,]+)",
    #             text_joined, re.IGNORECASE
    #         )
    #         if m_sl:
    #             result["stop_loss"] = self.clean_number(m_sl.group(1))

    #         # --- TP1 ---
    #         m_tp1 = re.search(
    #             r"\b(?:tp|take\s*profit)\s*1\b[\s:\-–—=]+([\d.,]+)",
    #             text_joined, re.IGNORECASE
    #         )
    #         if m_tp1:
    #             result["take_profit1"] = self.clean_number(m_tp1.group(1))

    #         # --- TP2 ---
    #         m_tp2 = re.search(
    #             r"\b(?:tp|take\s*profit)\s*2\b[\s:\-–—=]+([\d.,]+)",
    #             text_joined, re.IGNORECASE
    #         )
    #         if m_tp2:
    #             result["take_profit2"] = self.clean_number(m_tp2.group(1))

    #         # --- fallback TP: "Take profit: 0.12"
    #         if result["take_profit1"] is None:
    #             m_tp_fallback = re.search(
    #                 r"\btake\s*profit\b[\s:\-–—=]+([\d.,]+)",
    #                 text_joined, re.IGNORECASE
    #             )
    #             if m_tp_fallback:
    #                 result["take_profit1"] = self.clean_number(m_tp_fallback.group(1))

    #     # флаги
    #     text_lower = text_joined.lower()
    #     result["force_limit"] = "#limit" in text_lower
    #     result["half_margin"] = any(
    #         x in text_lower for x in
    #         ["1/2 size", "0.5% size", "1\2 size", "1\\2 size", "0.5 size"]
    #     )

    #     # нормализуем символ
    #     base_symbol = self.cyr_to_latin(result["symbol"]).upper()
    #     if not base_symbol:
    #         return {}, False

    #     result["symbol"] = base_symbol.replace("USDT", "").replace("_", "").replace("-", "") + "_USDT"

    #     # обязательные поля
    #     mandatory_keys = ["symbol", "pos_side", "entry_price", "stop_loss", "take_profit1", "leverage"]
    #     all_present = all(result[k] for k in mandatory_keys)

    #     return result, all_present


    # def parse_tg_message(self, message: str, tag: str = "") -> Tuple[dict, bool]:
    #     message = re.sub(r"[^\w\s.,:/\-#+]", " ", message)
    #     message = self.clean_whitespace(message.strip())
    #     text = self.cyr_to_latin(message) if tag != "#soft" else self.latin_to_cyr(message)
    #     lines = [l.strip() for l in text.split("\n") if l.strip()]
    #     text_joined = " ".join(lines)

    #     result = {
    #         "symbol": "", "pos_side": None, "entry_price": None,
    #         "stop_loss": None, "take_profit1": None, "take_profit2": None,
    #         "leverage": None, "force_limit": False, "half_margin": False
    #     }

    #     if tag == "trading pair":
    #         m_symbol = re.search(r"\btrading[_\- :]*pair[_\- :]*([a-z0-9]+)\s*(?:/ ?usdt)?", text_joined, re.IGNORECASE)
    #         if m_symbol:
    #             result["symbol"] = m_symbol.group(1).upper()
    #         m_pos = re.search(r"\b(long|short)\b\s*[xхXХ]\s*(\d+)", text_joined, re.IGNORECASE)
    #         if m_pos:
    #             result["pos_side"] = m_pos.group(1).upper()
    #             result["leverage"] = int(m_pos.group(2))
    #         m_entry = re.search(r"\bentry[_\- :]*price[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
    #         if m_entry:
    #             result["entry_price"] = self.clean_number(m_entry.group(1))
    #         m_sl = re.search(r"\bstop[_\- :]*loss[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
    #         if m_sl:
    #             result["stop_loss"] = self.clean_number(m_sl.group(1))
    #         m_tp1 = re.search(r"\btake[_\- :]*profit1?[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
    #         if m_tp1:
    #             result["take_profit1"] = self.clean_number(m_tp1.group(1))
    #         m_tp2 = re.search(r"\btake[_\- :]*profit2[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
    #         if m_tp2:
    #             result["take_profit2"] = self.clean_number(m_tp2.group(1))

    #     text_lower = text_joined.lower()
    #     result["force_limit"] = "#limit" in text_lower
    #     result["half_margin"] = any(x in text_lower for x in ["1/2 size", "0.5% size", "1\\2 size", "0.5 size", "half size", "половина позиции", "половина маржи"])

    #     base_symbol = self.cyr_to_latin(result["symbol"]).upper()
    #     if not base_symbol:
    #         return {}, False
    #     result["symbol"] = base_symbol.replace("USDT", "").replace("_", "").replace("-", "") + "_USDT"
    #     mandatory_keys = ["symbol", "pos_side", "entry_price", "stop_loss", "take_profit1", "leverage"]
    #     all_present = all(result[k] for k in mandatory_keys)
    #     return result, all_present


# ==== тестовые кейсы ====
test_messages = [
    """⏺️Trading pair : ETH / USDT 
    LONG X10 
    Entry price: 3832
    Stop Loss: 3800
    Take Profit1: 3935
    Take Profit2: 3982
    1/2 size""",

    """Trading pair : CRO / USDT 
    LONG X10 
    1/2 size
    Entry price: 0.153
    Stop-loss: 0.1433
    Take profit1: 0.1879""",

    """Trading pair: BTC / USDT
    SHORT X15
    Entry price - 66500
    Stop loss - 67200
    Take profit1 - 64000
    #limit""",
    """Trading pair 4/USDT
    long
    Х10
    Entry price: 0.102
    Take profit 1: 0.12
    Stop-loss: 0.07
    """,
    """Trading pair 4/USDT
    long
    Х10
    Entry price: 0.096
    Take profit: 0.11
    Stop-loss: 0.07
    """,
    """Trading pair :  4 / USDT
    long
    10X
    Entry price: 0.10
    Take profit 1 : 0.12
    Stop-loss: 0.07
    """
]

parser = TgParser()
for i, msg in enumerate(test_messages, start=1):
    result, ok = parser.parse_tg_message(msg, tag="trading pair")
    print(f"\n=== Test {i} ===")
    print(msg)
    print("Parsed:", result)
    print("All mandatory present:", ok)

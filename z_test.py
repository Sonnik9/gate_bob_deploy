import re
from typing import Tuple, Set


import re
from typing import Tuple, Optional


# === базовые таблицы символов ===
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
        """Удаляет лишние пробелы"""
        return " ".join(word.strip() for word in text.split())

    @staticmethod
    def cyr_to_latin(text: str) -> str:
        """Кириллицу в латиницу"""
        return "".join(CYR_TO_LATIN.get(ch, ch) for ch in text)

    @staticmethod
    def latin_to_cyr(text: str) -> str:
        """Латиницу в кириллицу"""
        text = "".join(LATIN_TO_CYR.get(ch, ch) for ch in text)
        return text.lower().replace(",", ".")

    @staticmethod
    def clean_number(num_str: str) -> float:
        """Очищает и преобразует строку с числом"""
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

    # ===============================================================
    #  ОСНОВНОЙ ПАРСЕР
    # ===============================================================
    def parse_tg_message(self, message: str, tag: str = "") -> Tuple[dict, bool]:
        raw_text = message or ""
        raw_lower = raw_text.lower()

        # --- Автоопределение ветки ---
        if not tag:
            if "#soft" in raw_lower:
                tag = "#soft"
            elif "trading pair" in raw_lower:
                tag = "trading pair"

        # --- Очистка мусора, но сохраняем кириллицу и # ---
        # message = re.sub(r"[^\w\s.,:/\-#+]", " ", raw_text)
        message = self.clean_whitespace(message.strip())

        # --- Выбор транслита (важно!) ---
        if tag == "#soft":
            text = self.latin_to_cyr(message)  # только заменяем латиницу на кириллицу
        else:
            text = self.cyr_to_latin(message)  # англ. формат

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        text_joined = " ".join(lines)

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

        # ===============================================================
        #  ВЕТКА #SOFT
        # ===============================================================
        if tag == "#soft":
            print("soft")

            # символ (ищем по $)
            if lines:
                m_symbol = re.search(r"\$([а-яa-z0-9]+)", lines[0], re.IGNORECASE)
                if m_symbol:
                    result["symbol"] = (
                        m_symbol.group(1).upper()
                    )

            # позиция: лонг/шорт
            for line in lines:
                if "лонг" in line:
                    result["pos_side"] = "LONG"
                elif "шорт" in line:
                    result["pos_side"] = "SHORT"

            patterns = {
                "entry_price": r"вход\s*[-–—:]?\s*([\d\s.]+)",
                "stop_loss": r"стоп\s*[-–—:]?\s*([\d\s.]+)",
                "take_profit1": r"тейк\s*[-–—:]?\s*([\d\s.]+)",
                "leverage": r"плечо\s*[-–—:]?\s*[хx]?\s*(\d+)"
            }

            for key, pattern in patterns.items():
                m = re.search(pattern, text)
                if m:
                    if key == "leverage":
                        try:
                            result[key] = int(m.group(1))
                        except ValueError:
                            pass
                    else:
                        result[key] = self.clean_number(m.group(1))

        # ===============================================================
        #  ВЕТКА TRADING PAIR
        # ===============================================================
        elif tag == "trading pair":
            m_pair = re.search(r"trading\s*pair\s*[:\- ]*\s*([a-zA-Zа-яА-Я0-9]+)", text_joined, re.IGNORECASE)
            if m_pair:
                result["symbol"] = m_pair.group(1)
            if not result["symbol"]:
                m_symbol = re.search(r"\b([A-Za-zА-Яа-я0-9]{2,6})\s*/\s*USDT\b", text_joined, re.IGNORECASE)
                if m_symbol:
                    result["symbol"] = m_symbol.group(1)

            if re.search(r"\blong\b", text_joined, re.IGNORECASE):
                result["pos_side"] = "LONG"
            elif re.search(r"\bshort\b", text_joined, re.IGNORECASE):
                result["pos_side"] = "SHORT"

            m_x = re.search(r"[xхXХ]\s*(\d{1,3})", text_joined)
            if m_x:
                result["leverage"] = int(m_x.group(1))
            m_rev = re.search(r"(\d{1,3})\s*[xхXХ]", text_joined)
            if not result["leverage"] and m_rev:
                result["leverage"] = int(m_rev.group(1))

            m_entry = re.search(r"\bentry[_\s\-:]*price\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
            if m_entry:
                result["entry_price"] = self.clean_number(m_entry.group(1))

            m_sl = re.search(r"\bstop[_\s\-:]*loss\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
            if m_sl:
                result["stop_loss"] = self.clean_number(m_sl.group(1))

            m_tp1 = re.search(r"\b(?:tp|take\s*profit)\s*1\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
            if m_tp1:
                result["take_profit1"] = self.clean_number(m_tp1.group(1))
            if result["take_profit1"] is None:
                m_tpf = re.search(r"take\s*profit\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
                if m_tpf:
                    result["take_profit1"] = self.clean_number(m_tpf.group(1))

            m_tp2 = re.search(r"\b(?:tp|take\s*profit)\s*2\b[\s:\-–—=]+([\d.,]+)", text_joined, re.IGNORECASE)
            if m_tp2:
                result["take_profit2"] = self.clean_number(m_tp2.group(1))

        # ===============================================================
        #  ДОП. ФЛАГИ
        # ===============================================================
        text_lower = text_joined.lower()
        result["force_limit"] = "#limit" in text_lower
        result["half_margin"] = any(x in text_lower for x in [
            "1/2 size", "1\\2 size", "0.5 size", "half size",
            "половина позиции", "половина маржи"
        ])

        # ===============================================================
        #  ФИНАЛ
        # ===============================================================
        # print(result)
        base_symbol = self.cyr_to_latin(result["symbol"]).upper()
        if not base_symbol:
            return {}, False
        result["symbol"] = base_symbol.replace("USDT", "").replace("_", "").replace("-", "") + "_USDT"

        mandatory = ["symbol", "pos_side", "entry_price", "stop_loss", "take_profit1", "leverage"]
        ok = all(result[k] for k in mandatory)
        return result, ok

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
    """,
    """Trading pair : CRO / USDT 
    LONG X10 

    1/2 size

    Entry price: 0.153
    Stop-loss: 0.1433

    Take profit1: 0.1879

    comment so vsyakoy huyney
    """,
    """Trading pair : ETH / USDT 
    LONG X10 

    Entry price: 3780
    Take Profit1: 3900
    Stop Loss: 3700
    1/2 size

    comment so vsyakoy huyney""",
    """Trading pair : ETH / USDT 
    LONG X10 

    Entry price: 3780
    Take Profit1: 3900
    Stop Loss: 3700
    comment so vsyakoy huyney
    1/2 size
    #limit
    """,
    """$ETHUSDT  

    шорт

    вход - 3720 ; cтоп - 3900
    тейк - 3500 ; плечо - х80

    #soft""",
    """$BTCUSDT  

    лонг

    вход - 107855.2 ; cтоp - 107323.2
    тейк - 108307.5 ; плечо - 80х

    #soft"""
]

parser = TgParser()
for i, msg in enumerate(test_messages, start=1):
    result, ok = parser.parse_tg_message(msg, tag="")
    print(f"\n=== Test {i} ===")
    print(msg)
    print("Parsed:", result)
    print("All mandatory present:", ok)

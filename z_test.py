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
        message = re.sub(r"[^\w\s.,:/\-#+]", " ", message)
        message = self.clean_whitespace(message.strip())
        text = self.cyr_to_latin(message) if tag != "#soft" else self.latin_to_cyr(message)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        text_joined = " ".join(lines)

        result = {
            "symbol": "", "pos_side": None, "entry_price": None,
            "stop_loss": None, "take_profit1": None, "take_profit2": None,
            "leverage": None, "force_limit": False, "half_margin": False
        }

        if tag == "trading pair":
            m_symbol = re.search(r"\btrading[_\- :]*pair[_\- :]*([a-z0-9]+)\s*(?:/ ?usdt)?", text_joined, re.IGNORECASE)
            if m_symbol:
                result["symbol"] = m_symbol.group(1).upper()
            m_pos = re.search(r"\b(long|short)\b\s*[xхXХ]\s*(\d+)", text_joined, re.IGNORECASE)
            if m_pos:
                result["pos_side"] = m_pos.group(1).upper()
                result["leverage"] = int(m_pos.group(2))
            m_entry = re.search(r"\bentry[_\- :]*price[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
            if m_entry:
                result["entry_price"] = self.clean_number(m_entry.group(1))
            m_sl = re.search(r"\bstop[_\- :]*loss[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
            if m_sl:
                result["stop_loss"] = self.clean_number(m_sl.group(1))
            m_tp1 = re.search(r"\btake[_\- :]*profit1?[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
            if m_tp1:
                result["take_profit1"] = self.clean_number(m_tp1.group(1))
            m_tp2 = re.search(r"\btake[_\- :]*profit2[_\- :]*[:\-]?\s*([\d.,]+)", text_joined, re.IGNORECASE)
            if m_tp2:
                result["take_profit2"] = self.clean_number(m_tp2.group(1))

        text_lower = text_joined.lower()
        result["force_limit"] = "#limit" in text_lower
        result["half_margin"] = any(x in text_lower for x in ["1/2 size", "0.5% size", "1\\2 size", "0.5 size", "half size", "половина позиции", "половина маржи"])

        base_symbol = self.cyr_to_latin(result["symbol"]).upper()
        if not base_symbol:
            return {}, False
        result["symbol"] = base_symbol.replace("USDT", "").replace("_", "").replace("-", "") + "_USDT"
        mandatory_keys = ["symbol", "pos_side", "entry_price", "stop_loss", "take_profit1", "leverage"]
        all_present = all(result[k] for k in mandatory_keys)
        return result, all_present


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
    """
]

parser = TgParser()
for i, msg in enumerate(test_messages, start=1):
    result, ok = parser.parse_tg_message(msg, tag="trading pair")
    print(f"\n=== Test {i} ===")
    print(msg)
    print("Parsed:", result)
    print("All mandatory present:", ok)



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

    #     if tag == "#soft" or not tag:
    #         # символ (ищем по $)
    #         if lines:
    #             m_symbol = re.search(r"\$([а-яa-z0-9]+)", lines[0], re.IGNORECASE)
    #             if m_symbol:
    #                 result["symbol"] = m_symbol.group(1).upper()

    #         # позиция: лонг/шорт
    #         for line in lines:
    #             if "лонг" in line:
    #                 result["pos_side"] = "LONG"
    #             elif "шорт" in line:
    #                 result["pos_side"] = "SHORT"

    #         patterns = {
    #             "entry_price": r"вход\s*[-–—:]?\s*([\d\s.]+)",
    #             "stop_loss": r"стоп\s*[-–—:]?\s*([\d\s.]+)",
    #             "take_profit1": r"тейк\s*[-–—:]?\s*([\d\s.]+)",
    #             "leverage": r"плечо\s*[-–—:]?\s*[хx]?\s*(\d+)"
    #         }

    #         for key, pattern in patterns.items():
    #             m = re.search(pattern, text_joined)
    #             if m:
    #                 if key == "leverage":
    #                     try:
    #                         result[key] = int(m.group(1))
    #                     except ValueError:
    #                         pass
    #                 else:
    #                     result[key] = self.clean_number(m.group(1))

    #     elif tag == "trading pair":
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

    #     text_lower = text_joined.lower()
    #     result["force_limit"] = "#limit" in text_lower
    #     result["half_margin"] = any(x in text_lower for x in ["1/2 size", "1\2 size", "0.5% size", "1\\2 size", "1\\2 size", "0.5 size", "half size", "половина позиции", "половина маржи"])

    #     base_symbol = self.cyr_to_latin(result["symbol"]).upper()
    #     if not base_symbol:
    #         return {}, False
    #     result["symbol"] = base_symbol.replace("USDT", "").replace("_", "").replace("-", "") + "_USDT"
    #     mandatory_keys = ["symbol", "pos_side", "entry_price", "stop_loss", "take_profit1", "leverage"]
    #     all_present = all(result[k] for k in mandatory_keys)
    #     return result, all_present
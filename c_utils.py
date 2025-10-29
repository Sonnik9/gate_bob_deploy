from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
from a_config import SLIPPAGE_PCT, PRECISION, EMO_SUCCESS, EMO_LOSE, EMO_ZERO
from b_context import BotContext, default_record
from c_log import ErrorHandler, TZ_LOCATION
import math
from pprint import pprint
from decimal import Decimal, getcontext
import time
import traceback


getcontext().prec = 28  # точность Decimal

def fix_price_scale(price: float, cur_price: float) -> float:
    """
    Универсальная поправка масштаба: ищем ближайшую степень 10,
    которая приближает цену к рыночной.
    """
    if not price or not cur_price or price <= 0 or cur_price <= 0:
        return price

    price_d = Decimal(price)
    cur_price_d = Decimal(cur_price)
    ratio = cur_price_d / price_d

    # Вычисляем оптимальную степень 10
    multiplier = Decimal(10) ** Decimal(round(math.log10(float(ratio))))

    # Слишком малая цена — увеличиваем
    if multiplier >= 10:
        return float(price_d * multiplier)
    # Слишком большая цена — уменьшаем
    elif multiplier <= Decimal("0.1"):
        return float(price_d * multiplier)
    return float(price_d)

def format_duration(ms: int) -> str:
    """
    Конвертирует миллисекундную разницу в формат "Xh Ym" или "Xm" или "Xs".
    :param ms: длительность в миллисекундах
    """
    if ms is None:
        return ""
    
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    if hours > 0 and minutes > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0 and seconds > 0:
        return f"{minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m"
    else:
        return f"{seconds}s"
    
def apply_slippage(price: float, slippage_pct: float, pos_side: str) -> float:
    """
    Корректирует цену закрытия с учётом проскальзывания.
    
    price: float - цена закрытия/текущая
    slippage_pct: float - проскальзывание в процентах (например 0.1 для 0.1%)
    pos_side: 'LONG' или 'SHORT'
    """
    if not (price and slippage_pct and pos_side):
        return price
    sign = 1 if pos_side.upper() == "LONG" else -1
    return price * (1 - sign * slippage_pct / 100)

def milliseconds_to_datetime(milliseconds):
    if milliseconds is None:
        return "N/A"
    try:
        ms = int(milliseconds)   # <-- приведение к int
        if milliseconds < 0: return "N/A"
    except (ValueError, TypeError):
        return "N/A"

    if ms > 1e10:  # похоже на миллисекунды
        seconds = ms / 1000
    else:
        seconds = ms

    dt = datetime.fromtimestamp(seconds, TZ_LOCATION)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def to_human_digit(value):
    if value is None:
        return "N/A"
    getcontext().prec = PRECISION
    dec_value = Decimal(str(value)).normalize()
    if dec_value == dec_value.to_integral():
        return format(dec_value, 'f')
    else:
        return format(dec_value, 'f').rstrip('0').rstrip('.')  

def safe_float(value: Any, default: float = 0.0) -> float:
    """Преобразует значение в float, если не удалось — возвращает default"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def safe_int(value: Any, default: int = 0) -> int:
    """Преобразует значение в int, если не удалось — возвращает default"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def safe_round(value: Any, ndigits: int = 2, default: float = 0.0) -> float:
    """Безопасный round для None или нечисловых значений"""
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return default



class Utils:
    def __init__(
            self,
            context: BotContext,
            info_handler: ErrorHandler,
        ):    
        info_handler.wrap_foreign_methods(self)
        self.context = context
        self.info_handler = info_handler


    @staticmethod
    def parse_precision(data: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
        """
        Возвращает параметры для торговли конкретным фьючерсом (Gate.io):
        - ctVal: стоимость контракта (quanto_multiplier)
        - lotSz: минимальный шаг размера позиции (order_size_min)
        - contract_precision: количество знаков после точки для лота
        - price_precision: количество знаков после точки для цены
        - max_leverage: максимальное плечо
        """
        if not data:
            return None

        for info in data:
            if info.get("name") == symbol:
                # вспомогательная функция для подсчёта знаков после точки
                def count_precision(value_str: str) -> int:
                    if not value_str:
                        return 0
                    parts = str(value_str).split(".")
                    return len(parts[1]) if len(parts) > 1 else 0

                try:
                    ctVal_str = str(info.get("quanto_multiplier") or "1")
                    lot_sz_str = str(info.get("order_size_min") or "1")
                    tick_sz_str = str(info.get("order_price_round") or "0.01")
                    max_leverage = (
                        info.get("leverage_max")
                        or info.get("max_leverage")
                        or info.get("leverUp")
                        or None
                    )

                    return {
                        "ctVal": float(ctVal_str),
                        "lotSz": float(lot_sz_str),
                        "contract_precision": count_precision(lot_sz_str),
                        "price_precision": count_precision(tick_sz_str),
                        "max_leverage": int(float(max_leverage)) if max_leverage else None,
                    }

                except Exception as e:
                    print(f"[parse_precision][{symbol}] Ошибка при парсинге: {e}")
                    return None

        print(f"[parse_precision] Нет данных для символа {symbol}")
        return None    

    def contract_calc(
        self,
        margin_size: float,
        entry_price: float,
        leverage: float,
        ctVal: float,
        lotSz: float,
        contract_precision: int,
        volume_rate: float = 100.0,
        debug_label: str = None
    ) -> Optional[float]:
        """
        Рассчитывает количество контрактов на основе входных параметров.
        """
        log_prefix = f"{debug_label}: " if debug_label else ""
        self.info_handler.debug_info_notes(f"{log_prefix}Starting contract_calc with inputs: margin_size={margin_size}, entry_price={entry_price}, leverage={leverage}, ctVal={ctVal}, lotSz={lotSz}, contract_precision={contract_precision}, volume_rate={volume_rate}")

        if any(x is None or not isinstance(x, (int, float)) or x <= 0 for x in [margin_size, entry_price, leverage, ctVal, lotSz]):
            self.info_handler.debug_error_notes(f"{log_prefix}Invalid input parameters in contract_calc: margin_size={margin_size}, entry_price={entry_price}, leverage={leverage}, ctVal={ctVal}, lotSz={lotSz}")
            return None

        try:
            deal_amount = margin_size * (volume_rate / 100.0)
            self.info_handler.debug_info_notes(f"{log_prefix}Calculated deal_amount = {deal_amount}")

            base_qty = (deal_amount * leverage) / entry_price
            self.info_handler.debug_info_notes(f"{log_prefix}Calculated base_qty = {base_qty}")

            raw_contracts = base_qty / ctVal
            self.info_handler.debug_info_notes(f"{log_prefix}Calculated raw_contracts = {raw_contracts}")

            rounded_steps = round(raw_contracts / lotSz) * lotSz
            self.info_handler.debug_info_notes(f"{log_prefix}Calculated rounded_steps = {rounded_steps}")

            contracts = round(rounded_steps, contract_precision)
            self.info_handler.debug_info_notes(f"{log_prefix}Calculated contracts = {contracts}")

            if contracts <= 0:
                self.info_handler.debug_error_notes(f"{log_prefix}Calculated contracts <= 0: {contracts}")
                return None
            return contracts
        except Exception as e:
            self.info_handler.debug_error_notes(f"{log_prefix}Error in contract_calc: {str(e)}")
            return None

    def build_take_profits(
        self,
        parsed_msg: dict,
        dop_tp: Optional[float],
        price_precision: Optional[int],
        pos_side: str,
        entry_price: float
    ) -> list[tuple[float, float]]:
        """
        Формирует список тейк-профитов (TP1, TP2) на основе данных из сообщения и дополнительного процента dop_tp.

        Args:
            parsed_msg (dict): Распарсенное сообщение с уровнями тейков.
            dop_tp (float | None): Дополнительный процент для расчёта TP2 от дистанции до TP1.
            price_precision (int | None): Точность округления цены.
            pos_side (str): "LONG" или "SHORT".
            entry_price (float): Цена входа в позицию.

        Returns:
            list[tuple[float, float]]: [(цена, доля)], где доля выражена в процентах (сумма 100).
        """

        # === Базовые проверки ===
        if not isinstance(entry_price, (int, float)) or entry_price <= 0:
            return []
        if not pos_side or pos_side.upper() not in {"LONG", "SHORT"}:
            return []

        # === Основная логика ===
        tp1_price = safe_float(parsed_msg.get("take_profit1")) or safe_float(parsed_msg.get("take_profit"))
        tp2_price = safe_float(parsed_msg.get("take_profit2"))

        # dop_tp — процент от расстояния до TP1
        if tp1_price and dop_tp and not tp2_price:
            sign = 1 if pos_side.upper() == "LONG" else -1
            try:
                distance = abs(tp1_price - entry_price)
                tp2_price = entry_price + sign * distance * dop_tp / 100
                if price_precision is not None:
                    tp2_price = round(tp2_price, price_precision)
            except Exception:
                return []

        # === Формируем финальный список ===
        prices = [p for p in (tp1_price, tp2_price) if isinstance(p, (int, float))]

        if not prices:
            return []
        if len(prices) == 1:
            return [(prices[0], 100.0)]

        # Всегда сортируем по возрастанию — TP1 ниже TP2
        prices.sort()
        return [(prices[0], 50.0), (prices[1], 50.0)]

    # // utils method:
    def format_pnl(
        self,
        data: dict,
        is_print: bool = True
    ) -> Optional[str]:
        cur_time = milliseconds_to_datetime(data.get("cur_time"))

        pnl_usdt = data.get("pnl_usdt")
        roi = data.get("roi")
        time_in_deal = data.get("time_in_deal", "N/A")

        emo = "N/A"
        pnl_usdt_str = "N/A"
        roi_str = "N/A"

        if roi is not None:
            roi_str = f"{roi:.3f} %"
            if roi > 0:
                emo = f"{EMO_SUCCESS} SUCCESS"
            elif roi < 0:
                emo = f"{EMO_LOSE} LOSE"
            else:
                emo = f"{EMO_ZERO} 0 P&L"

        if pnl_usdt is not None:
            if roi is not None:
                if roi > 0:
                    pnl_usdt_str = f"+ {pnl_usdt:.3f}"
                elif roi < 0:
                    pnl_usdt_str = f"- {abs(pnl_usdt):.3f}"
                else:
                    pnl_usdt_str = f"{pnl_usdt:.4f}"
            else:
                pnl_usdt_str = f"{pnl_usdt:.3f}"

        msg = (
            # f"PNL: {pnl_usdt_str} usdt | {roi_str}\n"
            f"PNL: {pnl_usdt_str} usdt\n"
            f"Close time - [{cur_time}]\n"
            # f"TIME IN DEAL - {time_in_deal}\n"
        )

        if is_print:
            print(msg)

        return msg

    async def pnl_report(
        self,
        symbol: str,
        pos_side: str,
        pos_data: dict,
        get_realized_pnl: Callable,
    ):
        """
        Отчет по реализованному PnL через API, с поправкой на плечо.
        Не использует текущую цену.
        """
        text_pnl = ""
        cur_time = int(time.time() * 1000)
        start_time = pos_data.get("c_time")

        realized_pnl = await get_realized_pnl(
            symbol=symbol,
            pos_side=pos_side.upper(),
            margin_size=pos_data.get("margin_vol"),
            start_time=start_time,
            end_time=cur_time,
            open_order_id=pos_data.get("order_id")
        )

        if realized_pnl is None or realized_pnl.get("pnl_usdt") is None:
            self.info_handler.debug_error_notes(f"[pnl_report] No PNL data for {symbol}_{pos_side}", is_print=True)
            return

        pnl_usdt = realized_pnl.get("pnl_usdt")
        roi = realized_pnl.get("roi")

        time_in_deal = cur_time - start_time if start_time else None
        time_in_deal_str = format_duration(time_in_deal) if time_in_deal else "N/A"

        # Корректный вызов format_pnl с позиционным аргументом data
        text_pnl = self.format_pnl(
            {
                "symbol": symbol,
                "pos_side": pos_side,
                "pnl_usdt": pnl_usdt,
                "roi": roi,
                "cur_time": cur_time,
                # "time_in_deal": time_in_deal_str,
            },
            is_print=True
        )

        return text_pnl
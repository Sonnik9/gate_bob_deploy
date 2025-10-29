import asyncio
import time
import aiohttp
from typing import *
from c_log import ErrorHandler


class BotContext:
    def __init__(self):
        """ Инициализируем глобальные структуры"""
        # //
        self.message_cache: list = []  # основной кеш сообщений
        self.tg_timing_cache = set()
        self.stop_bot = False
        self.start_bot_iteration = False
        self.stop_bot_iteration = False
        # //
        self.instruments_data: dict = None
        self.prices: dict = None   
        self.users_configs: dict = {}
        self.order_status_book: dict = {}
        self.force_close: dict = {}
        self.force_risks: dict = {}
        self.awaiting_input: dict = {}
        self.queues_msg: dict = {}    
        self.queues_msg_lock = asyncio.Lock() 
        self.position_vars: dict = {}
        self.report_list: list = []
        self.session: Optional[aiohttp.ClientSession] = None
        self.signal_locks: dict = {}
        self.lock = asyncio.Lock() 
        self.main_menu = None


def default_record(symbol: str, pos_side: str, leverage: int, entry_price: Optional[float]) -> dict:
    return {
        "symbol": symbol,
        "pos_side": pos_side,
        "leverage": leverage,
        "order_type": "none",
        "entry_price": entry_price,
        "entry_status": "none",
        "tp1": None,
        "tp2": None,
        "tp1_status": "none",
        "tp2_status": "none",
        "sl": None,
        "sl_status": "none",
        "pnl_text": "PNL: сделка не завершена",
        "time": int(time.time() * 1000),
    }


class PositionVarsSetup:
    def __init__(self, context: BotContext, info_handler: ErrorHandler, parse_precision: Callable):   
        self.context = context
        info_handler.wrap_foreign_methods(self)
        self.info_handler = info_handler
        self.parse_precision = parse_precision
    
    @staticmethod
    def pos_vars_root_template():
        """Базовый шаблон переменных позиции"""
        return {  
            "leverage": None,
            "margin_vol": None,
            "vol_usdt": None,   
            "vol_assets": None,
            "contracts": None,
            "entry_price": None,  
            "pending_open": False,    
            "in_position": False, 
            "order_id": None,
            "tp_order1_id": None,
            "tp_order2_id": None,
            "sl_order_id": None,
            "c_time": None,    
            "settings_tag": None        
        }
            
    def set_pos_defaults(
            self,
            symbol: str,
            pos_side: str,
            instruments_data: List = None,
            reset_flag: bool = False
        ):
        """Безопасная инициализация структуры данных контроля позиций."""

        # Убедимся, что pos_side существует в данных символа
        if symbol not in self.context.position_vars:
            self.context.position_vars[symbol] = {}
        specs = None
        if instruments_data and "spec" not in self.context.position_vars[symbol]:
            try:
                specs: Optional[dict] = self.parse_precision(
                    data=instruments_data,
                    symbol=symbol
                )
                if not specs or not all(v is not None for v in specs.values()):                    
                    print(f"Нет нужных инструментов для монеты {symbol}. Возможно токен недоступен для торговли.")
                    return False
                
            except Exception as e:
                print(f"⚠️ [ERROR] при получении инструментов для {symbol}: {e}")
                return False            
            
            self.context.position_vars[symbol]["spec"] = specs
        if pos_side not in self.context.position_vars[symbol] or reset_flag:
            self.context.position_vars[symbol][pos_side] = self.pos_vars_root_template()

        return True
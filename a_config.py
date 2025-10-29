from typing import *

# --- CORE ---
TEG_ANCHOR_SET: Set = {"#soft", "trading pair", } # --------------------------  # теги целевых сообщений. СТАТИКА

# --- SECRETS CONFIG ---                 
# TG_BOT_TOKEN: str = "8190920390:AAE09pWhSVguG0iiBNIztM2Pe8zECPV4vSg" # токен тг бота old fin klient
TG_BOT_TOKEN: str = "7950631691:AAFntHbnAJlKcaTkypXTw2lVPWagiz3b_ak" # токен тг бота2 old fin klient

# -- UTILS ---
# BLACK_SYMBOLS: set = {"BTC_USDT"} # -------------# символы-исключения (не используем в торговле)
BLACK_SYMBOLS: dict = {}
TIME_ZONE: str = "UTC"
SLIPPAGE_PCT: float = 0.09 # % -- поправка для расчетов PnL. Откл -- None | 0.0
PRECISION: int = 28 # -- точность округления для малых чисел
PING_URL = "https://api.gateio.ws/api/v4/spot/time"
PING_INTERVAL: float = 10 # sec

# --- SYSTEM ---
TG_UPDATE_FREQUENCY: float = 1 # sec ---- частота запросов к тг при парсинге
POSITIONS_UPDATE_FREQUENCY: float = 1 # sec --- частота обновления данных позиции
MAIN_CYCLE_FREQUENCY: float = 1 # sec  ---- частота главного цикла
SIGNAL_PROCESSING_LIMIT: int = 10 # --------- ограничивает количество одновременной обработки сигналов
PING_UPDATE_INTERVAL: int = 10 # sec --- через сколько обновляем сессию

# --- STYLES ---
HEAD_WIDTH: int = 35
HEAD_LINE_TYPE: str = "" #  либо "_"
EMO_SUCCESS:str = "🟢"
EMO_LOSE: str = "🔴"
EMO_ZERO: str = "⚪"
EMO_ORDER_FILLED: str = "🤞"


# ------- BUTTON SETTINGS DEFAULT ------

INIT_USER_CONFIG = {
    "config": {
        "GATE": {
            "api_key": "",
            "api_secret": "",
        },
        "fin_settings": {
            "trading pair": {
                "margin_size": 1, # размер маржи в долларах
                "margin_mode": 2, # 1 - Изолиркаб 2 -- CROSSED
                "trigger_order_type": 2, # Тип тригерного ордера -- 1 лимитный, 2 рыночный  
                "order_type": 2, # 1 Тип ордера -- лимитками, 2 -- по маркету. 
                "leverage": 0, #  плечо. 0 -- будет брать из сигналов
                "dop_tp": None, # дополнительный тейк профит в % 1 - 100          
                "order_timeout": 60, # таймаут лимитного ордера в секундах
            },
            "soft": {
                "margin_size": 1, # размер маржи в долларах
                "margin_mode": 2, # 1 - Изолиркаб 2 -- CROSSED
                "trigger_order_type": 2, # Тип тригерного ордера -- 1 лимитный, 2 рыночный  
                "order_type": 2, # 1 Тип ордера -- лимитками, 2 -- по маркету. 
                "leverage": 0, #  плечо. 0 -- будет брать из сигналов
                "dop_tp": None, # дополнительный тейк профит в % 1 - 100          
                "order_timeout": 60, # таймаут лимитного ордера в секундах
            }
        }
    },
    "_await_field": None
}

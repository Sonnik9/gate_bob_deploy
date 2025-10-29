import asyncio
import time
import aiohttp
from pprint import pprint
from typing import *
from a_config import *
from b_context import BotContext, PositionVarsSetup, default_record
from b_network import NetworkManager
from TG.tg_parser import TgBotWatcherAiogram
# from TG.tg_notifier import TelegramNotifier, order_buttons_handler
from TG.tg_notifier import TelegramNotifier
from TG.tg_buttons import TelegramUserInterface
from API.GATE.gate import GateFuturesClient, GateApiResponseValidator
from aiogram import Bot, Dispatcher

from c_sync import Synchronizer
from c_log import ErrorHandler, log_time
from c_utils import Utils, fix_price_scale, to_human_digit
import hashlib
from math import isfinite
from d_templates import OrderTemplates
import traceback
import os

SIGNAL_REPEAT_TIMEOUT = 5


def validate_risk_order(
    pos_side: str,
    cur_price: float,
    sl: float | None,
    tp: float | None,
    epsilon_pct: float = 0.05,  # в %
    price_precision: int = 3,
) -> str:
    """
    Простая валидация SL/TP:
      LONG:  SL < cur_price < TP
      SHORT: SL > cur_price > TP
      Проверяется, чтобы дистанция превышала epsilon_pct (%).
    """

    def ok(x): return isinstance(x, (int, float)) and isfinite(x) and x > 0
    if not ok(cur_price):
        return "Invalid current price"
    ps = (pos_side or "").upper()
    if ps not in {"LONG", "SHORT"}:
        return "Invalid position side"

    def pct_diff(a, b): return abs(a - b) / a * 100

    # === LONG ===
    if ps == "LONG":
        if sl is not None:
            if not ok(sl):
                return "Invalid stop-loss"
            if sl >= cur_price:
                return f"Stop-loss must be BELOW current price (sl={sl}, cur={cur_price})"
            if pct_diff(cur_price, sl) < epsilon_pct:
                return f"Stop-loss too close (< {epsilon_pct}%)"
        if tp is not None:
            if not ok(tp):
                return "Invalid take-profit"
            if tp <= cur_price:
                return f"Take-profit must be ABOVE current price (tp={tp}, cur={cur_price})"
            if pct_diff(cur_price, tp) < epsilon_pct:
                return f"Take-profit too close (< {epsilon_pct}%)"

    # === SHORT ===
    else:
        if sl is not None:
            if not ok(sl):
                return "Invalid stop-loss"
            if sl <= cur_price:
                return f"Stop-loss must be ABOVE current price (sl={sl}, cur={cur_price})"
            if pct_diff(cur_price, sl) < epsilon_pct:
                return f"Stop-loss too close (< {epsilon_pct}%)"
        if tp is not None:
            if not ok(tp):
                return "Invalid take-profit"
            if tp >= cur_price:
                return f"Take-profit must be BELOW current price (tp={tp}, cur={cur_price})"
            if pct_diff(cur_price, tp) < epsilon_pct:
                return f"Take-profit too close (< {epsilon_pct}%)"

    return "ok"


class TelegramHandlerRegistry:
    """
    Единый регистратор всех Aiogram-хендлеров.
    Обеспечивает правильный порядок регистрации.
    """

    def __init__(
        self,
        dp: Dispatcher,
        interface: TelegramUserInterface,
        notifier: TelegramNotifier,
        tg_watcher: TgBotWatcherAiogram
    ):
        self.dp = dp
        self.interface = interface
        self.notifier = notifier
        self.tg_watcher = tg_watcher

    # def register_all(self):
    #     # ===== 1. Сначала watcher канала =====
    #     self.tg_watcher.register_handlers()

    #     # ===== 2. Специфичные хендлеры интерфейса =====
    #     self.interface.register_handlers()

    #     # ===== 3. Глобальный handler Notifier =====
    #     self.notifier.register_handlers()

    def register_all(self):
        # ===== 1. Сначала watcher канала =====
        self.tg_watcher.register_handlers()

        # ===== 3. Глобальный handler Notifier =====
        self.notifier.register_handlers()

        # ===== 2. Специфичные хендлеры интерфейса =====
        self.interface.register_handlers()


class Core:
    def __init__(self):
        self.context = BotContext()
        self.info_handler = ErrorHandler()
        self.bot = Bot(token=TG_BOT_TOKEN)
        self.dp = Dispatcher()
        self.tg_watcher = None
        self.notifier = None
        self.tg_interface = None
        self.positions_task = None
        self.instruments_data = {}

    async def _start_user_context(self, chat_id: int):
        """Инициализация юзер-контекста (сессии, клиентов, стримов и контролов)"""
        user_context = self.context.users_configs[chat_id]
        gate_cfg = user_context.get("config", {}).get("GATE", {})
        api_key = gate_cfg.get("api_key")
        api_secret = gate_cfg.get("api_secret")

        print("♻️ Пересоздаём user_context сессию")

        if hasattr(self, "connector") and self.connector:
            await self.connector.shutdown_session()
            self.connector = None

        self.connector = NetworkManager(context=self.context, info_handler=self.info_handler)

        self.gate_client = GateFuturesClient(api_key=api_key, api_secret=api_secret, context=self.context, info_handler=self.info_handler)

        self.utils = Utils(context=self.context, info_handler=self.info_handler)

        self.pos_setup = PositionVarsSetup(context=self.context, info_handler=self.info_handler, parse_precision=self.utils.parse_precision)
      

        # ✅ если notifier уже существует — связываем
        if self.notifier:
            self.sync = Synchronizer(
                context=self.context,
                info_handler=self.info_handler,
                set_pos_defaults=self.pos_setup.set_pos_defaults,
                pnl_report=self.utils.pnl_report,
                gate_client=self.gate_client,
                notifier=self.notifier,
                positions_update_frequency=POSITIONS_UPDATE_FREQUENCY,
                chat_id=chat_id
            )  
            self.templates = OrderTemplates(
                context=self.context,
                info_handler=self.info_handler,
                utils=self.utils,
                gate_client=self.gate_client,
                notifier=self.notifier
            )

            self.notifier.bind_templates(
                modify_sl=lambda *args, **kwargs: self.templates.modify_risk_orders(*args, **kwargs, modify_sl=True, modify_tp=False),
                modify_tp=lambda *args, **kwargs: self.templates.modify_risk_orders(*args, **kwargs, modify_sl=False, modify_tp=True),
                force_close=self.templates.force_position_close
            )

        self.info_handler.debug_info_notes(f"[start_user_context] User context initialized for chat_id: {chat_id}")

    async def complete_until_cancel(self, session: aiohttp.ClientSession, chat_id: str, fin_settings: dict, symbol: str,
                                  pos_side: str, pos_data: dict, record: dict, last_timestamp: int) -> bool:
        debug_label = f"[complete_until_cancel_{symbol}_{pos_side}]"
        self.info_handler.debug_info_notes(f"{debug_label} Waiting for position to open", is_print=True)

        # start_time = last_timestamp / 1000
        start_time = time.time()
        timeout = fin_settings.get("order_timeout", 30)
        pos_data["pending_open"] = True
        try:
            print(f"wait for cancel intil {timeout} sec")
            while (time.time() - start_time) < timeout and not self.context.stop_bot and not self.context.stop_bot_iteration:
                if pos_data.get("in_position"):
                    self.info_handler.debug_info_notes(f"{debug_label} Position opened successfully")
                    async with self.context.queues_msg_lock:
                        record["entry_status"] = "filled"

                    return True
                await asyncio.sleep(0.1)

            self.info_handler.debug_info_notes(f"{debug_label} Timeout: Position not opened within {timeout} seconds")

            async with self.context.queues_msg_lock:
                record["entry_status"] = "failed. Reason: TIME-OUT"

            try:
                cancel_result = await self.gate_client.cancel_all_orders_by_symbol_and_side(session=session, instId=symbol, pos_side=pos_side)
                main_cancelled = cancel_result.get("main_cancelled_count", 0)
                price_cancelled = cancel_result.get("price_cancelled_count", 0)
                main_error = GateApiResponseValidator.get_code(cancel_result.get("main_orders", {}))
                price_error = GateApiResponseValidator.get_code(cancel_result.get("price_orders", {}))

                if main_error or price_error:
                    error_msg = f"Main error: {main_error or 'None'}, Price error: {price_error or 'None'}"
                    self.info_handler.debug_error_notes(f"{debug_label} Cancellation failed: {error_msg}")
                    async with self.context.queues_msg_lock:
                        record.update({
                            "tp1_status": f"failed. Reason: {error_msg}",
                            "tp2_status": f"failed. Reason: {error_msg}",
                            "sl_status": f"failed. Reason: {error_msg}",
                        })

                    return False

                if main_cancelled > 0 or price_cancelled > 0:
                    self.info_handler.debug_info_notes(f"{debug_label} Cancelled {main_cancelled} main orders and {price_cancelled} trigger orders")
                    async with self.context.queues_msg_lock:
                        record.update({
                            "tp1_status": "cancelled",
                            "tp2_status": "cancelled",
                            "sl_status": "cancelled",
                        })
                else:
                    pass
                    # self.info_handler.debug_info_notes(f"{debug_label} No orders to cancel")
                    # async with self.context.queues_msg_lock:
                    #     record.update({
                    #         "tp1_status": "failed",
                    #         "tp2_status": "failed",
                    #         "sl_status": "failed",
                    #     })

                return main_cancelled > 0 or price_cancelled > 0

            except Exception as e:
                self.info_handler.debug_error_notes(f"{debug_label} Cancellation error: {str(e)}", is_print=True)
                pos_data["order_id"] = None
        
        finally:
            pos_data["pending_open"] = False
            async with self.context.queues_msg_lock:
                current_anchor = self.context.order_status_book.get(chat_id, {}).get((symbol, pos_side))
                current_anchor["last_data"] = record

            if record and current_anchor:
                await self.notifier.update_anchor_state(
                    chat_id=chat_id,
                    symbol=symbol,
                    pos_side=pos_side,
                    body=record,
                    force_message_id=current_anchor.get("message_id")
                )        

    async def complete_signal_task(self, chat_id: str, fin_settings: dict, parsed_msg: dict, context_vars: dict, last_timestamp: int, cur_price: float):
        symbol = parsed_msg.get("symbol")
        pos_side = parsed_msg.get("pos_side")
        symbol_data = context_vars[symbol]
        pos_data = symbol_data[pos_side]

        leverage = pos_data.get("leverage")
        entry_price = parsed_msg.get("entry_price")

        stop_loss = parsed_msg.get("stop_loss")
        price_precision = symbol_data.get("spec", {}).get("price_precision")
        order_type = fin_settings.get("order_type")
        force_limit: bool = parsed_msg.get("force_limit")
        half_margin: bool = parsed_msg.get("half_margin")
        settings_order_type = "limit" if order_type == 1 else "market"
        order_type = "limit" if force_limit else settings_order_type

        record = default_record(
            symbol=symbol,
            pos_side=pos_side,
            leverage=pos_data.get("leverage"),
            entry_price=pos_data.get("entry_price")
        )
        record["symbol"] = symbol
        record["pos_side"] = pos_side
        record["leverage"] = leverage
        record["order_type"] = order_type
        record["entry_price"] = round(float(entry_price), price_precision)
        record["entry_status"] = "waiting"

        async with self.context.queues_msg_lock:
            self.context.order_status_book.setdefault(chat_id, {})[(symbol, pos_side)] = {
                "message_id": None,  # ещё нет сообщения
                "last_data": record,
                "hash": None,
                "closed": False
            }
            current_anchor = self.context.order_status_book.get(chat_id, {}).get((symbol, pos_side))

        take_profits = self.utils.build_take_profits(
            parsed_msg=parsed_msg,
            dop_tp=fin_settings.get("dop_tp"),
            price_precision=price_precision,
            pos_side=pos_side,
            entry_price=entry_price
        )
        # print("take_profits:")
        # print(take_profits)
        if not take_profits:
            record["tp1_status"] = "Invalid data"
            record["tp2_status"] = "Invalid data"

        else:
            risk_sl_ok_status = validate_risk_order(
                pos_side=pos_side,
                cur_price=cur_price,
                sl=stop_loss,
                tp=None,
                epsilon_pct=0.05,
            ) 

            if risk_sl_ok_status != "ok":
                record["entry_status"] = risk_sl_ok_status

            for tp_val, vol in take_profits:
                risk_tp_ok_status = validate_risk_order(
                    pos_side=pos_side,
                    cur_price=cur_price,
                    sl=None,
                    tp=tp_val,
                    epsilon_pct=0.05,
                ) 
                if risk_tp_ok_status != "ok":
                    break
                    
            if risk_tp_ok_status != "ok":
                record["entry_status"] = risk_tp_ok_status

        if record["entry_status"] != "waiting" or record.get("tp1_status") == "Invalid data":
            async with self.context.queues_msg_lock:
                current_anchor["last_data"] = record

            if record and current_anchor:
                await self.notifier.update_anchor_state(
                    chat_id=chat_id,
                    symbol=symbol,
                    pos_side=pos_side,
                    body=record,
                    force_message_id=current_anchor.get("message_id")
                )
            return

        order_template_response = await self.templates.initial_order_template(
            session=self.context.session,
            record=record,
            current_anchor=current_anchor,
            chat_id=chat_id,
            fin_settings=fin_settings,
            symbol=symbol,
            leverage=leverage,
            entry_price=entry_price,
            pos_side=pos_side,
            symbol_data=symbol_data,
            pos_data=pos_data,
            stop_loss=stop_loss,
            take_profits=take_profits,
            order_type=order_type,
            half_margin=half_margin
        )
        
        if order_type == "limit" and order_template_response:
            asyncio.create_task(
                self.complete_until_cancel(
                    session=self.context.session,
                    chat_id=chat_id,
                    fin_settings=fin_settings,
                    symbol=symbol,
                    pos_side=pos_side,
                    pos_data=pos_data,
                    record=record,
                    last_timestamp=last_timestamp
                )
            )

    async def handle_signal(self, chat_id: str, fin_settings: dict, settings_tag: str, parsed_msg: dict, symbol: str, pos_side: str,
                          last_timestamp: int, lock: asyncio.Lock, msg_key: str) -> None:
        
        async with lock:
            try:
                if not self.pos_setup.set_pos_defaults(symbol, pos_side, self.instruments_data):
                    return
                
                # pprint(self.context.position_vars)

                while not self.sync._first_update_done:
                    await asyncio.sleep(0.1)

                context_vars: Dict = self.context.position_vars
                pos_data = context_vars.get(symbol, {}).get(pos_side, {})

                if pos_data.get("in_position") or pos_data.get("pending_open"):
                    self.info_handler.debug_info_notes(f"[handle_signal] Skip: already in_position or pending {symbol} {pos_side}")
                    return

                pos_data["settings_tag"] = settings_tag

                max_leverage = context_vars.get(symbol, {}).get("spec", {}).get("max_leverage", 20)
                leverage = min(fin_settings.get("leverage") or parsed_msg.get("leverage"), max_leverage)
                pos_data["leverage"] = leverage
                pos_data["margin_vol"] = fin_settings.get("margin_size")

                self.context.prices = await self.gate_client.get_all_current_prices(session=self.context.session)
                cur_price = self.context.prices.get(symbol)
                for key in ("entry_price", "take_profit1", "take_profit2", "stop_loss", "take_profit"):
                    parsed_msg[key] = fix_price_scale(parsed_msg.get(key), cur_price)

                await self.complete_signal_task(chat_id=chat_id, fin_settings=fin_settings, parsed_msg=parsed_msg,
                                              context_vars=context_vars, last_timestamp=last_timestamp, cur_price=cur_price)
            except Exception as e:
                self.info_handler.debug_error_notes(f"[handle_signal] Error: {str(e)}\n{traceback.format_exc()}", is_print=True)
            finally:
                if msg_key in self.context.signal_locks:
                    try:
                        del self.context.signal_locks[msg_key]
                    except KeyError:
                        pass

    async def _run_iteration(self) -> None:
        print("[CORE] Iteration started")

        for num, (chat_id, user_cfg) in enumerate(self.context.users_configs.items(), start=1):
            print(f"[DEBUG] Processing user {num} | chat_id: {chat_id}")
            
            if num > 1:
                self.info_handler.debug_info_notes(f"Бот настроен только для одного пользователя! Для текущего chat_id: {chat_id} опция торговли недоступна. {log_time()}")
                continue

            try:
                print(f"[DEBUG] Starting user context for chat_id: {chat_id}")
                await self._start_user_context(chat_id=chat_id)

                user_config: Dict[str, Any] = self.context.users_configs.get(chat_id, {})
                gate_cfg: Dict[str, Any] = user_config.get("config", {}).get("GATE", {})
                print(f"[DEBUG] gate config for user {chat_id}: {gate_cfg}")

                required_keys = ["api_key", "api_secret"]
                for key in required_keys:
                    if key not in gate_cfg or gate_cfg[key] is None:
                        print(f"[WARNING] gate {key} not set for user {chat_id}")

            except Exception as e:
                err_msg = f"[ERROR] Failed to start user context for chat_id {chat_id}: {e}"
                self.info_handler.debug_error_notes(err_msg, is_print=True)
                continue

        self.connector.start_ping_loop()
        start_time = time.time()
        session_timeout = 300

        while not self.context.session and time.time() - start_time < session_timeout:
            await asyncio.sleep(0.2)
        if not self.context.session:
            self.info_handler.debug_error_notes("[_run_iteration] Failed to initialize session", is_print=True)
            self.context.stop_bot_iteration = True
            return

        try:
            self.instruments_data = await self.gate_client.get_instruments(session=self.context.session)
            if self.instruments_data:
                print(f"[DEBUG] Instruments fetched: {len(self.instruments_data)} items")
            else:
                self.info_handler.debug_error_notes(f"[ERROR] Failed to fetch instruments", is_print=True)
        except Exception as e:
            self.info_handler.debug_error_notes(f"[ERROR] Failed to fetch instruments: {e}", is_print=True)

        asyncio.create_task(self.sync.refresh_positions_task())

        instrume_update_interval = 3000.0
        last_instrume_time = time.monotonic()

        while not self.context.stop_bot_iteration and not self.context.stop_bot:
            try:
                signal_tasks_val = self.context.message_cache[-SIGNAL_PROCESSING_LIMIT:] if self.context.message_cache else None
                if not signal_tasks_val:
                    await asyncio.sleep(MAIN_CYCLE_FREQUENCY)
                    continue

                for signal_item in signal_tasks_val:
                    if not signal_item:
                        continue

                    matched_tag, message, last_timestamp = signal_item
                    if not (message and last_timestamp):
                        print("[DEBUG] Invalid signal item, skipping")
                        continue

                    # msg_key = f"{last_timestamp}_{hash(message)}"
                    msg_key = f"{last_timestamp}_{hashlib.md5(message.encode()).hexdigest()}"

                    if msg_key in self.context.tg_timing_cache:
                        continue
                    self.context.tg_timing_cache.add(msg_key)

                    parsed_msg, all_present = self.tg_watcher.parse_tg_message(message=message, tag=matched_tag)
                    print(f"[DEBUG] Parse msg: {parsed_msg}")
                    if not all_present:
                        print(f"[DEBUG] Parse error: {parsed_msg}")
                        continue

                    symbol = parsed_msg.get("symbol")
                    pos_side = parsed_msg.get("pos_side")

                    if symbol in BLACK_SYMBOLS:
                        continue

                    diff_sec = time.time() - (last_timestamp / 1000)

                    settings_tag = matched_tag.replace("#", "").lower()

                    print(f"[DEBUG] Handling signal for {symbol} {pos_side} with settings_tag: {settings_tag}")

                    for num, (chat_id, user_cfg) in enumerate(self.context.users_configs.items(), start=1):
                        if num > 1:
                            continue

                        fin_settings_root = user_cfg.get("config", {}).get("fin_settings", {})
                        fin_settings = fin_settings_root.get(settings_tag, {})

                        order_timeout = fin_settings.get("order_timeout", 60)
                        if diff_sec < order_timeout:
                            if msg_key in self.context.signal_locks:
                                continue

                            # print("fin_settings (_run_iteration):")
                            # pprint(fin_settings)

                            cur_lock = self.context.signal_locks[msg_key] = asyncio.Lock()

                            asyncio.create_task(self.handle_signal(
                                chat_id=chat_id,
                                fin_settings=fin_settings,
                                settings_tag=settings_tag,
                                parsed_msg=parsed_msg,
                                symbol=symbol,
                                pos_side=pos_side,
                                last_timestamp=last_timestamp,
                                lock=cur_lock,
                                msg_key=msg_key
                            ))

            except Exception as e:
                err_msg = f"[ERROR] main loop: {e}\n{traceback.format_exc()}"
                self.info_handler.debug_error_notes(err_msg, is_print=True)

            finally:
                now = time.monotonic()
                if now - last_instrume_time >= instrume_update_interval:
                    try:
                        self.instruments_data = await self.gate_client.get_instruments(session=self.context.session)
                        if not self.instruments_data:
                            self.info_handler.debug_error_notes(f"[ERROR] Failed to fetch instruments", is_print=True)
                    except Exception as e:
                        self.info_handler.debug_error_notes(f"[ERROR] Failed to fetch instruments: {e}", is_print=True)
                    last_instrume_time = now

                await asyncio.sleep(MAIN_CYCLE_FREQUENCY)

    async def run_forever(self, debug: bool = True):
        if debug:
            print("[CORE] run_forever started")

        if self.tg_interface is None:
            self.tg_watcher = TgBotWatcherAiogram(
                dp=self.dp,
                channel_id=None,
                tags_set=TEG_ANCHOR_SET,
                context=self.context,
                info_handler=self.info_handler
            )

            self.notifier = TelegramNotifier(
                bot=self.bot,
                dp=self.dp,
                context=self.context,
                info_handler=self.info_handler
            )

            self.tg_interface = TelegramUserInterface(
                bot=self.bot,
                dp=self.dp,
                context=self.context,
                info_handler=self.info_handler,
                notifier=self.notifier
            )

            # ===== Регистрируем все хендлеры =====
            TelegramHandlerRegistry(
                dp=self.dp,
                interface=self.tg_interface,
                notifier=self.notifier,
                tg_watcher=self.tg_watcher
            ).register_all()

            # ===== Запуск Telegram polling =====
            await self.tg_interface.run()
            await asyncio.sleep(1) 

        while not self.context.stop_bot:
            if debug: print("[CORE] Новый цикл run_forever, обнуляем флаги итерации")
            self.context.start_bot_iteration = False
            self.context.stop_bot_iteration = False

            if debug: print("[CORE] Ожидание кнопки START...")
            while not self.context.start_bot_iteration and not self.context.stop_bot:
                await asyncio.sleep(0.3)

            if self.context.stop_bot:
                if debug: print("[CORE] Stop флаг поднят, выходим из run_forever")
                break

            try:
                if debug: print("[CORE] Запуск торговой итерации (_run_iteration)...")
                await self._run_iteration()
                if debug: print("[CORE] Торговая итерация завершена")
            except Exception as e:
                self.info_handler.debug_error_notes(f"[CORE] Ошибка в итерации: {e}", is_print=True)

            try:
                if debug: print("[CORE] Очистка ресурсов итерации (_shutdown_iteration)...")
                await self._shutdown_iteration(debug=debug)
                if debug: print("[CORE] Очистка ресурсов завершена")
            except Exception as e:
                self.info_handler.debug_error_notes(f"[CORE] Ошибка при shutdown итерации: {e}", is_print=True)

            if self.context.stop_bot_iteration:
                self.info_handler.debug_info_notes("[CORE] Перезапуск по кнопке STOP", is_print=True)
                if debug: print("[CORE] Ожидание следующего START после STOP")
                continue

        if debug: print("[CORE] run_forever finished")

    async def _shutdown_iteration(self, debug: bool = True):
        if self.positions_task:
            self.positions_task.cancel()
            try:
                await self.positions_task
            except Exception as e:
                if debug:
                    print(f"[CORE] positions_flow_manager error: {e}")
            self.positions_task = None

        if getattr(self, "connector", None):
            try:
                await asyncio.wait_for(self.connector.shutdown_session(), timeout=5)
            except Exception as e:
                if debug:
                    print(f"[CORE] connector.shutdown_session() error: {e}")
            finally:
                self.context.session = None
                self.connector = None

        self.gate_client = None
        self.sync = None
        self.utils = None
        self.pos_setup = None

        self.context.position_vars = {}

        if debug:
            print("[CORE] Iteration shutdown complete")

async def main():
    instance = Core()
    try:
        await instance.run_forever()
    except Exception as e:
        print(f"🚨 Error caught: {e}")
    finally:
        print("♻️ Cleaning up iteration")
        instance.context.stop_bot = True
        await instance._shutdown_iteration()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("💥 Force exit")
    os._exit(1)
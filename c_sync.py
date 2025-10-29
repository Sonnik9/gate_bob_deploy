import aiohttp
import asyncio
import time
from typing import Callable, Dict, List, Set
from b_context import BotContext
from c_log import ErrorHandler
from c_utils import safe_float, safe_int, safe_round
from API.GATE.gate import GateFuturesClient
from TG.tg_notifier import TelegramNotifier
from pprint import pprint
import traceback


class PositionCleaner:
    def __init__(
        self,
        context: BotContext,
        info_handler: ErrorHandler,
        set_pos_defaults: Callable,
        pnl_report: Callable,
        gate_client: GateFuturesClient,
        notifier: TelegramNotifier,
        chat_id: str
    ):
        self.context = context
        info_handler.wrap_foreign_methods(self)
        self.info_handler = info_handler
        self.set_pos_defaults = set_pos_defaults
        self.gate_client = gate_client
        self.notifier = notifier
        self.pnl_report = pnl_report
        self.chat_id = chat_id
        self.lock_pos_vars = asyncio.Lock()

    # === сброс всей структуры для символа/стороны ===
    async def reset_position_vars(self, symbol: str, pos_side: str, text_pnl: str):
        """
        Полный сброс позиции:
        - фиксирует статус 'finished', если не был закрыт вручную;
        - обновляет уведомление в Telegram;
        - очищает данные из контекста и order_status_book.
        """
        key = (symbol, pos_side)
        current_anchor = self.context.order_status_book.get(self.chat_id, {}).get(key)
        record = current_anchor.get("last_data")
        text_pnl = text_pnl or "PNL: не удалось получить pNl"

        if not record:
            return
        
        try:
            # --- 1. актуализируем статус ---
            status = str(record.get("entry_status") or "").lower()
            if status not in ("closed manually", "finished", "none", None):

                # --- обновляем Telegram (финальный апдейт с закрытием) ---
                async with self.context.queues_msg_lock:
                    record["entry_status"] = "finished"
                    record["time"] = int(time.time() * 1000)
                    record["tp1_status"] = record["tp2_status"] = record["sl_status"] = "none"
                    record["pnl_text"] = text_pnl
                    current_anchor["last_data"] = record

            else:
                async with self.context.queues_msg_lock:
                    record["pnl_text"] = text_pnl

        finally:
            if record and current_anchor:
                try:
                    await self.notifier.update_anchor_state(
                        chat_id=self.chat_id,
                        symbol=symbol,
                        pos_side=pos_side,
                        body=record,
                        force_message_id=current_anchor.get("message_id"),
                        buttons_state=3
                    )
                except Exception as e:
                    self.info_handler.debug_error_notes(f"[reset_position_vars] Error: {str(e)}\n{traceback.format_exc()}", is_print=True)
            else:
                self.info_handler.debug_info_notes(f"[reset_position_vars] No record or anchor for {symbol}_{pos_side} in reset_position_vars", is_print=True)

            # --- 3. блокируем переменные и очищаем контекст ---
            async with self.lock_pos_vars:
                # сбрасываем торговые параметры под символ
                self.set_pos_defaults(symbol=symbol, pos_side=pos_side, instruments_data=None, reset_flag=True)

                # чистим order_status_book
                if self.chat_id in self.context.order_status_book:
                    self.context.order_status_book[self.chat_id].pop(key, None)

            self.info_handler.debug_info_notes(f"[reset_position_vars] {symbol}_{pos_side} fully reset")

    # === проверка необходимости сброса ===
    async def reset_if_needed(self, pos_data: dict, symbol: str, pos_side: str):
        """
        Проверяет: если позиция всё ещё 'in_position' — считает финальный PNL, отменяет ордера, сбрасывает всё.
        """
        if not pos_data.get("in_position", False):
            return
        
        text_pnl = ""
        try:
            await asyncio.sleep(0.5)
            text_pnl = await self.pnl_report(
                symbol=symbol,
                pos_side=pos_side,
                pos_data=pos_data,
                get_realized_pnl=self.gate_client.get_realized_pnl
            )
        except Exception as e:
            self.info_handler.debug_error_notes(f"[reset_if_needed] pnl_report error: {e}", is_print=True)
        finally:
            try:
                cancel_result = await self.gate_client.cancel_all_orders_by_symbol_and_side(
                    session=None,
                    instId=symbol,
                    pos_side=pos_side
                )
                self.info_handler.debug_info_notes(f"[reset_if_needed] {symbol}_{pos_side} cancel result: {cancel_result}")
            except Exception as e:
                self.info_handler.debug_error_notes(f"[reset_if_needed] cancel failed: {e}")

            # --- теперь сброс ---
            await self.reset_position_vars(symbol, pos_side, text_pnl)


class Synchronizer(PositionCleaner):
    def __init__(
        self,
        context: BotContext,
        info_handler: ErrorHandler,
        set_pos_defaults: Callable,
        pnl_report: Callable,
        gate_client: GateFuturesClient,
        notifier: TelegramNotifier,
        positions_update_frequency: float,
        chat_id: str
    ):
        super().__init__(context, info_handler, set_pos_defaults, pnl_report, gate_client, notifier, chat_id)       
        info_handler.wrap_foreign_methods(self)
  
        self.positions_update_frequency = positions_update_frequency
        self._first_update_done = False

    @staticmethod
    def unpack_position_info(position: dict) -> dict:
        """
        Распаковывает позицию Gate.io Futures из формата API Gate.io.
        Возвращает словарь с безопасными значениями.
        """
        if not isinstance(position, dict):
            return {
                "symbol": "N/A",
                "pos_side": "N/A",
                "contracts": 0.0,
                "entry_price": None,
                "notional_usd": None,
                "leverage": None,
                "margin_vol": None,
                "order_id": None
            }

        symbol = str(position.get("contract", "N/A")).upper()
        size = safe_float(position.get("size"), 0.0)
        pos_side = "LONG" if size > 0 else "SHORT" if size < 0 else "N/A"
        close_order = position.get("close_order", {})
        order_id = str(close_order.get("id")) if close_order and close_order.get("id") else None

        return {
            "symbol": symbol,
            "pos_side": pos_side,
            "contracts": abs(size),
            "entry_price": safe_float(position.get("entry_price"), 0.0),
            "notional_usd": abs(safe_float(position.get("value"), 0.0)),
            "margin_vol": abs(safe_float(position.get("margin"), 0.0)),
            "leverage": abs(safe_int(position.get("leverage"), 1)),
            "order_id": order_id
        }

    async def update_active_position(self, symbol: str, symbol_data: dict, pos_side: str, info: dict):
        """
        Updates position data in the bot's context when a position is detected or filled.
        """
        ctVal = symbol_data.get("spec", {}).get("ctVal")
        price_precision = symbol_data.get("spec", {}).get("price_precision")
        pos_data = symbol_data.get(pos_side, {})

        entry_price = safe_float(info.get("entry_price"), 0.0)
        contracts = safe_float(info.get("contracts"), 0.0)
        leverage = safe_int(info.get("leverage"), 1) or pos_data.get("leverage")
        vol_usdt = safe_float(info.get("notional_usd"), 0.0)
        margin_vol = safe_float(info.get("margin_vol"), 0.0)  or pos_data.get("margin_vol")
        
        vol_assets = contracts * ctVal

        if not pos_data.get("in_position") and entry_price:
            current_anchor = self.context.order_status_book.get(self.chat_id, {}).get((symbol, pos_side))
            record = current_anchor.get("last_data")
            record["entry_price"] = round(entry_price, price_precision)
            if record and current_anchor:
                try:
                    await self.notifier.update_anchor_state(
                        chat_id=self.chat_id,
                        symbol=symbol,
                        pos_side=pos_side,
                        body=record,
                        force_message_id=current_anchor.get("message_id")
                    )
                except Exception as e:
                    self.info_handler.debug_error_notes(f"[update_active_position] Error: {str(e)}\n{traceback.format_exc()}", is_print=True)

        pos_data.update({
            "entry_price": entry_price,
            "contracts": contracts,
            "in_position": True,
            "margin_vol": margin_vol,
            "vol_usdt": vol_usdt,
            "vol_assets": vol_assets,
            "leverage": leverage,
        })

    async def update_positions(
        self,  
        target_symbols: Set[str],
        positions: List[Dict],
    ) -> None:
        """
        Обновляет данные о позициях для указанной стратегии и символов.
        """
        try:
            # --- Словарь актуальных позиций по символу+стороне ---
            active_positions = {}
            for position in positions:
                if not position:
                    continue
                inst_id = position.get("contract", "").upper()
                if inst_id in target_symbols:
                    info = self.unpack_position_info(position)
                    # print(info)
                    if isinstance(info, dict):
                        active_positions[(info["symbol"], info["pos_side"])] = info

            # --- Сначала сброс: пройтись по локальным данным и убрать те позиции, которых нет в active_positions ---
            for symbol in target_symbols:
                symbol_data = self.context.position_vars.get(symbol, {})
                for pos_side in ("LONG", "SHORT"):
                    pos_data = symbol_data.get(pos_side, {})
                    if not pos_data:
                        continue
                    if (symbol, pos_side) not in active_positions:
                        # на бирже нет позиции → сбрасываем локальную
                        await self.reset_if_needed(
                            pos_data=pos_data,
                            symbol=symbol,
                            pos_side=pos_side
                        )

            # --- Теперь обновление / установка активных позиций ---
            for (symbol, pos_side), info in active_positions.items():
                contracts = info.get("contracts", 0.0)
                symbol_data = self.context.position_vars.get(symbol, {})
                pos_data = symbol_data.get(pos_side, {})
                if not pos_data:
                    continue


                if isinstance(contracts, (float, int)) and contracts > 0:
                    await self.update_active_position(
                        symbol=symbol,
                        symbol_data=symbol_data,
                        pos_side=pos_side,
                        info=info
                    )
                else:
                    await self.reset_if_needed(
                        pos_data=pos_data,
                        symbol=symbol,
                        pos_side=pos_side
                    )

            if not self._first_update_done:
                self._first_update_done = True
                self.info_handler.debug_info_notes("[update_positions] First update done, flag set")

        except KeyError as e:
            self.info_handler.debug_error_notes(f"[KeyError]: {e}")
        except Exception as e:
            self.info_handler.debug_error_notes(f"[Unexpected Error]: {e}")
            
    async def refresh_positions_state(
        self
    ) -> None:
        """
        Обновляет состояние позиций для всех стратегий пользователя.
        """
        try:
            symbols_set = set(self.context.position_vars.keys())
            if not symbols_set:
                # print("not symbols_set")
                return
            
            if not self.context.session or self.context.session.closed:
                return
            
            positions = await self.gate_client.fetch_positions(session=self.context.session)
            # print("positions")
            # pprint(positions)

            if positions is None:
                self.info_handler.debug_error_notes(
                    f"No 'data' field in positions response."
                )
                return

            await self.update_positions(
                symbols_set,
                positions
            )
        
        except aiohttp.ClientError as e:
            self.info_handler.debug_error_notes(
                f"[HTTP Error] Failed to fetch positions: {e}."
            )
        except Exception as e:
            self.info_handler.debug_error_notes(
                f"[Unexpected Error] Failed to refresh positions: {e}."
            )
            
    async def refresh_positions_task(self) -> None:
        while not self.context.stop_bot and not self.context.stop_bot_iteration:
            await self.refresh_positions_state()
            await asyncio.sleep(self.positions_update_frequency)
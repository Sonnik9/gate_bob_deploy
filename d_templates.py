import asyncio
import time
import aiohttp
import math
from typing import *
from a_config import *
from b_context import BotContext
from API.GATE.gate import GateFuturesClient, GateApiResponseValidator
from TG.tg_notifier import TelegramNotifier
from c_utils import Utils
from c_log import ErrorHandler


class OrderTemplates:
    """
    Финальная версия класса управления ордерами.
    Принципы:
      - Централизованный контроль ошибок (_exec_request)
      - Точное обновление record без перезаписи лишнего
      - entry_status отражает только факт открытия/закрытия
      - TP/SL ошибки и успехи фиксируются в *_status
    """

    def __init__(self, context: BotContext, info_handler: ErrorHandler, utils: Utils, gate_client: GateFuturesClient, notifier: TelegramNotifier):
        self.context = context
        self.info_handler = info_handler
        self.utils = utils
        self.gate_client = gate_client
        self.notifier = notifier

    # ============================================================
    #  UNIVERSAL HELPERS
    # ============================================================

    async def _exec_request(
        self,
        label: str,
        coro,
        *,
        record: Optional[dict] = None,
        status_key: Optional[str] = None,
        overwrite: bool = True
    ):
        """
        Централизованный обработчик ответов API.
        Если указаны record+status_key — пишет результат прямо в конкретное поле (например, tp1_status).
        """
        try:
            resp = await coro
            norm = GateApiResponseValidator.normalize_response(resp)

            # API-ошибка
            if norm.get("label"):
                msg = f"{label} failed: {norm['label']} - {norm.get('detail', 'No details')}"
                self.info_handler.debug_error_notes(msg)
                if record is not None and status_key:
                    if overwrite or not record.get(status_key):
                        async with self.context.queues_msg_lock:
                            record[status_key] = f"failed. Reason: {norm['label']} ({norm.get('detail', 'No details')})"
                return None

            return norm

        except Exception as e:
            msg = f"{label} exception: {e}"
            self.info_handler.debug_error_notes(msg)
            if record is not None and status_key:
                if overwrite or not record.get(status_key):
                    async with self.context.queues_msg_lock:
                        record[status_key] = f"failed. Reason: {e}"
            return None

    def _compose_trigger_payloads(
        self,
        contracts: float,
        take_profits: Optional[List[Tuple[float, float]]],
        stop_loss: Optional[float],
        spec: dict,
        trigger_type: str
    ) -> Tuple[list, Optional[dict]]:
        """
        Возвращает (tp_orders, sl_order).
        Пропускает невалидные цены (<=0). Контракты округляются по precision.
        """
        tp_orders, sl_order = [], None
        price_precision = spec.get("price_precision", 4)
        contract_precision = spec.get("contract_precision", 0)

        # Диагностический лог без привязки к несуществующим полям
        self.info_handler.debug_info_notes(
            f"[compose_trigger] specs: price_precision={price_precision}, contract_precision={contract_precision}, contracts={contracts}"
        )

        # TAKE PROFITS
        if take_profits:
            for price, portion in take_profits:
                if not price or float(price) <= 0:
                    self.info_handler.debug_error_notes(f"[compose_trigger] Invalid TP price: {price}")
                    continue

                portion = float(portion) / 100.0 if portion is not None else 1.0
                tp_contracts = round(contracts * portion, contract_precision)
                if tp_contracts <= 0:
                    self.info_handler.debug_error_notes(
                        f"[compose_trigger] TP contracts rounded to <= 0 (contracts={contracts}, portion={portion})"
                    )
                    continue

                px = round(float(price), price_precision)
                tp_orders.append({
                    "trigger_px": px,
                    "ord_px": "-1" if trigger_type == "market" else px,
                    "trigger_px_type": "last",
                    "contracts": tp_contracts
                })
                self.info_handler.debug_info_notes(
                    f"[compose_trigger] TP prepared: price={px}, portion={portion}, size={tp_contracts}"
                )

        # STOP LOSS
        if stop_loss and float(stop_loss) > 0:
            sl_px = round(float(stop_loss), price_precision)
            sl_order = {
                "trigger_px": sl_px,
                "ord_px": "-1" if trigger_type == "market" else sl_px,
                "trigger_px_type": "last",
                "contracts": contracts
            }
            self.info_handler.debug_info_notes(
                f"[compose_trigger] SL prepared: price={sl_px}, size={contracts}"
            )

        return tp_orders, sl_order

    async def _apply_trigger_result(self, trig: dict, pos_data: dict, record: dict, debug_label: str):
        """
        Обновляет record и pos_data после постановки TP/SL.
        Работает только с реальными Telegram-полями.
        """
        tp_results = trig.get("tp_results", [])
        sl_res = trig.get("sl_result")

        # --- TAKE PROFITS ---
        for i, tp in enumerate(tp_results, 1):
            norm = GateApiResponseValidator.normalize_response(tp)
            label = norm.get("label")

            if label:
                async with self.context.queues_msg_lock:
                    record[f"tp{i}_status"] = f"failed. Reason: {label} ({norm.get('detail', 'Unknown')})"
                self.info_handler.debug_error_notes(f"{debug_label} TP{i} failed: {label}")
                continue

            async with self.context.queues_msg_lock:
                pos_data[f"tp_order{i}_id"] = norm.get("id")
                record[f"tp{i}_status"] = "pending"
                record[f"tp{i}"] = (norm.get("trigger") or {}).get("price")
            self.info_handler.debug_info_notes(f"{debug_label} TP{i} placed successfully id={norm.get('id')}")

        # если меньше 2 TP — выставим оставшиеся как "none"
        for i in range(len(tp_results) + 1, 3):
            async with self.context.queues_msg_lock:
                record[f"tp{i}_status"] = record.get(f"tp{i}_status", "none")

        # --- STOP LOSS ---
        if sl_res:
            norm = GateApiResponseValidator.normalize_response(sl_res)
            label = norm.get("label")

            if label:
                async with self.context.queues_msg_lock:
                    record["sl_status"] = f"failed. Reason: {label} ({norm.get('detail', 'Unknown')})"
                self.info_handler.debug_error_notes(f"{debug_label} SL failed: {label}")
            else:
                async with self.context.queues_msg_lock:
                    pos_data["sl_order_id"] = norm.get("id")
                    record["sl_status"] = "pending"
                    record["sl"] = (norm.get("trigger") or {}).get("price")
                self.info_handler.debug_info_notes(f"{debug_label} SL placed successfully id={norm.get('id')}")
        else:
            async with self.context.queues_msg_lock:
                record["sl_status"] = record.get("sl_status", "none")

    async def _place_trigger_order(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        pos_side: str,
        tp_orders: list,
        sl_order: Optional[dict],
        client_ord_id: str,
        trigger_order_type: str
    ) -> dict:
        """
        Объединённая постановка TP/SL.
        Возвращает {"tp_results": [...], "sl_result": {...}} c нормализованными ответами.
        """
        debug_label = f"[_place_trigger_order_{symbol}_{pos_side}]"
        side = "sell" if pos_side.upper() == "LONG" else "buy"

        prepared_tp_orders = [tp.copy() for tp in tp_orders if isinstance(tp, dict)]
        for tp in prepared_tp_orders:
            tp["ord_px"] = "-1" if trigger_order_type == "market" else str(tp.get("trigger_px", "0"))

        prepared_sl_order = sl_order.copy() if isinstance(sl_order, dict) else None
        if prepared_sl_order:
            prepared_sl_order["ord_px"] = "-1" if trigger_order_type == "market" else str(prepared_sl_order.get("trigger_px", "0"))

        self.info_handler.debug_info_notes(
            f"{debug_label} Start placing triggers: side={side}, tp_orders={prepared_tp_orders}, sl_order={prepared_sl_order}"
        )

        results: Dict[str, Any] = {"tp_results": [], "sl_result": None}
        path = f"/futures/{self.gate_client.settle}/price_orders"
        client_suffix = client_ord_id[:26]
        price_type_map = {"last": 0, "mark": 1, "index": 2}

        # TAKE PROFITS
        for i, tp in enumerate(prepared_tp_orders, start=1):
            try:
                if not tp.get("trigger_px") or float(tp["trigger_px"]) <= 0:
                    self.info_handler.debug_info_notes(f"{debug_label} TP#{i} skipped (invalid trigger price: {tp.get('trigger_px')})")
                    continue

                tp_price_type = price_type_map.get(str(tp["trigger_px_type"]).lower(), 0)
                close_size = -int(float(tp["contracts"])) if side == "buy" else int(float(tp["contracts"]))
                rule = 1 if side == "sell" else 2

                tp_body = {
                    "initial": {
                        "contract": symbol,
                        "size": close_size,
                        "price": str(tp["ord_px"]) if tp["ord_px"] != "-1" else "0",
                        "reduce_only": True,
                        "tif": "gtc" if tp["ord_px"] != "-1" else "ioc",
                        "text": f"t-tp{i}-{client_suffix}"[:30],
                    },
                    "trigger": {
                        "strategy_type": 0,
                        "price_type": tp_price_type,
                        "price": str(tp["trigger_px"]),
                        "rule": rule,
                        "expiration": 86400,
                    },
                }

                self.info_handler.debug_info_notes(f"{debug_label} TP#{i} placing: {tp_body}")
                tp_raw = await self.gate_client._request(session, "POST", path, data=tp_body, private=True)
                norm = GateApiResponseValidator.normalize_response(tp_raw)

                if norm.get("label"):
                    self.info_handler.debug_error_notes(f"{debug_label} TP#{i} error: {norm['label']} - {norm.get('detail', 'No details')}")
                else:
                    self.info_handler.debug_info_notes(f"{debug_label} TP#{i} placed successfully id={norm.get('id')}")
                results["tp_results"].append(norm)

                if i < len(prepared_tp_orders):
                    await asyncio.sleep(0.5)

            except Exception as e:
                err = {"label": "EXCEPTION", "detail": str(e)}
                self.info_handler.debug_error_notes(f"{debug_label} TP#{i} exception: {e}")
                results["tp_results"].append(err)

        # STOP LOSS
        if prepared_sl_order:
            try:
                sl_price_type = price_type_map.get(str(prepared_sl_order["trigger_px_type"]).lower(), 0)
                close_size = -int(float(prepared_sl_order["contracts"])) if side == "buy" else int(float(prepared_sl_order["contracts"]))
                rule = 2 if side == "sell" else 1

                sl_body = {
                    "initial": {
                        "contract": symbol,
                        "size": close_size,
                        "price": str(prepared_sl_order["ord_px"]) if prepared_sl_order["ord_px"] != "-1" else "0",
                        "reduce_only": True,
                        "tif": "gtc" if prepared_sl_order["ord_px"] != "-1" else "ioc",
                        "text": f"t-sl-{client_suffix}"[:30],
                    },
                    "trigger": {
                        "strategy_type": 0,
                        "price_type": sl_price_type,
                        "price": str(prepared_sl_order["trigger_px"]),
                        "rule": rule,
                        "expiration": 86400,
                    },
                }

                self.info_handler.debug_info_notes(f"{debug_label} SL placing: {sl_body}")
                sl_raw = await self.gate_client._request(session, "POST", path, data=sl_body, private=True)
                norm = GateApiResponseValidator.normalize_response(sl_raw)

                if norm.get("label"):
                    self.info_handler.debug_error_notes(f"{debug_label} SL error: {norm['label']} - {norm.get('detail', 'No details')}")
                else:
                    self.info_handler.debug_info_notes(f"{debug_label} SL placed successfully id={norm.get('id')}")
                results["sl_result"] = norm

            except Exception as e:
                err = {"label": "EXCEPTION", "detail": str(e)}
                self.info_handler.debug_error_notes(f"{debug_label} SL exception: {e}")
                results["sl_result"] = err
        else:
            results["sl_result"] = None

        self.info_handler.debug_info_notes(f"{debug_label} Completed: {results}")
        return results

    # ============================================================
    #  ORDER WORKFLOW
    # ============================================================
    async def initial_order_template(
        self,
        session: aiohttp.ClientSession,
        record: dict,
        current_anchor: dict,
        chat_id: str,
        fin_settings: dict,
        symbol: str,
        leverage: int,
        entry_price: float,
        pos_side: str,
        symbol_data: dict,
        pos_data: dict,
        stop_loss: Optional[float] = None,
        take_profits: Optional[List[Tuple[float, float]]] = None,
        order_type: str = "market",
        half_margin: bool = False
    ) -> Optional[dict]:
        """Открытие позиции с корректной инициализацией статусов."""
        debug_label = f"[initial_order_{symbol}_{pos_side}]"
        spec = symbol_data.get("spec", {})
        price_precision = spec.get("price_precision", 4)
        ctVal = spec.get("ctVal", 1.0)
        lotSz = spec.get("lotSz", 1.0)
        contract_precision = spec.get("contract_precision", 0)

        try:
            async with self.context.queues_msg_lock:
                record["tp1"] = take_profits[0] if len(take_profits) > 0 else None
                record["tp2"] = take_profits[1] if len(take_profits) > 1 else None
                record["sl"] = round(float(stop_loss), price_precision) if stop_loss else None

            margin_mode = "ISOLATED" if fin_settings.get("margin_mode", 1) == 1 else "CROSS"
            margin_size = fin_settings.get("margin_size", 0.0) / 2 if half_margin else fin_settings.get("margin_size", 0.0)

            # --- Леверидж и режим маржи ---
            if not await self._exec_request(
                f"{debug_label} Leverage",
                self.gate_client.set_leverage(session, symbol, leverage, margin_mode.lower()),
                record=record,
                status_key="entry_status",
                overwrite=True
            ):
                return None

            if not await self._exec_request(
                f"{debug_label} MarginMode",
                self.gate_client.set_margin_mode(session, symbol, margin_mode),
                record=record,
                status_key="entry_status",
                overwrite=True
            ):
                return None

            # --- Расчёт контрактов ---
            contracts = self.utils.contract_calc(
                margin_size=margin_size,
                entry_price=entry_price,
                leverage=leverage,
                ctVal=ctVal,
                lotSz=lotSz,
                contract_precision=contract_precision,
                volume_rate=100,
                debug_label=debug_label
            )
            if not contracts or max(1 / (10 ** contract_precision), lotSz) < 1:
                self.info_handler.debug_error_notes(
                    f"{debug_label} Contract calc returned 0 or invalid precision (raw={contracts}). "
                    f"Try increasing margin_size or leverage."
                )
                async with self.context.queues_msg_lock:
                    record["entry_status"] = "failed. Reason: CONTRACTS=0"
                return None

            pos_data["contracts"] = contracts

            # --- Постановка основного ордера ---
            side = "buy" if pos_side.upper() == "LONG" else "sell"
            client_ord_id = f"{symbol}_{pos_side}_{int(time.time() * 1000)}"[:28]
            px = round(float(entry_price), price_precision) if order_type == "limit" else None

            main_order = await self._exec_request(
                f"{debug_label} Main order",
                self.gate_client.place_main_order(
                    session, symbol, contracts, side, margin_mode.lower(), pos_side.lower(),
                    False, order_type, px, client_ord_id
                ),
                record=record, status_key="entry_status", overwrite=True
            )

            # --- Обработка ошибок от биржи ---
            if isinstance(main_order, dict) and main_order.get("error"):
                async with self.context.queues_msg_lock:
                    record["entry_status"] = f"failed. Reason: {main_order['error']}"
                self.info_handler.debug_error_notes(f"{debug_label} Order failed: {main_order['error']}")
                return None

            if not main_order or not main_order.get("id"):
                async with self.context.queues_msg_lock:
                    record["entry_status"] = "failed. Reason: unknown"
                return None

            pos_data["order_id"] = str(main_order.get("id"))

            async with self.context.queues_msg_lock:
                record["entry_status"] = "filled" if order_type == "market" else "pending"
            pos_data["c_time"] = int(time.time() * 1000)

            # --- Триггеры TP/SL ---
            trigger_type = "limit" if fin_settings.get("trigger_order_type", 1) == 1 else "market"
            tp_data, sl_order = self._compose_trigger_payloads(contracts, take_profits, stop_loss, spec, trigger_type)

            try:
                trig = await self._place_trigger_order(session, symbol, pos_side, tp_data, sl_order, client_ord_id, trigger_type)
                if trig:
                    await self._apply_trigger_result(trig, pos_data, record, debug_label)
            except Exception as e:
                record["tp1_status"] = record["tp2_status"] = record["sl_status"] = f"failed. Reason: {e}"
                self.info_handler.debug_error_notes(f"{debug_label} Trigger placement exception: {e}")

        finally:
            # --- Обновляем Telegram-статус ---
            async with self.context.queues_msg_lock:
                current_anchor["last_data"] = record

            if record and current_anchor:
                await self.notifier.update_anchor_state(
                    chat_id=chat_id,
                    symbol=symbol,
                    pos_side=pos_side,
                    body=record,
                    force_message_id=current_anchor.get("message_id"),
                    buttons_state=1
                )

        return True

    # async def initial_order_template(
    #     self,
    #     session: aiohttp.ClientSession,
    #     record: dict,
    #     current_anchor: dict,
    #     chat_id: str,
    #     fin_settings: dict,
    #     symbol: str,
    #     leverage: int,
    #     entry_price: float,
    #     pos_side: str,
    #     symbol_data: dict,
    #     pos_data: dict,
    #     stop_loss: Optional[float] = None,
    #     take_profits: Optional[List[Tuple[float, float]]] = None,
    #     order_type: str = "market",
    #     half_margin: bool = False
    # ) -> Optional[dict]:
    #     """Открытие позиции с корректной инициализацией статусов."""
   
    #     debug_label = f"[initial_order_{symbol}_{pos_side}]"
    #     spec = symbol_data.get("spec", {})
    #     price_precision = spec.get("price_precision", 4)
    #     ctVal = spec.get("ctVal", 1.0)
    #     lotSz = spec.get("lotSz", 1.0)
    #     contract_precision = spec.get("contract_precision", 0)

    #     try:
    #         async with self.context.queues_msg_lock:
    #             record["tp1"] = take_profits[0] if len(take_profits) > 0 else None
    #             record["tp2"] = take_profits[1] if len(take_profits) > 1 else None
    #             record["sl"] = round(float(stop_loss), price_precision) if stop_loss else None

    #         margin_mode = "ISOLATED" if fin_settings.get("margin_mode", 1) == 1 else "CROSS"
    #         margin_size = fin_settings.get("margin_size", 0.0) / 2 if half_margin else fin_settings.get("margin_size", 0.0)

    #         if not await self._exec_request(
    #             f"{debug_label} Leverage",
    #             self.gate_client.set_leverage(session, symbol, leverage, margin_mode.lower()),
    #             record=record,
    #             status_key="entry_status",
    #             overwrite=True
    #             ):
    #             return None
            
    #         if not await self._exec_request(
    #             f"{debug_label} MarginMode",
    #             self.gate_client.set_margin_mode(session, symbol, margin_mode),
    #             record=record,
    #             status_key="entry_status",
    #             overwrite=True
    #             ):
    #             return None

    #         contracts = self.utils.contract_calc(
    #             margin_size=margin_size,
    #             entry_price=entry_price,
    #             leverage=leverage,
    #             ctVal=ctVal,
    #             lotSz=lotSz,
    #             contract_precision=contract_precision,
    #             volume_rate=100,
    #             debug_label=debug_label
    #         )

    #         if not contracts or max(1 / (10 ** contract_precision), lotSz) < 1:
    #             self.info_handler.debug_error_notes(
    #                 f"{debug_label} Contract calc returned 0 or invalid precision (raw={contracts}). "
    #                 f"Try increasing margin_size or leverage."
    #             )
    #             async with self.context.queues_msg_lock:
    #                 record["entry_status"] = "failed. Reason: CONTRACTS=0"
    #             return None

    #         pos_data["contracts"] = contracts

    #         side = "buy" if pos_side.upper() == "LONG" else "sell"
    #         client_ord_id = f"{symbol}_{pos_side}_{int(time.time() * 1000)}"[:28]
    #         px = round(float(entry_price), price_precision) if order_type == "limit" else None

    #         main_order = await self._exec_request(
    #             f"{debug_label} Main order",
    #             self.gate_client.place_main_order(
    #                 session, symbol, contracts, side, margin_mode.lower(), pos_side.lower(),
    #                 False, order_type, px, client_ord_id
    #             ),
    #             record=record, status_key="entry_status", overwrite=True
    #         )

    #         if not main_order or not main_order.get("id"):
    #             async with self.context.queues_msg_lock:
    #                 record["entry_status"] = "failed. Reason: unknown"               
    #             return None

    #         pos_data["order_id"] = str(main_order.get("id"))

    #         async with self.context.queues_msg_lock:          
    #             record["entry_status"] = "filled" if order_type == "market" else "pending"
    #         pos_data["c_time"] = int(time.time() * 1000)

    #         trigger_type = "limit" if fin_settings.get("trigger_order_type", 1) == 1 else "market"
    #         tp_data, sl_order = self._compose_trigger_payloads(contracts, take_profits, stop_loss, spec, trigger_type)

    #         try:
    #             trig = await self._place_trigger_order(session, symbol, pos_side, tp_data, sl_order, client_ord_id, trigger_type)
    #             if trig:
    #                 await self._apply_trigger_result(trig, pos_data, record, debug_label)
    #         except Exception as e:
    #             record["tp1_status"] = record["tp2_status"] = record["sl_status"] = f"failed. Reason: {e}"
    #             self.info_handler.debug_error_notes(f"{debug_label} Trigger placement exception: {e}")
    #     finally:
    #         async with self.context.queues_msg_lock:
    #             current_anchor["last_data"] = record
            
    #         if record and current_anchor:
    #             await self.notifier.update_anchor_state(
    #                 chat_id=chat_id,
    #                 symbol=symbol,
    #                 pos_side=pos_side,
    #                 body=record,
    #                 force_message_id=current_anchor.get("message_id"),
    #                 buttons_state=1
    #             )
           
    #     return True

    async def modify_risk_orders(
        self,
        session: aiohttp.ClientSession,
        record: dict,
        chat_id: str,
        symbol: str,
        pos_side: str,
        fin_settings: dict,
        symbol_data: dict,
        pos_data: dict,
        modify_sl: bool = False,
        new_sl: Optional[float] = None,
        modify_tp: bool = False,
        new_tp: Optional[Tuple[float, float]] = None,
        tp_index: Optional[int] = None,
    ) -> bool:
        """
        Изменение TP/SL:
        • Синхронизирует record с актуальным anchor;
        • Корректно работает с TP2 и дробным объёмом;
        • Избегает коллизий с виртуальными ID;
        • Автоматически обновляет Telegram-уведомление.
        """
        debug_label = f"[modify_risk_{symbol}_{pos_side}]"
        spec = symbol_data.get("spec", {})
        price_precision = spec.get("price_precision", 4)
        contract_precision = spec.get("contract_precision", 0)
        trigger_type = "limit" if fin_settings.get("trigger_order_type", 1) == 1 else "market"

        # === 1. Синхронизация record с актуальным anchor ===
        chat_orders = self.context.order_status_book.setdefault(chat_id, {})
        current_anchor = chat_orders.get((symbol, pos_side)) 
        record = current_anchor["last_data"]

        try:
            contracts = pos_data.get("contracts", 0.0)
            if not contracts:
                async with self.context.queues_msg_lock:
                    record["entry_status"] = record.get("entry_status", "failed. Reason: CONTRACTS=0")
                return False

            client_ord_id = f"{symbol}_{pos_side}_{int(time.time() * 1000)}"

            # ============================================================
            # === TP MODIFY =============================================
            # ============================================================
            if modify_tp and new_tp and tp_index in (1, 2):
                tp_price, tp_percent = new_tp
                if tp_price and float(tp_price) > 0 and (tp_percent is None or float(tp_percent) >= 0):
                    tp_px = round(float(tp_price), price_precision)
                    portion = float(tp_percent) / 100.0 if tp_percent is not None else 1.0

                    # Безопасное округление контрактов вверх, чтобы не было 0.0
                    tp_contracts = max(
                        round(math.ceil(contracts * portion * (10 ** contract_precision)) / (10 ** contract_precision), contract_precision),
                        1e-8  # минимальная страховка
                    )

                    if tp_contracts > 0:
                        old_tp_id = pos_data.get(f"tp_order{tp_index}_id")
                        tp_data = [{
                            "trigger_px": tp_px,
                            "ord_px": tp_px if trigger_type == "limit" else "-1",
                            "trigger_px_type": "last",
                            "contracts": tp_contracts,
                        }]

                        trig = await self._place_trigger_order(session, symbol, pos_side, tp_data, None, client_ord_id, trigger_type)

                        if trig and trig.get("tp_results"):
                            new_norm = GateApiResponseValidator.normalize_response(trig["tp_results"][0])
                            if not new_norm.get("label"):
                                new_id = new_norm.get("id") or f"virtual_tp_{tp_index}_{int(time.time())}"
                                async with self.context.queues_msg_lock:
                                    record[f"tp{tp_index}_status"] = "modified"
                                    record[f"tp{tp_index}"] = (new_norm.get("trigger") or {}).get("price") or tp_px
                                    pos_data[f"tp_order{tp_index}_id"] = new_id
                                    record[f"tp_order{tp_index}_id"] = new_id

                                # отменяем старый TP, если был, но не виртуальный
                                if old_tp_id and old_tp_id != new_id and not str(old_tp_id).startswith("virtual_"):
                                    await self._exec_request(
                                        f"{debug_label} TP{tp_index} cancel old",
                                        self.gate_client.cancel_order_by_id(session, symbol, old_tp_id),
                                        record=record,
                                        status_key=f"tp{tp_index}_status",
                                        overwrite=False
                                    )
                                self.info_handler.debug_info_notes(f"{debug_label} TP{tp_index} modified successfully id={new_id}")
                            else:
                                async with self.context.queues_msg_lock:
                                    record[f"tp{tp_index}_status"] = f"failed. Reason: {new_norm.get('label', 'UNKNOWN')} ({new_norm.get('detail', 'No details')})"
                        else:
                            async with self.context.queues_msg_lock:
                                record[f"tp{tp_index}_status"] = "failed. Reason: TP_ORDER_FAILED"
                    else:
                        async with self.context.queues_msg_lock:
                            record[f"tp{tp_index}_status"] = "failed. Reason: ZERO_CONTRACTS_FOR_TP"
                else:
                    async with self.context.queues_msg_lock:
                        record[f"tp{tp_index}_status"] = "failed. Reason: INVALID_TP_INPUT"

            # ============================================================
            # === SL MODIFY =============================================
            # ============================================================
            if modify_sl and new_sl is not None:
                if float(new_sl) > 0:
                    sl_px = round(float(new_sl), price_precision)
                    old_sl_id = pos_data.get("sl_order_id")
                    sl_order = {
                        "trigger_px": sl_px,
                        "ord_px": sl_px if trigger_type == "limit" else "-1",
                        "trigger_px_type": "last",
                        "contracts": contracts,
                    }
                    trig = await self._place_trigger_order(session, symbol, pos_side, [], sl_order, client_ord_id, trigger_type)
                    if trig and trig.get("sl_result"):
                        new_norm = GateApiResponseValidator.normalize_response(trig["sl_result"])
                        if not new_norm.get("label"):
                            new_id = new_norm.get("id") or f"virtual_sl_{int(time.time())}"
                            async with self.context.queues_msg_lock:
                                record["sl_status"] = "modified"
                                record["sl"] = (new_norm.get("trigger") or {}).get("price") or sl_px
                                pos_data["sl_order_id"] = new_id

                            if old_sl_id and old_sl_id != new_id and not str(old_sl_id).startswith("virtual_"):
                                await self._exec_request(
                                    f"{debug_label} SL cancel old",
                                    self.gate_client.cancel_order_by_id(session, symbol, old_sl_id),
                                    record=record,
                                    status_key="sl_status",
                                    overwrite=False
                                )
                            self.info_handler.debug_info_notes(f"{debug_label} SL modified successfully id={new_id}")
                        else:
                            async with self.context.queues_msg_lock:
                                record["sl_status"] = f"failed. Reason: {new_norm.get('label', 'UNKNOWN')} ({new_norm.get('detail', 'No details')})"
                    else:
                        async with self.context.queues_msg_lock:
                            record["sl_status"] = "failed. Reason: SL_ORDER_FAILED"
                else:
                    async with self.context.queues_msg_lock:
                        record["sl_status"] = "failed. Reason: INVALID_SL_INPUT"

            self.info_handler.debug_info_notes(f"{debug_label} Done")

        finally:
            # ============================================================
            # === Обновление контекста и уведомления =====================
            # ============================================================
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

        return True

    async def force_position_close(self, session, chat_id, key, fin_settings, close_type="market") -> bool:
        """Принудительное закрытие позиции (без триггеров, корректно обновляет статус и уведомление)."""
        symbol, pos_side = key
        debug_label = f"[force_close_{symbol}_{pos_side}_{close_type}]"
        pos_data = self.context.position_vars.get(symbol, {}).get(pos_side, {})
        margin_mode = "ISOLATED" if fin_settings.get("margin_mode", 1) == 1 else "CROSS"

        # === 1. Проверки базовых данных ===
        chat_orders = self.context.order_status_book.setdefault(chat_id, {})
        current_anchor = chat_orders.get((symbol, pos_side))
        record = current_anchor["last_data"]     

        try:
            contracts = pos_data.get("contracts", 0.0)
            if not contracts:            
                async with self.context.queues_msg_lock:
                    record["entry_status"] = "failed. Reason: NO_CONTRACTS"
                return False

            client_ord_id = f"close_{symbol}_{pos_side}_{int(time.time() * 1000)}"[:28]

            # === 2. Формируем запрос на закрытие ===
            if close_type.lower() == "limit":
                coro = self.gate_client.close_by_bid_ask(
                    session, symbol, contracts, pos_side, margin_mode.lower(), client_ord_id
                )
            else:
                side = "sell" if pos_side.upper() == "LONG" else "buy"
                coro = self.gate_client.place_main_order(
                    session, symbol, contracts, side, margin_mode.lower(),
                    pos_side.lower(), True, "market", None, client_ord_id
                )

            # === 3. Выполняем запрос и нормализуем результат ===
            close_info = await self._exec_request(
                f"{debug_label} Force close",
                coro,
                record=record, status_key="entry_status", overwrite=False
            )

            norm = close_info or {}
            label = norm.get("label")
            has_id = bool(norm.get("id"))

            # === 4. Интерпретация результата ===
            # Для market ожидаем ID, для limit часто None — считаем всё равно успешным
            if has_id or close_type.lower() == "limit":
                async with self.context.queues_msg_lock:
                    record["entry_status"] = "closed manually"
                    record["time"] = int(time.time() * 1000)
                    record["tp1_status"] = record["tp2_status"] = record["sl_status"] = "none"
                    record["pnl_text"] = record.get("pnl_text", "PNL: сделка закрыта вручную")

                self.info_handler.debug_info_notes(f"{debug_label} Position closed successfully ({close_type.upper()})")
                return True

            # === 5. Ошибка закрытия ===
            async with self.context.queues_msg_lock:
                if label:
                    record["entry_status"] = f"failed. Reason: {label} ({norm.get('detail', 'No details')})"
                else:
                    record["entry_status"] = "failed. Reason: UNKNOWN_RESPONSE"

        finally:
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
        
        return False

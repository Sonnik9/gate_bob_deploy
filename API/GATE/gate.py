import aiohttp
import asyncio
import hmac
import hashlib
import json
import time
import random
from typing import *
from urllib.parse import urlencode
from b_context import BotContext
from c_log import ErrorHandler


class GateApiResponseValidator:
    """
    Validator for Gate.io API responses.
    """
    @staticmethod
    def normalize_response(resp):
        if isinstance(resp, dict):
            return resp
        elif isinstance(resp, list) and resp and isinstance(resp[0], dict):
            return resp[0]
        return {}

    @staticmethod
    def get_code(resp):
        if isinstance(resp, dict):
            return str(resp.get("label", ""))
        return None

    @staticmethod
    def get_data_list(resp):
        if isinstance(resp, list):
            return resp
        elif isinstance(resp, dict):
            for key in ("orders", "positions", "price_orders", "data"):
                data = resp.get(key, [])
                if isinstance(data, list):
                    return data
        return []


class GateFuturesClient:
    """
    Async Gate.io Futures REST client (API v4).
    Поддерживает USDT-маркет и другие settle типы через параметр settle.
    """
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        context: BotContext,
        info_handler: ErrorHandler,
        base_url: str = "https://fx-api.gateio.ws/api/v4",
        recv_window: int = 5000,
        settle: str = "usdt",
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window
        self.settle = settle.lower()
        self.prefix = "/api/v4"
        self.order_ids: Dict[str, Dict[str, Any]] = {}  # Store main, TP, SL IDs

        info_handler.wrap_foreign_methods(self)
        self.info_handler = info_handler
        self.stop_bot = context.stop_bot
        self.stop_bot_iteration = context.stop_bot_iteration

    # ========= SIGNATURE ==================

    def _get_timestamp(self) -> str:
        return str(int(time.time()))

    def _sign(
        self,
        method: str,
        request_path: str,
        query_string: str,
        body_hash: str,
        timestamp: str,
    ) -> str:
        method_up = method.upper()
        full_request_path = self.prefix + request_path
        sig_str = f"{method_up}\n{full_request_path}\n{query_string}\n{body_hash}\n{timestamp}"
        h = hmac.new(self.api_secret.encode("utf-8"), sig_str.encode("utf-8"), hashlib.sha512)
        return h.hexdigest()

    async def _request(
        self,
        session: Optional[aiohttp.ClientSession],
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        private: bool = False,
        spec_marker: str = None,
    ) -> Any:
        method_up = method.upper()
        qs = ""
        if params:
            qs = urlencode(sorted(params.items()), doseq=True)
        request_path = path
        if qs:
            full_path = path + "?" + qs
        else:
            full_path = path

        body_str = json.dumps(data or {}, separators=(",", ":"), ensure_ascii=False) if method_up in ("POST", "PUT") else ""
        body_hash = hashlib.sha512(body_str.encode("utf-8") if body_str else b"").hexdigest()

        url = self.base_url + full_path

        headers = {"Content-Type": "application/json"}
        if private:
            ts = self._get_timestamp()
            signature = self._sign(method_up, path, qs, body_hash, ts)
            headers.update({
                "KEY": self.api_key,
                "Timestamp": ts,
                "SIGN": signature,
            })

        async def send_request(sess: aiohttp.ClientSession):
            if method_up == "GET":
                return await sess.get(url, headers=headers)
            elif method_up == "POST":
                return await sess.post(url, headers=headers, data=body_str.encode("utf-8"))
            elif method_up == "DELETE":
                return await sess.delete(url, headers=headers)
            elif method_up == "PUT":
                return await sess.put(url, headers=headers, data=body_str.encode("utf-8"))
            else:
                self.info_handler.debug_error_notes(f"Unsupported HTTP method: {method}", is_print=True)
                return None

        attempt = 0
        while not self.stop_bot and not self.stop_bot_iteration:
            attempt += 1
            try:
                if session and not session.closed:
                    use_sess = session
                    is_temp = False
                else:
                    use_sess = aiohttp.ClientSession()
                    is_temp = True

                if is_temp:
                    async with use_sess:
                        resp = await send_request(use_sess)
                else:
                    resp = await send_request(use_sess)

                text = await resp.text()
                try:
                    j = json.loads(text)
                except Exception:
                    self.info_handler.debug_error_notes(f"Non-JSON response: {resp.status} {text}", is_print=True)
                    j = {}

                if resp.status >= 400:
                    self.info_handler.debug_error_notes(f"HTTP {resp.status}: {j}", is_print=True)

                return j

            except asyncio.CancelledError:
                return {}
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(1)

    # ============ PIBLIC ===================

    async def get_instruments(
        self,
        session: aiohttp.ClientSession,
        instId: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        path = f"/futures/{self.settle}/contracts"
        params = {"contract": instId} if instId else {}

        r = await self._request(session, "GET", path, params=params, private=False)
        if not r:
            return []
        if not isinstance(r, list):
            self.info_handler.debug_error_notes(f"[get_instruments] Unexpected type: {type(r)}")
            return []
        return r

    async def get_current_price(self, instId: str = "BTC_USDT") -> Optional[float]:
        path = f"/futures/{self.settle}/tickers"
        params = {"contract": instId}
        r = await self._request(None, "GET", path, params=params, private=False)

        if not r or not isinstance(r, list):
            return None

        for item in r:
            try:
                cont = item.get("contract") or item.get("name")
                if cont == instId:
                    return float(item.get("last", 0.0))
            except (ValueError, TypeError, AttributeError):
                continue

        return None

    async def get_all_current_prices(self, session: aiohttp.ClientSession) -> Dict[str, float]:
        path = f"/futures/{self.settle}/tickers"
        r = await self._request(session, "GET", path, private=False)

        if not r or not isinstance(r, list):
            return {}

        res: Dict[str, float] = {}
        for item in r:
            try:
                cont = item.get("contract") or item.get("name")
                last = item.get("last")
                if cont is not None and last is not None:
                    res[cont] = float(last)
            except (ValueError, TypeError, AttributeError):
                continue

        return res
    
    # ================ PRIVATE =============
    # ======= GET ==========

    async def fetch_positions(
        self,
        session: aiohttp.ClientSession,
        instId: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        path = f"/futures/{self.settle}/positions"
        params = {"contract": instId} if instId else {}

        r = await self._request(session, "GET", path, params=params, private=True)
        if not r or not isinstance(r, list):
            return []
        return r
    

    async def get_order(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        order_id: str
    ) -> Dict[str, Any]:
        """
        Retrieves the status of a specific order.
        Endpoint: GET /futures/{settle}/orders/{order_id}
        Documentation: https://www.gate.io/docs/developers/apiv4/en/#get-a-single-order
        """
        path = f"/futures/{self.settle}/orders/{order_id}"
        params = {"contract": instId}
        self.info_handler.debug_info_notes(f"[get_order] Request: instId={instId}, order_id={order_id}")
        r = await self._request(session, "GET", path, params=params, private=True)
        if r is None or isinstance(r, dict) and r.get("label"):
            self.info_handler.debug_error_notes(f"[get_order] Error: {r.get('label', 'Unknown')}: {r.get('detail', 'No details')}")
            return {}
        return r

    async def get_realized_pnl(
        self,
        symbol: str,
        pos_side: str,
        margin_size: str,
        start_time: Optional[int],
        end_time: Optional[int],
        open_order_id: Optional[str] = None,  # New optional param for ironclad PNL via order ID
    ) -> dict:
        path = f"/futures/{self.settle}/position_close"
        params: Dict[str, Any] = {"contract": symbol}
        def normalize_timestamp(value):
            """Безопасно приводит время к секундам."""
            if value is None:
                return None
            try:
                ts = float(value)
                if ts > 1e12:
                    ts /= 1000
                return int(ts)
            except (ValueError, TypeError):
                return None

        start_ts = normalize_timestamp(start_time)
        end_ts = normalize_timestamp(end_time)

        if pos_side:
            params["side"] = str(pos_side.lower())

        # If open_order_id is provided, fetch first_open_time from my_trades for ironclad matching
        first_open_time = start_ts  # Default to start_ts if provided
        if open_order_id:
            trade_params = {"contract": symbol, "order": open_order_id}
            trade_resp = await self._request(session=None, method="GET", path=f"/futures/{self.settle}/my_trades", params=trade_params, private=True)
            if isinstance(trade_resp, list) and trade_resp:
                times = [int(row.get("create_time", 0)) for row in trade_resp if row.get("create_time")]
                if times:
                    first_open_time = min(times)
                    self.info_handler.debug_info_notes(f"[get_realized_pnl] Determined first_open_time={first_open_time} from my_trades for order_id={open_order_id}", is_print=True)
                else:
                    self.info_handler.debug_error_notes(f"[get_realized_pnl] No fill times found in my_trades for order_id={open_order_id}", is_print=True)
                    return {"pnl_usdt": None, "roi": None}
            else:
                self.info_handler.debug_error_notes(f"[get_realized_pnl] Failed to fetch my_trades for order_id={open_order_id}", is_print=True)
                return {"pnl_usdt": None, "roi": None}

        # Set 'from' and 'to' based on available times (API filters on close 'time', not first_open_time)
        if first_open_time:
            params["from"] = str(first_open_time - 60)  # Slightly before to catch edge cases
        if end_ts:
            params["to"] = str(end_ts)

        max_retries = 7
        for attempt in range(1, max_retries + 1):
            try:
                resp = await self._request(session=None, method="GET", path=path, params=params, private=True)
                if not resp:
                    self.info_handler.debug_error_notes(
                        f"[get_realized_pnl] Empty response for symbol={symbol}, attempt={attempt}/{max_retries}",
                        is_print=True
                    )
                elif isinstance(resp, dict) and resp.get("label"):
                    self.info_handler.debug_error_notes(
                        f"[get_realized_pnl] Error: {resp.get('label')}: {resp.get('detail', 'No details')}, attempt={attempt}/{max_retries}",
                        is_print=True
                    )
                elif isinstance(resp, list):
                    rows = resp
                    self.info_handler.debug_info_notes(f"[get_realized_pnl] Fetched {len(rows)} rows for symbol={symbol}", is_print=True)
                    break
                else:
                    self.info_handler.debug_error_notes(
                        f"[get_realized_pnl] Unexpected response format: {resp}, attempt={attempt}/{max_retries}",
                        is_print=True
                    )

                if attempt < max_retries:
                    await asyncio.sleep(random.uniform(1, 2))
            except Exception as e:
                self.info_handler.debug_error_notes(
                    f"[get_realized_pnl] Exception: {e}, attempt={attempt}/{max_retries}",
                    is_print=True
                )
                if attempt < max_retries:
                    await asyncio.sleep(random.uniform(1, 2))
        else:
            self.info_handler.debug_error_notes(f"[get_realized_pnl] No PNL data for symbol={symbol}", is_print=True)
            return {"pnl_usdt": None, "roi": None}

        pnl_usdt = 0.0
        was_filtered = False
        for row in rows:
            try:
                ts = int(row.get("first_open_time", 0))
                row_side = row.get("side", "").upper()

                # Strict filter for specific trade using first_open_time (ironclad match)
                if first_open_time and ts != first_open_time:
                    self.info_handler.debug_info_notes(
                        f"[get_realized_pnl] Skipping row: first_open_time={ts} != target={first_open_time}",
                        is_print=True
                    )
                    continue
                if end_ts and int(row.get("time", 0)) > end_ts:
                    self.info_handler.debug_info_notes(
                        f"[get_realized_pnl] Skipping row: close_time={row.get('time')} > end_ts={end_ts}",
                        is_print=True
                    )
                    continue
                if pos_side and row_side != pos_side.upper():
                    self.info_handler.debug_info_notes(
                        f"[get_realized_pnl] Skipping row: side={row_side} != pos_side={pos_side}",
                        is_print=True
                    )
                    continue

                pnl_usdt += float(row.get("pnl", 0))
                was_filtered = True
                self.info_handler.debug_info_notes(f"[get_realized_pnl] Processed row: {row}", is_print=True)
                break
            except Exception as e:
                self.info_handler.debug_error_notes(f"[get_realized_pnl] Error processing row: {e}", is_print=True)
                continue

        if not was_filtered:
            return {"pnl_usdt": None, "roi": None}

        try:
            margin_size_float = float(margin_size) if margin_size else 0.0
            roi = (
                0.0 if pnl_usdt == 0.0 or margin_size_float == 0.0
                else round(pnl_usdt / margin_size_float * 100, 5)
            )
        except (ValueError, TypeError) as e:
            self.info_handler.debug_error_notes(
                f"[get_realized_pnl] Error calculating ROI: margin_size={margin_size}, error: {str(e)}",
                is_print=True
            )
            roi = None

        return {"pnl_usdt": round(pnl_usdt, 5), "roi": roi}

    async def get_order_book(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        limit: int = 1
    ) -> Dict[str, Any]:
        """
        Retrieves the order book for the specified instrument.
        Endpoint: GET /futures/{settle}/order_book
        Documentation: https://www.gate.io/docs/developers/apiv4/en/#retrieve-order-book
        """
        path = f"/futures/{self.settle}/order_book"
        params = {"contract": instId, "limit": limit}
        self.info_handler.debug_info_notes(f"[get_order_book] Request: instId={instId}, limit={limit}")
        r = await self._request(session, "GET", path, params=params, private=False)
        if r is None or isinstance(r, dict) and not r.get("bids") and not r.get("asks"):
            self.info_handler.debug_error_notes(f"[get_order_book] Error: No valid order book data")
            return {}
        return r
    
    # ======== POST ========
    async def set_margin_mode(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        mode: str,
    ) -> Dict[str, Any]:
        if not instId or not mode:
            self.info_handler.debug_error_notes(f"[set_margin_mode] Missing required parameters: instId={instId}, mode={mode}")
            return {}

        mode = mode.upper()
        if mode not in ("ISOLATED", "CROSS"):
            self.info_handler.debug_error_notes(f"[set_margin_mode] Invalid mode: {mode}, must be ISOLATED or CROSS")
            return {}

        path = f"/futures/{self.settle}/dual_comp/positions/cross_mode"
        body = {
            "mode": mode,
            "contract": instId,
        }

        self.info_handler.debug_info_notes(f"[set_margin_mode] Request: contract={instId}, mode={mode}")
        r = await self._request(session, "POST", path, data=body, private=True)
        if r is None:
            self.info_handler.debug_error_notes(f"[set_margin_mode] Empty response for {instId}")
            return {}

        if isinstance(r, dict) and r.get("label"):
            self.info_handler.debug_error_notes(f"[set_margin_mode] Error: {r.get('label')}: {r.get('detail', 'No details')}")
            return r
        else:
            self.info_handler.debug_info_notes(f"[set_margin_mode] Success: Margin mode {mode} set for {instId}, response: {r}")
            return r

    async def set_leverage(
        self,
        session: aiohttp.ClientSession,
        instId: Optional[str] = None,
        lever: int | float | str = None,
        mgnMode: str | None = None,
        posSide: str | None = None,
        ccy: str | None = None,
    ) -> Dict[str, Any]:
        if not instId or not lever:
            self.info_handler.debug_error_notes(f"[set_leverage] Missing required parameters: instId={instId}, lever={lever}")
            return {}

        path = f"/futures/{self.settle}/dual_comp/positions/{instId}/leverage"
        params: Dict[str, Any] = {
            "leverage": str(lever),
        }
        if str(lever) == "0" and mgnMode and mgnMode.lower() == "cross":
            params["cross_leverage_limit"] = "0"

        self.info_handler.debug_info_notes(f"[set_leverage] Request: contract={instId}, leverage={lever}, params={params}")
        r = await self._request(session, "POST", path, params=params, private=True)
        if r is None:
            self.info_handler.debug_error_notes(f"[set_leverage] Empty response for {instId}")
            return {}

        if isinstance(r, dict) and r.get("label"):
            self.info_handler.debug_error_notes(f"[set_leverage] Error: {r.get('label')}: {r.get('detail', 'No details')}")
            return r
        elif isinstance(r, list):
            for position in r:
                if position.get("contract") == instId and position.get("leverage") == str(lever):
                    self.info_handler.debug_info_notes(f"[set_leverage] Success: Leverage {lever} set for {instId}, mode={position.get('mode')}")
                else:
                    self.info_handler.debug_error_notes(f"[set_leverage] Unexpected position data: {position}")
                    return {}
            return {"positions": r}
        else:
            self.info_handler.debug_error_notes(f"[set_leverage] Unexpected response format: {r}")
            return {}

    async def place_main_order(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        sz: float | int | str,
        side: str,
        tdMode: str,
        posSide: str,
        reduceOnly: bool,
        ordType: str = "limit",
        px: Optional[float | int | str] = None,
        client_ord_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Размещает основной ордер (лимитный или рыночный) для открытия или закрытия позиции.
        Эндпоинт: POST /futures/{settle}/orders
        """
        path = f"/futures/{self.settle}/orders"
        size_val = int(float(sz)) if side.lower() == "buy" else -int(float(sz))
        body: Dict[str, Any] = {
            "contract": instId,
            "size": size_val,
            "reduce_only": reduceOnly,
        }

        # --- Тип ордера ---
        if ordType.lower() == "market":
            body["price"] = "0"
            body["tif"] = "ioc"
        elif ordType.lower() == "limit":
            if px is None:
                raise ValueError("px is required for limit order")
            body["price"] = str(px)
            body["tif"] = "gtc"
        else:
            raise ValueError(f"Unsupported ordType for Gate: {ordType}")

        if client_ord_id:
            client_ord_id = client_ord_id[:28]
            body["text"] = f"t-{client_ord_id}"

        self.info_handler.debug_info_notes(f"[place_main_order] Request: {body}")
        r = await self._request(session, "POST", path, data=body, private=True)

        # --- Ошибка API ---
        if not r:
            err_msg = "Empty or invalid response"
            self.info_handler.debug_error_notes(f"[place_main_order] Error: {err_msg}")
            return {"error": err_msg}

        label = r.get("label")
        message = r.get("message") or r.get("detail")

        if label and label != "SUCCESS":
            err_msg = f"{label}: {message or 'No details'}"
            self.info_handler.debug_error_notes(f"[place_main_order] Error: {err_msg}")
            return {"error": err_msg}

        if "error" in r:
            err_msg = r.get("error")
            self.info_handler.debug_error_notes(f"[place_main_order] Error: {err_msg}")
            return {"error": err_msg}

        # --- Успешный ответ ---
        order_id = str(r.get("id"))
        client_suffix = client_ord_id or order_id[:26]
        self.order_ids[client_suffix] = {
            "main_order_id": order_id,
            "tp_order_id": None,
            "sl_order_id": None,
            "instId": instId,
        }

        return r

    # async def place_main_order(
    #     self,
    #     session: aiohttp.ClientSession,
    #     instId: str,
    #     sz: float | int | str,
    #     side: str,
    #     tdMode: str,
    #     posSide: str,
    #     reduceOnly: bool,
    #     ordType: str = "limit",
    #     px: Optional[float | int | str] = None,
    #     client_ord_id: Optional[str] = None,
    # ) -> Dict[str, Any]:
    #     """
    #     Размещает основной ордер (лимитный или рыночный) для открытия или закрытия позиции.
    #     Эндпоинт: POST /futures/{settle}/orders
    #     Документация: https://www.gate.io/docs/developers/apiv4/en/#place-an-order
    #     """
    #     path = f"/futures/{self.settle}/orders"
    #     size_val = int(float(sz)) if side.lower() == "buy" else -int(float(sz))
    #     body: Dict[str, Any] = {
    #         "contract": instId,
    #         "size": size_val,
    #         "reduce_only": reduceOnly,
    #     }

    #     if ordType.lower() == "market":
    #         body["price"] = "0"
    #         body["tif"] = "ioc"
    #     elif ordType.lower() == "limit":
    #         if px is None:
    #             raise ValueError("px is required for limit order")
    #         body["price"] = str(px)
    #         body["tif"] = "gtc"
    #     else:
    #         raise ValueError(f"Unsupported ordType for Gate: {ordType}")

    #     if client_ord_id:
    #         client_ord_id = client_ord_id[:28]
    #         body["text"] = f"t-{client_ord_id}"

    #     self.info_handler.debug_info_notes(f"[place_main_order] Request: {body}")
    #     r = await self._request(session, "POST", path, data=body, private=True)
    #     if r is None or isinstance(r, dict) and r.get("label"):
    #         self.info_handler.debug_error_notes(f"[place_main_order] Error: {r.get('label', 'Unknown')}: {r.get('detail', 'No details')}")
    #         return r

    #     order_id = str(r.get("id"))
    #     client_suffix = client_ord_id or order_id[:26]
    #     self.order_ids[client_suffix] = {
    #         "main_order_id": order_id,
    #         "tp_order_id": None,
    #         "sl_order_id": None,
    #         "instId": instId,
    #     }

    #     return r

    # === GATE CLIENT CORE ===
    async def place_tp_order(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        sz: float | int | str,
        side: str,
        client_ord_id: str,
        trigger_px: float | int | str,
        ord_px: float | int | str = "-1",
        trigger_px_type: str = "last",
        order_number: int = 1
    ) -> Dict[str, Any]:
        """Размещает тейк-профит ордер"""
        path = f"/futures/{self.settle}/price_orders"
        abs_size = abs(int(float(sz)))
        close_size = -abs_size if side.lower() == "buy" else abs_size
        client_suffix = client_ord_id[:26]

        price_type_map = {"last": 0, "mark": 1, "index": 2}
        price_type = price_type_map.get(trigger_px_type.lower(), 0)

        body = {
            "initial": {
                "contract": instId,
                "size": close_size,
                "price": str(ord_px) if ord_px != "-1" else "0",
                "reduce_only": True,
                "tif": "gtc" if ord_px != "-1" else "ioc",
                "text": f"t-tp-{client_suffix}"[:30],
            },
            "trigger": {
                "strategy_type": 0,
                "price_type": price_type,
                "price": str(trigger_px),
                "rule": 1 if side.lower() == "buy" else 2,
                "expiration": 86400,
            },
        }

        self.info_handler.debug_info_notes(f"[place_tp_order] Request: {body}")

        try:
            res = await self._request(session, "POST", path, data=body, private=True)
            if isinstance(res, dict) and res.get("label"):
                self.info_handler.debug_error_notes(f"[place_tp_order] Error: {res.get('label')}: {res.get('detail', 'No details')}")
                return res
            self.info_handler.debug_info_notes(f"[place_tp_order] Success: {res}")
            self.order_ids[client_suffix][f"tp_order{order_number}_id"] = str(res.get("id"))
            return res
        except Exception as e:
            self.info_handler.debug_error_notes(f"[place_tp_order] Exception: {str(e)}")
            return {"label": "EXCEPTION", "detail": str(e)}

    async def place_sl_order(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        sz: float | int | str,
        side: str,
        client_ord_id: str,
        trigger_px: float | int | str,
        ord_px: float | int | str = "-1",
        trigger_px_type: str = "last",
    ) -> Dict[str, Any]:
        """Размещает стоп-лосс ордер"""
        path = f"/futures/{self.settle}/price_orders"
        abs_size = abs(int(float(sz)))
        close_size = -abs_size if side.lower() == "buy" else abs_size
        client_suffix = client_ord_id[:26]

        price_type_map = {"last": 0, "mark": 1, "index": 2}
        price_type = price_type_map.get(trigger_px_type.lower(), 0)

        body = {
            "initial": {
                "contract": instId,
                "size": close_size,
                "price": str(ord_px) if ord_px != "-1" else "0",
                "reduce_only": True,
                "tif": "gtc" if ord_px != "-1" else "ioc",
                "text": f"t-sl-{client_suffix}"[:30],
            },
            "trigger": {
                "strategy_type": 0,
                "price_type": price_type,
                "price": str(trigger_px),
                "rule": 2 if side.lower() == "buy" else 1,
                "expiration": 86400,
            },
        }

        self.info_handler.debug_info_notes(f"[place_sl_order] Request: {body}")

        try:
            res = await self._request(session, "POST", path, data=body, private=True)
            if isinstance(res, dict) and res.get("label"):
                self.info_handler.debug_error_notes(f"[place_sl_order] Error: {res.get('label')}: {res.get('detail', 'No details')}")
                return res
            self.info_handler.debug_info_notes(f"[place_sl_order] Success: {res}")
            self.order_ids[client_suffix]["sl_order_id"] = str(res.get("id"))
            return res
        except Exception as e:
            self.info_handler.debug_error_notes(f"[place_sl_order] Exception: {str(e)}")
            return {"label": "EXCEPTION", "detail": str(e)}

    # async def close_by_bid_ask(
    #     self,
    #     session: aiohttp.ClientSession,
    #     instId: str,
    #     sz: float | int | str,
    #     posSide: str,
    #     tdMode: str,
    #     client_ord_id: Optional[str] = None
    # ) -> Dict[str, Any]:
    #     """
    #     Closes a position using a limit order at the best bid (for long) or ask (for short) price.
    #     Ensures the position is closed by setting reduceOnly=True.
    #     If no valid price from order book, falls back to market order.
    #     After placing limit, checks status; if not filled, cancels and places market order for guarantee.
    #     Endpoint: POST /futures/{settle}/orders
    #     Documentation: https://www.gate.io/docs/developers/apiv4/en/#place-an-order
    #     """
    #     debug_label = f"[close_by_bid_ask_{instId}_{posSide}]"
    #     side = "sell" if posSide.lower() == "long" else "buy"

    #     # Get order book to retrieve best bid/ask price
    #     order_book = await self.get_order_book(session, instId, limit=1)
    #     if not order_book:
    #         self.info_handler.debug_error_notes(f"{debug_label} Failed to retrieve order book - falling back to market")
    #         return await self.place_main_order(
    #             session=session,
    #             instId=instId,
    #             sz=sz,
    #             side=side,
    #             tdMode=tdMode,
    #             posSide=posSide.lower(),
    #             reduceOnly=True,
    #             ordType="market",
    #             client_ord_id=client_ord_id
    #         )

    #     # Extract best price based on position side (format: list of dicts {"p": str, "s": int})
    #     price = None
    #     if posSide.lower() == "long":
    #         bids = order_book.get("bids", [])
    #         if bids:
    #             price = bids[0].get("p")
    #     else:  # short
    #         asks = order_book.get("asks", [])
    #         if asks:
    #             price = asks[0].get("p")

    #     if price is None:
    #         self.info_handler.debug_error_notes(f"{debug_label} No valid bid/ask price found - falling back to market")
    #         return await self.place_main_order(
    #             session=session,
    #             instId=instId,
    #             sz=sz,
    #             side=side,
    #             tdMode=tdMode,
    #             posSide=posSide.lower(),
    #             reduceOnly=True,
    #             ordType="market",
    #             client_ord_id=client_ord_id
    #         )

    #     # Generate client order ID if not provided
    #     client_ord_id = client_ord_id or f"close_{instId}_{posSide}_{int(time.time() * 1000)}"[:28]

    #     self.info_handler.debug_info_notes(
    #         f"{debug_label} Placing limit order to close: size={sz}, side={side}, price={price}, client_ord_id={client_ord_id}"
    #     )

    #     # Place limit order at bid/ask price
    #     close_resp = await self.place_main_order(
    #         session=session,
    #         instId=instId,
    #         sz=sz,
    #         side=side,
    #         tdMode=tdMode,
    #         posSide=posSide.lower(),
    #         reduceOnly=True,
    #         ordType="limit",
    #         px=price,
    #         client_ord_id=client_ord_id
    #     )

    #     if close_resp.get("label"):
    #         self.info_handler.debug_error_notes(f"{debug_label} Limit order failed - falling back to market")
    #         return await self.place_main_order(
    #             session=session,
    #             instId=instId,
    #             sz=sz,
    #             side=side,
    #             tdMode=tdMode,
    #             posSide=posSide.lower(),
    #             reduceOnly=True,
    #             ordType="market",
    #             client_ord_id=client_ord_id
    #         )

    #     order_id = str(close_resp.get("id"))
    #     self.info_handler.debug_info_notes(f"{debug_label} Limit order placed: order_id={order_id}. Checking status in 2s...")

    #     # Wait and check status to guarantee closure
    #     await asyncio.sleep(2)
    #     status_resp = await self.get_order(session, instId, order_id)
    #     status = status_resp.get("status", "")

    #     if status != "filled":
    #         self.info_handler.debug_info_notes(f"{debug_label} Limit not filled (status: {status}) - canceling and falling back to market")
    #         await self.cancel_order_by_id(session, instId, order_id)
    #         return await self.place_main_order(
    #             session=session,
    #             instId=instId,
    #             sz=sz,
    #             side=side,
    #             tdMode=tdMode,
    #             posSide=posSide.lower(),
    #             reduceOnly=True,
    #             ordType="market",
    #             client_ord_id=client_ord_id
    #         )

    #     self.info_handler.debug_info_notes(f"{debug_label} Position closed successfully with limit order")
    #     return close_resp

    async def close_by_bid_ask(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        sz: float | int | str,
        posSide: str,
        tdMode: str,
        client_ord_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Closes a position using a limit order at the best bid (for long) or ask (for short) price.
        Ensures the position is closed by setting reduceOnly=True.
        If no valid price from order book, falls back to market order.
        After placing limit, checks status; if not filled, cancels and places market order for guarantee.
        """
        debug_label = f"[close_by_bid_ask_{instId}_{posSide}]"
        side = "sell" if posSide.lower() == "long" else "buy"
        validator = GateApiResponseValidator

        # === 1. Получаем стакан ===
        order_book = await self.get_order_book(session, instId, limit=1)
        if not order_book:
            self.info_handler.debug_error_notes(f"{debug_label} ❌ Failed to retrieve order book → fallback MARKET")
            resp = await self.place_main_order(
                session, instId, sz, side, tdMode, posSide.lower(),
                True, "market", None, client_ord_id
            )
            return validator.normalize_response(resp)

        # === 2. Извлекаем лучшую цену ===
        price = None
        if posSide.lower() == "long":
            bids = order_book.get("bids", [])
            if bids:
                price = bids[0].get("p")
        else:
            asks = order_book.get("asks", [])
            if asks:
                price = asks[0].get("p")

        if not price:
            self.info_handler.debug_error_notes(f"{debug_label} ❌ No valid bid/ask → fallback MARKET")
            resp = await self.place_main_order(
                session, instId, sz, side, tdMode, posSide.lower(),
                True, "market", None, client_ord_id
            )
            return validator.normalize_response(resp)

        # === 3. Формируем client_ord_id ===
        client_ord_id = client_ord_id or f"close_{instId}_{posSide}_{int(time.time() * 1000)}"[:28]
        self.info_handler.debug_info_notes(
            f"{debug_label} Placing LIMIT close @ {price} | side={side}, sz={sz}"
        )

        # === 4. Размещаем лимитный ордер ===
        close_resp = await self.place_main_order(
            session=session,
            instId=instId,
            sz=sz,
            side=side,
            tdMode=tdMode,
            posSide=posSide.lower(),
            reduceOnly=True,
            ordType="limit",
            px=price,
            client_ord_id=client_ord_id
        )
        norm_limit = validator.normalize_response(close_resp)

        # === 5. Проверяем, не вернула ли биржа ошибку ===
        if validator.get_code(norm_limit):
            self.info_handler.debug_error_notes(f"{debug_label} ❌ Limit failed ({norm_limit.get('label')}) → fallback MARKET")
            resp = await self.place_main_order(
                session, instId, sz, side, tdMode, posSide.lower(),
                True, "market", None, client_ord_id
            )
            return validator.normalize_response(resp)

        order_id = str(norm_limit.get("id"))
        self.info_handler.debug_info_notes(f"{debug_label} Limit placed (id={order_id}), waiting 2s...")

        # === 6. Проверяем статус ===
        await asyncio.sleep(2)
        status_resp = await self.get_order(session, instId, order_id)
        status = str(status_resp.get("status", "")).lower()

        if status not in ("filled", "closed"):
            self.info_handler.debug_info_notes(f"{debug_label} Not filled (status={status}) → cancel + MARKET")
            await self.cancel_order_by_id(session, instId, order_id)
            resp = await self.place_main_order(
                session, instId, sz, side, tdMode, posSide.lower(),
                True, "market", None, client_ord_id
            )
            return validator.normalize_response(resp)

        self.info_handler.debug_info_notes(f"{debug_label} ✅ Position closed successfully (LIMIT id={order_id})")
        return norm_limit


    # ====== DEL ===========
    async def cancel_order_by_id(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        order_id: str
    ) -> Dict[str, Any]:
        """Cancels a TP/SL (trigger) order by ID."""
        path = f"/futures/{self.settle}/price_orders/{order_id}"
        params = {"contract": instId}
        self.info_handler.debug_info_notes(f"[cancel_price_order_by_id] Request: instId={instId}, order_id={order_id}")
        r = await self._request(session, "DELETE", path, params=params, private=True)
        if not r or (isinstance(r, dict) and r.get("label")):
            self.info_handler.debug_error_notes(f"[cancel_price_order_by_id] Error: {r.get('label', 'Unknown')} - {r.get('detail', 'No details')}")
            return {}
        return r
    # async def cancel_order_by_id(
    #     self,
    #     session: aiohttp.ClientSession,
    #     instId: str,
    #     order_id: str
    # ) -> Dict[str, Any]:
    #     """
    #     Cancels a specific order.
    #     Endpoint: DELETE /futures/{settle}/orders/{order_id}
    #     Documentation: https://www.gate.io/docs/developers/apiv4/en/#cancel-a-single-order
    #     """
    #     path = f"/futures/{self.settle}/orders/{order_id}"
    #     params = {"contract": instId}
    #     self.info_handler.debug_info_notes(f"[cancel_order_by_id] Request: instId={instId}, order_id={order_id}")
    #     r = await self._request(session, "DELETE", path, params=params, private=True)
    #     if r is None or isinstance(r, dict) and r.get("label"):
    #         self.info_handler.debug_error_notes(f"[cancel_order_by_id] Error: {r.get('label', 'Unknown')}: {r.get('detail', 'No details')}")
    #         return {}
    #     return r
    
    async def cancel_trigger_orders(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        pos_side: str,
        order_type: str = "main",
    ) -> Dict[str, Any]:
        """
        Отменяет все ордера указанного типа (основные или триггерные) по символу и стороне позиции.
        Эндпоинт для основных: DELETE /futures/{settle}/orders
        Эндпоинт для триггерных: DELETE /futures/{settle}/price_orders
        Документация: 
        - https://www.gate.io/docs/developers/apiv4/en/#cancel-all-orders-with-open-status
        - https://www.gate.io/docs/developers/apiv4/en/#cancel-a-price-triggered-order-2
        """
        debug_label = f"[cancel_trigger_orders_{instId}_{pos_side}_{order_type}]"
        text_prefix = f"t-{instId}_{pos_side}_"
        side = "bid" if pos_side.lower() == "long" else "ask" if order_type == "main" else None

        if order_type not in ("main", "trigger"):
            self.info_handler.debug_error_notes(f"{debug_label} Invalid order_type: {order_type}, must be 'main' or 'trigger'")
            return {"cancelled_count": 0}

        path = f"/futures/{self.settle}/{'orders' if order_type == 'main' else 'price_orders'}"
        params = {"contract": instId}
        if order_type == "main":
            params["side"] = side
            params["exclude_reduce_only"] = False
        else:
            params["text"] = text_prefix

        self.info_handler.debug_info_notes(f"{debug_label} Cancel {order_type} orders: contract={instId}, params={params}")
        resp = await self._request(session, "DELETE", path, params=params, private=True)
        
        result = {f"{order_type}_orders": resp, "cancelled_count": 0}
        
        if isinstance(resp, dict) and resp.get("label"):
            self.info_handler.debug_error_notes(
                f"{debug_label} {order_type.capitalize()} orders cancel error: {resp.get('label')}: {resp.get('detail', 'No details')}"
            )
        else:
            cancelled_orders = GateApiResponseValidator.get_data_list(resp)
            result["cancelled_count"] = len(cancelled_orders)
            self.info_handler.debug_info_notes(f"{debug_label} {order_type.capitalize()} orders cancelled: {len(cancelled_orders)} orders")

        # Clean up order_ids
        try:
            for key in list(self.order_ids.keys()):
                if key.startswith(text_prefix):
                    if order_type == "main":
                        self.order_ids[key]["main_order_id"] = None
                    else:
                        self.order_ids[key]["tp_order_id"] = None
                        self.order_ids[key]["sl_order_id"] = None
        except Exception as e:
            self.info_handler.debug_error_notes(
                f"{debug_label} Error cleaning order_ids: {str(e)}",
                is_print=True
            )

        return result

    async def cancel_all_orders_by_symbol_and_side(
        self,
        session: aiohttp.ClientSession,
        instId: str,
        pos_side: str,
    ) -> Dict[str, Any]:
        """
        Отменяет все ордера (основные и триггерные) по символу и стороне позиции.
        """
        debug_label = f"[cancel_all_orders_{instId}_{pos_side}]"
        result = {
            "main_orders": {},
            "price_orders": {},
            "main_cancelled_count": 0,
            "price_cancelled_count": 0
        }

        try:
            main_result = await self.cancel_trigger_orders(session, instId, pos_side, order_type="main")
            result["main_orders"] = main_result.get("main_orders", {})
            result["main_cancelled_count"] = main_result.get("cancelled_count", 0)
            self.info_handler.debug_info_notes(f"{debug_label} Cancelled {result['main_cancelled_count']} main orders")
        except Exception as e:
            self.info_handler.debug_error_notes(f"{debug_label} Error in cancel_main_orders: {str(e)}", is_print=True)

        try:
            trigger_result = await self.cancel_trigger_orders(session, instId, pos_side, order_type="trigger")
            result["price_orders"] = trigger_result.get("price_orders", {})
            result["price_cancelled_count"] = trigger_result.get("cancelled_count", 0)
            self.info_handler.debug_info_notes(f"{debug_label} Cancelled {result['price_cancelled_count']} trigger orders")
        except Exception as e:
            self.info_handler.debug_error_notes(f"{debug_label} Error in cancel_trigger_orders: {str(e)}", is_print=True)

        # Очистка order_ids
        try:
            text_prefix = f"t-{instId}_{pos_side}_"
            for key in list(self.order_ids.keys()):
                if key.startswith(text_prefix):
                    self.order_ids[key]["main_order_id"] = None
                    self.order_ids[key]["tp_order_id"] = None
                    self.order_ids[key]["sl_order_id"] = None
        except Exception as e:
            self.info_handler.debug_error_notes(f"{debug_label} Error cleaning order_ids: {str(e)}", is_print=True)

        return result

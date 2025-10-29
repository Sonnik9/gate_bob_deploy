import asyncio
import aiohttp
from a_config import PING_URL, PING_INTERVAL
from b_context import BotContext
from c_log import ErrorHandler


class NetworkManager:
    def __init__(self, context: BotContext, info_handler: ErrorHandler, proxy_url: str = None):
        info_handler.wrap_foreign_methods(self)
        self.context = context
        self.info_handler = info_handler
        self._ping_task: asyncio.Task | None = None
        self.proxy_url = proxy_url

    async def initialize_session(self):
        if not self.context.session or self.context.session.closed:
            # Если прокси задан — используем connector с ним
            if self.proxy_url:
                connector = aiohttp.TCPConnector(ssl=False)  # отключаем SSL проверки, если нужно
                self.context.session = aiohttp.ClientSession(
                    connector=connector,
                    # trust_env=False,  # игнорировать системные прокси
                    # proxy=self.proxy_url
                )
            else:
                self.context.session = aiohttp.ClientSession()

    async def _ping_once(self) -> bool:
        """Пинг для проверки живости сессии."""
        if not self.context.session or self.context.session.closed:
            await self.initialize_session()
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with self.context.session.get(PING_URL, timeout=timeout) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def _ping_loop(self):
        """Фоновый таск: держим сессию живой."""
        attempt = 0
        while not self.context.stop_bot and not self.context.stop_bot_iteration:
            attempt += 1
            alive = await self._ping_once()
            if not alive:
                self.info_handler.debug_info_notes(f"🔁 Пинг неудачен, пересоздаем сессию (попытка {attempt})")
                try:
                    if self.context.session and not self.context.session.closed:
                        await self.context.session.close()
                except Exception as e:
                    self.info_handler.debug_error_notes(f"Ошибка при закрытии сессии: {e}")
                await self.initialize_session()
            await asyncio.sleep(PING_INTERVAL)

    def start_ping_loop(self):
        """Запуск фонового пинга."""
        if self._ping_task is None or self._ping_task.done():
            self._ping_task = asyncio.create_task(self._ping_loop())

    async def shutdown_session(self):
        """Закрытие сессии и остановка фонового пинга."""
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self.context.session and not self.context.session.closed:
            try:
                await self.context.session.close()
            except Exception as e:
                self.info_handler.debug_error_notes(f"Ошибка при закрытии сессии: {e}")


# if __name__ == "__main__":
#     import datetime

#     async def test_ping_urls():
#         urls = [
#             "https://api.gateio.ws/api/v4/futures/usdt/server_time",
#             "https://api.gateio.ws/api/v4/spot/time",
#             "https://api.gateio.ws/api/v4/futures/server_time",
#             "https://api.gateio.ws/api/v4/futures/usdt/tickers",  # тоже живой, можно использовать как fallback
#         ]

#         print(f"=== Проверка пинг-урлов Gate.io ===")
#         async with aiohttp.ClientSession() as session:
#             for url in urls:
#                 try:
#                     t0 = datetime.datetime.now()
#                     async with session.get(url, timeout=5) as resp:
#                         dt = (datetime.datetime.now() - t0).total_seconds()
#                         print(f"[{resp.status}] {url} — ok, time: {dt:.3f}s")
#                         text = await resp.text()
#                         print(f"Ответ: {text[:100]}...\n")
#                 except Exception as e:
#                     print(f"[ERR] {url} — {e}\n")

#     asyncio.run(test_ping_urls())

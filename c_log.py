from datetime import datetime
import pytz
import inspect
from types import FunctionType, MethodType, BuiltinFunctionType
from pprint import pformat
from a_config import TIME_ZONE
import traceback

TZ_LOCATION = pytz.timezone(TIME_ZONE)               

def log_time():
    now = datetime.now(TZ_LOCATION)
    return now.strftime("%Y-%m-%d %H:%M:%S")


class Total_Logger:
    def __init__(self): 
        self.debug_err_list: list = []
        self.debug_info_list: list = []

    # debug    
    def debug_error_notes(self, data: str, is_print: bool=True):
        data += f" Time: {log_time()}" + "[ERROR]"
        # self.debug_err_list.append(data)
        # if is_print:
        print(data)

    def debug_info_notes(self, data: str, is_print: bool=True):
        data += f" Time: {log_time()}" + "[INFO]"
        # self.debug_info_list.append(data)
        # if is_print:
        print(data)

    def _log_decor_notes(self, ex, is_print: bool=True):
        """Логирование исключений с указанием точного места ошибки."""
        exception_message = str(ex)
        stack = inspect.trace()

        if stack:
            last_frame = stack[-1]
            file_name = last_frame.filename
            line_number = last_frame.lineno
            func_name = last_frame.function

            message = f"Error in '{func_name}' at {file_name}, line {line_number}: {exception_message}"
        else:
            message = f"Error: {exception_message}"
        # if is_print:
        print(message)
        # self.debug_err_list.append(message)

    async def _async_log_exception(self, ex):
        """Асинхронное логирование без блокировок."""
        self._log_decor_notes(ex)

    def total_exception_decor(self, func):
        """Универсальный и безопасный декоратор логирования исключений с контекстом."""
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as ex:
                arg_str = pformat({"args": args, "kwargs": kwargs})
                stack = "".join(traceback.format_exc())
                self.debug_error_notes(
                    f"[ASYNC ERROR] {func.__qualname__} -> {ex}\nArgs:\n{arg_str}\nStack:\n{stack}"
                )
                return None

        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as ex:
                arg_str = pformat({"args": args, "kwargs": kwargs})
                stack = "".join(traceback.format_exc())
                self.debug_error_notes(
                    f"[SYNC ERROR] {func.__qualname__} -> {ex}\nArgs:\n{arg_str}\nStack:\n{stack}"
                )
                return None

        return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper


class ErrorHandler(Total_Logger):
    def __init__(self):
        super().__init__()

    def wrap_foreign_methods(self, obj):
        """
        Оборачивает все методы объекта obj декоратором total_exception_decor,
        включая обычные методы, classmethod и staticmethod.
        Не трогает магические методы и уже обёрнутые.
        """
        for name, attr in obj.__class__.__dict__.items():
            if name.startswith("__"):
                continue  # пропускаем магические методы

            original = getattr(obj, name)

            # Проверка, чтобы не обернуть уже обёрнутый
            if hasattr(original, "_is_wrapped"):
                continue

            # Staticmethod
            if isinstance(attr, staticmethod):
                func = attr.__func__
                wrapped_func = self.total_exception_decor(func)
                wrapped_func._is_wrapped = True
                setattr(obj, name, staticmethod(wrapped_func))

            # Classmethod
            elif isinstance(attr, classmethod):
                func = attr.__func__
                wrapped_func = self.total_exception_decor(func)
                wrapped_func._is_wrapped = True
                setattr(obj, name, classmethod(wrapped_func))

            # Обычные методы
            elif isinstance(attr, (FunctionType, MethodType, BuiltinFunctionType)):
                wrapped_func = self.total_exception_decor(original)
                wrapped_func._is_wrapped = True
                setattr(obj, name, wrapped_func)

import logging
import threading
from pathlib import Path
from ..configuration import LoggingConfig

from colorama import Fore, Style

file_path = Path(__file__).parent

# Predefined list of colors
THREAD_COLORS = [Fore.MAGENTA, Fore.GREEN, Fore.YELLOW, Fore.BLUE, Fore.WHITE, Fore.MAGENTA]
ASSIGNED_COLORS = {}

TRACE_LOGLEVEL = 5
logging.addLevelName(TRACE_LOGLEVEL, "TRACE")


def get_thread_color(thread_id):
    """
    Retrieves a color for the thread, assigning and remembering it for future calls.
    :param thread_id: Unique thread identifier (integer).
    :return: A color from the available list, or a default color if none are available.
    """
    if thread_id in ASSIGNED_COLORS:
        return ASSIGNED_COLORS[thread_id]

    if THREAD_COLORS:
        color = THREAD_COLORS.pop(0)
        ASSIGNED_COLORS[thread_id] = color
        return color
    else:
        return Fore.RESET  # Default when no colors are available


class ThreadColorFormatter(logging.Formatter):
    """
    Custom logging formatter that assigns colors to threads dynamically.
    """

    def format(self, record):
        # Get the thread ID and determine its color
        thread_id = threading.get_ident()
        thread_color = get_thread_color(thread_id)

        # Determine color based on log level
        if record.levelno >= logging.WARNING:
            thread_color = Fore.RED

        # Add color to the entire log line
        log_line = super().format(record)
        return f"{thread_color}{log_line}{Style.RESET_ALL}"


class MyLogger(logging.Logger):
    def __init__(self, name, level=logging.NOTSET):
        super().__init__(name, level)

    def trace(self, msg, *args, **kwargs):
        if self.isEnabledFor(TRACE_LOGLEVEL):
            self._log(TRACE_LOGLEVEL, msg, args, **kwargs)


logging.setLoggerClass(MyLogger)


def init_logger(logging_config: LoggingConfig, name="model-orchestrator"):
    logger = get_logger(name)
    logger.setLevel(logging.getLevelNamesMapping()[logging_config.level.upper()])

    for handler in logger.handlers:
        logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    # console_handler.setLevel(config.get("log.console.level", 1))
    formatter_args = {
        'fmt': "{asctime:^19} | {name:^20.20} | {levelname[0]:^1} | {thread:^10} | {message}",
        'style': "{",
        'datefmt': "%Y-%m-%d %H:%M:%S"
    }

    color_formatter = ThreadColorFormatter(**formatter_args)
    console_handler.setFormatter(color_formatter)
    logger.addHandler(console_handler)

    file_config = logging_config.file
    if file_config:
        file_handler = logging.FileHandler(file_config.path, mode='a')
        file_handler.setFormatter(logging.Formatter(**formatter_args,))
        logger.addHandler(file_handler)


def get_logger(name="model-orchestrator"):
    """
    Returns a logger instance configured for the given name.
    """
    logger = logging.getLogger(name)
    logger.propagate = False

    return logger


def get_colored_logger(level, message, color: Fore, name="model-orchestrator"):
    logger = get_logger(name)
    if logger.isEnabledFor(level):
        colored_message = f"{color}{message}{Style.RESET_ALL}"
        logger.log(level, colored_message)


def attach_uvicorn_to_my_logger(base_logger_name: str = "model-orchestrator") -> None:
    base = logging.getLogger(base_logger_name)

    # Reuse *your* handlers/formatter (console + file if any)
    handlers = list(base.handlers)
    level = base.level

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        l = logging.getLogger(name)
        l.handlers.clear()
        for h in handlers:
            l.addHandler(h)
        l.setLevel(level)
        l.propagate = False
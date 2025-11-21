from ._thread_poller import ThreadPoller
import importlib.resources

dockerfile = importlib.resources.read_text(__package__, 'Dockerfile')
import threading


class ThreadPoller:
    def __init__(self,
                 task,
                 stop_event: threading.Event,
                 interval=5,
                 on_start=None,
                 on_stop=None):
        self.task = task
        self.interval = interval
        self.previous_state = None
        self.stop_event = stop_event

        self.on_start = on_start
        self.on_stop = on_stop

    def start_polling(self):
        if self.on_start:
            self.on_start()
        while not self.stop_event.is_set():
            self.task()
            self.stop_event.wait(self.interval)
        if self.on_stop:
            self.on_stop()

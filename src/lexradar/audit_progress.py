"""Local stage timings and passive heartbeat; never retries or contacts a provider."""

import json
import threading
import time
from contextlib import contextmanager

from .audit_capture import scrub


class Progress:
    def __init__(self, root, *, stream=None):
        self.root, self.stream = root, stream
        self.started = time.monotonic()
        self.lock = threading.Lock()

    def __call__(self, stage, **details):
        event = dict(
            stage=stage, elapsed_seconds=round(time.monotonic() - self.started, 3), **details
        )
        line = scrub(json.dumps(event, ensure_ascii=False))
        with self.lock:
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as file:
                file.write(line + "\n")
            if self.stream:
                print(line, file=self.stream, flush=True)

    @contextmanager
    def stage(self, name):
        started = time.monotonic()
        self(name + "_started")
        try:
            yield
        except Exception as exc:
            self(
                name + "_error",
                error_type=type(exc).__name__,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            raise
        else:
            self(name + "_completed", duration_seconds=round(time.monotonic() - started, 3))


@contextmanager
def waiting(callback, interval=10):
    stop = threading.Event()

    def pulse():
        while not stop.wait(interval):
            callback("waiting_for_response")

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()

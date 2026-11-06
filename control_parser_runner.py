"""Terminate the parser process group if its owning Control Center disappears."""
import os
import fcntl
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def main():
    owner = os.getppid()
    if owner == 1:
        return 1
    os.umask(0o077)

    def watch():
        while True:
            if os.getppid() != owner:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            time.sleep(0.25)

    threading.Thread(target=watch, daemon=True).start()
    command = sys.argv[1:]
    if '--sequential' in command:
        database = Path(command[command.index('--database') + 1])
        database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(database.parent / 'parser.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('MedAdvisor уже запущен; повторите задание после завершения.', file=sys.stderr)
                return 1
            # Keep the lock in the child until it exits, including runner termination.
            return subprocess.call(command, stdin=subprocess.DEVNULL, pass_fds=(lock.fileno(),))
    return subprocess.call(command, stdin=subprocess.DEVNULL)


if __name__ == '__main__':
    raise SystemExit(main())

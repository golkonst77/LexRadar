"""Terminate the parser process group if its owning Control Center disappears."""
import os
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
    return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)


if __name__ == '__main__':
    raise SystemExit(main())

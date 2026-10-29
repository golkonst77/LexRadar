"""Legacy module entry point delegates to the common bounded PDF worker."""

import sys

from ..collector.pdf import worker

if __name__ == "__main__":
    if len(sys.argv) == 3:
        sys.argv.extend(["100000", "1000000"])
    worker()

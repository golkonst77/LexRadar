"""Private, local audit artifacts. Never accepts request headers or credentials."""

import json
import os
import re
from pathlib import Path


def scrub(text):
    text = re.sub(r'(?i)Bearer\s+[^\s"<>]+', "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|authorization|credential|password)"
        r'["\s]*[:=]["\s]*)([^\s",<>]+)',
        r"\1[REDACTED]",
        text,
    )


def private_write(path, text):
    path = Path(path)
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
        "w",
        encoding="utf-8",
        newline="",
    ) as stream:
        stream.write(scrub(text))


class Capture:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)

    def write(self, name, text):
        private_write(self.root / name, text)

    def response(self, content):
        self.write("original.txt", content)
        self.write(
            "capture.json",
            json.dumps(
                {
                    "redacted": scrub(content) != content,
                    "scope": "model message content, before validation; no HTTP headers",
                }
            ),
        )

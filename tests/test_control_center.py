"""Exercise HTTP request handling without requiring a listening socket."""

import io
import json
import unittest

from control_center import ControlHandler


class MemoryConnection:
    def __init__(self, path):
        self.request = io.BytesIO(f"GET {path} HTTP/1.0\r\n\r\n".encode("ascii"))
        self.response = bytearray()

    def makefile(self, *args, **kwargs):
        return self.request

    def sendall(self, data):
        self.response.extend(data)


class ControlCenterTest(unittest.TestCase):
    def request(self, path):
        connection = MemoryConnection(path)
        ControlHandler(connection, ("127.0.0.1", 0), None)
        headers, body = bytes(connection.response).split(b"\r\n\r\n", 1)
        self.assertIn(f"Content-Length: {len(body)}".encode(), headers)
        return headers.decode("ascii"), body.decode("utf-8")

    def test_routes(self):
        headers, page = self.request("/")
        self.assertIn("200 OK", headers)
        self.assertIn("text/html; charset=utf-8", headers)
        self.assertIn('<html lang="ru">', page)
        self.assertIn("Центр управления LexRadar", page)
        for section in ("projects", "reports", "settings"):
            self.assertIn(f'href="#{section}"', page)
            self.assertIn(f'id="{section}"', page)
        for path, expected in (
            ("/health", {"status": "ok"}),
            ("/api/status", {
                "service": "LexRadar Control Center", "status": "ok",
                "mode": "read_only", "analysis_enabled": False,
                "mailing_enabled": False,
            }),
        ):
            with self.subTest(path=path):
                headers, body = self.request(path)
                self.assertIn("200 OK", headers)
                self.assertIn("application/json", headers)
                self.assertEqual(json.loads(body), expected)
        headers, body = self.request("/missing")
        self.assertIn("404 Not Found", headers)
        self.assertEqual(json.loads(body), {"error": "not_found"})


if __name__ == "__main__":
    unittest.main()

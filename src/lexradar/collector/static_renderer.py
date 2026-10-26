"""Explicit static rendering adapter: no browser, scripts or visual evidence."""

from contextlib import contextmanager
import html
import re
from types import SimpleNamespace

from playwright.sync_api import Error as BrowserError

from ..primary.markup import SavedMarkup, compact


class StaticPage:
    def set_default_timeout(self, timeout):
        pass

    def route(self, pattern, callback):
        self.callback = callback

    def goto(self, url, **kwargs):
        self.url = url
        request = SimpleNamespace(url=url, is_navigation_request=lambda: True)
        route = SimpleNamespace(request=request, fulfill=self.fulfill, abort=lambda: None)
        self.callback(route, request)

    def fulfill(self, *, body, **kwargs):
        self.raw_html = body.decode("utf-8", errors="replace")
        self.parser = SavedMarkup(self.url, recover=True)
        self.parser.feed(self.raw_html)
        self.parser.close()

    def title(self):
        return ""

    def evaluate(self, script):
        consent_controls = []
        controls = re.finditer(
            r"<(button|a)\b([^>]*)>(.*?)</\1\s*>|<(input)\b([^>]*)/?>",
            self.raw_html,
            re.I | re.S,
        )
        for match in controls:
            label = html.unescape(re.sub(r"<[^>]*>", " ", match.group(3) or ""))
            attributes = match.group(2) or match.group(5) or ""
            value = " ".join((attributes, label))
            if re.search(r"cookie|куки|consent|соглас|настро|accept|reject|отказ", value, re.I):
                consent_controls.append(re.sub(r"\s+", " ", label or attributes).strip()[:300])
            if len(consent_controls) >= 50:
                break
        return {
            "forms": [
                {
                    "purpose": form.purpose,
                    "fields": [f.model_dump() for f in form.fields],
                    "submission_context": compact(form.parts)[:3000],
                    "links": [
                        {
                            "url": link.url,
                            "text": link.text,
                            "visible": link.visible_in_static_html,
                        }
                        for link in self.parser.links
                        if link.form_index == form.index
                    ],
                }
                for form in self.parser.forms
            ],
            "links": [
                {
                    "url": link.url,
                    "text": link.text,
                    "visible": link.visible_in_static_html,
                }
                for link in self.parser.links
            ],
            "consentControls": consent_controls,
        }

    def locator(self, selector):
        raise BrowserError("Static mode: form visual evidence unavailable")

    def screenshot(self):
        raise BrowserError("Static mode: visual evidence and dynamic behavior unavailable")

    def close(self):
        pass


class StaticBrowser:
    def new_context(self, **kwargs):
        return self

    def new_page(self):
        return StaticPage()

    def close(self):
        pass


@contextmanager
def static_playwright():
    yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: StaticBrowser()))

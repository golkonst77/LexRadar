"""Explicit static rendering adapter: no browser, scripts or visual evidence."""

import html
import re
from contextlib import contextmanager
from html.parser import HTMLParser
from types import SimpleNamespace

from playwright.sync_api import Error as BrowserError

from ..primary.markup import SavedMarkup, compact
from .extract import legal_link_kind


class _FormFragments(HTMLParser):
    """Locate complete standalone forms despite unrelated malformed page wrappers."""

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.excluded = []
        self.start = None
        self.depth = 0
        self.nested = False
        self.fragments = []
        self.links = []
        self.anchor = None
        self.footer_depth = 0

    def handle_starttag(self, tag, attrs):
        # An explicit body closes an omitted head end tag; never leave raw-text exclusions.
        if tag == "body" and self.excluded == ["head"]:
            self.excluded.clear()
        if tag in {"head", "script", "style", "template", "noscript"}:
            self.excluded.append(tag)
        if self.excluded:
            return
        if tag == "footer":
            self.footer_depth += 1
        if tag == "a" and len(self.links) < 200:
            values = dict(attrs)
            self.anchor = [values.get("href", ""), [], self.footer_depth > 0]
        if len(self.fragments) >= 30:
            return
        if tag == "form":
            if self.depth == 0:
                line, column = self.getpos()
                self.start = sum(len(x) for x in self.source.splitlines(True)[: line - 1]) + column
                self.nested = False
            else:
                self.nested = True
            self.depth += 1

    def handle_endtag(self, tag):
        if self.excluded:
            if tag == self.excluded[-1]:
                self.excluded.pop()
            return
        if tag == "a" and self.anchor:
            href, parts, in_footer = self.anchor
            text = compact(parts)
            if (
                href
                and not href.strip().lower().startswith(("#", "javascript:", "data:"))
                and legal_link_kind(href, text)
            ):
                self.links.append(
                    dict(
                        url=href,
                        text=text,
                        visible=False,
                        footer_policy=bool(in_footer and re.search(r"\bполитик", text, re.I)),
                    )
                )
            self.anchor = None
        if tag == "footer" and self.footer_depth:
            self.footer_depth -= 1
        if tag == "form" and self.depth:
            self.depth -= 1
            if self.depth == 0 and not self.nested:
                line, column = self.getpos()
                end = sum(len(x) for x in self.source.splitlines(True)[: line - 1]) + column
                end = self.source.find(">", end) + 1
                self.fragments.append(self.source[self.start : end])

    def handle_data(self, data):
        if not self.excluded and self.anchor:
            self.anchor[1].append(data)


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
        self.form_parsers = [self.parser]
        # shortcut: recover only when no forms survived; extend after confirmed partial-loss cases.
        fragments = _FormFragments(self.raw_html)
        fragments.feed(self.raw_html)
        fragments.close()
        from ..primary.markup import static_reference

        self.recovered_policy_links = [
            dict(link, url=static_reference(self.url, link["url"])) for link in fragments.links
        ]
        if not self.parser.forms:
            for fragment in fragments.fragments:
                parser = SavedMarkup(self.url, recover=True)
                parser.feed(fragment)
                parser.close()
                # Recover only a complete form; never bless a malformed or partial fragment.
                if (
                    len(parser.forms) == 1
                    and not parser.quarantined
                    and parser.forms[0].end_exclusive
                ):
                    self.form_parsers.append(parser)

    def title(self):
        match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", self.raw_html, re.I | re.S)
        return compact([html.unescape(re.sub(r"<[^>]*>", " ", match[1]))]) if match else ""

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
                        for link in parser.links
                        if link.form_index == form.index
                    ],
                }
                for parser in self.form_parsers
                for form in parser.forms
            ],
            "links": [
                {
                    "url": link.url,
                    "text": link.text,
                    "visible": link.visible_in_static_html,
                }
                for link in self.parser.links
            ]
            + self.recovered_policy_links,
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

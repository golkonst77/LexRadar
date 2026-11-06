"""Chromium compatibility retry inside Fetcher's existing crawl/byte budgets."""

import base64
import os
import signal
import threading
import time
from contextlib import nullcontext
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as BrowserError, sync_playwright

from .network import AddressPolicy, CollectionError, Response, canonical_url, origin
from .robots import USER_AGENT


def browser_get(
    fetcher, url, allowed_origin, *, before_request=None, on_event=None,
    allow_redirects=True, allow_cross_origin=False, max_file_bytes=None, max_redirects=None,
):
    limits = fetcher.limits
    file_limit = min(limits.max_file_bytes, max_file_bytes or limits.max_file_bytes)
    max_redirects = limits.max_redirects if max_redirects is None else max_redirects
    # Keep the pinned transport's egress policy; Chromium must not bypass a proxy.
    if any(os.environ.get(k) for k in (
        "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"
    )):
        raise CollectionError("Browser transport requires direct egress; proxy configured")
    current = canonical_url(url)
    if origin(current) != allowed_origin:
        raise CollectionError("Cross-origin browser navigation blocked")
    deadline = time.monotonic() + limits.timeout_seconds

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise CollectionError("Request timeout")
        return value

    host, port = urlsplit(current).hostname, origin(current)[2]
    # Reuse public-address validation and pin Chromium's DNS for this origin.
    if type(fetcher.policy) is not AddressPolicy:
        raise CollectionError("Browser transport requires the public address policy")
    ip = fetcher.policy.resolve(host, port, timeout=remaining())
    ip = f"[{ip}]" if ":" in ip else ip
    try:
        driver = (nullcontext(fetcher.browser_playwright)
                  if fetcher.browser_playwright is not None else sync_playwright())
        with driver as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("LEXRADAR_CHROMIUM_PATH"),
                args=[f"--host-resolver-rules=MAP {host} {ip}", "--no-proxy-server", "--disable-quic"],
                timeout=remaining() * 1000,
            )
            try:
                browser_cdp = browser.new_browser_cdp_session()
                pid = next(p["id"] for p in browser_cdp.send("SystemInfo.getProcessInfo")["processInfo"]
                           if p["type"] == "browser")
                context = browser.new_context(
                    java_script_enabled=False, service_workers="block", accept_downloads=False,
                )
                page = context.new_page()
                cdp = context.new_cdp_session(page)
                cdp.send("Network.enable")
                cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
                agent = cdp.send("Browser.getVersion")["userAgent"]
                cdp.send("Network.setUserAgentOverride", {"userAgent": f"{agent} {USER_AGENT}"})
                captured, errors = [], []

                def paused(event):
                    request_id = event["requestId"]
                    try:
                        request = event["request"]
                        if (event["resourceType"] != "Document" or request["method"] != "GET"
                                or canonical_url(request["url"]) != current):
                            cdp.send("Fetch.failRequest", {"requestId": request_id,
                                                          "errorReason": "BlockedByClient"})
                            return
                        if "responseStatusCode" not in event:
                            if on_event:
                                on_event("request_sent", current)
                            cdp.send("Fetch.continueRequest", {"requestId": request_id})
                            return
                        status = event["responseStatusCode"]
                        if on_event:
                            on_event("http_response", current, status)
                        headers = {h["name"].lower(): h["value"]
                                   for h in event.get("responseHeaders", [])}
                        body = bytearray()
                        if 200 <= status < 300:
                            stream = cdp.send("Fetch.takeResponseBodyAsStream",
                                              {"requestId": request_id})["stream"]
                            try:
                                while True:
                                    remaining()
                                    size = min(65536, file_limit + 1 - len(body),
                                               limits.max_total_bytes + 1 - fetcher.received)
                                    if size <= 0:
                                        raise CollectionError("Download byte limit exceeded")
                                    chunk = cdp.send("IO.read", {"handle": stream, "size": size})
                                    data = (base64.b64decode(chunk["data"])
                                            if chunk.get("base64Encoded")
                                            else chunk["data"].encode())
                                    body.extend(data)
                                    fetcher.received += len(data)
                                    if len(body) > file_limit:
                                        raise CollectionError("File size limit exceeded")
                                    if fetcher.received > limits.max_total_bytes:
                                        raise CollectionError("Total download limit exceeded")
                                    if chunk.get("eof"):
                                        break
                            finally:
                                cdp.send("IO.close", {"handle": stream})
                        captured.append((Response(current, status,
                                                   headers.get("content-type", ""), bytes(body)),
                                         headers.get("location")))
                    except (CollectionError, BrowserError, ValueError) as exc:
                        errors.append(str(exc))
                    finally:
                        if "responseStatusCode" in event or errors:
                            cdp.send("Fetch.failRequest", {"requestId": request_id,
                                                          "errorReason": "Aborted"})

                cdp.on("Fetch.requestPaused", paused)
                cdp.send("Fetch.enable", {"patterns": [
                    {"urlPattern": "*", "requestStage": "Request"},
                    {"urlPattern": "*", "requestStage": "Response"},
                ]})
                # Browser retry stays on the pinned origin, even if HTTP allowed other origins.
                for step in range(max_redirects + 1):
                    if origin(current) != allowed_origin:
                        raise CollectionError("Cross-origin browser navigation blocked")
                    if fetcher.received >= limits.max_total_bytes:
                        raise CollectionError("Total download limit exceeded")
                    if before_request:
                        began = time.monotonic()
                        crawl_remaining = before_request(current)
                        deadline += time.monotonic() - began
                        if crawl_remaining is not None:
                            deadline = min(deadline, time.monotonic() + crawl_remaining)
                    context.clear_cookies()
                    captured.clear()
                    # IO.read has no Playwright timeout; kill our browser if the stream stalls.
                    watchdog = threading.Timer(remaining(), os.kill, args=(pid, signal.SIGTERM))
                    watchdog.daemon = True
                    watchdog.start()
                    try:
                        page.goto(current, wait_until="commit", timeout=remaining() * 1000)
                    except BrowserError:
                        if not captured and not errors:
                            raise
                    finally:
                        watchdog.cancel()
                    if errors:
                        raise CollectionError(errors[0])
                    if not captured:
                        raise CollectionError("Browser returned no response")
                    response, location = captured[0]
                    if response.status not in {301, 302, 303, 307, 308}:
                        return response
                    if not allow_redirects or not location or step == max_redirects:
                        raise CollectionError("Browser redirect refused or limit exceeded")
                    current = canonical_url(urljoin(current, location))
                    if on_event:
                        on_event("redirect_target", current)
            finally:
                browser.close()
    except BrowserError as exc:
        raise CollectionError(f"Browser fetch failed: {str(exc)[:200]}") from exc

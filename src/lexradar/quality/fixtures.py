"""Socket-free synthetic transport and replay-only provider; no production SSRF bypass."""

import copy
import io
import json
from urllib.parse import urlsplit

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ..auditors.models import Usage
from ..auditors.provider import ProviderError, ProviderReply
from ..collector.network import CollectionError, Response, origin


class FixtureFetcher:
    def __init__(self, site):
        self.site = site
        self.calls = []

    def get(self, url, allowed_origin):
        if origin(url) != origin("https://benchmark.example/") or allowed_origin != origin(url):
            raise CollectionError("Synthetic transport accepts fixture origin only")
        path = urlsplit(url).path
        self.calls.append(url)
        item = self.site["routes"].get(path)
        if item is None:
            raise CollectionError("Unknown synthetic route")
        if item.get("error"):
            raise CollectionError("Synthetic unavailable page")
        if "pdf_pages" in item:
            content, mime = pdf_bytes(item["pdf_pages"]), "application/pdf"
        else:
            content, mime = item["html"].encode(), "text/html; charset=utf-8"
        return Response(url, item.get("status", 200), mime, content)


def pdf_bytes(pages):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=300, height=300)
        if text:
            if any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ .-" for c in text
            ):
                raise ValueError("Synthetic PDF fixtures accept plain ASCII text only")
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class ReplayProvider:
    """Only returns saved responses; never calls a network provider or reads labels."""

    def __init__(self, responses):
        self.responses = copy.deepcopy(responses)
        self.calls = []

    def complete(self, settings, messages):
        self.calls.append(copy.deepcopy(messages))
        packet = json.loads(messages[1]["content"])
        stage = packet.get("stage") or ("A" if settings.model.endswith("/a") else "B")
        item = self.responses[stage]
        if isinstance(item, dict) and "provider_error" in item:
            raise ProviderError(item["provider_error"], retryable=False)
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return ProviderReply(content, Usage(cost_usd=0))

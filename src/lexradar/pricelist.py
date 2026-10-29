"""Deterministic service-price observations; 30 is a commercial threshold, not law."""

import re
from html.parser import HTMLParser
from urllib.parse import unquote

NUMBER = r"(?:\d{1,3}(?:[ \u00a0\u202f.]\d{3})+|\d+)(?:[.,]\d{1,2})?"
AMOUNT = rf"(?:от\s+)?{NUMBER}(?:\s*(?:[-–—]|до)\s*{NUMBER})?"
CURRENCY = r"(?:₽|руб(?:\.|лей|ля|ль)?|р\.)(?!\w)"
PRICE = re.compile(
    rf"(?<![\d.,])(?:(?:от\s+)?{NUMBER}\s*{CURRENCY}\s*(?:до|[-–—])\s*{NUMBER}\s*{CURRENCY}|{AMOUNT}\s*{CURRENCY})",
    re.I,
)
BARE_PRICE = re.compile(rf"^\s*{AMOUNT}\s*$", re.I)
PRICE_HINT = re.compile(
    r"\b(?:цен[аыуе]|ценам|ценах|стоимост[ьи]|прайс(?:-лист)?|прейскурант[а-яё]*|тариф[а-яё]*)\b|"
    r"(?:^|[/_.-])(?:prices?|pricelist|prajs|prays|pra[iy]s|ceny|tseny|czeny|stoimost|tariffs?)(?:[/_.-]|$)",
    re.I,
)
SERVICE = re.compile(
    r"при[её]м|консультац|диагност|лечени|удалени|анализ|\bузи\b|\bмрт\b|\bкт\b|рентген|"
    r"терап|операци|имплант|протез|отбелив|пломб|реабилитац|осмотр|коррекц|эндоскоп|массаж|"
    r"исследован|профилак|пункц|чистк|гигиен|анестези|инъекц|экстракц|сканирован",
    re.I,
)
NON_SERVICE = re.compile(
    r"^(?:итого|всего|скидк|экономи|рассрочк|ежемесячн|в\s+месяц|кредит|стоимость|цена|руб)", re.I
)


def price_hint(value):
    return bool(PRICE_HINT.search(unquote(value)))


def catalog_heading(value):
    value = compact(value)
    return bool(
        re.fullmatch(
            r"(?:наши\s+)?(?:прейскурант|прайс(?:-лист)?|цены(?:\s+на\s+[\wё-]+(?:\s+[\wё-]+){0,3})?|"
            r"стоимость\s+(?:услуг|лечения|при[её]ма))",
            value.rstrip(":.!?"),
            re.I,
        )
    )


def compact(value):
    return re.sub(r"\s+", " ", value).strip()


def service_key(name):
    name = re.sub(r"^\d+[.)]\s*", "", name)
    name = re.sub(r"^[ABАВ]\d{2}(?:\.\d+)+\s*", "", name, flags=re.I)
    return compact(re.sub(r"[^\w\s]", " ", name.casefold().replace("ё", "е")))


def valid_name(name):
    return (
        3 <= len(name) <= 250
        and len(re.findall(r"[а-яёa-z]", name, re.I)) >= 3
        and not NON_SERVICE.search(name)
    )


class PriceHTML(HTMLParser):
    """Count closed table rows or atomic single-price blocks, never currency occurrences."""

    EXCLUDED = {"head", "script", "style", "template", "noscript", "svg"}
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.excluded = []
        self.positions = []
        self.links = []
        self.rows = []
        self.ambiguous = []
        self.price_sections = False
        self.table_headers = []
        self.heading = None
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.EXCLUDED:
            self.excluded.append(tag)
        if self.excluded:
            return
        if tag == "a" and self.stack and self.stack[-1]["tag"] == "a":
            self.handle_endtag("a")
        if tag == "table":
            self.table_headers.append(None)
        if tag not in self.VOID:
            self.stack.append(
                dict(tag=tag, attrs=attrs, parts=[], cells=[], nested_priced=False, headings=[])
            )

    def handle_startendtag(self, tag, attrs):
        if tag not in self.VOID:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.excluded:
            return
        self.text_parts.append(data)
        for node in self.stack:
            node["parts"].append(data)

    def handle_endtag(self, tag):
        if self.excluded:
            if tag == self.excluded[-1]:
                self.excluded.pop()
            return
        index = next(
            (n for n in range(len(self.stack) - 1, -1, -1) if self.stack[n]["tag"] == tag), None
        )
        if index is None:
            return
        node = self.stack[index]
        balanced = index == len(self.stack) - 1
        self.stack = self.stack[:index]
        text = compact(" ".join(node["parts"]))
        if tag == "a" and node["attrs"].get("href"):
            self.links.append(dict(url=node["attrs"]["href"], text=text))
        if tag in {"h1", "h2", "h3", "h4"}:
            for parent in self.stack:
                parent["headings"].append(text)
        if tag in {"h1", "h2", "h3", "caption"} and catalog_heading(text):
            self.price_sections = True
        if tag in {"td", "th"} and balanced:
            row = next((n for n in reversed(self.stack) if n["tag"] == "tr"), None)
            if row is not None:
                row["cells"].append(text)
        if tag == "tr" and balanced and node["cells"]:
            cells = node["cells"]
            price_columns = [
                i for i, c in enumerate(cells) if price_hint(c) and not PRICE.search(c)
            ]
            names = [
                i for i, c in enumerate(cells) if re.search(r"наименован|названи|услуг", c, re.I)
            ]
            if price_columns and names:
                if self.table_headers:
                    self.table_headers[-1] = (names[0], price_columns)
                self.price_sections = True
            else:
                header = self.table_headers[-1] if self.table_headers else None
                amounts = [i for i, c in enumerate(cells) if PRICE.search(c)]
                if header:
                    amounts += [
                        i for i in header[1] if i < len(cells) and BARE_PRICE.fullmatch(cells[i])
                    ]
                if amounts:
                    name_index = (
                        header[0]
                        if header
                        else next(
                            (
                                i
                                for i, c in enumerate(cells)
                                if i not in amounts
                                and valid_name(c)
                                and SERVICE.search(c)
                                and not BARE_PRICE.fullmatch(c)
                            ),
                            None,
                        )
                    )
                    if name_index is not None and name_index < len(cells):
                        name = cells[name_index]
                        if valid_name(name):
                            self.positions.append(
                                dict(
                                    name=name,
                                    price=" / ".join(cells[i] for i in sorted(set(amounts))),
                                    quote=text,
                                    kind="html_table",
                                )
                            )
                            for parent in self.stack:
                                parent["nested_priced"] = True
            self.rows.append(cells)
        elif (
            tag in {"p", "li", "div", "article", "span", "a"}
            and balanced
            and not node["nested_priced"]
        ):
            matches = list(PRICE.finditer(text))
            if len(matches) == 1:
                m = matches[0]
                name = (
                    node["headings"][0]
                    if len(node["headings"]) == 1
                    else text[: m.start()].strip(" :—–-")
                )
                # shortcut: atomic cards must name a medical service; complex widgets stay UNKNOWN.
                if (
                    valid_name(name)
                    and SERVICE.search(name)
                    and len(text[m.end() :].strip()) < 30
                    and not re.search(r"сэконом|скидк|рассроч|кредит|штраф|неустойк", text, re.I)
                ):
                    self.positions.append(
                        dict(name=name, price=m[0], quote=text, kind="html_atomic")
                    )
                    for parent in self.stack:
                        parent["nested_priced"] = True
            elif len(matches) > 1:
                # Multiple prices in one unresolved block are not multiple proven services.
                self.ambiguous.append(text[:350])
        if tag == "table" and self.table_headers:
            self.table_headers.pop()


def html_positions(body):
    parser = PriceHTML()
    parser.feed(body.decode("utf-8"))
    parser.close()
    return parser


def text_positions(text):
    """Only explicit currency on the same line proves a PDF service-price association."""
    positions = []
    ambiguous = []
    for line in text.splitlines():
        line = compact(line)
        matches = list(PRICE.finditer(line))
        if len(matches) == 1:
            match = matches[0]
            name = re.sub(r"^\d+[.)]\s*", "", line[: match.start()].strip(" :—–-"))
            if valid_name(name) and SERVICE.search(name) and len(line[match.end() :].strip()) < 20:
                positions.append(dict(name=name, price=match[0], quote=line, kind="document_line"))
            else:
                ambiguous.append(line[:350])
        elif matches:
            ambiguous.append(line[:350])
    return positions, ambiguous


def assess_pricelist(
    positions, *, coverage_complete, blockers=(), medical=True, count_confident=True
):
    unique = {}
    for position in positions:
        key = service_key(position["name"])
        if key and key not in unique:
            unique[key] = position
    count = len(unique)
    status = (
        "UNKNOWN"
        if not medical
        else "PASS"
        if count >= 30
        else "REVIEW"
        if coverage_complete and count_confident and not blockers
        else "UNKNOWN"
    )
    return dict(
        status=status,
        count=count,
        positions=list(unique.values()),
        coverage_complete=coverage_complete,
        count_confident=count_confident,
        blockers=list(blockers),
        medical=medical,
        threshold=30,
        threshold_is_legal_requirement=False,
        legal_violation_established=False,
    )


def review_supported(result):
    """An incomplete price inventory can never open the proposal gate."""
    return bool(
        result
        and result.get("status") == "REVIEW"
        and result.get("medical") is True
        and result.get("coverage_complete") is True
        and result.get("count_confident") is True
        and not result.get("blockers")
        and type(result.get("count")) is int
        and 0 <= result["count"] < 30
        and result["count"] == len({service_key(p["name"]) for p in result.get("positions", [])})
        and result.get("sources")
        and all(s.get("complete") is True for s in result["sources"])
    )


def proposal_block(result):
    if not review_supported(result):
        raise ValueError("Incomplete or unsupported price review")
    return dict(
        title="ПЕРЕЧЕНЬ МЕДИЦИНСКИХ УСЛУГ И ЦЕН",
        paragraphs=[
            f"В обследованных сохранённых материалах подтверждено {result['count']} отдельных "
            "услуг с явно указанными ценами. Предлагаем проверить и при необходимости расширить "
            "публикуемый перечень медицинских услуг и цен. Ориентир 30 позиций используется как "
            "внутренний коммерческий критерий, а не законодательный норматив. "
            "Вывод относится к обследованным материалам, а не к отсутствию цен на всём сайте."
        ],
    )

"""Parse a Google results page (the raw HTML chromint-mcp's `fetch` returns
with `format="html"`) into plain data.

Google's class names are generated and change often, so the parser leans on
what changes less: ARIA roles and levels (`role="heading"`), `data-*`
attributes (`data-rpos`, `data-q`, `data-attrid`), element ids (`rso`,
`botstuff`, `rhs`) and the visible heading of each block ("People also ask",
"Things to know", ...). Class names are used only as a fallback.

Two things about the HTML Google sends a browser-less client in 2026, both
seen on every page fetched for this example:

- Parts of the page arrive as deferred HTML: an empty `<div id="sdh_...">`
  in the markup and, further down, a script that fills it
  (`jsl.dh(this.id, '<html>')`). The AI Overview's later paragraphs, its
  tables and most of its list items are only there. `load()` puts them back
  before anything is parsed.
- Result links point at `/goto?url=<opaque token>`, not at the site. The
  visible breadcrumb (`https://site.com › blog › post`) gives the host and
  usually the path; the AI Overview's sources carry real URLs in its data
  comments. The exact URL of a web result needs the `/goto` link followed
  (see `search.py --resolve`).

`parse(html_text)` returns a dict; nothing here does network I/O.
"""

from __future__ import annotations

import html as htmllib
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import html

GOOGLE = "https://www.google.com"

_SPACE = re.compile(r"\s+")
#: Deferred HTML: `{id:'sdh_x'},function(){jsl.dh(this.id,'<escaped html>');}`
_DEFERRED = re.compile(r"\{id:'([^']+)'\},function\(\)\{jsl\.dh\(this\.id,'((?:[^'\\]|\\.)*)'\);\}")
_JS_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.S)
#: "Feb 27, 2026 — snippet" / "3 days ago — snippet"
_AGO = r"\d+ (?:minutes?|hours?|days?|weeks?|months?|years?) ago"
_DATED = re.compile(rf"^(?P<date>[A-Z][a-z]{{2}} \d{{1,2}}, \d{{4}}|{_AGO}) — ")
_DURATION = re.compile(r"^\d{1,2}(?::\d{2}){1,2}$")
_DATE = re.compile(rf"^[A-Z][a-z]{{2}} \d{{1,2}}, \d{{4}}$|^{_AGO}$")
_URL_IN_DATA = re.compile(r'"(https?://[^"\s]+)"')


def text(node: Any) -> str:
    """The node's visible text, whitespace collapsed."""
    return _SPACE.sub(
        " ", node.text_content() if hasattr(node, "text_content") else str(node)
    ).strip()


def _unescape_js(literal: str) -> str:
    def one(match: re.Match[str]) -> str:
        escape = match.group(1)
        if escape[0] in "xu" and len(escape) > 1:
            return chr(int(escape[1:], 16))
        return {"n": "\n", "t": "\t", "r": ""}.get(escape, escape)

    return _JS_ESCAPE.sub(one, literal)


def load(html_text: str) -> tuple[Any, dict[str, int]]:
    """The page as an lxml tree with Google's deferred HTML put back, and how
    many deferred fragments were found and placed."""
    # Only `sdh_` slots hold page content (others are CSS and image data).
    fragments = {
        key: _unescape_js(body)
        for key, body in _DEFERRED.findall(html_text)
        if key.startswith("sdh_")
    }
    found = len(fragments)
    doc = html.fromstring(html_text)
    placed = 0
    for _ in range(4):  # fragments can hold more placeholders
        progress = 0
        for slot in doc.xpath("//*[starts-with(@id,'sdh_')]"):
            fragment = fragments.pop(slot.get("id"), None)
            if fragment is None:
                continue
            wrapper = html.fragment_fromstring(fragment, create_parent="div")
            slot.text = (slot.text or "") + (wrapper.text or "")
            for child in list(wrapper):
                slot.append(child)
            progress += 1
        placed += progress
        if not progress:
            break
    for junk in doc.xpath("//script|//style|//noscript|//svg"):
        junk.drop_tree()
    return doc, {"deferred_found": found, "deferred_placed": placed}


def _absolute(href: str | None) -> str | None:
    return urljoin(GOOGLE, href) if href else None


def url_from_breadcrumb(cite: str) -> tuple[str | None, bool]:
    """`https://site.com › blog › post` -> (`https://site.com/blog/post`,
    complete). A guess: breadcrumbs often are the path, but not always
    (some sites show a category trail, and Google drops trailing slashes and
    shortens long ones with `...`: then only the host and the start of the
    path are known, complete = False)."""
    parts = [p.strip() for p in cite.split("›")]
    if not parts or not parts[0].startswith("http"):
        return None, False
    origin = parts[0].rstrip("/")
    truncated = parts[-1].endswith("...") or parts[-1].endswith("…")
    parts[-1] = parts[-1].rstrip(".…").strip()
    segments = [p for p in parts[1:] if p]
    if any(" " in s or re.fullmatch(r"[A-Z][a-z]+", s) for s in segments):
        # A category trail ("› Blog › Insights", "› DATA SOURCES"), not a
        # path: only the host is known.
        return origin, False
    return origin + ("/" + "/".join(segments) if segments else ""), not truncated


def _block_of(node: Any) -> Any:
    """The top-level results block (`div.MjjYud` or `data-rpos`) around node."""
    for xpath in (
        "ancestor::div[@data-rpos][1]",
        "ancestor::div[contains(concat(' ',@class,' '),' MjjYud ')][1]",
    ):
        found = node.xpath(xpath)
        if found:
            return found[0]
    return node.getparent()


def _heading_block(doc: Any, *titles: str) -> list[Any]:
    """Blocks whose level-2 heading reads one of `titles`."""
    blocks = []
    for title in titles:
        for heading in doc.xpath(
            "//*[@role='heading' and @aria-level='2'][normalize-space(.)=$t]", t=title
        ):
            block = _block_of(heading)
            if block is not None and block not in blocks:
                blocks.append(block)
    return blocks


def _cards(block: Any) -> list[Any]:
    """The cards of a carousel-like block: around each level-3 heading, the
    largest ancestor that holds no other card's heading."""
    headings = block.xpath(".//*[@role='heading' and @aria-level='3']")
    cards = []
    for heading in headings:
        card = heading
        while True:
            parent = card.getparent()
            if parent is None or parent is block:
                break
            if len(parent.xpath(".//*[@role='heading' and @aria-level='3']")) > 1:
                break
            card = parent
        cards.append((heading, card))
    return cards


def _texts(node: Any) -> list[str]:
    return [t for t in (_SPACE.sub(" ", s).strip() for s in node.xpath(".//text()")) if t]


# --- organic results --------------------------------------------------------


def organic(doc: Any) -> list[dict[str, Any]]:
    results = []
    for link in doc.xpath("//*[@id='rso' or @id='botstuff']//a[.//h3][@href]"):
        block = _block_of(link)
        title = text(link.xpath(".//h3")[0])
        container = link.xpath("ancestor::div[@data-snc or @data-hveid][1]")
        scope = block if block is not None else (container[0] if container else link)
        cite_nodes = scope.xpath(".//cite")
        cite = text(cite_nodes[0]) if cite_nodes else None
        meta = None
        if cite and not cite.startswith("http"):
            # Forum results show "1.2K+ comments · 8 years ago" where the URL goes.
            meta, cite = cite, None
        source = scope.xpath(".//span[contains(@class,'VuuXrf')]")
        snippet_nodes = scope.xpath(
            ".//div[contains(@class,'VwiC3b')] | .//div[@data-sncf='1']//div[span]"
        )
        snippet = text(snippet_nodes[0]) if snippet_nodes else None
        date = None
        if snippet:
            snippet = re.sub(r"\s*Read more$", "", snippet)
            dated = _DATED.match(snippet)
            if dated:
                date, snippet = dated.group("date"), snippet[dated.end() :]
        url, complete = url_from_breadcrumb(cite) if cite else (None, False)
        sitelinks = [
            {"title": text(a), "link": _absolute(a.get("href"))}
            for a in scope.xpath(".//a[@href][not(.//h3)][not(contains(@class,'vzmbzf'))]")
            if text(a) and text(a) not in ("Read more", title) and "/goto" in a.get("href", "")
        ]
        results.append(
            {
                "title": title,
                "source": text(source[0]) if source else None,
                "displayed_url": cite,
                # The site's URL isn't in the page (see the module docstring):
                # `url` is filled by following google_link (search.py --resolve).
                "url": None,
                "breadcrumb_url": url,
                "breadcrumb_truncated": not complete if url else None,
                "google_link": _absolute(link.get("href")),
                "date": date,
                "snippet": snippet,
                "meta": meta,
                "sitelinks": sitelinks,
                "block_position": int(block.get("data-rpos")) if block.get("data-rpos") else None,
            }
        )
    for position, result in enumerate(results, 1):
        result["position"] = position
    return results


# --- AI Overview ------------------------------------------------------------

_BLOCK_TAGS = {"div", "p", "section", "ul", "ol", "li", "h3", "h4"}
#: Boilerplate lines in the overview's answer.
_OVERVIEW_NOISE = {"Use code with caution.", "Show more", "Show all", "Dive deeper in AI Mode"}


def _overview_markdown(node: Any) -> str:
    """The AI Overview's answer as markdown: headings, paragraphs, nested
    lists, tables and code, without buttons, citation chips and the sources
    column."""
    # (kind, indent, text): kind is "p", "h", "li", "table" or "code".
    lines: list[tuple[str, int, str]] = []
    current: list[str] = []
    state = {"kind": "p", "indent": 0}

    def flush() -> None:
        line = _SPACE.sub(" ", "".join(current)).strip()
        if line and line not in _OVERVIEW_NOISE:
            lines.append((state["kind"], state["indent"], line))
        current.clear()
        state.update(kind="p", indent=0)

    def walk(el: Any, depth: int) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if (
            not tag
            or tag in ("button", "img", "svg")
            or el.get("data-ignore-copy") is not None
            or "display: none" in (el.get("style") or "")
            or el.get("aria-hidden") == "true"
        ):
            if tag and el.tail:
                current.append(el.tail)
            return
        if tag in ("table", "pre"):
            flush()
            if tag == "table":
                rows = [[text(c) for c in tr.xpath("./th|./td")] for tr in el.xpath(".//tr")]
                rows = [r for r in rows if any(r)]
                if rows:
                    width = max(len(r) for r in rows)
                    rows = [r + [""] * (width - len(r)) for r in rows]
                    body = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
                    body += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                    lines.append(("table", 0, "\n".join(body)))
            else:
                # The language label sits just before the <pre> (a "bash" div).
                language = ""
                if lines and lines[-1][0] == "p" and re.fullmatch(r"[a-z+#]{1,12}", lines[-1][2]):
                    language = lines.pop()[2]
                code = el.text_content().rstrip("\n")
                lines.append(("code", 0, f"```{language}\n{code}\n```"))
            if el.tail:
                current.append(el.tail)
            return
        heading = el.get("role") == "heading" and el.get("aria-level") == "3"
        # Deferred slots are `display: contents`: inline with their parent.
        inline_slot = "display: contents" in (el.get("style") or "")
        block = (tag in _BLOCK_TAGS or heading) and not inline_slot
        if block:
            flush()
        if heading:
            state["kind"] = "h"
        elif tag == "li":
            state.update(kind="li", indent=max(depth - 1, 0))
        if el.text:
            current.append(el.text)
        for child in el:
            walk(child, depth + (1 if tag in ("ul", "ol") else 0))
        if block:
            flush()
        if el.tail:
            current.append(el.tail)

    walk(node, 0)
    out: list[str] = []
    previous = None
    for kind, indent, line in lines:
        if kind == "h":
            rendered = f"### {line}"
        elif kind == "li":
            rendered = "  " * indent + f"- {line}"
        else:
            rendered = line
        # A list's items and a paragraph continuing an item stay together.
        joiner = "\n" if previous == "li" and kind == "li" else "\n\n"
        out.append((joiner if out else "") + rendered)
        previous = kind
    return "".join(out)


def _overview_sources(section: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """The sources column (title + Google link) and every real URL in the
    overview's data comments (the cited pages; Google keeps them there)."""
    sources = [
        {"title": a.get("aria-label"), "google_link": _absolute(a.get("href"))}
        for a in section.xpath(".//*[@data-container-id='rhs-col']//a[@aria-label][@href]")
    ]
    urls: list[str] = []
    for comment in section.xpath(".//comment()"):
        for url in _URL_IN_DATA.findall(htmllib.unescape(comment.text or "")):
            url = _unescape_js(url)
            host = urlsplit(url).hostname or ""
            if host.endswith(("gstatic.com", "google.com", "googleusercontent.com")):
                continue
            if url.rstrip("/") not in {u.rstrip("/") for u in urls}:
                urls.append(url)
    # A bare origin next to a deeper URL on the same host adds nothing.
    deep = {urlsplit(u).hostname for u in urls if urlsplit(u).path not in ("", "/")}
    cited = [
        u for u in urls if urlsplit(u).path not in ("", "/") or urlsplit(u).hostname not in deep
    ]
    return sources, cited


def ai_overview(doc: Any) -> dict[str, Any] | None:
    heads = doc.xpath("//*[@role='heading' and @aria-level='2'][normalize-space(.)='AI Overview']")
    if not heads:
        return None
    section = heads[0].xpath("ancestor::section[1]") or [_block_of(heads[0])]
    section = section[0]
    answer = section.xpath(".//*[@data-container-id='main-col']")
    body = answer[0] if answer else section
    markdown = _overview_markdown(body)
    sources, cited = _overview_sources(section)
    return {
        "markdown": markdown,
        "headings": [text(h) for h in body.xpath(".//*[@role='heading' and @aria-level='3']")],
        "entities": sorted({text(s) for s in body.xpath(".//strong") if 1 < len(text(s)) < 40}),
        "sources": sources,
        "cited_urls": cited,
        "chars": len(markdown),
    }


# --- the other blocks -------------------------------------------------------


def people_also_ask(doc: Any) -> list[str]:
    """The questions. Their answers aren't in the page: Google loads one
    when it's opened."""
    questions = []
    for node in doc.xpath("//*[@data-q][contains(@class,'related-question-pair') or @data-lk]"):
        if node.get("data-q") not in questions:
            questions.append(node.get("data-q"))
    return questions


PLATFORMS = (
    "Reddit",
    "Quora",
    "Stack Overflow",
    "YouTube",
    "Facebook",
    "Instagram",
    "TikTok",
    "X",
    "LinkedIn",
    "Vimeo",
    "Medium",
)
_COUNT = re.compile(r"^\d[\d.,]*K?\+? (comments?|answers?|replies|posts?)$")
_SEPARATORS = ("·", "•", ".", "")


def _card_item(heading: Any, card: Any) -> dict[str, Any]:
    """A card's title (the first text of its heading) and what its other
    texts look like: platform, channel or community, count, date, duration."""
    head = [t for t in _texts(heading) if t not in _SEPARATORS]
    item: dict[str, Any] = {"title": head[0] if head else text(heading)}
    links = card.xpath(".//a[@href][contains(@href,'/goto')]") or card.xpath("ancestor::a[@href]")
    item["google_link"] = _absolute(links[0].get("href")) if links else None
    rest = head[1:] + [t for t in _texts(card) if t not in _SEPARATORS and t not in head]
    for value in rest:
        if _DURATION.match(value):
            item.setdefault("duration", value)
        elif _DATE.match(value):
            item.setdefault("date", value)
        elif _COUNT.match(value):
            item.setdefault("comments", value)
        elif value.startswith("r/"):
            item.setdefault("community", value)
        elif value in PLATFORMS:
            item.setdefault("platform", value)
        elif "platform" in item and "channel" not in item and len(value) < 60:
            item["channel"] = value
    description = _description(card, heading)
    if description and description != item["title"]:
        item["description"] = description
    return item


def _description(card: Any, heading: Any) -> str | None:
    """The card's longest run of text outside its heading, whole (a
    description split by <em> highlights comes back as one string)."""

    def in_heading(node: Any) -> bool:
        parent = node.getparent()
        return parent is heading or heading in parent.iterancestors()

    nodes = [n for n in card.xpath(".//text()") if not in_heading(n)]
    if not nodes:
        return None
    longest = max(nodes, key=lambda n: len(n.strip()))
    if len(longest.strip()) < 30:
        return None
    element = longest.getparent()
    while element is not None and element is not card:
        parent = element.getparent()
        if parent is None or parent is card or heading in parent.iterdescendants():
            break
        element = parent
    return text(element) if element is not None else None


def discussions(doc: Any) -> dict[str, Any] | None:
    """'What people are saying' / 'Discussions and forums': Google's
    generated summary of the thread (when there is one) and the posts."""
    blocks = _heading_block(doc, "What people are saying", "Discussions and forums")
    if not blocks:
        return None
    out: dict[str, Any] = {"summary": None, "posts": []}
    for block in blocks:
        texts = _texts(block)
        if "Generative AI is experimental." in texts and "Trending posts and discussions" in texts:
            body = texts[
                texts.index("Trending posts and discussions") + 1 : texts.index(
                    "Generative AI is experimental."
                )
            ]
            body = [t for t in dict.fromkeys(body) if t not in _SEPARATORS]
            out["summary"] = " ".join(body) or None
        for heading, card in _cards(block):
            post = _card_item(heading, card)
            # A post can carry its title twice (link and expanded view): keep
            # one, with the link.
            same = next((p for p in out["posts"] if p["title"] == post["title"]), None)
            if same is None:
                out["posts"].append(post)
            elif not same.get("google_link"):
                same.update(post)
    return out


def videos(doc: Any) -> list[dict[str, Any]]:
    """'Videos' and 'Short videos': title, platform, channel, date, duration."""
    out = []
    for block in _heading_block(doc, "Videos", "Short videos"):
        kind = text(block.xpath(".//*[@role='heading' and @aria-level='2']")[0])
        for heading, card in _cards(block):
            out.append({"kind": kind, **_card_item(heading, card)})
    return out


def things_to_know(doc: Any) -> list[dict[str, str]]:
    out = []
    for node in doc.xpath("//*[starts-with(@data-attrid,'lab/title/')]"):
        topic = node.get("data-attrid").removeprefix("lab/title/")
        question = text(node).removeprefix(topic).lstrip(". ").strip()
        out.append({"topic": topic, "question": question})
    return out


def related_searches(doc: Any) -> list[dict[str, str]]:
    out = []
    for block in _heading_block(doc, "People also search for", "Related searches"):
        if block.xpath("ancestor-or-self::*[@id='rhs']"):
            continue  # the knowledge panel's "People also search for" (entities)
        for a in block.xpath(".//a[@href][contains(@href,'/search')]"):
            query = parse_qs(urlsplit(a.get("href")).query).get("q", [text(a)])[0]
            if text(a):
                out.append({"query": query, "link": _absolute(a.get("href"))})
    return out


def knowledge_panel(doc: Any) -> dict[str, Any] | None:
    """The right-hand panel for an entity: title, subtitle, description and
    the `label: value` facts Google marks with `data-attrid`."""
    titles = doc.xpath("//*[@data-attrid='title']")
    if not titles:
        return None
    panel: dict[str, Any] = {"title": text(titles[0])}
    subtitle = doc.xpath("//*[@data-attrid='subtitle']")
    if subtitle:
        panel["subtitle"] = text(subtitle[0])
    description = doc.xpath("//*[@data-attrid='description']")
    if description:
        spans = description[0].xpath(".//span[not(ancestor::a)]")
        body = max((text(s) for s in spans), key=len, default=text(description[0]))
        panel["description"] = body.removeprefix("Description").strip()
        source = description[0].xpath(".//a[@href]")
        panel["description_source"] = text(source[0]) if source else None
    facts = {}
    for node in doc.xpath(
        "//*[starts-with(@data-attrid,'kc:/') or starts-with(@data-attrid,'ss:/')]"
    ):
        value = text(node)
        label, sep, rest = value.partition(":")
        if sep and 0 < len(label) < 40 and rest.strip():
            facts[label.strip()] = rest.strip()
    panel["facts"] = facts
    rating = re.match(r"^(\d\.\d)\(([\d.,]+[KM]?)\)", panel.get("subtitle") or "")
    if rating:
        panel["rating"], panel["reviews"] = float(rating.group(1)), rating.group(2)
    for node in doc.xpath("//*[@id='rhs']//text()[contains(., 'Google reviews')]"):
        if re.match(r"^[\d,]+ Google reviews$", node.strip()):
            panel["reviews"] = int(node.strip().split()[0].replace(",", ""))
    profiles = doc.xpath("//*[@data-attrid='kc:/common/topic:social media presence']//a[@href]")
    panel["profiles"] = [
        {"name": text(a), "google_link": _absolute(a.get("href"))} for a in profiles if text(a)
    ]
    entities = []
    for block in _heading_block(doc, "People also search for"):
        if block.xpath("ancestor-or-self::*[@id='rhs']"):
            entities += [text(h) for h, _ in _cards(block)]
    if not entities:
        rhs = doc.xpath("//*[@id='rhs']")
        if rhs:
            heads = rhs[0].xpath(
                ".//*[@role='heading'][normalize-space(.)='People also search for']"
            )
            if heads:
                scope = heads[0].getparent().getparent()
                entities = [
                    text(h) for h in scope.xpath(".//*[@role='heading' and @aria-level='3']")
                ]
    panel["people_also_search_for"] = [e for e in entities if e != "People also search for"]
    return panel


def ads(doc: Any) -> list[dict[str, Any]]:
    """Text ads above and below the results (`#tads`, `#bottomads`)."""
    out = []
    for where in ("tads", "bottomads"):
        for ad in doc.xpath(f"//*[@id='{where}']//*[@data-text-ad]"):
            heading = ad.xpath(".//*[@role='heading']")
            link = ad.xpath(".//a[@href]")
            out.append(
                {
                    "placement": "top" if where == "tads" else "bottom",
                    "title": text(heading[0]) if heading else None,
                    "displayed_url": text(ad.xpath(".//cite")[0]) if ad.xpath(".//cite") else None,
                    "link": link[0].get("href") if link else None,
                    "text": text(ad),
                }
            )
    return out


def pagination(doc: Any) -> dict[str, Any]:
    nav = doc.xpath("//*[@role='navigation']//table | //table[.//td[normalize-space(.)='Next']]")
    pages, next_link = [], None
    if nav:
        for a in nav[0].xpath(".//a[@href]"):
            label = text(a)
            start = parse_qs(urlsplit(a.get("href")).query).get("start", ["0"])[0]
            if label.isdigit():
                pages.append({"page": int(label), "start": int(start)})
            elif label == "Next":
                next_link = _absolute(a.get("href"))
    nxt = doc.xpath("//a[@id='pnnext'][@href]")
    if nxt and not next_link:
        next_link = _absolute(nxt[0].get("href"))
    return {"has_next": next_link is not None, "next_link": next_link, "pages": pages}


_NOT_FEATURES = {
    "Search Results",
    "Page Navigation",
    "Footer Links",
    "Next",
    "Previous",
    "Complementary Results",
}
#: The surveys Google sometimes appends ("How helpful were the results on
#: Google Search?", "How visually appealing is the design on this page?").
#: Block titles never end in a question mark; survey questions do.
_FEEDBACK = re.compile(r"\?$|helpful|tell us|feedback", re.IGNORECASE)


def features(doc: Any) -> list[str]:
    """Every block heading on the page, in page order (what Google showed)."""
    seen = []
    for heading in doc.xpath("//*[@role='heading' and @aria-level='2'] | //h2"):
        label = text(heading)
        if (
            label
            and label not in seen
            and label not in _NOT_FEATURES
            and not _FEEDBACK.search(label)
        ):
            seen.append(label)
    return seen


def parse(html_text: str) -> dict[str, Any]:
    doc, deferred = load(html_text)
    return {
        "organic": organic(doc),
        "ai_overview": ai_overview(doc),
        "people_also_ask": people_also_ask(doc),
        "discussions": discussions(doc),
        "videos": videos(doc),
        "things_to_know": things_to_know(doc),
        "related_searches": related_searches(doc),
        "knowledge_panel": knowledge_panel(doc),
        "ads": ads(doc),
        "pagination": pagination(doc),
        "features": features(doc),
        "parser": deferred,
    }


# --- the markdown route (for comparison) ------------------------------------

_MD_RESULT = re.compile(
    r"^\[(?P<label>[^\]]+)\]\((?P<link>https://www\.google\.com/goto\?[^)]+)\)$"
)


def parse_markdown(markdown: str) -> dict[str, Any]:
    """What the server's markdown of a results page gives: the organic
    results (title, source, breadcrumb, snippet) and the AI Overview text as
    far as it's in the markup (no deferred parts)."""
    lines = [line.strip() for line in markdown.splitlines()]
    results = []
    for index, line in enumerate(lines):
        match = _MD_RESULT.match(line)
        if not match:
            continue
        # The link text is "Title Source https://host › path"; the lines after
        # it repeat the source and the breadcrumb, then the snippet.
        following = [x for x in lines[index + 1 : index + 8] if x]
        label = match.group("label")
        cite = next((x for x in following[:3] if x.startswith("http")), None)
        if cite is None:
            # A forum entry: "Title", "Quora ·", "30+ answers", "· 5 years ago".
            titled = bool(following) and label.startswith(following[0])
            meta = []
            for x in following[1:] if titled else following:
                if x.startswith("[") or len(meta) == 3:
                    break
                meta.append(x.strip(" ·"))
            results.append(
                {
                    "kind": "discussion",
                    "title": following[0] if titled else label,
                    "meta": " · ".join(m for m in meta if m) or None,
                }
            )
            continue
        snippet = None
        after = following[following.index(cite) + 1 :]
        snippet = after[0] if after and not after[0].startswith("[") else None
        if snippet:
            snippet = re.sub(r"\s*\[Read more\]\([^)]*\)$", "", snippet)
        source = following[0] if following and following[0] != cite else None
        title = label.split(f" {source} ")[0] if source and f" {source} " in label else label
        results.append(
            {
                "kind": "web",
                "title": title,
                "source": source,
                "displayed_url": cite,
                "snippet": snippet,
            }
        )
    overview = None
    if "AI Overview" in lines:
        start = lines.index("AI Overview") + 1
        end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
        overview = "\n".join(x for x in lines[start:end] if x).strip()
    return {"organic": results, "ai_overview": overview}

"""Extract persistent course-navigation links, including external LTI tools."""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class _NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "d2l-navigation-s-link" in classes:
            self.anchor = [attrs.get("href", ""), []]
        elif tag == "d2l-menu-item-link" and attrs.get("text"):
            self.links.append((attrs.get("href", ""), attrs["text"]))

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.links.append((self.anchor[0], "".join(self.anchor[1])))
            self.anchor = None


def navigation_links(markup: str, page_url: str) -> list[tuple[str, str]]:
    """Return (title, absolute URL), deduplicating desktop/mobile menus."""
    parser = _NavigationParser()
    parser.feed(markup)
    links = []
    seen = set()
    for href, title in parser.links:
        title = " ".join(title.split())
        url = urljoin(page_url, href)
        if (not title or not href or href.startswith("#")
                or urlsplit(url).scheme not in ("http", "https") or url in seen):
            continue
        seen.add(url)
        links.append((title, url))
    return links

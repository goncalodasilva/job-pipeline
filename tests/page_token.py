"""Read the launch token a page embeds in ``<body hx-headers=...>`` (spec 2.9 rule 2).

``raw_body_hx_headers`` returns the attribute exactly as written in the HTML, so tests can
check its escaping; ``page_token`` decodes it the way a browser and htmx would.
"""

import html
import json
import re
from html.parser import HTMLParser

TOKEN_HEADER_NAME = "X-JP-Token"  # noqa: S105 - a header name, not a secret
_BODY_TAG = re.compile(r"<body\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HX_HEADERS_ATTR = re.compile(
    r"""\bhx-headers\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)')""", re.IGNORECASE
)


class _BodyAttrs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.bodies.append(dict(attrs))


def raw_body_hx_headers(page: str) -> str:
    """The quoted value of ``hx-headers`` on the single ``<body>`` tag, still HTML-escaped."""
    bodies = _BODY_TAG.findall(page)
    assert len(bodies) == 1, f"expected one <body> tag, found {len(bodies)}"
    match = _HX_HEADERS_ATTR.search(bodies[0])
    assert match is not None, "the <body> tag has no quoted hx-headers attribute"
    raw = match.group("double") if match.group("double") is not None else match.group("single")
    assert raw is not None
    return raw


def body_hx_headers(page: str) -> dict[str, object]:
    """``hx-headers`` decoded as a browser would: parsed HTML attribute, then JSON."""
    parser = _BodyAttrs()
    parser.feed(page)
    parser.close()
    assert len(parser.bodies) == 1, f"expected one <body>, found {len(parser.bodies)}"
    value = parser.bodies[0].get("hx-headers")
    assert value is not None, "the <body> element has no hx-headers attribute"
    decoded = json.loads(value)
    assert isinstance(decoded, dict), "hx-headers is not a JSON object"
    # The raw attribute must decode the same way (no parser-dependent escaping).
    assert json.loads(html.unescape(raw_body_hx_headers(page))) == decoded
    return decoded


def page_token(page: str) -> str:
    """The launch token the page tells htmx to send as ``X-JP-Token``."""
    token = body_hx_headers(page).get(TOKEN_HEADER_NAME)
    assert isinstance(token, str), f"hx-headers has no string {TOKEN_HEADER_NAME}"
    return token

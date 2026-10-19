"""The fictional job pages the fake fetcher serves in the app (tests and the demo; ADR-054).

Every company, board and host is made up and lives under ``example.test``.
"""

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from app.modules.fetch.providers.fake import FakePage

__all__ = ["demo_pages"]

_HTML: Final = "text/html; charset=utf-8"

_WIDGET_ENGINEER: Final = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Widget Engineer - Northwind Fictional Labs</title></head>
<body>
<nav><a href="/">Lumen Board (fictional)</a></nav>
<main>
<h1>Widget Engineer</h1>
<p>Northwind Fictional Labs builds widgets for imaginary factories. Hybrid, Lisbon.</p>
<h2>Requirements</h2>
<ul><li>Three years of Python</li><li>Experience with SQLite</li></ul>
<p>Salary: 50,000 to 60,000 EUR per year. Full-time.</p>
</main>
</body></html>
"""

_DATA_GARDENER_POSTING: Final = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Data Gardener",
    "hiringOrganization": {"@type": "Organization", "name": "Quill Orchard Fictional"},
    "description": (
        "<p>Quill Orchard Fictional grows tidy datasets. Remote.</p>"
        "<ul><li>Two years of SQL</li><li>Patience with spreadsheets</li></ul>"
    ),
}

_DATA_GARDENER: Final = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Data Gardener - Quill Orchard Fictional</title>
<script type="application/ld+json">{json.dumps(_DATA_GARDENER_POSTING)}</script></head>
<body><div id="app"><p>Loading the fictional board.</p></div></body></html>
"""

_GIZMO_ANALYST: Final = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Gizmo Analyst - Brightpond Fictional</title></head>
<body>
<h1>Gizmo Analyst</h1>
<p>Brightpond Fictional analyses gizmos for a made-up market. On-site, Porto.</p>
<h2>Requirements</h2>
<ul><li>One year of spreadsheet work</li><li>Clear written English</li></ul>
</body></html>
"""

_PAGES: Final = {
    "https://jobs.lumenboard.example.test/postings/widget-engineer-1001": _WIDGET_ENGINEER,
    "https://careers.quillorchard.example.test/jobs/data-gardener": _DATA_GARDENER,
    "https://jobs.brightpond.example.test/openings/gizmo-analyst": _GIZMO_ANALYST,
}


def demo_pages() -> Mapping[str, FakePage]:
    """URL to page for every built-in fictional page."""
    return MappingProxyType(
        {url: FakePage(content_type=_HTML, body=html.encode()) for url, html in _PAGES.items()}
    )

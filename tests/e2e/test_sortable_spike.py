"""Vendored drag spike only; the board's keyboard menu comes in M5.4."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_sortable_drop_reports_source_and_target_list(desktop_page: Page) -> None:
    page = desktop_page
    page.set_content("""<main><h1>Fictional drag fixture</h1>
        <ul id="preparing" aria-label="Preparing"
            style="display:inline-block;width:250px;min-height:150px">
            <li id="example" style="height:60px">Example application</li></ul>
        <ul id="active" aria-label="Active"
            style="display:inline-block;width:250px;min-height:150px">
            <li style="height:60px">Other example</li></ul></main>""")
    vendor = Path(__file__).resolve().parents[2] / "app/static/vendor/Sortable.min.js"
    page.add_script_tag(path=str(vendor))
    page.evaluate("""() => {
        window.moves = [];
        for (const id of ["preparing", "active"]) new Sortable(document.getElementById(id), {
            group: "applications", forceFallback: true, animation: 0,
            onEnd(e) { window.moves.push({from: e.from.id, to: e.to.id, id: e.item.id}); }
        });
    }""")
    source = page.locator("#example").bounding_box()
    target = page.locator("#active").bounding_box()
    assert source and target
    page.mouse.move(source["x"] + 50, source["y"] + 20)
    page.mouse.down()
    page.mouse.move(source["x"] + 55, source["y"] + 25, steps=4)
    page.mouse.move(target["x"] + 100, target["y"] + 90, steps=25)
    # Wait for the fallback drag's actual insertion, not a fixed delay.
    expect(page.locator("#active #example")).to_have_count(1)
    page.mouse.up()
    assert page.evaluate("window.moves") == [{"from": "preparing", "to": "active", "id": "example"}]

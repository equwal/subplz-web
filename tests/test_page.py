"""The first screen: what a visitor sees before choosing a file."""

from __future__ import annotations

import re


def test_the_javascript_switch_is_on_the_first_screen_and_on(client):
    page = client.get("/").text
    switch = re.search(r'<input[^>]*\bid="in-browser"[^>]*>', page)
    assert switch, "the page has no in-browser switch"
    tag = switch.group(0)
    # On when the page opens. autocomplete="off" stops a browser from
    # bringing back an "off" from an earlier visit.
    assert re.search(r"\schecked\b", tag) and 'autocomplete="off"' in tag

    # Above the drop zone, so that it shows before a file is chosen.
    dropzone = page.index('id="dropzone"')
    assert switch.start() < dropzone
    assert "JavaScript" in page[switch.start():dropzone]


def test_the_browser_mode_says_that_it_uses_no_credit(client):
    # The note under the switch put the price of the server after the browser
    # mode, and it read as if the browser mode took a credit. The page writes
    # that note when the account loads.
    app = client.get("/app.js").text
    assert "'Uses no credit, and has no limit." in app
    assert "do the work.' + serverPrice()" not in app

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


def test_the_browser_mode_note_says_only_that_it_uses_no_cloud_credits(client):
    # The note under the switch must carry no other message: not the beta
    # debug-copy disclosure, not the server price, not an offer to turn it off.
    page = client.get("/").text
    app = client.get("/app.js").text
    note = re.search(r'<p[^>]*id="mode-note"[^>]*>(.*?)</p>', page, re.S)
    assert note
    assert note.group(1).strip() == "The JavaScript version does not use any cloud credits."
    assert "'The JavaScript version does not use any cloud credits.'" in app
    for gone in ("has no limit", "Turn this off", "serverCost"):
        assert gone not in app

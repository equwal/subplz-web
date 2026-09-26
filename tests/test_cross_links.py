"""The pages link the other sites and apps of the author, but not SubRead
itself."""

from __future__ import annotations

import pytest

OTHERS = (
    'href="https://booksimulator.com/"',
    'href="https://sbmsync.com/"',
    'href="https://github.com/equwal/subread-overlay/releases/latest"',
    'href="https://recentlywritten.com/projects.html"',
)


@pytest.mark.parametrize("path", ["/", "/subrep.html"])
def test_the_page_links_the_other_sites_and_apps(client, path):
    page = client.get(path).text
    for link in OTHERS:
        assert link in page, (path, link)
    assert 'href="https://subread.space/"' not in page, path

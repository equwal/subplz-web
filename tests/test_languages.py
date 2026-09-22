"""The languages SubRead is checked against must reach the server backend.

The registry decides which sentence splitter subplz gets. A language that is
missing from it is refused before a run starts, so the five checked languages
must be present, and Portuguese must ask for stanza: pysbd does not know it
and raises ValueError on it.
"""

import pytest

from backend import languages

CHECKED = ["en", "pt", "es", "ru", "ja"]


@pytest.mark.parametrize("code", CHECKED)
def test_checked_language_is_supported(code):
    lang = languages.require(code)
    assert lang.code == code
    assert lang.splitter in ("pysbd", "stanza")


def test_portuguese_needs_stanza():
    assert languages.get("pt").needs_nlp_flag is True


@pytest.mark.parametrize("code", ["en", "es", "ru", "ja"])
def test_pysbd_languages_do_not_need_a_model_download(code):
    assert languages.get(code).needs_nlp_flag is False


def test_lookup_ignores_case_and_spacing():
    assert languages.get(" PT ").code == "pt"
    assert languages.get("xx") is None
    with pytest.raises(languages.UnsupportedLanguage):
        languages.require("xx")


def test_the_registry_is_offered_to_the_browser(client):
    codes = {entry["code"] for entry in client.get("/api/languages").json()}
    assert set(CHECKED) <= codes

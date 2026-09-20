"""Generate languages.json from the actually-installed pysbd + stanza.

Run once (or after upgrading either package):
    python -m backend.gen_languages

The registry is what makes "works for all languages" concrete: every language
subplz can segment, tagged with the splitter backend it needs.
"""

import json
from pathlib import Path

OUT = Path(__file__).with_name("languages.json")

# Languages where subplz's default splitter (pysbd) works. Fast, no model download.
# Everything else must run with --nlp so subplz uses stanza instead.
def pysbd_languages() -> set[str]:
    from pysbd.languages import LANGUAGE_CODES

    return set(LANGUAGE_CODES.keys())


def stanza_languages() -> set[str]:
    from stanza.resources.common import list_available_languages

    return set(list_available_languages())


def english_name(code: str) -> str:
    import pycountry

    for attr in ("alpha_2", "alpha_3"):
        try:
            hit = pycountry.languages.get(**{attr: code})
        except (KeyError, LookupError):
            hit = None
        if hit is not None:
            return getattr(hit, "name", code)
    return code


def build() -> dict:
    pysbd = pysbd_languages()
    stanza = stanza_languages()

    entries = []
    for code in sorted(pysbd | stanza):
        entries.append(
            {
                "code": code,
                "name": english_name(code),
                # pysbd wins when available: no model download, much faster start.
                "splitter": "pysbd" if code in pysbd else "stanza",
            }
        )
    return {
        "generated_from": {"pysbd": len(pysbd), "stanza": len(stanza)},
        "languages": entries,
    }


if __name__ == "__main__":
    data = build()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(data["languages"])
    n_pysbd = sum(1 for e in data["languages"] if e["splitter"] == "pysbd")
    print(f"wrote {OUT} - {n} languages ({n_pysbd} pysbd, {n - n_pysbd} stanza)")

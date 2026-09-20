"""Language registry.

subplz defaults to Japanese and splits sentences with pysbd, which only knows 23
languages - pysbd raises ValueError on anything else (Portuguese and Finnish
included). subplz can use stanza instead when passed --nlp, which covers 74 more.

So: resolve the language here, decide the splitter here, and never let a request
reach subplz with a language its splitter cannot handle.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_REGISTRY = Path(__file__).with_name("languages.json")


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    splitter: str  # "pysbd" | "stanza"

    @property
    def needs_nlp_flag(self) -> bool:
        """stanza languages require subplz's --nlp flag (and a one-off model download)."""
        return self.splitter == "stanza"


@lru_cache(maxsize=1)
def _table() -> dict[str, Language]:
    raw = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    return {e["code"]: Language(e["code"], e["name"], e["splitter"]) for e in raw["languages"]}


def all_languages() -> list[Language]:
    # Sort by display name so the dropdown reads naturally.
    return sorted(_table().values(), key=lambda l: l.name.lower())


def get(code: str) -> Language | None:
    return _table().get((code or "").strip().lower())


def is_supported(code: str) -> bool:
    return get(code) is not None


class UnsupportedLanguage(ValueError):
    def __init__(self, code: str):
        super().__init__(
            f"Language {code!r} is not supported. subplz can segment "
            f"{len(_table())} languages; see GET /api/languages for the list."
        )
        self.code = code


def require(code: str) -> Language:
    lang = get(code)
    if lang is None:
        raise UnsupportedLanguage(code)
    return lang

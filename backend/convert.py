"""Accept fb2, mobi and azw3 by turning them into a chaptered epub.

Chapters are the whole point, and it took a measurement to see why. subplz
matches audio to text by scoring the *opening* of each audio chapter against
the opening of each text chapter, and it splits an epub into one text chapter
per spine document but a .txt into exactly one chapter for the entire file.

Measured on Moskva-Petushki (Russian, 4h40m):

    same book, per chapter   69.1
    same book, one flat file 38.6   <- subplz's threshold is 40

So converting to plain text would push a perfectly good book *below* the
threshold and produce the "transcript and text are too different" failure. The
structure has to survive the conversion.

Nothing here parses an ebook format or writes a container by hand:

  azw3 / KF8    -> the `mobi` package unpacks it straight to epub
  mobi (older)  -> `mobi` unpacks to HTML; ebooklib rebuilds the epub
  fb2, fb2.zip  -> it is XML; lxml reads it, ebooklib writes the epub

`mobi` is the only added dependency; lxml, BeautifulSoup and ebooklib already
ship with subplz.
"""

from __future__ import annotations

import html
import shutil
import zipfile
from pathlib import Path

# Accepted and converted on upload. Keep in sync with runner.TEXT_SUFFIXES
# and the frontend accept list.
CONVERTIBLE_SUFFIXES = {".fb2", ".mobi", ".azw", ".azw3", ".prc"}

_MOBI_SUFFIXES = {".mobi", ".azw", ".azw3", ".prc"}

# fb2 bodies named this way hold footnotes, not the story.
_FB2_SKIP_BODIES = {"notes", "comments"}

# Text blocks worth keeping: prose, verse lines, subheadings.
_FB2_BLOCKS = {"p", "v", "subtitle"}


class ConversionError(ValueError):
    pass


def needs_conversion(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(".fb2.zip") or Path(lowered).suffix in CONVERTIBLE_SUFFIXES


def to_readable(src: Path, out_stem: Path) -> Path:
    """Convert `src` into an epub subplz can align against."""
    name = src.name.lower()

    if name.endswith(".epub"):
        return src

    if name.endswith(".fb2.zip"):
        return _fb2_to_epub(_unzip_fb2(src), src, out_stem)

    if name.endswith(".fb2"):
        return _fb2_to_epub(src.read_bytes(), src, out_stem)

    if Path(name).suffix in _MOBI_SUFFIXES:
        return _from_mobi(src, out_stem)

    raise ConversionError(f"Cannot read {src.name}.")


# ---------------------------------------------------------------------------
# fb2
# ---------------------------------------------------------------------------

def _unzip_fb2(src: Path) -> bytes:
    try:
        with zipfile.ZipFile(src) as zf:
            inner = next((n for n in zf.namelist() if n.lower().endswith(".fb2")), None)
            if inner is None:
                raise ConversionError(f"{src.name} contains no .fb2 file.")
            return zf.read(inner)
    except zipfile.BadZipFile as exc:
        raise ConversionError(f"{src.name} is not a readable zip archive.") from exc


def _fb2_to_epub(raw: bytes, src: Path, out_stem: Path) -> Path:
    from lxml import etree

    # recover=True: fb2 in the wild is frequently not well-formed.
    root = etree.fromstring(raw, etree.XMLParser(recover=True, huge_tree=True))
    if root is None:
        raise ConversionError(f"{src.name} could not be parsed as fb2.")

    def localname(el) -> str | None:
        # Comments and processing instructions have a callable .tag, which
        # QName rejects. Real fb2 files contain both.
        return etree.QName(el).localname if isinstance(el.tag, str) else None

    def text_of(el) -> str:
        return " ".join(t.strip() for t in el.itertext() if t and t.strip())

    title = _first_text(root, localname, "book-title") or src.stem
    language = _first_text(root, localname, "lang") or "en"

    chapters: list[tuple[str, list[str]]] = []
    for body in root.iter():
        if localname(body) != "body" or body.get("name") in _FB2_SKIP_BODIES:
            continue

        sections = [el for el in body if localname(el) == "section"]
        targets = sections or [body]
        for i, section in enumerate(targets, start=1):
            heading = None
            paragraphs: list[str] = []
            last = None
            for el in section.iter():
                tag = localname(el)
                if tag == "title" and heading is None:
                    heading = text_of(el) or None
                    continue
                if tag not in _FB2_BLOCKS:
                    continue
                t = text_of(el)
                # fb2 nests <p> inside <title>, so a heading is reached twice.
                if t and t != last:
                    paragraphs.append(t)
                    last = t
            if paragraphs:
                chapters.append((heading or f"Section {i}", paragraphs))

    if not chapters:
        raise ConversionError(f"No text could be extracted from {src.name}.")

    return write_epub(out_stem.with_suffix(".epub"), title, language, chapters)


def _first_text(root, localname, tag: str) -> str | None:
    for el in root.iter():
        if localname(el) == tag and el.text and el.text.strip():
            return el.text.strip()
    return None


# ---------------------------------------------------------------------------
# mobi / azw3
# ---------------------------------------------------------------------------

def _from_mobi(src: Path, out_stem: Path) -> Path:
    try:
        import mobi
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ConversionError(
            "mobi/azw3 support needs the `mobi` package: pip install mobi"
        ) from exc

    tempdir = None
    try:
        # KF8 (azw3) unpacks straight to epub, chapters and all.
        tempdir, produced = mobi.extract(str(src))
    except Exception as exc:  # noqa: BLE001 - the library raises bare exceptions
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)
        raise ConversionError(
            f"Could not read {src.name}. If it is DRM-protected, it cannot be "
            "converted."
        ) from exc

    try:
        out = Path(produced)
        if not out.exists():
            raise ConversionError(f"Nothing could be unpacked from {src.name}.")

        if out.suffix.lower() == ".epub":
            dest = out_stem.with_suffix(".epub")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, dest)
            return dest

        pages = [out] if out.suffix.lower() in {".html", ".xhtml", ".htm"} else []
        pages += sorted(
            p for p in Path(tempdir).rglob("*")
            if p.suffix.lower() in {".html", ".xhtml", ".htm"} and p != out
        )
        if not pages:
            raise ConversionError(f"No readable text found inside {src.name}.")

        chapters = []
        for i, page in enumerate(pages, start=1):
            paragraphs = _html_paragraphs(page)
            if paragraphs:
                chapters.append((page.stem or f"Section {i}", paragraphs))
        if not chapters:
            raise ConversionError(f"No text could be extracted from {src.name}.")

        return write_epub(
            out_stem.with_suffix(".epub"), src.stem, "en", chapters
        )
    finally:
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)


def _html_paragraphs(path: Path) -> list[str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        path.read_text(encoding="utf-8", errors="replace"), "html.parser"
    )
    for bad in soup(["script", "style"]):
        bad.decompose()

    blocks = [
        el.get_text(" ", strip=True)
        for el in soup.find_all(["p", "h1", "h2", "h3", "h4", "blockquote"])
    ]
    blocks = [b for b in blocks if b]
    if blocks:
        return blocks
    # Some MOBI files are one long <div> soup with no paragraph tags.
    return [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# epub output
# ---------------------------------------------------------------------------

def write_epub(
    dest: Path,
    title: str,
    language: str,
    chapters: list[tuple[str, list[str]]],
) -> Path:
    """Write a chaptered epub with ebooklib - one spine document per chapter."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier(f"subplz-{abs(hash(title)) % 10**12}")
    book.set_title(title)
    book.set_language((language or "en")[:8])

    items = []
    for i, (heading, paragraphs) in enumerate(chapters, start=1):
        doc = epub.EpubHtml(
            title=heading, file_name=f"chapter{i:04d}.xhtml", lang=language
        )
        body = "\n".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
        doc.content = f"<h1>{html.escape(heading)}</h1>\n{body}"
        book.add_item(doc)
        items.append(doc)

    book.toc = tuple(items)
    book.spine = ["nav", *items]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    dest.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(dest), book)
    return dest

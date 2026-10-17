"""Small text helpers shared by the pipeline and the engines."""
from __future__ import annotations

import re
from urllib.parse import quote, quote_plus

_LETTER = re.compile(r"[^\W\d_]")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?\u0964\u0965])\s+")  # . ! ? and the Bengali danda marks
_QUERY_TEXT = re.compile(r"([?&\s](?:q|text|query)=)[^&\s)'\"]*", re.IGNORECASE)


def has_letters(text: str) -> bool:
    """False for strings that are only numbers, punctuation or whitespace."""
    return bool(_LETTER.search(text))


def scrub(message: object, text: str = "", limit: int = 200) -> str:
    """Make an error message safe to log or return: no segment text, no long output."""
    msg = " ".join(str(message).split())
    msg = _QUERY_TEXT.sub(r"\1<text>", msg)
    for base in {text.strip(), " ".join(text.split())}:
        if len(base) < 4:
            continue
        for candidate in (base, quote(base), quote_plus(base)):  # plain and URL-encoded copies
            msg = msg.replace(candidate, "<text>")
    if len(msg) > limit:
        msg = msg[: limit - 1] + "\u2026"
    return msg


_TAG_PATTERN = re.compile(r"\{\s*[\w\d]+\s*\}|<[^>]+>")

# Map non-ASCII localized digits to standard ASCII 0-9
_VERNACULAR_DIGITS = {
    # Bengali ০-৯
    0x09E6: "0", 0x09E7: "1", 0x09E8: "2", 0x09E9: "3", 0x09EA: "4",
    0x09EB: "5", 0x09EC: "6", 0x09ED: "7", 0x09EE: "8", 0x09EF: "9",
    # Devanagari ०-९
    0x0966: "0", 0x0967: "1", 0x0968: "2", 0x0969: "3", 0x096A: "4",
    0x096B: "5", 0x096C: "6", 0x096D: "7", 0x096E: "8", 0x096F: "9",
    # Arabic-Indic ٠-٩
    0x0660: "0", 0x0661: "1", 0x0662: "2", 0x0663: "3", 0x0664: "4",
    0x0665: "5", 0x0666: "6", 0x0667: "7", 0x0668: "8", 0x0669: "9",
}


def normalize_ascii_digits(text: str) -> str:
    """Ensure all numeric characters are normalized to standard ASCII '0'-'9'."""
    return text.translate(_VERNACULAR_DIGITS)


BN_DIGITS = "০১২৩৪৫৬৭৮৯"
_KEEP_RE = re.compile(
    r"(?:https?://|www\.)\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\{[0-9\s]+\}|[A-Za-z0-9_#-]*[A-Za-z][A-Za-z0-9_#-]*|[0-9]"
)


def to_bn_digits(s: str) -> str:
    """Converts standalone ASCII digits '0'-'9' to Bengali digits '০'-'৯'.
    Preserves numbers in URLs, emails, placeholder tags like {0}, and codes touching Latin letters (e.g. COVID-19, A123, MP3).
    """
    def _repl(m: re.Match) -> str:
        matched = m.group(0)
        if len(matched) == 1 and "0" <= matched <= "9":
            return BN_DIGITS[int(matched)]
        return matched

    return _KEEP_RE.sub(_repl, s)


def find_tags(text: str) -> list[str]:
    """Find placeholder tags like {0}, { 1 }, {name}, or HTML tags in text."""
    return _TAG_PATTERN.findall(text)


def check_tags_preserved(source: str, translation: str) -> tuple[bool, str | None]:
    """Check that all placeholder tags in source text exist in translation. Returns (ok, missing_tag)."""
    tags = find_tags(source)
    for tag in tags:
        if tag not in translation:
            return False, tag
    return True, None


def _hard_split(piece: str, size: int) -> list[str]:
    if len(piece) <= size:
        return [piece]
    out: list[str] = []
    current = ""
    for word in piece.split():
        while len(word) > size:  # a single very long word: slice it
            if current:
                out.append(current)
                current = ""
            out.append(word[:size])
            word = word[size:]
        if current and len(current) + 1 + len(word) > size:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out


def chunk_text(text: str, size: int) -> list[str]:
    """Split text into pieces of at most `size` characters, preferring sentence boundaries."""
    text = text.strip()
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_BREAK.split(text):
        for piece in _hard_split(sentence, size):
            if current and len(current) + 1 + len(piece) > size:
                chunks.append(current)
                current = piece
            else:
                current = f"{current} {piece}".strip() if current else piece
    if current:
        chunks.append(current)
    return chunks


def normalize_text(text: str) -> str:
    """Normalize text for TM exact-match lookup.
    
    Matches browser's norm() behavior from src/translator/lib/docx.js:
    - NFC unicode normalization
    - Remove zero-width space (U+200B) and BOM (U+FEFF)
    - Collapse whitespace to single spaces
    - Trim leading/trailing whitespace
    
    Args:
        text: Raw source text to normalize.
        
    Returns:
        Normalized text suitable for TM dictionary lookup.
    """
    import unicodedata
    
    # NFC normalization
    norm = unicodedata.normalize("NFC", text)
    # Remove zero-width space and BOM
    norm = norm.replace("\u200B", "").replace("\uFEFF", "")
    # Collapse whitespace and trim
    norm = " ".join(norm.split()).strip()
    return norm

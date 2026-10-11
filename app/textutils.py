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

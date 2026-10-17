"""Glossary protection and restoration utilities for machine translation."""
from __future__ import annotations

import re
from typing import Mapping

from app.textutils import normalize_ascii_digits


def protect_terms(source_text: str, glossary: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """Replaces glossary terms in source text with placeholder tokens (e.g. {0}, {1}).

    Glossary keys are sorted by length in descending order to avoid partial substring
    replacement (e.g. 'Safety Coordinator' is protected before 'Safety').

    Args:
        source_text: The input text segment.
        glossary: Dictionary mapping source terms to desired target terms.

    Returns:
        A tuple of (protected_text, token_map), where token_map maps '{0}' -> target_term.
    """
    if not source_text or not glossary:
        return source_text, {}

    # Filter out empty or whitespace-only keys/values
    valid_entries = [
        (k.strip(), v.strip())
        for k, v in glossary.items()
        if k and k.strip() and v and v.strip()
    ]
    if not valid_entries:
        return source_text, {}

    # Sort terms by length in descending order to prevent partial substring overlaps
    sorted_entries = sorted(valid_entries, key=lambda item: len(item[0]), reverse=True)

    token_map: dict[str, str] = {}
    term_to_token: dict[str, str] = {}
    lookup: dict[str, tuple[str, str]] = {}
    pattern_parts: list[str] = []

    for src_term, tgt_term in sorted_entries:
        escaped = re.escape(src_term)
        # Apply boundary checks for alphanumeric terms to avoid matching inside other words
        prefix = r"(?<!\w)" if src_term[0].isalnum() or src_term[0] == "_" else ""
        suffix = r"(?!\w)" if src_term[-1].isalnum() or src_term[-1] == "_" else ""
        pattern_parts.append(f"({prefix}{escaped}{suffix})")
        lookup[src_term.lower()] = (src_term, tgt_term)

    combined_regex = re.compile("|".join(pattern_parts), re.IGNORECASE)

    def _repl(match: re.Match) -> str:
        matched_text = match.group(0)
        norm_key = matched_text.lower()
        if norm_key in lookup:
            orig_src, target_term = lookup[norm_key]
        else:
            return matched_text

        # Reuse existing token for duplicate occurrences of the same term in this segment
        canonical_key = orig_src.lower()
        if canonical_key in term_to_token:
            return term_to_token[canonical_key]

        token = f"{{{len(token_map)}}}"
        token_map[token] = target_term
        term_to_token[canonical_key] = token
        return token

    protected_text = combined_regex.sub(_repl, source_text)
    return protected_text, token_map


def restore_terms(translated_text: str, token_map: Mapping[str, str]) -> str:
    """Restores placeholder tokens back to their designated glossary target terms.

    Handles standard ASCII tokens ({0}), spaced tokens ({ 0 }), and localized vernacular
    digits ({০}) that may be returned by translation engines.

    Args:
        translated_text: Output text containing placeholder tokens.
        token_map: Mapping of tokens (e.g. '{0}' or '0') to target translations.

    Returns:
        The finalized translated text with glossary terms in place.
    """
    if not translated_text or not token_map:
        return translated_text

    # Standardize token_map so lookups match '{0}' regardless of whether key was '0' or '{0}'
    normalized_map: dict[str, str] = {}
    for k, v in token_map.items():
        key_str = str(k).strip()
        if not key_str.startswith("{"):
            key_str = f"{{{key_str}}}"
        normalized_map[key_str] = v

    # Match any placeholder token: {0}, { 0 }, or vernacular digit variants like {০}
    token_re = re.compile(r"\{\s*[\w\d\u09e6-\u09ef\u0966-\u096f]+\s*\}")

    def _repl(match: re.Match) -> str:
        raw_tag = match.group(0)
        # Normalize vernacular digits to ASCII and remove internal whitespace
        norm_tag = "{" + normalize_ascii_digits(raw_tag[1:-1].strip()) + "}"
        return normalized_map.get(norm_tag, raw_tag)

    return token_re.sub(_repl, translated_text)

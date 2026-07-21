"""
spoken_text.py  -  defense-in-depth hygiene for text that will be spoken aloud
(goal.md 2.4, case-study lesson).

The system prompt asks the model for spoken-friendly replies, but a prompt is a
request, not a guarantee: a stray `**bold**` or bullet list would be read aloud
verbatim by TTS. Everything headed for a voice is normalized here first.

`chunk_text` splits a long reply into sentence-boundary chunks so Phase 3.2 can
synthesize incrementally (first audio before the full reply exists).
"""

from __future__ import annotations

import re

_MARKDOWN_BULLET = re.compile(r"^\s*(?:[-*•]|\d+\.)\s+")
_MARKDOWN_HEADER = re.compile(r"^\s*#{1,6}\s+")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_INLINE_MARKDOWN = re.compile(r"[*_`#]+")
_DASH_BREAK = re.compile(r"\s*[—–]\s*")   # em/en dashes only; word hyphens survive
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def normalize_spoken_text(text: str) -> str:
    """Return `text` safe to hand to a TTS voice: no markdown, no list syntax."""
    cleaned_lines: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = _MARKDOWN_HEADER.sub("", line)
        line = _MARKDOWN_BULLET.sub("", line)
        line = _MARKDOWN_LINK.sub(r"\1", line)
        line = _INLINE_MARKDOWN.sub("", line)
        line = _DASH_BREAK.sub(", ", line)
        cleaned_lines.append(line)
    return re.sub(r"\s+", " ", " ".join(cleaned_lines)).strip()


def chunk_text(text: str, max_chars: int = 240) -> list[str]:
    """Split `text` into sentence-boundary chunks of at most `max_chars`.

    Oversized single sentences fall back to word-boundary splits so no chunk
    ever exceeds the limit (TTS providers cap input length).
    """
    normalized = (text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(normalized):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_by_words(sentence, max_chars))
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def _split_by_words(sentence: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for word in sentence.split():
        if len(word) > max_chars:          # pathological token: hard cut
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(word[i:i + max_chars] for i in range(0, len(word), max_chars))
            continue
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            pieces.append(current)
            current = word
    if current:
        pieces.append(current)
    return pieces

"""Whole-word term matching shared by skill extraction, stack-role labels and resume-version choice.

Plain substring checks match inside other words ("java" in "javascript", "ml" in "html", "ai" in "maintain"),
so text and terms are both reduced to space-separated words and compared with surrounding spaces.
"""

import re

# Keeps + # . inside words so c++, c#, node.js and next.js survive; everything else separates words.
_SEPARATORS = re.compile(r"[^a-z0-9+#.]+")


def word_text(text: str) -> str:
    """Lower-cased words joined by single spaces, padded with a space on each side."""
    words = (w.strip(".") for w in _SEPARATORS.sub(" ", (text or "").lower()).split())
    return " " + " ".join(w for w in words if w) + " "


def has_term(words: str, term: str) -> bool:
    """True if `term` (one or more words) appears as whole words in text already passed through word_text."""
    return word_text(term) in words

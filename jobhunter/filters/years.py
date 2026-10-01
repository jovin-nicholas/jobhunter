"""The years of experience a job description requires, read from its sentences and words rather than whole-phrase
patterns.

A mention is a number or range ("5", "5+", "3-10", "8–10+", "two") followed by "years". It is a requirement when the
words near it name experience ("of hands-on software development experience", "Experience: 15+ Years", "minimum 12
years work experience"), and not when the sentence is about the company ("With 40+ years of experience in the
Insurtech game, we're…"), about something else ("18 years or older", "vests over 4 years"), or only preferred.

Mentions joined by "or" are alternatives and the lowest counts ("5 years, or 2 with a Master's"); everything else is
required and the highest counts ("7+ years of Java. 3+ years with Spring."). Years under a "Preferred qualifications"
or "Nice to have" heading are not required.
"""
from __future__ import annotations

WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20}
YEAR_WORDS = {"year", "years", "yr", "yrs"}
# Words that make a years mention about work experience.
EXPERIENCE_WORDS = {"experience", "experienced", "professional", "hands", "industry", "work", "working", "developing",
                    "development", "building", "designing", "engineering", "programming", "coding", "exp", "software"}
# Just before a mention these say it is a requirement even without an experience word ("Requires 10+ years").
REQUIREMENT_LEADS = {"minimum", "min", "least", "requires", "required", "require", "need", "needs", "must"}
# The sentence speaks about the company, not the candidate ("With 40+ years of experience … we're").
COMPANY_WORDS = {"we", "we're", "we've", "we'd", "we'll", "our", "us", "founded", "since", "celebrated", "established",
                 "history", "legacy", "helping", "serving"}
# Right before a mention these make it the company's years ("a company with 18 years of engineering excellence").
COMPANY_NOUNS = {"our", "company", "firm", "team", "organization", "organisation", "agency", "business"}
# The mention's clause speaks to or about the candidate.
CANDIDATE_WORDS = {"you", "your", "you'll", "you've", "candidate", "candidates", "required", "requires", "require",
                   "requirement", "requirements", "qualification", "qualifications", "minimum", "must", "least",
                   "looking", "seeking", "ideal", "need", "needs", "targeting", "hiring", "engineer", "developer"}
# "5+ years preferred", "3 years is a plus": the clause after the mention says it is optional.
PREFERRED_AFTER = {"preferred", "plus", "bonus", "desired", "desirable"}
# "Preferred: 5 years", "Ideally 3+ years": said before the mention. After it ("…, preferably in a startup") these
# words qualify something else.
PREFERRED_BEFORE = {"preferred", "bonus", "desired", "desirable", "preferably", "ideally", "nice"}
# Headings that open a section of optional qualifications, and words that mark a line as a heading.
PREFERRED_HEADING = {"preferred", "bonus", "desired", "desirable", "plus"}      # and "nice to have"
HEADING_WORDS = {"qualifications", "requirements", "required", "basic", "minimum", "preferred", "bonus", "nice", "desired",
                 "responsibilities", "benefits"}
# "Bachelor's degree or equivalent (minimum 12 years)": years that stand in for a degree.
DEGREE_WORDS = {"degree", "bachelor", "bachelor's", "bachelors", "associate", "associate's", "diploma"}
SUBSTITUTE_WORDS = {"equivalent", "if", "lieu", "or"}
BOTH_WORDS = {"with", "and", "plus"}      # "degree or equivalent experience with 5+ years": the degree and the years
NOT_EXPERIENCE_WORDS = {"old", "older", "age", "ago", "vest", "vests", "vesting", "warranty", "contract", "term"}
MAX_YEARS = 20                # more than this is company history ("over 100 years of experience"), not a requirement
BEFORE, AFTER = 4, 16         # how many words around a mention are looked at
EXPERIENCE_BEFORE = 6         # "Experience Level: Mid (3-5 years)"
MAX_HEADING_WORDS = 8
OR_REACH = 3                  # "degree or a minimum of 4 years": words between "or" and the number
MAX_LABEL_WORDS = 5          # "Preferred Qualifications (Nice-to-Have)"; longer lines are bullets
CLAUSE_ENDS = {",", ";", ":", "(", ")"}


def _sentences(text: str) -> list[str]:
    """Split at line breaks, bullets and sentence punctuation; a dot only ends a sentence before a space (1.5, Node.js)."""
    out, current = [], []
    for i, ch in enumerate(text or ""):
        nxt = text[i + 1] if i + 1 < len(text) else " "
        if ch in "\n!?;•" or (ch == "." and nxt.isspace()):
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
    out.append("".join(current))
    return [s for s in out if s.strip()]


def _tokens(sentence: str) -> list[str]:
    """Numbers ("1.5" kept whole), words (with apostrophes: "we're") and single punctuation marks, lower-cased."""
    tokens, i, text = [], 0, sentence.lower().replace("’", "'")
    while i < len(text):
        ch = text[i]
        if ch.isascii() and ch.isdigit():
            j = i
            while j < len(text) and ((text[j].isascii() and text[j].isdigit())
                                     or (text[j] == "." and j + 1 < len(text) and text[j + 1].isdigit())):
                j += 1
            tokens.append(text[i:j])
            i = j
        elif ch.isalpha() and ch.isascii():
            j = i
            while j < len(text) and ((text[j].isalpha() and text[j].isascii()) or (text[j] == "'" and j + 1 < len(text)
                                                                                  and text[j + 1].isalpha())):
                j += 1
            tokens.append(text[i:j])
            i = j
        elif ch.isspace():
            i += 1
        else:
            tokens.append(ch)       # punctuation, dashes, and stray characters from garbled text ("8â10+")
            i += 1
    return tokens


def _number(token: str) -> int | None:
    """5, 1.5 (as 1) or "five"; None for anything else, such as a phone number ("555.010.0199") or a version."""
    whole, _, fraction = token.partition(".")
    if whole.isascii() and whole.isdigit() and (not fraction or (fraction.isascii() and fraction.isdigit())):
        return int(whole)
    return WORD_NUMBERS.get(token)


def _mentions(tokens: list[str]) -> list[tuple[int, int]]:
    """(index of the first number, lowest years) for every "<number or range> years" in the tokens."""
    found, i = [], 0
    while i < len(tokens):
        low = _number(tokens[i])
        if low is None:
            i += 1
            continue
        j = i + 1
        # A range whose dash was lost ("3 5 years"): the next token is a larger number right before "years".
        nxt = _number(tokens[j]) if j < len(tokens) else None
        if nxt is not None and nxt > low and j + 1 < len(tokens) and tokens[j + 1] in YEAR_WORDS | {"+"}:
            j += 1
        # A range: "3-10", "3 to 10", or a garbled dash ("8â10") — one to three joining tokens, then a number.
        for gap in (1, 2, 3):
            if j + gap < len(tokens) and _number(tokens[j + gap]) is not None \
                    and all(not tokens[k].isalnum() or tokens[k] in ("to", "and") or not tokens[k].isascii()
                            for k in range(j, j + gap)):
                j = j + gap + 1
                break
        while j < len(tokens) and tokens[j] in ("+", ")"):
            j += 1
        if j < len(tokens) and tokens[j] in YEAR_WORDS:
            found.append((i, low))
            i = j + 1               # past the range and "years"
        else:
            i += 1
    return found


def _clause_after(tokens: list[str], years_at: int) -> list[str]:
    """The words after "years" up to the next comma, colon, semicolon or bracket."""
    clause = []
    for token in tokens[years_at + 1:]:
        if token in CLAUSE_ENDS:
            break
        clause.append(token)
    return clause


def _clause(tokens: list[str], start: int, years_at: int) -> list[str]:
    """The mention's own clause: from the last clause end before it to the next one after "years"."""
    first = max((k + 1 for k in range(start) if tokens[k] in CLAUSE_ENDS), default=0)
    return tokens[first:start] + tokens[start:years_at + 1] + _clause_after(tokens, years_at)


def _stands_in_for_a_degree(before: list[str]) -> bool:
    """ "Bachelor's degree or equivalent (minimum 12 years)", "If Associate's Degree, must have 6 years", "Bachelor's
    degree or a minimum of 4 years": the years replace a degree. Not "degree or equivalent experience with 5+ years"
    (both are asked for) or "degree in CS or related field, 3+ years" (that "or" is about the field)."""
    if not set(before) & DEGREE_WORDS:
        return False
    last = max((k for k, token in enumerate(before) if token in SUBSTITUTE_WORDS), default=None)
    if last is None or set(before[last:]) & BOTH_WORDS:
        return False
    if before[last] == "or":
        between = before[last + 1:]
        return len(between) <= OR_REACH and not set(between) & {",", ";"}
    return True


def _judge(tokens: list[str], start: int, years_at: int, low: int, company: frozenset[str] = frozenset()) -> str:
    """ "rejected" (not a requirement), "weak" (no experience word: counts only as an alternative to a requirement)
    or "required"."""
    before = set(tokens[max(0, start - BEFORE):start])
    if low > MAX_YEARS or set(tokens[years_at + 1:years_at + 4]) & NOT_EXPERIENCE_WORDS:
        return "rejected"
    if before & PREFERRED_BEFORE or set(_clause_after(tokens, years_at)) & PREFERRED_AFTER:
        return "rejected"
    if _stands_in_for_a_degree(tokens[:start]):
        return "rejected"
    if before & (COMPANY_NOUNS | company):
        return "rejected"           # "a company with 18 years", "working with Genesis10 for 5-20+ years"
    if set(tokens) & COMPANY_WORDS and not set(_clause(tokens, start, years_at)) & CANDIDATE_WORDS:
        return "rejected"           # "With 40+ years of experience in the Insurtech game, we're…"
    near = set(tokens[max(0, start - EXPERIENCE_BEFORE):start]) | set(tokens[years_at + 1:years_at + 1 + AFTER])
    if near & EXPERIENCE_WORDS or set(tokens[max(0, start - EXPERIENCE_BEFORE):start]) & REQUIREMENT_LEADS:
        return "required"
    return "weak"


def _heading(sentence: str, tokens: list[str]) -> bool | None:
    """For a heading line: True when it opens optional qualifications ("Preferred Qualifications", "Nice to have:"),
    False for any other heading ("Basic Qualifications", "What you'll bring:"); None for other lines."""
    words = [t for t in tokens if t.isalpha() or "'" in t]
    if not words or len(words) > MAX_HEADING_WORDS or any(t[0].isdigit() for t in tokens):
        return None
    text = sentence.strip().lstrip("-*• ").strip()
    letters = [c for c in text if c.isalpha()]
    # A heading ends with ":", is in capitals, opens with a heading word ("Preferred Qualifications") or is a short
    # label with one in brackets ("Certifications (Preferred)"); a bullet that mentions "preferred" is not one.
    bracketed = text.endswith(")") and len(words) <= 3 and words[-1] in HEADING_WORDS
    labelled = words[0] in HEADING_WORDS and len(words) <= MAX_LABEL_WORDS
    if not (text.endswith(":") or (letters and all(c.isupper() for c in letters)) or labelled or bracketed):
        return None
    return bool(set(words) & PREFERRED_HEADING) or ("nice" in words and "have" in words)


def required_years(text: str, company: str = "") -> int | None:
    """The years of experience the text requires, or None when it states no requirement.

    Mentions joined by "or" are alternatives and the lowest counts; everything else is required and the highest
    counts. Lines under a preferred-qualifications heading are skipped. `company` (the job's company) marks years
    right after its name as the company's history.
    """
    names = frozenset(t for t in _tokens(company) if t.isalpha() and len(t) >= 3) - {"inc", "llc", "ltd", "the", "corp"}
    required, in_preferred = [], False
    for sentence in _sentences(text):
        tokens = _tokens(sentence)
        heading = _heading(sentence, tokens)
        if heading is not None:
            in_preferred = heading
            continue
        if in_preferred:
            continue
        groups, previous_end = [], None
        for start, low in _mentions(tokens):
            years_at = next(k for k in range(start, len(tokens)) if tokens[k] in YEAR_WORDS)
            verdict = _judge(tokens, start, years_at, low, names)
            if verdict == "rejected":
                continue
            if groups and "or" in tokens[previous_end:start]:
                groups[-1].append((low, verdict))
            else:
                groups.append([(low, verdict)])
            previous_end = years_at
        for group in groups:
            if any(verdict == "required" for _, verdict in group):
                required.append(min(low for low, _ in group))
    return max(required) if required else None

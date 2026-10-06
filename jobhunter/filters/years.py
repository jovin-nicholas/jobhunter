"""The years of experience a job description requires, read from its sentences and words rather than whole-phrase
patterns.

A mention is a number or range ("5", "5+", "3-10", "8–10+", "two") followed by "years". It is a requirement when the
words near it name experience ("of hands-on software development experience", "Experience: 15+ Years", "minimum 12
years work experience"), and not when the sentence is about the company ("With 40+ years of experience in the
Insurtech game, we're…"), about something else ("18 years or older", "vests over 4 years"), or only preferred.

A mention with no experience word still counts when it reads as one: "8+ years of Java", "7+ years with Go" (a "+"
before "years" and of/with/in after it), "5 years of Python" on a bulleted line or under a requirements heading, "2 years
required", or a label such as "Years of experience: 4". The skill form counts only when it opens its clause (or follows
"Must have", "At least"...), is not "in a row" or "of the …", is not called optional on its line ("(preferred)", "a
plus"), and is not a bullet under a section about the company or the offer (Benefits, Why Acme?, About us).

Mentions joined by "or" are alternatives and the lowest counts ("5 years, or 2 with a Master's"); everything else is
required and the highest counts ("7+ years of Java. 3+ years with Spring."). Years under a "Preferred qualifications"
or "Nice to have" heading are not required.
"""
from __future__ import annotations

import html
import re

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
PREFERRED_BEFORE = {"preferred", "bonus", "desired", "desirable", "preferably", "ideally", "nice", "typically"}
# Headings that open a section of optional qualifications, and words that mark a line as a heading.
PREFERRED_HEADING = {"preferred", "bonus", "desired", "desirable", "plus", "typical"}      # and "nice to have"
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
# "2 years required": right after "years" these make it a requirement.
REQUIRED_AFTER = {"required", "minimum", "mandatory", "must"}
# "8+ years of Java", "7+ years with Go", "10+ years in backend roles": a skill or field after "years".
SKILL_LINKS = {"of", "with", "in"}
# A heading that opens the requirements ("Qualifications:", "Requirements", "What you'll need"): years under it with
# a skill after them count, as on a bullet.
REQUIRED_HEADING = {"qualifications", "requirements", "requirement", "required", "minimum", "basic", "skills",
                    "experience", "need", "bring", "must"}
BULLETS = ("-", "*", "\u2013", "\u2022")
# Before "N years of <skill>" only these may open its clause ("Must have 6 years of Java", "At least 4 years with
# Go"): any other word makes the years something else ("401k matching after 5 years of service").
SKILL_LEADS = {"minimum", "min", "at", "least", "requires", "required", "require", "need", "needs", "must", "have",
               "a", "of"}
ARTICLES = {"a", "an", "the"}               # "5 years of the company's history" (but "5+ years in a SaaS role")
# "20+ years in business", "15 years in operation." ending the phrase: the company's age (but "5 years in business
# operations" is a field).
COMPANY_AGE = {"business", "operation", "existence"}
_BLOCK_TAG = re.compile(r"<\s*(?:br|/?p|/?li|/?ul|/?ol|/?div|/?h[1-6]|/?tr)\b[^>]*>", re.I)
_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")
LINE_HEADING_ENDS = {":", "-", "\u2013", "\u2014"}      # "Qualifications - 5+ years", "Requirements: 6 years"
# On the same line these make "N years of <skill>" optional ("5 years of Java (preferred)", "Go is a plus").
SOFT_ON_LINE = {"preferred", "preferably", "ideally", "bonus", "desired", "desirable"}
# Short lines that open a section about the company or the offer: bullets under them are not requirements.
OTHER_SECTION_FIRST = {"benefits", "perks", "why", "about", "compensation", "culture", "what's", "whats", "our"}


def _sentences(text: str) -> list[str]:
    """Split at line breaks, bullets and sentence punctuation; a dot only ends a sentence before a space (1.5, Node.js)."""
    out, current = [], []
    for i, ch in enumerate(text or ""):
        nxt = text[i + 1] if i + 1 < len(text) else " "
        if ch in "\n!?;\u2022" or (ch == "." and nxt.isspace()):
            out.append("".join(current))
            current = ["\u2022 "] if ch == "\u2022" else []      # a bullet stays marked as one
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


def _clause_before(tokens: list[str], start: int) -> list[str]:
    """The words of the mention's clause before its number."""
    first = max((k + 1 for k in range(start) if tokens[k] in CLAUSE_ENDS), default=0)
    return tokens[first:start]


def _clause(tokens: list[str], start: int, years_at: int) -> list[str]:
    """The mention's own clause: from the last clause end before it to the next one after "years"."""
    return _clause_before(tokens, start) + tokens[start:years_at + 1] + _clause_after(tokens, years_at)


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


def _judge(tokens: list[str], start: int, years_at: int, low: int, company: frozenset[str] = frozenset(),
           listed: bool = False) -> str:
    """ "rejected" (not a requirement), "weak" (no experience word: counts only as an alternative to a requirement)
    or "required". `listed`: the mention is on a bulleted line or under a requirements heading."""
    before = set(tokens[max(0, start - BEFORE):start])
    if low > MAX_YEARS or set(tokens[years_at + 1:years_at + 4]) & NOT_EXPERIENCE_WORDS:
        return "rejected"
    if tokens[years_at + 1:years_at + 4] == ["in", "a", "row"]:
        return "rejected"           # "Best Places to Work 8 years in a row, with a strong engineering culture"
    if _company_age(tokens, years_at):
        return "rejected"           # "20+ years in business, Acme is a leader"
    soft = before & PREFERRED_BEFORE
    if soft == {"typically"} and before & REQUIREMENT_LEADS:
        soft = set()                # "Typically requires 8+ years" is still a requirement
    if soft or set(_clause_after(tokens, years_at)) & PREFERRED_AFTER:
        return "rejected"
    if _stands_in_for_a_degree(tokens[:start]):
        return "rejected"
    if before & (COMPANY_NOUNS | company):
        return "rejected"           # "a company with 18 years", "working with Genesis10 for 5-20+ years"
    clause = set(_clause(tokens, start, years_at))
    # A company noun anywhere before the mention in its clause makes the company its subject ("The team has built
    # software for 15 years"); after it ("5+ years at a top technology firm") it is where the candidate worked.
    subject = set(_clause_before(tokens, start))
    if (set(tokens) & COMPANY_WORDS or subject & COMPANY_NOUNS) and not clause & CANDIDATE_WORDS:
        return "rejected"           # "With 40+ years of experience in the Insurtech game, we're…", "The team has…"
    near = set(tokens[max(0, start - EXPERIENCE_BEFORE):start]) | set(tokens[years_at + 1:years_at + 1 + AFTER])
    if near & EXPERIENCE_WORDS or set(tokens[max(0, start - EXPERIENCE_BEFORE):start]) & REQUIREMENT_LEADS:
        return "required"
    if set(tokens[years_at + 1:years_at + 3]) & REQUIRED_AFTER:
        return "required"           # "2 years required"
    if _skill_years(tokens, start, years_at) and ("+" in tokens[start:years_at] or listed):
        return "required"           # "8+ years of Java"; "- 5 years of Python"
    return "weak"


def _company_age(tokens: list[str], years_at: int) -> bool:
    """ "years in business", "years in operation" with nothing after but punctuation or the line's end."""
    after = tokens[years_at + 1:years_at + 4]
    return len(after) >= 2 and after[0] == "in" and after[1] in COMPANY_AGE and \
        not (len(after) == 3 and after[2].isalpha())


def _skill_years(tokens: list[str], start: int, years_at: int) -> bool:
    """ "8+ years of Java", "7+ years with Go", "10+ years in MLOps", opening their clause (or after a lead such as
    "Must have"), not "in a row", and not on a line that calls them optional."""
    if years_at + 2 >= len(tokens) or tokens[years_at + 1] not in SKILL_LINKS:
        return False
    nxt = tokens[years_at + 2]
    if not nxt.isalpha() or (tokens[years_at + 1] == "of" and nxt in ARTICLES):
        return False                # "in a row" is rejected in _judge
    before = _clause_before(tokens, start)
    if _line_heading(tokens):               # "Qualifications - 5+ years in QA": the heading is not the clause
        end = next(k for k, t in enumerate(tokens) if t in LINE_HEADING_ENDS)
        before = tokens[end + 1:start] if end < start else before
    if not set(t for t in before if t.isalnum()) <= SKILL_LEADS:
        return False
    return not (set(tokens) & SOFT_ON_LINE or _has(tokens, "a", "plus") or _has(tokens, "nice", "to", "have"))


def _plain(text: str) -> str:
    """Text without HTML: block tags become line breaks (each <li> its own line), other tags go, and entities
    (&nbsp;, &amp;, &#39;) are decoded."""
    text = _TAG.sub(" ", _BLOCK_TAG.sub("\n", text or ""))
    return html.unescape(text).replace("\xa0", " ")


def _line_heading(tokens: list[str]) -> bool:
    """ "Qualifications - 5+ years in QA roles", "What You Bring: ...": a requirements heading opening the line."""
    end = next((k for k, t in enumerate(tokens) if t in LINE_HEADING_ENDS), None)
    if not end:
        return False
    words = tokens[:end]
    if len(words) > MAX_LABEL_WORDS or not all(t.isalpha() or "'" in t for t in words):
        return False
    return bool(set(words) & REQUIRED_HEADING) and not set(words) & PREFERRED_HEADING


def _has(tokens: list[str], *words: str) -> bool:
    n = len(words)
    return any(tuple(tokens[k:k + n]) == words for k in range(len(tokens) - n + 1))


def _other_section(tokens: list[str]) -> bool:
    """ "Benefits", "Perks:", "Why Acme?", "What's in it for you", "About Acme", "Who we are": a short heading-like
    line that opens a section about the company or the offer ("About you" is about the candidate)."""
    words = [t for t in tokens if t.isalpha() or "'" in t]
    if not words or len(words) > MAX_LABEL_WORDS or any(t[0].isdigit() for t in tokens):
        return False
    if words[:3] == ["who", "we", "are"]:
        return True
    return words[0] in OTHER_SECTION_FIRST and words[1:2] != ["you"] and (len(words) > 1 or words[0] != "our")


def _labelled_years(tokens: list[str]) -> list[int]:
    """ "Years of experience: 4", "Minimum years of relevant experience - 6": the number after the label."""
    found = []
    for k, token in enumerate(tokens):
        if token not in YEAR_WORDS:
            continue
        label = tokens[k + 1:k + 5]
        if "experience" not in label:
            continue
        e = k + 1 + label.index("experience")
        if e + 2 < len(tokens) and tokens[e + 1] in (":", "-"):
            n = _number(tokens[e + 2])
            if n is not None and n <= MAX_YEARS:
                found.append(n)
    return found


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
    required, in_preferred, in_required, in_other, joined = [], False, False, False, False
    for sentence in _sentences(_plain(text)):
        tokens = _tokens(sentence)
        if [t for t in tokens if t.isalpha()] == ["or"]:
            joined = True               # a line that only says "OR": the lines around it are alternatives
            continue
        if _other_section(tokens):
            in_preferred, in_required, in_other = False, False, True     # Benefits, Why Acme?, About Acme
            continue
        heading = _heading(sentence, tokens)
        if heading is not None:
            in_preferred = heading
            in_required = not heading and bool(set(tokens) & REQUIRED_HEADING)
            in_other = not heading and not in_required
            continue
        if in_preferred:
            continue
        listed = in_required or _line_heading(tokens) or (not in_other and sentence.lstrip().startswith(BULLETS))
        groups, previous_end = [[(n, "required")] for n in _labelled_years(tokens)], None
        for start, low in _mentions(tokens):
            years_at = next(k for k in range(start, len(tokens)) if tokens[k] in YEAR_WORDS)
            verdict = _judge(tokens, start, years_at, low, names, listed)
            if verdict == "rejected":
                continue
            if groups and "or" in tokens[previous_end:start]:
                groups[-1].append((low, verdict))
            else:
                groups.append([(low, verdict)])
            previous_end = years_at
        found = [min(low for low, _ in group) for group in groups if any(v == "required" for _, v in group)]
        if found and joined and required:
            required[-1] = min(required[-1], *found)
        else:
            required += found
        if found:
            joined = False
    return max(required) if required else None

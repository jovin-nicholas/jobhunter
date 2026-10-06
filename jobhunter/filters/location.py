"""Keeps jobs located in the configured countries (ported from job-notifier's corrected US check)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from jobhunter.errors import SettingsError
from jobhunter.models import KEEP, FilterResult, Job, skip
from jobhunter.settings import LocationFilterSettings
from jobhunter.text_match import has_term, word_text

if TYPE_CHECKING:
    from jobhunter.systemone import SystemOneClient

US_STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
             "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND",
             "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"]

US_STATE_NAMES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
                  "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
                  "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
                  "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
                  "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
                  "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
                  "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia"]
# Job boards often give a bare city; these are the common US tech hubs.
US_CITIES = ["new york city", "nyc", "san francisco", "sf bay area", "bay area", "silicon valley", "los angeles",
             "seattle", "boston", "chicago", "austin", "denver", "atlanta", "san jose", "san diego", "palo alto",
             "mountain view", "menlo park", "sunnyvale", "santa clara", "redmond", "cambridge, ma", "pittsburgh",
             "philadelphia", "dallas", "houston", "miami", "portland", "salt lake city", "raleigh", "washington dc",
             "washington d.c.", "minneapolis", "detroit", "phoenix", "nashville", "cincinnati", "columbus"]

BUILTIN_COUNTRIES = {
    "US": {"names": ["united states", "united states of america", "usa", "u.s.", "u.s.a.", "us",
                     *US_STATE_NAMES, *US_CITIES],
           "region_codes": US_STATES},
    "CA": {"names": ["canada", "toronto", "vancouver", "montreal", "montréal", "ottawa", "calgary", "edmonton",
                     "waterloo", "mississauga"],
           "region_codes": ["AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"]},
    "GB": {"names": ["united kingdom", "uk", "england", "scotland", "wales", "northern ireland", "great britain"],
           "region_codes": []},
    "IN": {"names": ["india", "bengaluru", "bangalore", "hyderabad", "chennai", "pune", "mumbai", "delhi", "new delhi",
                     "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad"],
           "region_codes": ["AP", "DL", "GJ", "HR", "KA", "KL", "MH", "RJ", "TG", "TN", "TS", "UP", "WB"]},
    "DE": {"names": ["germany", "deutschland", "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne",
                     "köln", "stuttgart", "düsseldorf", "dusseldorf", "muenchen"],
           "region_codes": ["BE", "BW", "BY", "HB", "HE", "HH", "NI", "NW", "RP", "SH", "SN", "TH"]},
}

# Places outside every built-in country; a job naming one is skipped unless the user allowed it.
OTHER_PLACES = ["france", "spain", "italy", "netherlands", "poland", "ireland", "singapore", "australia",
                "new zealand", "japan", "china", "hong kong", "mexico", "brazil", "emea", "europe", "apac", "latam",
                "international", "worldwide"]

CODE_ALIASES = {"UK": "GB"}
# "Georgia" is a US state and a country; these cities mean the country.
GEORGIA_COUNTRY_CITIES = ["tbilisi", "batumi", "kutaisi"]


# Results the rules cannot decide; with a System One model these are asked instead of skipped.
UNDECIDED = ("no location given", "no place named", "location not recognised")
LOCATION_WORDS = {"based", "office", "offices", "located", "location", "locations", "remote", "hybrid", "onsite",
                  "on-site", "relocate", "relocation", "country", "countries", "authorization", "visa", "sponsorship",
                  "eligible", "headquarters", "site", "city", "region"}
EXCERPT_CHARS = 1500


class LocationFilter:
    name = "location"

    def __init__(self, settings: LocationFilterSettings, model: SystemOneClient | None = None):
        self.allowed: dict[str, dict] = {}
        unknown = []
        for item in settings.countries:
            if isinstance(item, str):
                code = CODE_ALIASES.get(item.upper(), item.upper())
                if code not in BUILTIN_COUNTRIES:
                    unknown.append(item)
                    continue
                self.allowed[code] = BUILTIN_COUNTRIES[code]
            else:
                self.allowed[item["code"].upper()] = {
                    "names": [n.lower() for n in item.get("names") or []],
                    "region_codes": [r.upper() for r in item.get("region_codes") or []],
                }
        if unknown:
            raise SettingsError([f"filters.location.countries: no built-in data for {', '.join(unknown)}; "
                                 f"built in: {', '.join(BUILTIN_COUNTRIES)}. Add it as {{code, names, region_codes}}."])
        allowed_names = {n for spec in self.allowed.values() for n in spec["names"]}
        self.others = [p for code, spec in BUILTIN_COUNTRIES.items() if code not in self.allowed for p in spec["names"]]
        self.others = [p for p in self.others + OTHER_PLACES if p not in allowed_names]
        self.model = model
        countries = " or ".join(spec["names"][0].title() if spec["names"] else code
                                for code, spec in self.allowed.items())
        self.countries_text = countries
        self.question = {
            "type": "noul",
            "instructions": f"Is `job` based in {countries}, or open to people working from {countries}?",
            "criteria": {
                "true": f"`job.location`, `job.title` or `job.description` places the job in a city or region of "
                        f"{countries}, or says it is remote within {countries} or open to candidates there.",
                "false": f"The job is in another country, or remote only for other countries or regions that "
                         f"exclude {countries}.",
            },
        }

    def _foreign_city_with_shared_code(self, location: str) -> bool:
        """True for "Toronto, CA" or "DE - Berlin" when CA/DE is also an allowed country's region code (California,
        Delaware): the city then decides. "San Jose, CA" and "Hamburg, NY" are not affected."""
        found = _city_and_code(location)
        if not found:
            return False
        city, code = found
        code = CODE_ALIASES.get(code.upper(), code.upper())
        if not any(code in spec["region_codes"] for spec in self.allowed.values()):
            return False            # not ambiguous: the usual rules decide
        city = word_text(city)
        # The code is the other country itself ("Toronto, CA") or one of its regions ("Chennai, TN", Tamil Nadu).
        return any((code == other or code in spec["region_codes"]) and any(has_term(city, n) for n in spec["names"])
                   for other, spec in BUILTIN_COUNTRIES.items() if other not in self.allowed)

    def check(self, job: Job) -> FilterResult:
        result = self._rules(job)
        if result.keep or self.model is None or not (result.reason or "").startswith(UNDECIDED):
            return result
        p = self.model.noul("in_allowed_country", _location_state(job), self.question)
        if p is None:
            return result               # no answer: the rules' result stands
        return KEEP if p >= 0.5 else skip(f"{result.reason}; model: not in an allowed country ({p:.2f})")

    def report(self) -> str | None:
        return self.model.report() if self.model is not None else None

    def _rules(self, job: Job) -> FilterResult:
        location = (job.location or "").strip()
        if not location:
            return self._from_title_and_description(job, location)  # boards leave an unknown location empty
        results = [self._check_place(place, job) for place in _places(location)]
        if any(r is not None and r.keep for r in results):
            return KEEP                     # a list of places: one allowed place is enough
        if all(r is None for r in results):
            return self._from_title_and_description(job, location)
        if len(results) == 1:
            return results[0]
        if any(r is not None and (r.reason or "").startswith(UNDECIDED) for r in results):
            return skip(f"location not recognised: {location}")
        return skip(f"location outside allowed countries: {location}")

    def _check_place(self, place: str, job: Job) -> FilterResult | None:
        """KEEP or a skip for one place; None when it names no place at all ("In-Office", "Flexible - Any Site")."""
        outside = skip(f"location outside allowed countries: {place}")
        words = word_text(place)
        parts = [p.strip() for p in place.split(",") if p.strip()]

        # "City, Region, CC": a trailing country code is the country, so "Bengaluru, KA, IN" is India. Not when it is
        # only a state ("Remote: CA, NY, TX", "New York, New York, NY"), or when the part before it is an allowed
        # country's region ("San Francisco, California, CA").
        if len(parts) >= 3 and _is_code(parts[-1]):
            code = CODE_ALIASES.get(parts[-1].upper(), parts[-1].upper())
            if code in self.allowed:
                return KEEP
            region = parts[-2].upper()
            in_allowed_region = any(region in spec["region_codes"] or has_term(word_text(parts[-2]), name)
                                    for spec in self.allowed.values() for name in spec["names"]
                                    if len(name.replace(".", "")) > 2)
            if code in BUILTIN_COUNTRIES and not in_allowed_region:
                return outside

        if self._foreign_city_with_shared_code(place):
            return outside
        if has_term(words, "georgia") and any(has_term(words, c) for c in GEORGIA_COUNTRY_CITIES) \
                and "GE" not in self.allowed \
                and not any(has_term(words, n) for spec in self.allowed.values() for n in spec["names"] if n != "georgia"):
            return outside

        # Allowed places are checked before other places, so "Albuquerque, New Mexico" is not taken for Mexico.
        codes = _region_codes(place)
        for spec in self.allowed.values():
            if any(has_term(words, n) for n in spec["names"]) or codes & set(spec["region_codes"]):
                return KEEP
        if any(has_term(words, other) for other in self.others):
            return outside
        if any(codes & set(spec["region_codes"]) for code, spec in BUILTIN_COUNTRIES.items() if code not in self.allowed):
            return outside              # "Halifax, NS": a region of a country that is not allowed

        if has_term(words, "remote") or has_term(words, "anywhere"):
            # In prose a bare "us" is almost always the pronoun ("join us"), so only explicit names count here.
            text = word_text(f"{job.title} {(job.description or '')[:1200]}")
            for spec in self.allowed.values():
                if any(has_term(text, n) for n in spec["names"] if len(n.replace(".", "")) > 2):
                    return KEEP
            return skip(f"remote role without an allowed country named: {place}")
        if "," in place:
            return skip(f"location not recognised: {place}")    # "Cajamarca, Peru": a place we do not know
        return None

    def _from_title_and_description(self, job: Job, location: str) -> FilterResult:
        """The location field is empty or names no place: the title ("Intern (2027) - Austin, TX"), then the
        description. Still undecided, it goes to System One's location question when one is set up."""
        for text in (job.title or "", (job.description or "")[:2000]):
            words, codes = word_text(text), _city_code_pairs(text)
            for spec in self.allowed.values():
                if any(has_term(words, n) for n in spec["names"] if len(n.replace(".", "")) > 2) \
                        or codes & set(spec["region_codes"]):
                    return KEEP
            if any(has_term(words, other) for other in self.others):
                return skip(f"location outside allowed countries: {location or 'none given'} ({text[:60]})")
        if not location:
            return skip("no location given, and no place named in the title or description")
        return skip(f"no place named in the location ({location}), title or description")


def _location_state(job: Job) -> dict:
    """The title, location, the opening of the description and its lines about offices, remote work or eligibility."""
    description = job.description or ""
    lines = [description[:600]]
    for line in description[600:].split("\n"):
        if set(word_text(line).split()) & LOCATION_WORDS:
            lines.append(line.strip())
    return {"job": {"title": job.title, "company": job.company, "location": job.location or "",
                    "description": "\n".join(lines)[:EXCERPT_CHARS]}}


def _is_code(part: str) -> bool:
    """A two-letter code as boards write it: "CA", "TX", "IN" (upper case, so "In-Office" is not Indiana)."""
    part = part.strip().strip("()")
    return len(part) == 2 and part.isalpha() and part.isupper()


def _city_and_code(place: str) -> tuple[str, str] | None:
    """("Toronto", "CA") from "Toronto, CA (Hybrid)", and ("Berlin", "DE") from "DE - Berlin"; anything may follow."""
    head, comma, rest = place.partition(",")
    if comma and "/" not in head and "(" not in head:
        code = rest.split()[0].strip(".,;:()/") if rest.split() else ""
        if len(code) == 2 and code.isalpha():
            return head.strip(), code
    code, dash, rest = place.partition("-")
    if dash and len(code.strip()) == 2 and code.strip().isalpha():
        return rest.split(",")[0].split("/")[0].split("(")[0].strip(), code.strip()
    return None


def _region_codes(text: str) -> set[str]:
    """Codes after a comma ("Austin, TX", "Hawthorne, CA and …"), before a dash ("CA - San Francisco") or after one at
    the end ("Jacksonville - FL")."""
    codes = set()
    for segment in text.split(",")[1:]:
        first = segment.split()[0].strip(".;:()") if segment.split() else ""
        if _is_code(first):
            codes.add(first)
    for line in text.splitlines():
        lead = line.split("-", 1)[0].strip() if "-" in line else ""
        tail = line.rsplit("-", 1)[1].strip() if "-" in line else ""
        for code in (lead, tail):
            if _is_code(code):
                codes.add(code)
    return codes


def _city_code_pairs(text: str) -> set[str]:
    """Codes in prose only where they follow a city: "Austin, TX and Denver, CO" gives TX and CO; "Jira, MS Teams" gives
    nothing, because a capitalised word after the code makes it part of a name."""
    codes = set()
    segments = text.split(",")
    for before, segment in zip(segments, segments[1:]):
        words = segment.split()
        if not words or not before.split() or not before.split()[-1][:1].isupper():
            continue
        code = words[0].rstrip(".;:)")
        following = words[1] if len(words) > 1 and words[0] == code else ""
        if _is_code(code) and not following[:1].isupper():
            codes.add(code)
    return codes


def _places(location: str) -> list[str]:
    """The places in a location field: split at ";", "|", " / " and line breaks, at "/" between two "City, ST"
    places ("Sunnyvale, CA/Toronto, ON"), and "New York, NY, San Francisco, CA" or "Austin, TX, US, Toronto, ON, CA"
    into their places."""
    chunks = [location]
    for separator in (";", "|", " / ", "\n"):
        chunks = [piece for chunk in chunks for piece in chunk.split(separator)]
    chunks = [piece for chunk in chunks
              for piece in (chunk.split("/") if all("," in p for p in chunk.split("/")) else [chunk])]
    places = []
    for chunk in (c.strip() for c in chunks):
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(",")]
        if len(parts) >= 4 and len(parts) % 2 == 0 and all(_is_code(parts[k]) for k in range(1, len(parts), 2)):
            places += [f"{parts[k]}, {parts[k + 1]}" for k in range(0, len(parts), 2)]
        elif len(parts) >= 6 and len(parts) % 3 == 0 and all(_is_code(parts[k]) for k in range(len(parts)) if k % 3):
            places += [", ".join(parts[k:k + 3]) for k in range(0, len(parts), 3)]
        else:
            places.append(chunk)
    return places or [location]

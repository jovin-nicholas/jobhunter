import unittest

from jobhunter.errors import SettingsError
from jobhunter.filters import build_filters
from jobhunter.filters.keywords import KeywordFilter
from jobhunter.filters.location import LocationFilter
from jobhunter.filters.seniority import SeniorityFilter, required_years
from jobhunter.models import Job
from jobhunter.settings import (FilterSettings, KeywordFilterSettings, LocationFilterSettings, RoleGroup,
                                SeniorityFilterSettings)


def job(title="Software Engineer", location="Austin, TX", description="", company="Acme"):
    return Job("t_1", title, company, location, "https://example.com/1", description=description)


class TestLocation(unittest.TestCase):
    us = LocationFilter(LocationFilterSettings(["US"]))

    def test_pronoun_us_in_the_description_is_not_a_us_signal(self):
        self.assertFalse(self.us.check(job(location="Toronto, ON", description="Come join us")).keep)
        self.assertFalse(self.us.check(job(location="London", description="About us")).keep)

    def test_trailing_country_code_after_a_region_is_the_country(self):
        self.assertFalse(self.us.check(job(location="Bengaluru, KA, IN")).keep)
        self.assertTrue(self.us.check(job(location="Austin, TX, US")).keep)

    def test_us_locations_pass(self):
        for loc in ("Austin, TX", "Indianapolis, IN", "United States", "Remote - US",
                    "Irvine, California, USA", "New York, NY 10001", "CA - San Francisco"):
            with self.subTest(loc=loc):
                self.assertTrue(self.us.check(job(location=loc)).keep)

    def test_remote_needs_explicit_country_wording(self):
        self.assertTrue(self.us.check(job(location="Remote", description="Open to people in the United States only")).keep)
        self.assertFalse(self.us.check(job(location="Remote", description="Tell us about yourself")).keep)

    def test_named_other_countries_and_missing_location_are_skipped(self):
        self.assertFalse(self.us.check(job(location="Remote, Canada")).keep)
        self.assertEqual(self.us.check(job(location="")).reason,
                         "no location given, and no place named in the title or description")

    def test_an_empty_location_is_read_from_the_title_then_the_description(self):
        self.assertTrue(self.us.check(job(location="", description="Our office is in Austin, TX, United States.")).keep)
        self.assertTrue(self.us.check(job("Software Engineer - Austin, TX", location="")).keep)
        result = self.us.check(job(location="", description="Our office is in Berlin, Germany."))
        self.assertFalse(result.keep)
        self.assertTrue(result.reason.startswith("location outside allowed countries"), result.reason)

    def test_an_empty_location_with_no_place_anywhere_asks_system_one(self):
        class Model:
            def __init__(self, p):
                self.p, self.calls = p, []

            def noul(self, name, state, question):
                self.calls.append(name)
                return self.p

        for p, keep in ((0.9, True), (0.1, False)):
            with self.subTest(p=p):
                model = Model(p)
                result = LocationFilter(LocationFilterSettings(["US"]), model=model).check(
                    job(location="", description="A great team."))
                self.assertEqual(model.calls, ["in_allowed_country"])
                self.assertEqual(result.keep, keep)

    def test_other_built_in_and_custom_countries(self):
        ca = LocationFilter(LocationFilterSettings(["CA"]))
        self.assertTrue(ca.check(job(location="Toronto, ON")).keep)
        self.assertFalse(ca.check(job(location="Austin, TX")).keep)
        nl = LocationFilter(LocationFilterSettings([{"code": "NL", "names": ["netherlands", "amsterdam"]}]))
        self.assertTrue(nl.check(job(location="Amsterdam")).keep)

    def test_unknown_country_code_is_a_settings_problem(self):
        with self.assertRaises(SettingsError) as caught:
            build_filters(FilterSettings(location=LocationFilterSettings(["ZZ"])))
        self.assertIn("ZZ", caught.exception.problems[0])


class TestSeniority(unittest.TestCase):
    f = SeniorityFilter(SeniorityFilterSettings(["intern", "entry"], max_years_required=3))

    def test_title_levels(self):
        self.assertFalse(self.f.check(job("Senior Software Engineer")).keep)
        self.assertFalse(self.f.check(job("Staff Engineer")).keep)
        self.assertFalse(self.f.check(job("Software Engineer II")).keep)
        self.assertTrue(self.f.check(job("Software Engineer I")).keep)
        self.assertTrue(self.f.check(job("New Grad Software Engineer")).keep)
        self.assertTrue(self.f.check(job("Software Engineer")).keep)

    def test_years_required(self):
        self.assertFalse(self.f.check(job(description="Requires 5+ years of software experience.")).keep)
        self.assertFalse(self.f.check(job(description="4-6 years of experience")).keep)
        self.assertTrue(self.f.check(job(description="1-3 years of experience with Python")).keep)
        self.assertEqual(required_years("minimum 7 years of engineering, or 2 years of experience with Go"), 2)
        self.assertIsNone(required_years("We value curiosity"))


class TestKeywords(unittest.TestCase):
    f = KeywordFilter(KeywordFilterSettings(
        exclude_title=["manager"], exclude_phrases=["security clearance"], exclude_companies=["spam staffing"],
        exclude_roles={"embedded": RoleGroup(["c++", "rtos", "firmware", "microcontroller"], 2)}))

    def test_title_phrase_and_company(self):
        self.assertIn("manager", self.f.check(job("Engineering Manager")).reason)
        self.assertIn("security clearance", self.f.check(job(description="Active Security Clearance required")).reason)
        self.assertIn("spam staffing", self.f.check(job(company="Spam Staffing LLC")).reason)
        self.assertTrue(self.f.check(job("Management Tools Engineer")).keep)

    def test_role_group_threshold(self):
        self.assertTrue(self.f.check(job(description="Full-stack role; C++ a plus")).keep)
        result = self.f.check(job(description="C++ on RTOS, writing firmware"))
        self.assertFalse(result.keep)
        self.assertEqual(result.reason, "excluded role 'embedded': c++, rtos, firmware")


class TestBuildFilters(unittest.TestCase):
    def test_order_is_location_seniority_keywords(self):
        filters = build_filters(FilterSettings(LocationFilterSettings(["US"]), SeniorityFilterSettings(["entry"]),
                                               KeywordFilterSettings()))
        self.assertEqual([f.name for f in filters], ["location", "seniority", "keywords"])
        self.assertEqual(build_filters(FilterSettings()), [])



class TestLocationReviewFixes(unittest.TestCase):
    us = LocationFilter(LocationFilterSettings(["US"]))

    def test_full_state_names_and_major_cities_are_us(self):
        for loc in ("Albuquerque, New Mexico", "Albuquerque, New Mexico, United States", "Austin, Texas",
                    "San Francisco", "New York City", "Seattle"):
            with self.subTest(loc=loc):
                self.assertTrue(self.us.check(job(location=loc)).keep)

    def test_mexico_itself_is_still_outside(self):
        self.assertFalse(self.us.check(job(location="Mexico City, Mexico")).keep)


class TestSharedCodes(unittest.TestCase):
    """Two-letter codes that are both a US state and another country: CA (Canada), IN (India), DE (Germany)."""

    def check(self, location, countries=("US",)):
        from jobhunter.filters.location import LocationFilter
        from jobhunter.settings import LocationFilterSettings
        job = Job("x", "Software Engineer", "Acme", location, "https://example.com")
        return LocationFilter(LocationFilterSettings(list(countries))).check(job).keep

    def test_a_foreign_city_with_a_shared_code_is_outside_the_us(self):
        for location in ("Toronto, CA", "Hyderabad, IN", "Munich, DE", "DE - Berlin", "Bangalore, IN", "Tbilisi, Georgia"):
            self.assertFalse(self.check(location), location)

    def test_us_places_with_those_codes_are_kept(self):
        for location in ("San Jose, CA", "Indianapolis, IN", "Wilmington, DE", "CA - San Francisco", "Atlanta, Georgia",
                         "Hamburg, NY", "Paris, TX", "Albuquerque, New Mexico", "London, KY"):
            self.assertTrue(self.check(location), location)

    def test_the_foreign_city_counts_when_that_country_is_allowed(self):
        self.assertTrue(self.check("Toronto, CA", countries=("CA",)))
        self.assertTrue(self.check("Hyderabad, IN", countries=("IN",)))


class TestSharedCodesMore(unittest.TestCase):
    def check(self, location):
        from jobhunter.filters.location import LocationFilter
        from jobhunter.settings import LocationFilterSettings
        return LocationFilter(LocationFilterSettings(["US"])).check(
            Job("x", "Software Engineer", "Acme", location, "https://example.com")).keep

    def test_more_foreign_forms(self):
        for location in ("Toronto, CA (Hybrid)", "Toronto, CA / Remote", "Toronto, CA, Canada", "Muenchen, DE",
                         "Chennai, TN", "Chennai, TN, IND"):
            self.assertFalse(self.check(location), location)

    def test_us_places_stay(self):
        for location in ("San Jose, CA (Hybrid)", "Nashville, TN", "Atlanta, Georgia; Tbilisi, Georgia",
                         "Delhi, NY", "Waterloo, IA", "Berlin, NH"):
            self.assertTrue(self.check(location), location)


class TestRequiredYearsFromRealPostings(unittest.TestCase):
    """Sentences from the 2026-09-29 comparison run: requirements the old patterns missed, and company history they
    took for a requirement."""

    def test_numbers_that_are_not_amounts_are_ignored(self):
        for text in ("Call 555.010.0199 for 3 years of experience details.", "Area: 5,000 m² and 2⁵ years",
                     "Version 1.2.3; 10..12 years"):
            required_years(text)                    # does not raise
        self.assertEqual(required_years("Call 555.010.0199. Requires 3 years of experience."), 3)

    def test_requirements_are_read(self):
        cases = {
            "5+ years of hands-on software development experience with Java.": 5,
            "8+ years of hands-on software engineering and full-stack development.": 8,
            "You have 5+ years of experience in software engineering": 5,
            "Experience: 15+ Years": 15,
            "8\u00e2\u0080\u009310+ years of professional experience in backend software engineering": 8,
            "Experience: 3-10 years building 0?1 products": 3,
            "At least two years of professional experience with Python": 2,
            "1.5 years of experience with React": 1,
            "4+ years of software, infrastructure, platform, or DevOps engineering experience": 4,
            "5+ years software development experience at a venture-backed startup or top technology firm": 5,
            "We're targeting a Senior backend engineer with 6+ years of experience or an equivalent track record": 6,
            "Minimum 5 years of dedicated professional experience in a Data Engineering role, preferably within a "
            "startup": 5,
            "4+ years of experience in financial services, preferably with pricing": 4,
            "8+ years of experience in a client-facing role ideally customer success": 8,
            "Bachelor's degree and 3+ years of experience": 3,
            "Bachelor's degree or equivalent experience with 5+ years of software engineering experience": 5,
            "Bachelor's degree or equivalent practical experience with 8+ years of software engineering experience, "
            "or Master's degree with 6+ years of experience": 6,
        }
        for text, expected in cases.items():
            self.assertEqual(required_years(text), expected, text)

    def test_company_history_and_other_years_are_not_requirements(self):
        for text in ("With 40+ years of experience in the Insurtech game, we\u2019re not just building software.",
                     "A staffing, consulting, and technology solutions company with more than 28 years of experience "
                     "helping organizations build teams.",
                     "BCBSNE has celebrated 80+ years of Nebraskans serving Nebraskans.",
                     "Applicants must be 18 years or older.",
                     "Our 401(k) vests over 4 years.",
                     "Founded 25 years ago, we have grown to 400 people.",
                     "With over 100 years of experience and colleagues in over 30 countries, Fitch Group's culture of "
                     "credibility is embedded throughout its structure.",
                     "Bachelor's degree or equivalent (minimum 12 years) work experience",
                     "(If Associate's Degree, must have minimum 6 years work experience)"):
            self.assertIsNone(required_years(text), text)

    def test_several_requirements_take_the_highest_alternatives_the_lowest(self):
        self.assertEqual(required_years("7+ years of Java development. 3+ years of experience with Spring."), 7)
        self.assertEqual(required_years("5 years of experience, or 2 years of experience with a Master's degree."), 2)
        # Two bullets that lost their line break are both required, not alternatives.
        self.assertEqual(required_years("Experience - 8+ years of software engineering experience building systems "
                                        "2+ years of recent experience building low latency trading systems"), 8)

    def test_preferred_years_are_not_required(self):
        self.assertIsNone(required_years("5+ years of experience preferred."))
        self.assertEqual(required_years("2+ years of experience required; 6+ years of experience is a plus."), 2)

    def test_the_filter_uses_it(self):
        f = SeniorityFilter(SeniorityFilterSettings(["intern", "entry", "mid", "senior"], 3))
        self.assertFalse(f.check(job(description="Qualifications\n- 5+ years of hands-on software development experience")).keep)
        self.assertTrue(f.check(job(description="With 40+ years of experience in the Insurtech game, we're hiring "
                                                "new grads.")).keep)


class TestLocationPlaces(unittest.TestCase):
    us = LocationFilter(LocationFilterSettings(["US"]))

    def test_a_list_of_places_is_kept_when_any_place_is_allowed(self):
        for location in ("Los Angeles, CA; New York, NY; San Francisco, CA", "New York, NY, San Francisco, CA",
                         "Toronto, ON; Austin, TX", "Seattle, WA | Vancouver, BC"):
            self.assertTrue(self.us.check(job(location=location)).keep, location)
        for location in ("Toronto, ON; Vancouver, BC", "Toronto, Ontario, CA", "Bengaluru, KA, IN"):
            self.assertFalse(self.us.check(job(location=location)).keep, location)

    def test_a_location_that_names_no_place_falls_back_to_the_title_then_the_description(self):
        self.assertTrue(self.us.check(job(title="Software Engineer Intern (2027) - Austin, TX", location="In-Office")).keep)
        self.assertTrue(self.us.check(job(location="Flexible - Any SpaceX Site",
                                          description="Roles are based in Hawthorne, CA and Starbase, TX.")).keep)
        result = self.us.check(job(location="Flexible - Any SpaceX Site", description="Build rockets."))
        self.assertFalse(result.keep)
        self.assertIn("no place", result.reason)
        self.assertFalse(self.us.check(job(title="Engineer - Berlin", location="Hybrid")).keep)
        # "City, Country" is a place even when the country is not in the built-in data: no fallback.
        self.assertFalse(self.us.check(job(location="Cajamarca, Peru", description="Work with clients across the United States.")).keep)


if __name__ == "__main__":
    unittest.main()


class TestRequiredYearsReviewCases(unittest.TestCase):
    """Inputs from the code review of the sentence parser."""

    def test_years_under_a_preferred_heading_are_not_required(self):
        self.assertEqual(required_years("BASIC QUALIFICATIONS\n- 1+ years of software development experience\n"
                                        "PREFERRED QUALIFICATIONS\n- 5+ years of software development experience"), 1)
        self.assertEqual(required_years("Preferred Qualifications\n- 5+ years of experience with Go\n"
                                        "Required Qualifications\n- 2+ years of experience with Python"), 2)
        self.assertEqual(required_years("Nice to have:\n- 6+ years of experience\nWhat you'll bring:\n"
                                        "- 3 years of experience"), 3)
        # A short bullet under the preferred heading does not end the section.
        self.assertIsNone(required_years("Bonus points:\n- Kafka\n- 5+ years of experience with Go"))
        # A bullet that mentions "preferred" is not a heading.
        self.assertEqual(required_years("Requirements\n- Identity tools (Saviynt exposure preferred)\n"
                                        "- 7+ years in Site Reliability Engineering"), 7)
        self.assertIsNone(required_years("Certifications (Preferred)\n- 5+ years of experience with AWS"))
        # "NICE" the product is not "nice to have"; a bullet opening with "basic" is not a heading.
        self.assertEqual(required_years("NICE CXone Expertise:\n- 5+ years of hands-on experience with NICE CXone"), 5)
        self.assertEqual(required_years("Qualifications\n- basic secure coding techniques a plus\n"
                                        "- Must possess a minimum of 3 years' experience programming"), 3)

    def test_degree_or_years_stand_in_for_the_degree(self):
        self.assertIsNone(required_years("Bachelor's degree or a minimum of 4 years of experience developing software"))
        self.assertEqual(required_years("Bachelor's degree or equivalent experience with 5+ years of experience"), 5)
        self.assertEqual(required_years("Bachelor's degree in Computer Science or related field, 3+ years of experience"), 3)

    def test_company_history_in_the_mention_s_own_clause(self):
        for text in ("With 15+ years of experience in the Insurtech game, we're looking for engineers.",
                     "We've been building software for 15 years.",
                     "You'll join a company with 18 years of engineering excellence."):
            self.assertIsNone(required_years(text), text)
        self.assertEqual(required_years("We've been building software for 15 years. You need 3+ years experience."), 3)

    def test_an_alternative_without_an_experience_word_still_counts(self):
        self.assertEqual(required_years("5 years of experience with a BS or 3 years with an MS."), 3)

    def test_plus_and_nice_earlier_in_the_sentence_do_not_cancel_a_requirement(self):
        self.assertEqual(required_years("BS plus 5 years of experience"), 5)
        self.assertEqual(required_years("Plus, you need 5 years of experience."), 5)
        self.assertIsNone(required_years("Ideally 5 years of experience."))

    def test_requirement_words_stand_in_for_experience(self):
        self.assertEqual(required_years("Minimum 8 years in software."), 8)
        self.assertEqual(required_years("Requires 10+ years."), 10)
        self.assertEqual(required_years("Experience Level: Mid (3-5 years)"), 3)


class TestLocationListFormats(unittest.TestCase):
    us = LocationFilter(LocationFilterSettings(["US"]))

    def test_city_region_country_triples_and_slashes(self):
        for location in ("Austin, TX, US, Toronto, ON, CA", "Sunnyvale, CA/Toronto, ON", "Toronto, ON/Sunnyvale, CA"):
            self.assertTrue(self.us.check(job(location=location)).keep, location)
        for location in ("Toronto, ON, CA, Vancouver, BC, CA", "Toronto, ON/Vancouver, BC"):
            self.assertFalse(self.us.check(job(location=location)).keep, location)

    def test_the_fallback_reads_only_city_and_code_pairs(self):
        self.assertFalse(self.us.check(job(location="In-Office",
                                           description="We are hiring in London. Tools: Jira, MS Teams")).keep)
        self.assertTrue(self.us.check(job(location="In-Office",
                                          description="The office is in Austin, TX and Denver, CO.")).keep)

    def test_city_dash_state(self):
        self.assertTrue(self.us.check(job(location="Jacksonville - FL")).keep)
        self.assertFalse(self.us.check(job(location="Vancouver - BC")).keep)

    def test_a_trailing_state_code_is_not_a_country(self):
        for location in ("Remote - USA (Must reside in CA, OR, WA)", "Remote - US (CA, NY, TX)", "Remote: CA, NY, TX",
                         "New York, New York, NY", "San Francisco, California, CA", "Wilmington, DE, US"):
            self.assertTrue(self.us.check(job(location=location)).keep, location)
        for location in ("Toronto, Ontario, CA", "Bengaluru, KA, IN", "Munich, Bavaria, DE", "Toronto, ON, CA"):
            self.assertFalse(self.us.check(job(location=location)).keep, location)


class TestRequiredYearsFromLabels(unittest.TestCase):
    """Good jobs the years filter dropped, found by checking it against hand labels."""

    def test_a_range_whose_dash_was_lost(self):
        self.assertEqual(required_years("3 5 years of professional experience in AI/ML engineering"), 3)
        self.assertEqual(required_years("Experience: 2 4 years"), 2)

    def test_years_after_the_company_s_own_name_are_its_history(self):
        text = "Access to hundreds of clients, most who have been working with Genesis10 for 5-20+ years."
        self.assertIsNone(required_years(text, company="Genesis10"))
        self.assertEqual(required_years("Python AI Engineer. 2+ years of Python experience. " + text,
                                        company="Genesis10"), 2)

    def test_the_filter_passes_the_company(self):
        f = SeniorityFilter(SeniorityFilterSettings(["entry", "mid"], 3))
        description = "Clients have been working with Genesis10 for 5-20+ years."
        self.assertTrue(f.check(job(title="Python AI Engineer", company="Genesis10", description=description)).keep)


class TestRequiredYearsReviewRound(unittest.TestCase):
    """Common requirement wording the parser missed, and company history it took for a requirement."""

    def test_required_after_the_number(self):
        self.assertEqual(required_years("5 years experience preferred; 2 years required"), 2)
        self.assertEqual(required_years("3 years required."), 3)

    def test_a_years_of_experience_label(self):
        self.assertEqual(required_years("Years of experience: 4"), 4)
        self.assertEqual(required_years("Minimum years of experience: 4+"), 4)
        self.assertEqual(required_years("Years of relevant experience - 6"), 6)
        self.assertIsNone(required_years("Preferred Qualifications:\nYears of experience: 4"))

    def test_years_of_with_or_in_a_skill(self):
        self.assertEqual(required_years("8+ years of Java"), 8)
        self.assertEqual(required_years("- 7+ years with Go and Kubernetes"), 7)
        self.assertEqual(required_years("10+ years in backend roles"), 10)
        self.assertEqual(required_years("Qualifications:\n- 8+ years of Python"), 8)
        self.assertEqual(required_years("- 5 years of Python"), 5)
        self.assertEqual(required_years("Requirements:\n5 years with AWS"), 5)

    def test_years_that_are_not_the_candidates_experience(self):
        for text in ("Jane Doe is a Top Talent Award winner 12 years in a row",
                     "The platform is the result of 7+ years of DARPA-funded R&D",
                     "- Recognized 8 years in a row as a top employer",
                     "Qualifications:\n- BS in CS\n- 401k matching after 5 years of service",
                     "Requirements:\n- 5 years of Java (preferred)",
                     "Requirements:\n- 5 years of Java, a plus",
                     "Requirements:\n- Python\nBenefits\n- 5 years of tuition support",
                     "Qualifications:\n- Python\nWhy Acme?\n- 6 years of profitable growth",
                     "- 10+ years in a regulated industry is what our clients have",
                     "Named a top place for engineering 8 years in a row"):
            with self.subTest(text=text):
                self.assertIsNone(required_years(text))

    def test_skill_years_that_open_their_clause_still_count(self):
        self.assertEqual(required_years("5+ years in applied computer vision"), 5)
        self.assertEqual(required_years("10+ years in MLOps"), 10)
        self.assertEqual(required_years("- 7+ years with Go and Kubernetes"), 7)
        self.assertEqual(required_years("Must have 6 years of Java"), 6)
        self.assertEqual(required_years("Qualifications:\n- At least 4 years with Go"), 4)

    def test_requirements_missed_in_live_postings(self):
        cases = {
            "<ul><li>7+ years of managing partnerships with enterprise clients</li></ul>": 7,
            "<p>Requirements:</p><ul><li>5 years&nbsp;of Python</li></ul>": 5,
            "5+ years in a SaaS Account Executive role": 5,
            "7+ years in an Ops, DevOps, or SRE role": 7,
            "12+years in the highlighted skill set": 12,
            "Qualifications - 5+ years in QA/SDET roles": 5,
            "What You Bring - 5\u20138+ years building backend services": 5,
            "Requirements: 6 years of Go": 6,
        }
        for text, years in cases.items():
            with self.subTest(text=text):
                self.assertEqual(required_years(text), years)

    def test_html_years_that_are_not_requirements(self):
        self.assertIsNone(required_years("<li>Named a top place to work 8 years in a row</li>"))
        self.assertIsNone(required_years("<li>R&amp;D: 7+ years of the company&#39;s history</li>"))

    def test_without_a_plus_bullet_or_section_a_skill_mention_is_not_a_requirement(self):
        self.assertIsNone(required_years("Our platform has handled 5 years of Java upgrades."))

    def test_company_history_anywhere_in_the_clause_is_not_a_requirement(self):
        self.assertIsNone(required_years("The team has built software for 15 years."))
        self.assertIsNone(required_years("Our company has 12+ years in fintech."))
        self.assertIsNone(required_years("We have shipped software for 9 years."))
        self.assertEqual(required_years("5+ years of software development at a top technology firm"), 5)


class TestRequiredYearsFromBenchmark(unittest.TestCase):
    """Real fits the years filter dropped in the 500-posting benchmark."""

    def test_alternatives_on_separate_lines_joined_by_or(self):
        text = ("Basic Qualifications\nBachelor's degree and 2 years of software development experience\nOR\n"
                "Associate's degree and 6 years of software development experience\nOR\n"
                "High school diploma / GED and 8 years of software development experience")
        self.assertEqual(required_years(text), 2)
        # Without the OR lines they are separate requirements, and the highest counts.
        self.assertEqual(required_years("- 2 years of Python experience\n- 4 years of SQL experience"), 4)

    def test_typical_experience_is_not_a_requirement(self):
        self.assertIsNone(required_years("Typical Skills and Experiences:\n"
                                         "Experience: Typically, six years of experience in technology"))
        self.assertIsNone(required_years("Typically 6+ years of software engineering experience."))
        self.assertEqual(required_years("Typical day: you build APIs. Requires 3+ years of experience."), 3)
        self.assertEqual(required_years("Typically requires 8+ years of experience in the relevant technologies"), 8)



class TestRequiredYearsCompanyAge(unittest.TestCase):
    """ "20+ years in business" is the company's age; a "+" before "years" made it count as a requirement."""

    def test_years_in_business_are_the_companys(self):
        for text in ("Why Acme?\n- 20+ years in business",
                     "- 10+ years in business, Acme is a leader",
                     "- 15+ years in operation",
                     "About us\n- 30+ years in existence."):
            with self.subTest(text=text):
                self.assertIsNone(required_years(text))

    def test_a_field_named_business_still_counts(self):
        self.assertEqual(required_years("Required Qualifications\nMinimum 5 years in business operations, "
                                        "data analytics, BI development"), 5)
        self.assertEqual(required_years("5+ years in business development"), 5)
        self.assertEqual(required_years("5+ years in industry"), 5)

    def test_plus_years_after_a_section_about_the_company_still_count(self):
        # Live postings: the requirements often follow "About the Role", "Our Team" or "Who we are" with no heading.
        self.assertEqual(required_years("About the Role\n10+ years in product management or a related role"), 10)
        self.assertEqual(required_years("Our Team\n10+ years in MLOps, DevOps, or Cloud Infrastructure"), 10)
        self.assertEqual(required_years("Who we are:\n8+ years in social media, creator marketing"), 8)
        self.assertEqual(required_years("Responsibilities:\n- 5+ years of Python"), 5)

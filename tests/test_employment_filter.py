import unittest

from jobhunter.errors import SettingsError
from jobhunter.filters import build_filters
from jobhunter.filters.employment import EmploymentFilter
from jobhunter.models import Job
from jobhunter.settings import EmploymentFilterSettings, FilterSettings, KeywordFilterSettings, load_settings


def job(title="Software Engineer", description="", company="Acme"):
    return Job("t_1", title, company, "Austin, TX", "https://example.com/1", description=description)


def f(exclude=("contract", "hourly"), contract_to_hire="keep", hourly_internships="keep"):
    return EmploymentFilter(EmploymentFilterSettings(list(exclude), contract_to_hire, hourly_internships))


class TestContractTitleWords(unittest.TestCase):
    def test_positive_title_words(self):
        cases = {
            "Contract Software Engineer": "contract",
            "Software Engineer (Contractor)": "contractor",
            "Software Engineer - C2C": "c2c",
            "Software Engineer - Corp to Corp": "corp to corp",
            "Software Engineer - Corp-to-Corp": "corp to corp",
            "Software Engineer (1099 Only)": "1099 only",
            "Software Engineer - W2 Contract": "w2 contract",
            "Software Engineer (Temp)": "temp",
            "Software Engineer - Temp": "temp",
            "Temp Role: Software Engineer": "temp",
            "Software Engineer, Temp Position": "temp",
            "Temporary Software Engineer": "temporary",
            "Freelance Software Engineer": "freelance",
        }
        for title, snippet in cases.items():
            with self.subTest(title=title):
                result = f().check(job(title))
                self.assertFalse(result.keep, title)
                self.assertEqual(result.reason, f"contract: {snippet}")

    def test_words_that_only_look_like_contract_words_are_kept(self):
        for title in ("Contracts Analyst", "Smart Contracts Engineer", "Smart Contract Engineer",
                      "Senior Smart Contract Developer", "Temp Sensor Firmware Engineer", "Temperature Controls Engineer",
                      "Temp Software Engineer"):
            with self.subTest(title=title):
                self.assertTrue(f().check(job(title)).keep, title)


class TestContractDescriptionPhrases(unittest.TestCase):
    def test_positive_phrases(self):
        cases = {
            "This is a contract role for a growing team.": "contract role",
            "We are hiring for a contract position.": "contract position",
            "A great contract opportunity for the right person.": "contract opportunity",
            "This contract assignment runs through the year.": "contract assignment",
            "6-month contract to build a new platform.": "6-month contract",
            "This is a 6 months contract engagement.": "6 months contract",
            "This is a six month contract engagement.": "six month contract",
            "Duration: Contract": "duration: contract",
            "Duration: 6 months contract": "6 months contract",
            "Duration: 6 months (contract)": "duration: 6 months (contract",
            "Duration: 6 months": "duration: 6 months",
            "Duration: 6 Months +": "duration: 6 months +",
            "Duration: 12+ Months with possible extension": "duration: 12+ months",
            "Duration: 3+ Month": "duration: 3+ month",
            "Duration: 6 months. Training provided.": "duration: 6 months",
            "Duration: 6 months\nOnboarding: two weeks": "duration: 6 months",
            "Employment Type: Contract": "employment type: contract",
            "Employment Type: Temporary": "employment type: temporary",
            "Job Type: Contract": "job type: contract",
            "Position Type - Contract": "position type - contract",
            "We need C2C candidates only.": "c2c",
            "Open to Corp to Corp arrangements.": "corp to corp",
            "Paid on 1099 for the first year.": "on 1099",
            "This is a 1099 contract.": "1099 contract",
            "1099 or C2C welcome.": "1099 or c2c",
            "C2C or 1099 welcome.": "c2c or 1099",
            "Work as a contractor with our team.": "as a contractor",
            "This is an independent contractor engagement.": "independent contractor",
            "A contractor role on the payments team.": "contractor role",
            "A contractor position on the payments team.": "contractor position",
            "This is a W2 Contract engagement.": "w2 contract",
            "W2 Only, no exceptions.": "w2 only",
            "Not a 1099 job. Only W2.": "only w2",
            "W2 only, full time with benefits.": "w2 only",
            "Only W2 candidates, no third parties.": "only w2",
        }
        for description, snippet in cases.items():
            with self.subTest(description=description):
                result = f().check(job(description=description))
                self.assertFalse(result.keep, description)
                self.assertEqual(result.reason, f"contract: {snippet}")

    def test_only_w2_in_the_title_skips(self):
        result = f().check(job("Azure Data Engineer (ONLY W2)"))
        self.assertFalse(result.keep)
        self.assertEqual(result.reason, "contract: only w2")

    def test_negative_phrases_are_kept(self):
        for description in ("We manage vendor contracts for clients.", "We build smart contracts on-chain.",
                            "Must honor contractual obligations.", "Experience with government contracts a plus.",
                            "No subcontract work is involved.", "Form 1099 processing for fintech clients.",
                            "Experience with 1099 tax forms.",
                            "Duration: 12 months onboarding program.", "Duration: 6 months of paid training",
                            "Duration: 12 month rotation program", "Duration: 3 months internship",
                            "We pay our subcontractor partners promptly.", "Contractors we work with love us.",
                            "Build smart contract tooling for DeFi."):
            with self.subTest(description=description):
                self.assertTrue(f().check(job(description=description)).keep, description)


class TestContractToHire(unittest.TestCase):
    def test_kept_by_default_unless_another_contract_signal_fires(self):
        for description in ("This role is contract to hire.", "This is a contract-to-hire position.",
                            "CTH opportunity available.", "This is a temp to perm role.",
                            "This is a temp-to-hire role."):
            with self.subTest(description=description):
                self.assertTrue(f(contract_to_hire="keep").check(job(description=description)).keep, description)

    def test_kept_when_a_label_or_duration_names_contract_to_hire(self):
        for description in ("Employment type: Contract to Hire", "Job Type: Contract-to-Hire",
                            "Position type - temp to perm", "Duration: contract to hire",
                            "This is a 6-month contract to hire role.", "Six month contract-to-hire."):
            with self.subTest(description=description):
                self.assertTrue(f(contract_to_hire="keep").check(job(description=description)).keep, description)

    def test_label_naming_contract_to_hire_skips_when_excluded(self):
        result = f(contract_to_hire="exclude").check(job(description="Employment type: Contract to Hire"))
        self.assertFalse(result.keep)
        self.assertEqual(result.reason, "contract: contract to hire")

    def test_a_plain_contract_label_next_to_contract_to_hire_still_skips(self):
        result = f(contract_to_hire="keep").check(
            job(description="Not contract to hire. Employment type: Contract"))
        self.assertFalse(result.keep)
        self.assertEqual(result.reason, "contract: employment type: contract")

    def test_excluded_when_configured(self):
        cases = {
            "This role is contract to hire.": "contract to hire",
            "This is a contract-to-hire position.": "contract to hire",
            "CTH opportunity available.": "cth",
            "This is a temp to perm role.": "temp to perm",
            "This is a temp-to-hire role.": "temp to hire",
        }
        for description, snippet in cases.items():
            with self.subTest(description=description):
                result = f(contract_to_hire="exclude").check(job(description=description))
                self.assertFalse(result.keep, description)
                self.assertEqual(result.reason, f"contract: {snippet}")

    def test_another_contract_signal_still_skips_when_kept(self):
        result = f(contract_to_hire="keep").check(job("Contract to Hire Software Engineer (Contractor)"))
        self.assertFalse(result.keep)
        self.assertEqual(result.reason, "contract: contractor")


class TestHourlyPay(unittest.TestCase):
    def test_positive_amounts(self):
        cases = {
            "Pay: $55/hr": "$55/hr",
            "Pay: $55 / hour": "$55 / hour",
            "Rate: $50-60 per hour": "$50-60 per hour",
            "Rate: $50 - $60/hr": "$50 - $60/hr",
            "Rate: $70.23/hr": "$70.23/hr",
            "This offers an hourly rate of $40.": "hourly rate of $40",
            "$40 hourly, paid weekly.": "$40 hourly",
            "Pay is USD 40 per hour.": "usd 40 per hour",
            "Pay: 40 USD per hour": "40 usd per hour",
        }
        for description, snippet in cases.items():
            with self.subTest(description=description):
                result = f().check(job(description=description))
                self.assertFalse(result.keep, description)
                self.assertEqual(result.reason, f"hourly pay: {snippet}")

    def test_a_rate_in_the_title_needs_no_currency(self):
        for title, snippet in (("Data Engineer 65/hr W2", "65/hr"), ("Java Developer - 70 per hour", "70 per hour"),
                               ("Python Developer $60/hr", "$60/hr")):
            with self.subTest(title=title):
                result = f(exclude=["hourly"]).check(job(title))
                self.assertFalse(result.keep, title)
                self.assertEqual(result.reason, f"hourly pay: {snippet}")
        self.assertTrue(f(exclude=["hourly"]).check(job("Platform Engineer", description="Pay: 50/hr")).keep)
        self.assertTrue(f(exclude=["hourly"]).check(job("Backend Engineer 24/7 On-call")).keep)

    def test_boilerplate_without_a_number_is_kept(self):
        for description in ("Please review the salary or hourly rate offered.",
                            "This posting lists a base hourly rate or base annual full-time salary.",
                            "Pay shown is an annualized hourly rate.",
                            "The hourly rate or salary will be discussed in the interview.",
                            "Employees may be paid hourly or salaried.",
                            "Our engine processes 10,000/hr transactions.", "We hold a 500 per hour SLA.",
                            "Salary $150k with overtime of 1.5 per hour.", "Pay: 50/hr",
                            "PTO accrues at 1.54 per hour worked.", "Join the on-call 1/hr rotation."):
            with self.subTest(description=description):
                self.assertTrue(f().check(job(description=description)).keep, description)

    def test_internships_are_kept_when_hourly_internships_is_keep(self):
        for title in ("Software Engineering Intern", "Summer Internship - Backend", "Engineering Co-op"):
            with self.subTest(title=title):
                result = f(hourly_internships="keep").check(job(title, description="Pay: $25/hr"))
                self.assertTrue(result.keep, title)

    def test_internships_are_excluded_when_hourly_internships_is_exclude(self):
        result = f(hourly_internships="exclude").check(job("Software Engineering Intern", description="Pay: $25/hr"))
        self.assertFalse(result.keep)
        self.assertEqual(result.reason, "hourly pay: $25/hr")


class TestExcludeToggle(unittest.TestCase):
    def test_exclude_contract_only_ignores_hourly(self):
        only_contract = f(exclude=("contract",))
        self.assertTrue(only_contract.check(job(description="Pay: $55/hr")).keep)
        self.assertFalse(only_contract.check(job("Contract Software Engineer")).keep)

    def test_exclude_hourly_only_ignores_contract(self):
        only_hourly = f(exclude=("hourly",))
        self.assertTrue(only_hourly.check(job("Contract Software Engineer")).keep)
        self.assertFalse(only_hourly.check(job(description="Pay: $55/hr")).keep)

    def test_contract_checked_before_hourly(self):
        result = f().check(job("Contract Software Engineer", description="Pay: $55/hr"))
        self.assertEqual(result.reason, "contract: contract")


class TestSettingsValidation(unittest.TestCase):
    def test_exclude_is_required_and_non_empty(self):
        with self.assertRaises(SettingsError) as caught:
            parse_employment_settings({})
        self.assertTrue(any("needs at least one of" in p for p in caught.exception.problems))

    def test_exclude_only_allows_contract_and_hourly(self):
        with self.assertRaises(SettingsError) as caught:
            parse_employment_settings({"exclude": ["contract", "freelance"]})
        self.assertTrue(any("unknown value 'freelance'" in p for p in caught.exception.problems))

    def test_contract_to_hire_and_hourly_internships_only_allow_keep_or_exclude(self):
        with self.assertRaises(SettingsError) as caught:
            parse_employment_settings({"exclude": ["contract"], "contract_to_hire": "maybe"})
        self.assertTrue(any("contract_to_hire" in p and "expected keep or exclude" in p
                           for p in caught.exception.problems))
        with self.assertRaises(SettingsError) as caught:
            parse_employment_settings({"exclude": ["hourly"], "hourly_internships": "maybe"})
        self.assertTrue(any("hourly_internships" in p and "expected keep or exclude" in p
                           for p in caught.exception.problems))

    def test_valid_settings_parse(self):
        settings = parse_employment_settings({"exclude": ["contract", "hourly"], "contract_to_hire": "exclude",
                                              "hourly_internships": "exclude"})
        self.assertEqual(settings.exclude, ["contract", "hourly"])
        self.assertEqual(settings.contract_to_hire, "exclude")
        self.assertEqual(settings.hourly_internships, "exclude")


def parse_employment_settings(employment_raw: dict) -> EmploymentFilterSettings:
    """Parses a `filters.employment` mapping through the real settings loader, using a tmp settings file."""
    import tempfile
    from pathlib import Path

    import yaml

    raw = {
        "resumes": {"folder": "resumes", "default": "resume.pdf"},
        "search": {"queries": ["software engineer"], "locations": ["United States"]},
        "filters": {"employment": employment_raw},
        "scorers": ["ollama"],
    }
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "resumes").mkdir()
        (base / "resumes" / "resume.pdf").write_text("resume")
        path = base / "jobhunter.yaml"
        path.write_text(yaml.safe_dump(raw))
        settings = load_settings(path, env={})
        return settings.filters.employment


class TestBuildFiltersOrder(unittest.TestCase):
    def test_employment_runs_after_keywords_and_before_systemone(self):
        filters = build_filters(FilterSettings(keywords=KeywordFilterSettings(),
                                               employment=EmploymentFilterSettings(["contract", "hourly"])))
        self.assertEqual([x.name for x in filters], ["keywords", "employment"])


class TestContractWordsAboutTheProduct(unittest.TestCase):
    """ "on 1099", "independent contractor" and "as a contractor" skipped jobs whose product handles 1099s or
    contractors. They now need wording that puts the candidate in the arrangement."""

    def test_product_wording_is_kept(self):
        for description in ("Build tools for reporting on 1099 forms.",
                            "You will be working on 1099-NEC filings for clients.",
                            "Our platform supports independent contractor onboarding.",
                            "We help freelancers work as a contractor or employee.",
                            "Payroll for teams that pay independent contractors and employees."):
            with self.subTest(description=description):
                self.assertTrue(f().check(job(description=description)).keep, description)

    def test_the_candidates_arrangement_still_skips(self):
        cases = {
            "You will be paid on a 1099 basis.": "on 1099",
            "This role is on 1099.": "on 1099",
            "You will be hired as an independent contractor.": "as a contractor",
            "You'll work as a contractor for six months.": "as a contractor",
            "Engaged as a contractor through our agency.": "as a contractor",
            "This is an independent contractor position.": "contractor position",
            "Independent contractor role, fully remote.": "contractor role",
            "Hired on an independent contractor basis.": "independent contractor",
            "Employment Type: Independent Contractor": "employment type: independent contractor",
        }
        for description, snippet in cases.items():
            with self.subTest(description=description):
                result = f().check(job(description=description))
                self.assertFalse(result.keep, description)
                self.assertEqual(result.reason, f"contract: {snippet}")


class TestHourlyAmountsThatAreNotPay(unittest.TestCase):

    def test_large_amounts_per_hour_are_throughput(self):
        for description in ("Our system processes $10,000/hr of transactions.",
                            "We settle 10,000 USD per hour in payments.",
                            "Ads spend of $2,500 per hour at peak."):
            with self.subTest(description=description):
                self.assertTrue(f().check(job(description=description)).keep, description)

    def test_pay_rates_still_skip(self):
        for description, snippet in {"Pay: $55/hr": "$55/hr", "Rate: $150/hr for senior consultants": "$150/hr",
                                     "USD 300 per hour": "usd 300 per hour"}.items():
            with self.subTest(description=description):
                self.assertEqual(f().check(job(description=description)).reason, f"hourly pay: {snippet}")


if __name__ == "__main__":
    unittest.main()

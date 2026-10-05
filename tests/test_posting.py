import unittest
from types import SimpleNamespace

from jobhunter.posting import SECTIONS, condense, is_heading, section_kind, split_posting

HEADED = """About Acme
Acme builds payment software for small shops.

What you'll do:
Build and run backend services in Java.
Own features from design to production.

Requirements
3+ years of experience with Java or Python.
Bachelor's degree in Computer Science or similar.

Nice to have
Kubernetes in production.

Benefits
Medical, dental and vision insurance.
Acme is an equal opportunity employer and considers all applicants without regard to race or religion.
#LI-Hybrid"""


def job(description, location="Austin, TX"):
    return SimpleNamespace(title="Backend Engineer", company="Acme", location=location, description=description)


class TestHeadings(unittest.TestCase):
    def test_heading_shapes(self):
        for line in ("Requirements", "What you’ll do:", "WHAT WE OFFER", "## The role", "**Nice to have**"):
            self.assertTrue(is_heading(line), line)

    def test_not_headings(self):
        for line in ("#LI-Hybrid", "USD", "CI/CD", "Email: jobs@example.com", "R",
                     "Build and run backend services in Java.", "Contact our recruiting team for details"):
            self.assertFalse(is_heading(line), line)

    def test_kinds(self):
        self.assertEqual(section_kind("What you’ll do:"), "responsibilities")
        self.assertEqual(section_kind("Basic Qualifications"), "requirements")
        self.assertEqual(section_kind("What you’ll need"), "requirements")
        self.assertEqual(section_kind("Preferred Qualifications"), "preferred")
        self.assertEqual(section_kind("Benefits & Perks"), "drop")
        self.assertEqual(section_kind("About Acme"), "drop")
        self.assertEqual(section_kind("Job Details"), "other")


class TestSplitPosting(unittest.TestCase):
    def test_headed_posting(self):
        p = split_posting(HEADED)
        self.assertEqual(list(p.sections), ["requirements", "preferred", "responsibilities"])
        self.assertIn("3+ years of experience with Java", p.sections["requirements"])
        self.assertIn("Kubernetes in production.", p.sections["preferred"])
        self.assertIn("Own features from design", p.sections["responsibilities"])
        whole = "\n".join(p.sections.values())
        for gone in ("payment software", "dental", "equal opportunity", "#LI-Hybrid"):
            self.assertNotIn(gone, whole)
        self.assertFalse(p.empty_requirements)

    def test_requirement_lines_move_out_of_other_sections(self):
        p = split_posting("The role\nBuild services.\n5+ years of Go required.\nShip weekly.")
        self.assertEqual(p.sections["requirements"], "5+ years of Go required.")
        self.assertEqual(p.sections["responsibilities"], "Build services.\nShip weekly.")

    def test_unheaded_posting_sorts_sentences(self):
        p = split_posting("We build tools for clinics. You need 2+ years of Python experience. "
                          "We offer dental insurance. You will design APIs.")
        self.assertEqual(p.headings, 0)
        self.assertEqual(p.sections["requirements"], "You need 2+ years of Python experience.")
        self.assertIn("You will design APIs.", p.sections["other"])
        self.assertNotIn("dental", "\n".join(p.sections.values()))

    def test_short_phrase_line_is_kept_as_content(self):
        p = split_posting("Requirements:\nJava.\nExperience with Kubernetes\nSQL.")
        self.assertIn("Experience with Kubernetes", p.sections["requirements"])

    def test_unknown_headings_are_counted_and_kept(self):
        p = split_posting("Job Details:\nHybrid, three days a week.\nRequirements:\nPython.")
        self.assertEqual(p.unknown_headings, 1)
        self.assertEqual(p.sections["other"], "Hybrid, three days a week.")

    def test_empty_posting(self):
        p = split_posting("")
        self.assertEqual((p.sections, p.headings, p.empty_requirements), ({}, 0, True))

    def test_narrowed_boilerplate_keeps_health_and_dental_requirement_lines(self):
        p = split_posting("Requirements:\nExperience building health insurance claims systems.\n"
                          "3+ years in dental practice software.")
        whole = "\n".join(p.sections.values())
        self.assertIn("Experience building health insurance claims systems.", whole)
        self.assertIn("3+ years in dental practice software.", whole)

    def test_narrowed_boilerplate_still_drops_the_benefits_line(self):
        p = split_posting("Requirements:\nPython.\nBenefits:\nMedical, dental and vision insurance.")
        self.assertNotIn("Medical, dental and vision insurance.", "\n".join(p.sections.values()))


class TestCondense(unittest.TestCase):
    def test_fields_in_order(self):
        out = condense(job(HEADED))
        self.assertEqual(list(out)[:3], ["title", "company", "location"])
        self.assertEqual([k for k in out if k in SECTIONS], ["requirements", "preferred", "responsibilities"])

    def test_no_location_is_omitted(self):
        self.assertNotIn("location", condense(job("Requirements:\nPython.", location="")))

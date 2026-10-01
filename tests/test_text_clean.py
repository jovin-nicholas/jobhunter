import unittest

from jobhunter.text_clean import repair_text


class TestRepairText(unittest.TestCase):
    def test_utf8_read_as_latin1_or_cp1252_is_decoded_back(self):
        right = "Senior Platform Engineer \u2014 Google Cloud (Caf\u00e9)"
        self.assertEqual(repair_text(right.encode("utf-8").decode("latin-1")), right)
        self.assertEqual(repair_text(right.encode("utf-8").decode("cp1252")), right)

    def test_entities_are_decoded(self):
        self.assertEqual(repair_text("R&amp;D &lt;team&gt;"), "R&D <team>")

    def test_ordinary_text_is_left_alone(self):
        for text in ("Caf\u00e9 d\u00e9j\u00e0 vu \u00e2 la carte", "S\u00e3o Paulo", "plain ascii", ""):
            self.assertEqual(repair_text(text), text)

    def test_none_becomes_empty(self):
        self.assertEqual(repair_text(None), "")


class TestRepairNonText(unittest.TestCase):
    def test_numbers_become_text(self):
        self.assertEqual(repair_text(123), "123")


if __name__ == "__main__":
    unittest.main()

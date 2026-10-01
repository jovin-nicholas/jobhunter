import unittest

from jobhunter.text_match import has_term, word_text


class TestHasTerm(unittest.TestCase):
    def test_whole_words_not_substrings(self):
        words = word_text("We need JavaScript and HTML; results matter; maintain NoSQL")
        for term in ("java", "ml", "ts", "ai", "sql"):
            with self.subTest(term=term):
                self.assertFalse(has_term(words, term))
        self.assertTrue(has_term(words, "javascript"))

    def test_symbols_and_phrases(self):
        words = word_text("C++, Node.js and full-stack work; REST API design.")
        for term in ("c++", "node.js", "full stack", "full-stack", "rest api", "api design"):
            with self.subTest(term=term):
                self.assertTrue(has_term(words, term))


if __name__ == "__main__":
    unittest.main()

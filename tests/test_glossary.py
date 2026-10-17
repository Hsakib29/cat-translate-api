"""Unit tests for glossary protection and restoration (app/utils/glossary.py)."""
import unittest

from app.utils.glossary import protect_terms, restore_terms


class TestGlossaryProtection(unittest.TestCase):
    def test_empty_inputs(self):
        self.assertEqual(protect_terms("", {"Safety": "নিরাপত্তা"}), ("", {}))
        self.assertEqual(protect_terms("Hello", {}), ("Hello", {}))
        self.assertEqual(restore_terms("", {"{0}": "নিরাপত্তা"}), "")
        self.assertEqual(restore_terms("Hello", {}), "Hello")

    def test_longest_match_priority(self):
        glossary = {
            "Safety": "নিরাপত্তা",
            "Safety Coordinator": "নিরাপত্তা সমন্বয়কারী",
            "Coordinator": "সমন্বয়কারী",
        }
        text = "Contact the Safety Coordinator for guidance."
        protected, token_map = protect_terms(text, glossary)
        self.assertEqual(protected, "Contact the {0} for guidance.")
        self.assertEqual(token_map, {"{0}": "নিরাপত্তা সমন্বয়কারী"})

        restored = restore_terms("{0}-এর সাথে যোগাযোগ করুন।", token_map)
        self.assertEqual(restored, "নিরাপত্তা সমন্বয়কারী-এর সাথে যোগাযোগ করুন।")

    def test_word_boundary_isolation(self):
        glossary = {"Cat": "বিড়াল"}
        text = "The Certificate was issued for the Cat."
        protected, token_map = protect_terms(text, glossary)
        # 'Certificate' should NOT have 'Cat' replaced inside it
        self.assertEqual(protected, "The Certificate was issued for the {0}.")
        self.assertEqual(token_map, {"{0}": "বিড়াল"})

    def test_case_insensitive_matching(self):
        glossary = {"Safety Guidelines": "নিরাপত্তা নির্দেশিকা"}
        text = "Please read the safety guidelines carefully."
        protected, token_map = protect_terms(text, glossary)
        self.assertEqual(protected, "Please read the {0} carefully.")
        self.assertEqual(token_map, {"{0}": "নিরাপত্তা নির্দেশিকা"})

    def test_duplicate_terms_share_token(self):
        glossary = {"Module": "মডিউল"}
        text = "Complete Module 1 before starting Module 2."
        protected, token_map = protect_terms(text, glossary)
        self.assertEqual(protected, "Complete {0} 1 before starting {0} 2.")
        self.assertEqual(token_map, {"{0}": "মডিউল"})

        restored = restore_terms("{0} ১ সম্পন্ন করুন {0} ২ শুরু করার আগে।", token_map)
        self.assertEqual(restored, "মডিউল ১ সম্পন্ন করুন মডিউল ২ শুরু করার আগে।")

    def test_restore_with_vernacular_digits_and_spacing(self):
        token_map = {"{0}": "নিরাপত্তা", "{1}": "মডিউল"}
        # Translation models may emit { 0 } or Bengali digits {০}, {১}
        translated = "{ 0 } এবং {১}"
        restored = restore_terms(translated, token_map)
        self.assertEqual(restored, "নিরাপত্তা এবং মডিউল")

    def test_unrelated_tags_preserved(self):
        token_map = {"{0}": "অনুমোদিত"}
        translated = "স্থিতি {0}, ব্যবহারকারী {name}, কোড {99}"
        restored = restore_terms(translated, token_map)
        self.assertEqual(restored, "স্থিতি অনুমোদিত, ব্যবহারকারী {name}, কোড {99}")


if __name__ == "__main__":
    unittest.main()

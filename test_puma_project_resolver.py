import unittest

from puma_project_resolver import (
    clean_subject_project,
    normalize_project_key,
    strip_context,
)


class PumaProjectResolverTests(unittest.TestCase):
    def test_qbo_parent_prefix_and_residence(self):
        self.assertEqual(
            normalize_project_key("Arcadia:Kinney Residence"),
            "kinney",
        )

    def test_residence_is_optional_for_identity(self):
        self.assertEqual(
            normalize_project_key("Harrison Smith"),
            normalize_project_key("Harrison Smith Residence"),
        )

    def test_dock_pickup_context_removed(self):
        self.assertEqual(
            normalize_project_key("Campbell Residence (Dock Pickup)"),
            "campbell",
        )

    def test_housing_sample_context_removed(self):
        self.assertEqual(
            normalize_project_key("Stony Shore Residence (Housing Sample)"),
            "stony shore",
        )

    def test_hyphenated_project_name_preserved(self):
        self.assertEqual(
            normalize_project_key("Bryson-Kleine Residence"),
            "bryson kleine",
        )

    def test_rfps_subject_without_dash(self):
        self.assertEqual(
            clean_subject_project(
                "RFPS Harrison Smith Residence",
                ["RFPS", "Request for Packing Slip"],
            ),
            "Harrison Smith Residence",
        )

    def test_receiving_typo_remains_for_review_not_silently_changed(self):
        # Normalizer cleans structure, but does not invent a corrected spelling.
        self.assertEqual(
            normalize_project_key("Norcorss Receiving Report"),
            "norcorss receiving report",
        )

    def test_context_suffix(self):
        self.assertEqual(
            strip_context("Hopkins Residence - Owner"),
            "Hopkins Residence",
        )


if __name__ == "__main__":
    unittest.main()

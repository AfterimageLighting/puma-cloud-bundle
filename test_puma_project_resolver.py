import unittest

from puma_project_resolver import (
    clean_subject_project,
    normalize_project_key,
    strip_context,
    resolve_existing_tracker,
)


class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeValues:
    def __init__(self, owner):
        self.owner = owner

    def get(self, spreadsheetId=None, range=None):
        if range == "'Open Projects'!A2:A":
            return _FakeRequest({
                "values": [[x] for x in self.owner.open_projects]
            })

        title = ""
        if range and range.startswith("'"):
            title = range.split("'!")[0][1:].replace("''", "'")
        return _FakeRequest({
            "values": self.owner.tracker_rows.get(title, [])
        })


class _FakeSpreadsheets:
    def __init__(self, tracker_rows, open_projects=None):
        self.tracker_rows = tracker_rows
        self.open_projects = open_projects or []

    def get(self, spreadsheetId=None):
        return _FakeRequest({
            "sheets": [
                {"properties": {"title": title}}
                for title in self.tracker_rows
            ]
        })

    def values(self):
        return _FakeValues(self)


class _FakeSheetsService:
    def __init__(self, tracker_rows, open_projects=None):
        self._spreadsheets = _FakeSpreadsheets(
            tracker_rows,
            open_projects=open_projects,
        )

    def spreadsheets(self):
        return self._spreadsheets


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

    def test_one_part_is_suggestion_only(self):
        svc = _FakeSheetsService({
            "Stock - Project Tracker": [
                ["Project", "Source", "Type", "Part Number"],
                ["Stock", "PO Import", "HR.a1", "801.42.041"],
            ]
        })
        result = resolve_existing_tracker(
            svc,
            "TEST",
            "Barb Residence typo",
            parts=["801.42.041"],
        )
        self.assertEqual(result.status, "REVIEW")
        self.assertEqual(result.method, "PART_SUGGESTION_ONLY")

    def test_two_distinct_parts_can_confirm_unique_tracker(self):
        svc = _FakeSheetsService({
            "Harrison Smith Residence - Project Tracker": [
                ["Project", "Source", "Type", "Part Number"],
                ["Harrison Smith Residence", "Quote", "A", "PART-1"],
                ["Harrison Smith Residence", "Quote", "B", "PART-2"],
            ],
            "Other - Project Tracker": [
                ["Project", "Source", "Type", "Part Number"],
                ["Other", "Quote", "A", "PART-1"],
            ],
        })
        result = resolve_existing_tracker(
            svc,
            "TEST",
            "Harrison Smit",
            parts=["PART-1", "PART-2"],
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(
            result.tracker_title,
            "Harrison Smith Residence - Project Tracker",
        )
        self.assertEqual(result.method, "MULTI_PART_CROSSCHECK")

    def test_open_project_without_tracker_blocks_part_reroute(self):
        svc = _FakeSheetsService(
            {
                "Misc Stock Purchases - Project Tracker": [
                    ["Project", "Source", "Type", "Part Number"],
                    ["Misc Stock Purchases", "PO Import", "HR.a1", "801.42.041"],
                    ["Misc Stock Purchases", "PO Import", "HR.a2", "803.39.910"],
                ]
            },
            open_projects=["Barb Residence"],
        )
        result = resolve_existing_tracker(
            svc,
            "TEST",
            "Parallel Construction:Barb Residence",
            parts=["801.42.041", "803.39.910"],
        )
        self.assertEqual(result.status, "REVIEW")
        self.assertEqual(result.method, "OPEN_PROJECT_TRACKER_MISSING")


if __name__ == "__main__":
    unittest.main()

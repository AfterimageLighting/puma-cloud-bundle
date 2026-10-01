import unittest

from puma_status import can_advance_status, can_auto_omit


class PumaStatusTests(unittest.TestCase):
    def test_forward_progression_is_allowed(self):
        self.assertTrue(can_advance_status("", "Received"))
        self.assertTrue(can_advance_status("Ordered", "Received"))
        self.assertTrue(can_advance_status("Received", "Scheduled"))
        self.assertTrue(can_advance_status("Scheduled", "Delivered"))
        self.assertTrue(can_advance_status("Delivered", "Delivered"))

    def test_backward_progression_is_blocked(self):
        self.assertFalse(can_advance_status("Scheduled", "Received"))
        self.assertFalse(can_advance_status("Delivered", "Scheduled"))
        self.assertFalse(can_advance_status("Received", "Ordered"))

    def test_special_and_unknown_states_are_protected(self):
        self.assertFalse(can_advance_status("Omitted", "Delivered"))
        self.assertFalse(can_advance_status("Note", "Scheduled"))
        self.assertFalse(can_advance_status("Needs Review", "Delivered"))

    def test_rfa_auto_omit_stops_before_ordered(self):
        for status in ("", "Unapproved", "Approved", "To Be Ordered"):
            self.assertTrue(can_auto_omit(status))
        for status in ("Ordered", "Received", "Scheduled", "Delivered", "Omitted", "Note"):
            self.assertFalse(can_auto_omit(status))


if __name__ == "__main__":
    unittest.main()

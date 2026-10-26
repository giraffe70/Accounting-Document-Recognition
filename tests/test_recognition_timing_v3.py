"""Contract tests for v3 timing aggregation and v2 export compatibility."""
import unittest

from app.core import recognition_timing


class RecognitionTimingV3Tests(unittest.TestCase):
    def test_orientation_model_and_ocr_are_separate_and_init_is_a_subset(self):
        timings = recognition_timing.from_trace([
            {"kind": "orientation_classifier", "seconds": 0.42,
             "engine_init_seconds": 0.12, "cache_hit": False},
            {"kind": "ocr_result", "purpose": "orientation", "seconds": 0.8,
             "engine_init_seconds": 0.2},
        ])
        self.assertEqual(timings["orientation_model_seconds"], 0.42)
        self.assertEqual(timings["orientation_model_init_seconds"], 0.12)
        self.assertEqual(timings["orientation_ocr_seconds"], 0.8)
        self.assertEqual(timings["ocr_seconds"], 0.8)
        self.assertEqual(timings["ocr_init_seconds"], 0.2)

    def test_v2_csv_keeps_old_fields_and_marks_v3_fields_unrecorded(self):
        values = recognition_timing.csv_values({
            "timings_version": 2, "orientation_ocr_seconds": 1.5,
            "ocr_seconds": 2.25, "model_seconds": 3,
        })
        self.assertEqual(values["orientation_ocr_seconds"], 1.5)
        self.assertEqual(values["ocr_seconds"], 2.25)
        self.assertIsNone(values["orientation_model_seconds"])
        self.assertIsNone(values["first_result_seconds"])

    def test_merge_takes_earliest_first_result_without_summing(self):
        merged = recognition_timing.merge(
            {"first_result_seconds": 4.2, "ocr_seconds": 1.0},
            {"first_result_seconds": 2.1, "ocr_seconds": 3.0},
            {"first_result_seconds": None, "ocr_seconds": 1.0},
        )
        self.assertEqual(merged["first_result_seconds"], 2.1)
        self.assertEqual(merged["ocr_seconds"], 5.0)
        self.assertIsNone(recognition_timing.merge({})["first_result_seconds"])

    def test_unfinished_classifier_is_incomplete(self):
        timings = recognition_timing.from_trace([
            {"kind": "orientation_classifier_start"},
        ])
        self.assertTrue(timings["incomplete"])


if __name__ == "__main__":
    unittest.main()

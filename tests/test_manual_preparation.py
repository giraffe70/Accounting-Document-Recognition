"""Offline contracts for image-bound manual preparation state."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from app.core import preparation


class ManualPreparationContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "source.png"
        Image.new("RGB", (24, 18), (240, 240, 240)).save(self.image)

    def prepared_data(self, *, pdf=True):
        return {"origin": {"preparation": preparation.fresh(pdf=pdf)}}

    def test_confirmation_is_bound_to_pixels_and_same_pixel_copy(self):
        data = self.prepared_data()

        self.assertFalse(preparation.confirmed(data, self.image))
        preparation.confirm(data, self.image, type_confirmed=True)
        self.assertTrue(preparation.confirmed(data, self.image))

        duplicate = self.root / "copy.png"
        Image.open(self.image).save(duplicate)
        self.assertTrue(preparation.confirmed(data, duplicate))

    def test_changed_pixels_invalidate_confirmation(self):
        data = self.prepared_data()
        preparation.confirm(data, self.image)
        self.assertTrue(preparation.confirmed(data, self.image))

        Image.new("RGB", (24, 18), (239, 240, 240)).save(self.image)
        self.assertFalse(preparation.confirmed(data, self.image))

    def test_rotate_and_crop_record_transform_and_clear_direction(self):
        for kind in ("rotate", "crop"):
            with self.subTest(kind=kind):
                data = self.prepared_data()
                prep = data["origin"]["preparation"]
                prep.update(direction_confirmed=True, image_sha256="old", type_confirmed=True,
                            text_evidence={"text": "stale"})
                operation = {"kind": kind, "degrees": 90} if kind == "rotate" else {
                    "kind": kind, "box": [0, 0, 10, 10]}

                preparation.changed(data, operation)

                updated = preparation.state(data)
                self.assertFalse(updated["direction_confirmed"])
                self.assertIsNone(updated["image_sha256"])
                self.assertEqual(updated["transforms"], [operation])
                self.assertTrue(updated["type_confirmed"])
                self.assertTrue(updated["pdf_enabled"])
                self.assertNotIn("text_evidence", updated)

    def test_unrecognized_operations_disable_pdf(self):
        for kind in ("fine", "mask"):
            with self.subTest(kind=kind):
                data = self.prepared_data(pdf=True)
                preparation.changed(data, {"kind": kind})
                updated = preparation.state(data)
                self.assertFalse(updated["direction_confirmed"])
                self.assertFalse(updated["pdf_enabled"])
                self.assertEqual(updated["transforms"], [])

    def test_missing_metadata_does_not_force_manual_preparation(self):
        legacy_data = {}
        self.assertFalse(preparation.manual(legacy_data))
        self.assertEqual(preparation.state(legacy_data), {})

    def test_unknown_document_mode_is_rejected(self):
        data = self.prepared_data()
        with self.assertRaises(ValueError):
            preparation.confirm(data, self.image, document_mode="unknown")


if __name__ == "__main__":
    unittest.main()

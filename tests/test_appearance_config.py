import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from appearance_config import compress_background, normalize_appearance
from gateway_state import GatewayStateStore


class AppearanceConfigTest(unittest.TestCase):
    def test_normalize_rejects_unknown_values_and_unavailable_asset(self):
        value = normalize_appearance({
            "theme": "unknown",
            "background": {"kind": "upload", "assetId": "missing"},
            "glass": {"blur": 100, "opacity": -1},
            "font": {"display": "unknown", "scale": 2},
            "effects": {"rain": {"mode": "weather", "intensity": 9}},
        })
        self.assertEqual(value["theme"], "linen")
        self.assertEqual(value["background"], {"kind": "gradient"})
        self.assertEqual(value["glass"], {"blur": 30, "opacity": 0.4})
        self.assertEqual(value["font"], {"display": "serif", "scale": 1.3})
        self.assertEqual(value["effects"]["rain"], {"mode": "weather", "intensity": 1})

    def test_old_database_upgrade_repeat_init_and_background_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway_state.db"
            sqlite3.connect(path).close()
            store = GatewayStateStore(str(path))
            self.assertEqual(store.load_cc_appearance()["background"]["kind"], "gradient")
            GatewayStateStore(str(path))  # migration remains idempotent

            image = Image.new("RGB", (2400, 1200), "#aa9988")
            source = io.BytesIO()
            image.save(source, format="PNG")
            asset_id, mime, compressed = compress_background(source.getvalue(), "image/png")
            self.assertEqual(mime, "image/jpeg")
            with Image.open(io.BytesIO(compressed)) as saved_image:
                self.assertLessEqual(saved_image.width, 1920)
            store.save_cc_appearance_background(asset_id, mime, compressed)
            self.assertEqual(store.load_cc_appearance_background()["asset_id"], asset_id)

            saved = store.save_cc_appearance({
                "background": {"kind": "upload", "assetId": asset_id},
                "font": {"display": "sans", "scale": 1.1},
            })
            self.assertEqual(saved["background"]["assetId"], asset_id)
            self.assertEqual(GatewayStateStore(str(path)).load_cc_appearance(), saved)
            self.assertEqual(store.save_cc_appearance(saved), saved)

            store.delete_cc_appearance_background()
            store.delete_cc_appearance_background()
            self.assertIsNone(store.load_cc_appearance_background())
            self.assertEqual(store.load_cc_appearance()["background"], {"kind": "gradient"})

    def test_image_validation(self):
        with self.assertRaises(ValueError):
            compress_background(b"not an image", "image/png")
        with self.assertRaises(ValueError):
            compress_background(b"anything", "image/svg+xml")


if __name__ == "__main__":
    unittest.main()

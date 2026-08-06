import re
import struct
import unittest
from pathlib import Path


class MetadataTests(unittest.TestCase):
    def test_version_is_a_nonempty_quoted_string(self):
        metadata_path = Path(__file__).resolve().parents[1] / "metadata.yaml"
        metadata_text = metadata_path.read_text(encoding="utf-8")

        match = re.search(
            r'^version:\s*(["\'])(.+?)\1\s*$',
            metadata_text,
            flags=re.MULTILINE,
        )

        self.assertIsNotNone(match)
        self.assertEqual(match.group(2), "1.0.2")
        self.assertRegex(match.group(2), r"^\d+\.\d+\.\d+$")

    def test_logo_is_recommended_square_png(self):
        logo_path = Path(__file__).resolve().parents[1] / "logo.png"
        logo_bytes = logo_path.read_bytes()

        self.assertTrue(logo_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", logo_bytes[16:24])
        self.assertEqual((width, height), (256, 256))


if __name__ == "__main__":
    unittest.main()

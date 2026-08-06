import re
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
        self.assertEqual(match.group(2), "1.0")


if __name__ == "__main__":
    unittest.main()

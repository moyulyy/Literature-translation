import tempfile
import unittest
from pathlib import Path

from app.pdf_parser import parse_pdf

PDF = Path(__file__).resolve().parent.parent / "jp309203k.pdf"


@unittest.skipUnless(
    PDF.is_file(),
    "Place a test PDF at jp309203k.pdf to run the parser integration tests",
)
class ParserSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.assets = Path(cls._tmp.name) / "assets"
        cls.doc = parse_pdf(PDF, cls.assets, "test")
        cls.blocks = cls.doc["blocks"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_pages_and_title(self):
        self.assertEqual(self.doc["page_count"], 7)
        self.assertTrue(self.doc["title"].startswith("Oxygen Reduction Activity"))

    def test_block_kinds(self):
        kinds = {block["kind"] for block in self.blocks}
        for kind in (
            "title",
            "abstract",
            "heading",
            "body",
            "figure",
            "table",
            "equation",
            "reference",
        ):
            self.assertIn(kind, kinds)

    def test_heading_leaks_removed(self):
        for block in self.blocks:
            self.assertNotIn("■", block["text"])

    def test_figures_are_linked_to_captions(self):
        figures = [block for block in self.blocks if block["kind"] == "figure"]
        self.assertGreaterEqual(len(figures), 3)
        targets = {block["target"] for block in self.blocks if block.get("target")}
        self.assertTrue(any(figure["id"] in targets for figure in figures))

    def test_cropped_assets_exist(self):
        assets = [block["asset"] for block in self.blocks if block.get("asset")]
        self.assertGreater(len(assets), 5)
        for name in assets:
            self.assertTrue((self.assets / name).is_file(), name)

    def test_column_split_paragraph_is_merged(self):
        merged = next(block for block in self.blocks if block["id"] == "p1-t6")
        self.assertIn("local DFT methods", merged["text"])
        self.assertGreater(len(merged["text"]), 800)

    def test_references_are_not_translated(self):
        references = [block for block in self.blocks if block["kind"] == "reference"]
        self.assertGreater(len(references), 20)
        self.assertFalse(any(block["translate"] for block in references))

    def test_formulas_are_kept_as_images(self):
        equations = [block for block in self.blocks if block["kind"] == "equation"]
        self.assertGreaterEqual(len(equations), 8)
        for equation in equations:
            self.assertTrue(equation["asset"])
            self.assertFalse(equation["translate"])


if __name__ == "__main__":
    unittest.main()

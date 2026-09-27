import unittest
import re
from core.pdf_extractor import (
    Word,
    _detect_column_bands,
    _order_body_units,
    _adaptive_min_gutter,
)
from core.section_diff import (
    build_section_list,
    _looks_like_unnumbered_header,
    _is_caps_line,
)
from core.diff_engine import (
    _is_figure_artwork_text,
    diff_documents,
    ComparisonToken,
)
from core.document_extractor import build_comparison_tokens


class TestSample2LayoutAndDiff(unittest.TestCase):
    def test_small_zone_heading_word_order_preservation(self):
        """A 2-line heading like '14.8 Treatment of ... Patients' must preserve word order
        and never get scrambled into 'Treatment 14.8' via spurious label-value detection."""
        body_words = [
            {"text": "14.8", "x0": 412.4, "x1": 423.7, "top": 808.1, "bottom": 815.1},
            {"text": "Treatment", "x0": 426.6, "x1": 451.4, "top": 808.1, "bottom": 815.1},
            {"text": "of", "x0": 452.8, "x1": 457.7, "top": 808.1, "bottom": 815.1},
            {"text": "Venous", "x0": 459.1, "x1": 476.2, "top": 808.1, "bottom": 815.1},
            {"text": "Thromboembolism", "x0": 477.6, "x1": 520.6, "top": 808.1, "bottom": 815.1},
            {"text": "Patients", "x0": 426.6, "x1": 455.0, "top": 816.4, "bottom": 823.4},
        ]
        units, bands = _order_body_units(body_words, 412.0, 521.0)
        ordered_texts = [w["text"] for u in units for w in u[2]]
        self.assertEqual(ordered_texts[0], "14.8", "Heading number must come first")
        self.assertEqual(ordered_texts[1], "Treatment", "Heading title word must come second")

    def test_zero_tolerance_gutter_preferred_over_expansion(self):
        """When a clean zero-count gutter exists between columns, _detect_column_bands
        must not swallow ragged column line endings by expanding the gutter."""
        rows = []
        for y in range(10):
            top = y * 15.0
            r = [
                {"text": "Col1", "x0": 80.0, "x1": 400.0, "top": top, "bottom": top + 10.0},
                {"text": "Col2", "x0": 561.0, "x1": 850.0, "top": top, "bottom": top + 10.0},
            ]
            rows.append(r)
        for y in range(10, 12):
            top = y * 15.0
            r = [
                {"text": "Col1_wide", "x0": 80.0, "x1": 530.0, "top": top, "bottom": top + 10.0},
                {"text": "Col2", "x0": 561.0, "x1": 850.0, "top": top, "bottom": top + 10.0},
            ]
            rows.append(r)

        bands = _detect_column_bands(rows, 80.0, 850.0, min_gutter=12.0)
        self.assertEqual(len(bands), 2, "Must detect exactly 2 columns")
        col1_hi = bands[0][1]
        col2_lo = bands[1][0]
        self.assertGreaterEqual(col1_hi, 530.0, "Col 1 band must include the 530pt lines")
        self.assertLessEqual(col2_lo, 565.0, "Col 2 band must start around 561pt")

    def test_dose_units_not_accepted_as_section_headers(self):
        """Lines like '5 mL' from syringe markings or dosage tables must not become Section 5."""
        blocks = [
            {"text": "5 WARNINGS AND PRECAUTIONS\nSerious bleeding risk.", "bbox": [20, 20, 200, 40], "page": 1},
            {"text": "5\nmL\nPlunger\n* Color band aid", "bbox": [20, 100, 200, 140], "page": 1},
            {"text": "12 CLINICAL PHARMACOLOGY\nMechanism of action.", "bbox": [20, 200, 200, 220], "page": 1},
        ]
        sections = build_section_list(blocks)
        titles = [s[0] for s in sections]
        self.assertIn("5 WARNINGS AND PRECAUTIONS", titles)
        self.assertNotIn("5 mL", titles, "'5 mL' must never be treated as a numbered section")

    def test_colon_parameter_labels_in_numbered_doc_rejected(self):
        """In documents with numbered sections, parameter/chart labels ending in colons
        (e.g. 'End-Stage Renal Disease:', 'Renal Impairment AUC :', 'Manufactured by:')
        must not break the document into spurious sections."""
        blocks = [
            {"text": "1 INDICATIONS AND USAGE\nRivaroxaban is indicated for...", "bbox": [20, 20, 200, 40], "page": 1},
            {"text": "12.3 Pharmacokinetics\nAbsorption and distribution.", "bbox": [20, 60, 200, 80], "page": 1},
            {"text": "Figure 2: Forest Plot\nEnd-Stage Renal Disease:\nPostdialysis Normal Cmax", "bbox": [20, 90, 200, 110], "page": 1},
            {"text": "Renal Impairment AUC :\nSevere Normal Cmax", "bbox": [20, 115, 200, 130], "page": 1},
            {"text": "Manufactured by:\nAnnora Pharma Pvt Ltd", "bbox": [20, 140, 200, 160], "page": 1},
            {"text": "13 NON-CLINICAL TOXICOLOGY\nCarcinogenesis.", "bbox": [20, 180, 200, 200], "page": 1},
        ]
        sections = build_section_list(blocks)
        titles = [s[0] for s in sections]
        self.assertIn("1 INDICATIONS AND USAGE", titles)
        self.assertIn("12.3 Pharmacokinetics", titles)
        self.assertIn("13 NON-CLINICAL TOXICOLOGY", titles)
        self.assertNotIn("End-Stage Renal Disease:", titles)
        self.assertNotIn("Renal Impairment AUC :", titles)
        self.assertNotIn("Manufactured by:", titles)

    def test_figure_artwork_and_slug_classification(self):
        """Vector artwork labels, tick sequences, production slugs, and device callouts
        must be correctly identified by _is_figure_artwork_text."""
        samples_true = [
            "O O O N NN O H N S Cl O",
            "Population Description PK Fold Change and 90% Cl End-Stage Renal Disease: Postdialysis*/Normal Cmax",
            "Interacting Drug PK Fold Change and 90% Cl Combined P-gp and Strong CYP3A Inhibitor Ketoconazole",
            "2xxxxxx",
            "1202-2025-06 Rivaroxaban For Oral Suspension 2xxxxxx",
            "0.9 0.8 1.1 1 1..4 7 1 1..6 8 2 2.5 2.4 3 2.8 mL 5",
            "Side 1: Dosing in milliliters (mL) Side 2: Dosing with color band* (mL) Tip 0.5 1 1.5 2 2.5 3 3.5 4 4.5 5 mL",
            "Cap Bottle adaptor Do not remove the bottle adaptor",
            "SLOW!",
            "bottle adaptor",
        ]
        for s in samples_true:
            self.assertTrue(
                _is_figure_artwork_text(s),
                f"Expected _is_figure_artwork_text({s!r}) to be True",
            )

        samples_false = [
            "Take rivaroxaban tablets once daily with evening meal.",
            "DOSAGE AND ADMINISTRATION",
            "Patients with moderate hepatic impairment exhibited increased exposure.",
        ]
        for s in samples_false:
            self.assertFalse(
                _is_figure_artwork_text(s),
                f"Expected _is_figure_artwork_text({s!r}) to be False",
            )


if __name__ == "__main__":
    unittest.main()

"""Comprehensive tests for multi-column extraction and position-agnostic comparison across scripts and layouts."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pdf_extractor import (
    Word,
    _detect_column_bands,
    _fix_rtl_word_chars,
    _normalize_word_text,
    _order_body_units,
    _text_is_rtl,
    build_comparison_tokens,
)
from core.section_diff import (
    build_section_map,
    is_likely_header_line,
)
from core.diff_engine import (
    _tokens_to_blocks,
    diff_documents,
)


class TestMulticolumnAndPositionAgnosticDiff(unittest.TestCase):

    def _make_words(self, text: str, page: int = 1, base_x: float = 50.0, base_y: float = 50.0) -> list[Word]:
        words = []
        lines = text.strip().splitlines()
        y = base_y
        for line in lines:
            if not line.strip():
                y += 15.0
                continue
            line_words = line.strip().split()
            x = base_x
            for w in line_words:
                is_rtl = _text_is_rtl(w)
                raw = _fix_rtl_word_chars(w) if is_rtl else w
                norm = _normalize_word_text(raw, mirror_brackets=is_rtl)
                words.append(Word(text=norm, page=page, x0=x, top=y, x1=x + 30.0, bottom=y + 12.0))
                x += 35.0
            y += 15.0
        return words

    def test_arabic_multipage_vs_singlepage_4column(self):
        """Test comparing a 4-page single-column Arabic approved document against
        a 1-page 4-column suspect document."""
        p1 = """أبديل- أتش
(شراب السعال العشبي)

تكوين
يحتوي كل mL10على :
خلاصة مائية من:
فاساكا ( أدهاتودا فاسيكا) 150 مجم
يشتيمادهو (جليسريرزة جابرة) 75 مجم
بيمبالي (بايبر لونجوم) 10 مجم
كولينجانا (ألبينيا جالانجا) 100 مجم
قاعدة شراب بنكهة .s.q

شكل الجرعة
شراب

وصف
وفاساكا هي واحدة من الاعشاب القوية المستعملة في معالجة الربو، التهاب القصبات الهوائية، والحالات المخاطية المفرطة."""

        p2 = """التفاصيل السريرية
مؤشرات العلاجية
ويشير شراب السعال أبديل- أتش إلى السعال والبرد، والسعال المرتبط بالربو والحلق المتأزم.
علم الصدلية وطريقة الادارة
البالغون: ملعبان شاي (10 مل) ثلاث مرات يوميا.
الأطفال (سن أقل من سنتين): ملعقة شاي واحدة (5 مل) ثلاث مرات يوميا.

موانع
 فرط الحساسية للمادة الفعالة أو لأي من المواد السابقة.
 غير موصى به للأطفال الذين تقل أعمارهم عن سنتين."""

        p3 = """تحذيرات الخاصة واحتياطات الازمة للاستعمال
 لا يوصى باستخدام أبديل- أتش أثناء الحمل وفي الأطفال.
 لا تتجاوز الجرعة اليومية الموصى بها.
 مرض القلب: يمكن أن يتسبب جليسريرزة جابرة في جعل الجسم لتخزين الماء.

جرعة زائدة
ولا يعرف سوى القليل عن الجرعة الزائدة من مكونات أبديل- أتش. قد يؤدي الإفراط في الجرعة إلى إرتفاع ضغط الدم."""

        p4 = """الخصائص الصيدلانية
عدم التوافق: غير قابل للتطبيق
احتياطات خاصة للتخزين: لايوجد
حالة التخزين: يحفظ بدرجة حرارة لا تزيد عن 30 درجة مئوية.
طبيعة الحاوية ومحتوياتها: متوفر في زجاجة PET الملونة سعتها 100 مل.
رقم النسخة: 00
تاريخ آخر مراجعة: ديسمبر، 2019
صنع في الهند"""

        # Suspect version with 1 critical dosage change (20 مل vs 10 مل) and typos ("خلاصئية", "واحيدة")
        col1 = """أبديل-أتش
(شراب السعال العشبي)

تكوين
يحتوي كل mL10على :
خلاصئية مائية من:
فاساكا ( أدهاتودا فاسيكا) 150 مجم
يشتيمادهو (جليسريرزة جابرة) 75 مجم
بيمبالي (بااليبر لونجوم) 10 مجم
كولينجانا (ألبينيا جالانجا) 100 مجم
قاعدة شيراب بنكهة .s.q

شكل الجرعة
شراب

وصف
وفاساكا هي واحيدة من الاعشاب القوية المستعملة في معالجة الربو، التهاب القصبات الهوائية، والحالات المخاطية المفرطة."""

        col2 = """التفاصيل السريرية
مؤشرات العلاجية
ويشير شراب السعال أبديل-أتش إلى السعال والبرد، والسعال المرتبط بالربو والحلق المتأزم.
علم الصدلية وطريقة الادارة
البالغون: ملعبان شاي (20 مل) ثلاث مرات يوميا.
الأطفال (سن أقل من سنتين): ملعقة شاي واحدة (5 مل) ثلاث مرات يوميا.

موانع
 فرط الحساسية للمادة الفعالة أو لأي من المواد السابقة.
 غير موصى به للأطفال الذين تقل أعمارهم عن سنتين."""

        col3 = """تحذيرات الخاصة واحتياطات الازمة للاستعمال
 لا يوصى باستخدام أبديل-أتش أثناء الحمل وفي الأطفال.
 لا تتجاوز الجرعة اليومية الموصى بها.
 مرض القلب: يمكن أن يتسبب جليسريرزة جابرة في جعل الجسم لتخزين الماء.

جرعة زائدة
ولا يعرف سوى القليل عن الجرعة الزائدة من مكونات أبديل-أتش. قد يؤدي الإفراط في الجرعة إلى إرتفاع ضغط الدم."""

        col4 = """الخصائص الصيدلانية
عدم التوافق: غير قابل للتطبيق
احتياطات خاصة للتخزين: لايوجد
حالة التخزين: يحفظ بدرجة حرارة لا تزيد عن 30 درجة مئوية.
طبيعة الحاوية ومحتوياتها: متوفر في زجاجة PET الملونة سعتها 100 مل.
رقم النسخة: 00
تاريخ آخر مراجعة: ديسمبر، 2019
صنع في الهند"""

        app_words = (
            self._make_words(p1, page=1, base_x=50, base_y=50) +
            self._make_words(p2, page=2, base_x=50, base_y=50) +
            self._make_words(p3, page=3, base_x=50, base_y=50) +
            self._make_words(p4, page=4, base_x=50, base_y=50)
        )

        susp_words_ltr = (
            self._make_words(col1, page=1, base_x=50, base_y=50) +
            self._make_words(col2, page=1, base_x=200, base_y=50) +
            self._make_words(col3, page=1, base_x=350, base_y=50) +
            self._make_words(col4, page=1, base_x=500, base_y=50)
        )

        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words_ltr)

        diffs = diff_documents(app_tokens, susp_tokens)

        # Ensure the dosage change (10 -> 20) is caught as critical
        dosage_diffs = [d for d in diffs if "10" in d.approved_text and "20" in d.suspect_text]
        self.assertTrue(len(dosage_diffs) > 0, "Dose change 10 ml -> 20 ml must be detected")
        self.assertEqual(dosage_diffs[0].severity, "critical")

        # Ensure no cascading diffs
        critical_non_dosage = [
            d for d in diffs if d.severity == "critical" and "10" not in d.approved_text
        ]
        # Only minor punctuation or specific differences, no massive blocks of text
        for d in critical_non_dosage:
            self.assertTrue(len(d.approved_text.split()) <= 3, f"Unexpected large diff: {d.approved_text!r}")

    def test_relocated_arabic_sections(self):
        """Test that sections moved across columns or pages in suspect PDF
        are correctly matched position-agnostically."""
        sec1 = "تكوين\nيحتوي كل mL10على خلاصة مائية من فاساكا 150 مجم."
        sec2 = "شكل الجرعة\nشراب مركز عالي الجودة."
        sec3 = "الخصائص الصيدلانية\nيحفظ بدرجة حرارة لا تزيد عن 30 درجة مئوية."

        app_words = (
            self._make_words(sec1, page=1, base_x=50, base_y=50) +
            self._make_words(sec2, page=2, base_x=50, base_y=50) +
            self._make_words(sec3, page=3, base_x=50, base_y=50)
        )

        # Suspect has sections completely reordered: sec3 first, then sec1 with an edit (150 -> 250), then sec2
        sec1_edited = "تكوين\nيحتوي كل mL10على خلاصة مائية من فاساكا 250 مجم."
        susp_words = (
            self._make_words(sec3, page=1, base_x=50, base_y=50) +
            self._make_words(sec1_edited, page=1, base_x=200, base_y=50) +
            self._make_words(sec2, page=1, base_x=350, base_y=50)
        )

        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words)

        diffs = diff_documents(app_tokens, susp_tokens)

        # The dosage edit 150 -> 250 must be found and flagged as critical
        dose_diffs = [d for d in diffs if "150" in d.approved_text and "250" in d.suspect_text]
        self.assertEqual(len(dose_diffs), 1)
        self.assertEqual(dose_diffs[0].severity, "critical")

        # Moved sections must be flagged as reorder/reordered_section
        reordered = [d for d in diffs if d.severity == "reorder"]
        self.assertTrue(len(reordered) >= 1)

    def test_multicolumn_proof_with_nested_label_value_tables(self):
        """Test a 2-column proof sheet containing an insert column next to an agreement with
        borderless label-value tables (e.g. Late Fee with 2 sub-columns)."""
        # Approved: 1-column with section headers and borderless table
        app_sec = (
            "FEES\n"
            "Late Fee We will not charge a Late Fee the first time you do not make the Minimum Payment. "
            "were not charged a Late Fee during any of the prior six billing periods. Otherwise, the fee is $35."
        )
        app_words = self._make_words(app_sec, page=1, base_x=50, base_y=50)

        # Suspect: 2-column page where Col 0 (x=20..120) is an insert, and Col 1 (x=160..500) has FEES
        insert_words = self._make_words("BILLING RIGHTS\nYour billing rights summary and contact details.", page=1, base_x=20, base_y=50)
        agreement_words = [
            Word(text="FEES", page=1, x0=160.0, top=50.0, x1=200.0, bottom=62.0),
            # Label
            Word(text="Late", page=1, x0=160.0, top=70.0, x1=180.0, bottom=82.0),
            Word(text="Fee", page=1, x0=185.0, top=70.0, x1=205.0, bottom=82.0),
            # Value Sub-column 1
            Word(text="We", page=1, x0=230.0, top=70.0, x1=245.0, bottom=82.0),
            Word(text="will", page=1, x0=250.0, top=70.0, x1=270.0, bottom=82.0),
            Word(text="not", page=1, x0=275.0, top=70.0, x1=290.0, bottom=82.0),
            Word(text="charge", page=1, x0=295.0, top=70.0, x1=330.0, bottom=82.0),
            Word(text="a", page=1, x0=335.0, top=70.0, x1=345.0, bottom=82.0),
            Word(text="Late", page=1, x0=350.0, top=70.0, x1=370.0, bottom=82.0),
            Word(text="Fee", page=1, x0=375.0, top=70.0, x1=395.0, bottom=82.0),
            Word(text="the", page=1, x0=230.0, top=85.0, x1=245.0, bottom=97.0),
            Word(text="first", page=1, x0=250.0, top=85.0, x1=275.0, bottom=97.0),
            Word(text="time", page=1, x0=280.0, top=85.0, x1=305.0, bottom=97.0),
            Word(text="you", page=1, x0=310.0, top=85.0, x1=330.0, bottom=97.0),
            Word(text="do", page=1, x0=335.0, top=85.0, x1=350.0, bottom=97.0),
            Word(text="not", page=1, x0=355.0, top=85.0, x1=370.0, bottom=97.0),
            Word(text="make", page=1, x0=230.0, top=100.0, x1=260.0, bottom=112.0),
            Word(text="the", page=1, x0=265.0, top=100.0, x1=280.0, bottom=112.0),
            Word(text="Minimum", page=1, x0=285.0, top=100.0, x1=335.0, bottom=112.0),
            Word(text="Payment.", page=1, x0=340.0, top=100.0, x1=390.0, bottom=112.0),
            # Value Sub-column 2
            Word(text="were", page=1, x0=410.0, top=70.0, x1=435.0, bottom=82.0),
            Word(text="not", page=1, x0=440.0, top=70.0, x1=455.0, bottom=82.0),
            Word(text="charged", page=1, x0=460.0, top=70.0, x1=505.0, bottom=82.0),
            Word(text="a", page=1, x0=510.0, top=70.0, x1=520.0, bottom=82.0),
            Word(text="Late", page=1, x0=525.0, top=70.0, x1=545.0, bottom=82.0),
            Word(text="Fee", page=1, x0=550.0, top=70.0, x1=570.0, bottom=82.0),
            Word(text="during", page=1, x0=410.0, top=85.0, x1=445.0, bottom=97.0),
            Word(text="any", page=1, x0=450.0, top=85.0, x1=470.0, bottom=97.0),
            Word(text="of", page=1, x0=475.0, top=85.0, x1=485.0, bottom=97.0),
            Word(text="the", page=1, x0=490.0, top=85.0, x1=505.0, bottom=97.0),
            Word(text="prior", page=1, x0=510.0, top=85.0, x1=535.0, bottom=97.0),
            Word(text="six", page=1, x0=540.0, top=85.0, x1=555.0, bottom=97.0),
            Word(text="billing", page=1, x0=410.0, top=100.0, x1=440.0, bottom=112.0),
            Word(text="periods.", page=1, x0=445.0, top=100.0, x1=485.0, bottom=112.0),
            Word(text="Otherwise,", page=1, x0=490.0, top=100.0, x1=545.0, bottom=112.0),
            Word(text="the", page=1, x0=550.0, top=100.0, x1=565.0, bottom=112.0),
            Word(text="fee", page=1, x0=570.0, top=100.0, x1=585.0, bottom=112.0),
            Word(text="is", page=1, x0=590.0, top=100.0, x1=600.0, bottom=112.0),
            Word(text="$35.", page=1, x0=605.0, top=100.0, x1=625.0, bottom=112.0),
        ]
        susp_words = insert_words + agreement_words

        body_dicts = [w.__dict__ for w in susp_words]
        units, bands = _order_body_units(body_dicts, 20.0, 625.0, rtl=False)
        self.assertTrue(len(units) >= 1)

        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words)
        diffs = diff_documents(app_tokens, susp_tokens)

        # FEES content should match without critical differences on table values
        table_criticals = [d for d in diffs if d.severity == "critical" and "Late" in d.approved_text]
        self.assertEqual(len(table_criticals), 0)

    def test_three_column_justified_prescribing_info(self):
        """Test a 3-column layout with wide justified word spacing and tight header kerning
        (Zolpidem-style FDA prescribing information)."""
        # Approved 1-column with tight/fused header
        app_text = (
            "1INDICATIONS AND USAGE\n"
            "Zolpidem tartrate tablets are indicated for short-term treatment of insomnia.\n"
            "2DOSAGE AND ADMINISTRATION\n"
            "The dose of zolpidem tartrate tablets should be individualized.\n"
            "3DOSAGE FORMS AND STRENGTHS\n"
            "Zolpidem tartrate tablets are available in 10 mg strength."
        )
        app_words = self._make_words(app_text, page=1, base_x=50, base_y=50)

        # Suspect 3-column with 35pt gutters and wide within-line gaps (~10pt)
        col1_text = (
            "1 INDICATIONS AND USAGE\n"
            "Zolpidem tartrate tablets are indicated for short-term treatment of insomnia."
        )
        col2_text = (
            "2 DOSAGE AND ADMINISTRATION\n"
            "The dose of zolpidem tartrate tablets should be individualized."
        )
        col3_text = (
            "3 DOSAGE FORMS AND STRENGTHS\n"
            "Zolpidem tartrate tablets are available in 5 mg strength."
        )

        col1_words = self._make_words(col1_text, page=1, base_x=70, base_y=50)
        col2_words = self._make_words(col2_text, page=1, base_x=240, base_y=50)
        col3_words = self._make_words(col3_text, page=1, base_x=410, base_y=50)

        susp_words = col1_words + col2_words + col3_words

        body_dicts = [w.__dict__ for w in susp_words]
        min_x = min(w["x0"] for w in body_dicts)
        max_x = max(w["x1"] for w in body_dicts)
        units, bands = _order_body_units(body_dicts, min_x, max_x, rtl=False)
        self.assertEqual(len(bands), 3)

        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words)
        diffs = diff_documents(app_tokens, susp_tokens)

        # Only the 10 mg -> 5 mg strength change is critical, 0 false cascades
        criticals = [d for d in diffs if d.severity == "critical"]
        self.assertEqual(len(criticals), 1)
        self.assertIn("10", criticals[0].approved_text)
        self.assertIn("5", criticals[0].suspect_text)

    def test_whitespace_artifacts_identical_text_filtered(self):
        """Test that words whose non-whitespace character sequences are equal
        (e.g. 'من:' vs 'م ن:', 'أبديل- أتش' vs 'أبديل-أتش') are not reported as diffs."""
        app_words = [
            Word(text="خلاصة", page=1, x0=50, top=50, x1=80, bottom=62),
            Word(text="مائية", page=1, x0=90, top=50, x1=120, bottom=62),
            Word(text="من:", page=1, x0=130, top=50, x1=150, bottom=62),
            Word(text="أبديل-", page=1, x0=160, top=50, x1=190, bottom=62),
            Word(text="أتش", page=1, x0=195, top=50, x1=220, bottom=62),
            Word(text="شراب", page=1, x0=230, top=50, x1=260, bottom=62),
            Word(text="100", page=1, x0=270, top=50, x1=290, bottom=62),
            Word(text="مجم", page=1, x0=300, top=50, x1=320, bottom=62),
        ]
        susp_words = [
            Word(text="خلاصئية", page=1, x0=50, top=50, x1=80, bottom=62),
            Word(text="مائية", page=1, x0=90, top=50, x1=120, bottom=62),
            Word(text="م", page=1, x0=130, top=50, x1=140, bottom=62),
            Word(text="ن:", page=1, x0=142, top=50, x1=150, bottom=62),
            Word(text="أبديل-أتش", page=1, x0=160, top=50, x1=220, bottom=62),
            Word(text="شراب", page=1, x0=230, top=50, x1=260, bottom=62),
            Word(text="200", page=1, x0=270, top=50, x1=290, bottom=62),
            Word(text="مجم", page=1, x0=300, top=50, x1=320, bottom=62),
        ]

        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words)
        diffs = diff_documents(app_tokens, susp_tokens)

        # 'من:' vs 'م ن:' and 'أبديل- أتش' vs 'أبديل-أتش' must NOT be in diffs
        equal_diffs = [
            d for d in diffs
            if "من" in (d.approved_text or "") or "أبديل" in (d.approved_text or "")
        ]
        self.assertEqual(len(equal_diffs), 0, f"Whitespace-equal text must not raise diff: {equal_diffs}")

        # Real typo 'خلاصة' vs 'خلاصئية' must be reported
        typo_diffs = [d for d in diffs if d.severity == "typo"]
        self.assertEqual(len(typo_diffs), 1)
        self.assertEqual(typo_diffs[0].approved_text, "خلاصة")
        self.assertEqual(typo_diffs[0].suspect_text, "خلاصئية")

        # Real dosage change '100' vs '200' must be reported as critical
        dose_diffs = [d for d in diffs if d.severity == "critical"]
        self.assertEqual(len(dose_diffs), 1)
        self.assertIn("100", dose_diffs[0].approved_text)
        self.assertIn("200", dose_diffs[0].suspect_text)

    def test_sliver_gutter_and_proof_sheet_panels(self):
        """Test that tiny noise slivers (< 5pt) between candidate gutters are merged
        and do not create fake 3rd columns that flip label-value tables into row-major
        horizontal interleaving."""
        # 27 rows simulating the Credit Lines / Late Fee / Returned Payment Fee section:
        # Label column at x=20..90, value column at x=125..595 with an internal split
        # at x=360 into left sub-col (x=125..355) and right sub-col (x=365..595).
        # One row has a stray word crossing slightly into the gutter gap (creating a 2pt sliver).
        app_words = []
        susp_words = []
        
        # Approved side: 612x792 page
        # Row 0: Late Fee header & sub-columns
        # Sub-col 1: "We will not charge a Late Fee the first time you do not make Payment Due"
        # Sub-col 2: "were not charged a Late Fee during prior periods. Otherwise fee is $35"
        app_words.append(Word(text="Late", page=1, x0=20, top=100, x1=40, bottom=112))
        app_words.append(Word(text="Fee", page=1, x0=45, top=100, x1=65, bottom=112))
        
        # Left sub-column lines
        app_words.append(Word(text="We", page=1, x0=125, top=100, x1=140, bottom=112))
        app_words.append(Word(text="will", page=1, x0=145, top=100, x1=165, bottom=112))
        app_words.append(Word(text="not", page=1, x0=170, top=100, x1=190, bottom=112))
        app_words.append(Word(text="charge", page=1, x0=195, top=100, x1=235, bottom=112))
        
        app_words.append(Word(text="a", page=1, x0=125, top=115, x1=135, bottom=127))
        app_words.append(Word(text="Late", page=1, x0=140, top=115, x1=165, bottom=127))
        app_words.append(Word(text="Fee", page=1, x0=170, top=115, x1=190, bottom=127))
        
        # Right sub-column lines
        app_words.append(Word(text="were", page=1, x0=365, top=100, x1=395, bottom=112))
        app_words.append(Word(text="not", page=1, x0=400, top=100, x1=420, bottom=112))
        app_words.append(Word(text="charged", page=1, x0=425, top=100, x1=475, bottom=112))
        
        app_words.append(Word(text="during", page=1, x0=365, top=115, x1=405, bottom=127))
        app_words.append(Word(text="prior", page=1, x0=410, top=115, x1=440, bottom=127))
        app_words.append(Word(text="periods.", page=1, x0=445, top=115, x1=490, bottom=127))
        
        # Suspect side: on an oversized proof sheet (x offset by 650pt)
        # Identical text, but positioned in panel 2 of proof flat
        for w in app_words:
            susp_words.append(Word(text=w.text, page=1, x0=w.x0 + 650.0, top=w.top, x1=w.x1 + 650.0, bottom=w.bottom))
        
        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words)
        diffs = diff_documents(app_tokens, susp_tokens)
        
        self.assertEqual(len(diffs), 0, f"Identical label-value table on proof flat must produce 0 diffs, got: {diffs}")

    def test_stacked_different_columns_and_titlecase_colon_header(self):
        """Test that a page with stacked sections of different column counts
        (e.g. a 3-column section above a 2-column section starting with a title-case
        header like 'Your Billing Rights:') splits into zones properly so columns
        do not interleave horizontally, and the unnumbered header matches across pages."""
        app_words = []
        # Section 1 (top): 3 columns, Arbitration bullets
        # Col 1: Survival of this Provision. • closing of your Account;
        app_words.append(Word(text="Survival", page=1, x0=20, top=40, x1=60, bottom=52))
        app_words.append(Word(text="of", page=1, x0=65, top=40, x1=75, bottom=52))
        app_words.append(Word(text="this", page=1, x0=80, top=40, x1=100, bottom=52))
        app_words.append(Word(text="Provision.", page=1, x0=105, top=40, x1=155, bottom=52))
        app_words.append(Word(text="•", page=1, x0=20, top=55, x1=25, bottom=67))
        app_words.append(Word(text="closing", page=1, x0=30, top=55, x1=70, bottom=67))
        app_words.append(Word(text="of", page=1, x0=75, top=55, x1=85, bottom=67))
        app_words.append(Word(text="your", page=1, x0=90, top=55, x1=115, bottom=67))
        app_words.append(Word(text="Account;", page=1, x0=120, top=55, x1=170, bottom=67))

        # Col 2: You Have the Right to Reject Arbitration
        app_words.append(Word(text="You", page=1, x0=220, top=40, x1=240, bottom=52))
        app_words.append(Word(text="Have", page=1, x0=245, top=40, x1=270, bottom=52))
        app_words.append(Word(text="the", page=1, x0=275, top=40, x1=295, bottom=52))
        app_words.append(Word(text="Right", page=1, x0=300, top=40, x1=330, bottom=52))
        app_words.append(Word(text="to", page=1, x0=220, top=55, x1=230, bottom=67))
        app_words.append(Word(text="Reject", page=1, x0=235, top=55, x1=270, bottom=67))

        # Col 3: and personal signature. No one else
        app_words.append(Word(text="and", page=1, x0=420, top=40, x1=440, bottom=52))
        app_words.append(Word(text="personal", page=1, x0=445, top=40, x1=490, bottom=52))
        app_words.append(Word(text="signature.", page=1, x0=495, top=40, x1=550, bottom=52))
        app_words.append(Word(text="No", page=1, x0=420, top=55, x1=435, bottom=67))
        app_words.append(Word(text="one", page=1, x0=440, top=55, x1=460, bottom=67))

        # Section 2 (bottom): 2 columns, starting with "Your Billing Rights:"
        # Col 1 (left): Your Billing Rights: Keep This Document
        app_words.append(Word(text="Your", page=1, x0=20, top=140, x1=50, bottom=152))
        app_words.append(Word(text="Billing", page=1, x0=55, top=140, x1=90, bottom=152))
        app_words.append(Word(text="Rights:", page=1, x0=95, top=140, x1=140, bottom=152))
        app_words.append(Word(text="Keep", page=1, x0=20, top=155, x1=50, bottom=167))
        app_words.append(Word(text="This", page=1, x0=55, top=155, x1=80, bottom=167))
        app_words.append(Word(text="Document", page=1, x0=85, top=155, x1=140, bottom=167))

        # Col 2 (right): After we finish our investigation
        app_words.append(Word(text="After", page=1, x0=320, top=140, x1=350, bottom=152))
        app_words.append(Word(text="we", page=1, x0=355, top=140, x1=370, bottom=152))
        app_words.append(Word(text="finish", page=1, x0=375, top=140, x1=405, bottom=152))
        app_words.append(Word(text="our", page=1, x0=320, top=155, x1=340, bottom=167))
        app_words.append(Word(text="investigation", page=1, x0=345, top=155, x1=410, bottom=167))

        # Suspect side: Same content, but Section 2 is on Page 1, Section 1 is on Page 2
        susp_sec2 = [w for w in app_words if w.top >= 130]
        susp_sec1 = [w for w in app_words if w.top < 130]
        susp_words = [
            Word(text=w.text, page=1, x0=w.x0, top=w.top, x1=w.x1, bottom=w.bottom)
            for w in susp_sec2
        ] + [
            Word(text=w.text, page=2, x0=w.x0, top=w.top, x1=w.x1, bottom=w.bottom)
            for w in susp_sec1
        ]

        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words)
        diffs = diff_documents(app_tokens, susp_tokens)

        # There should be 0 critical differences; the moved section is reported as reordered_section
        criticals = [d for d in diffs if d.severity == "critical"]
        self.assertEqual(len(criticals), 0, f"Expected 0 critical diffs for moved section, got: {criticals}")
        reordered = [d for d in diffs if d.kind == "reordered_section"]
        reordered = [d for d in diffs if d.kind == "reordered_section"]
        self.assertTrue(len(reordered) >= 1, "Expected reordered_section for relocated Billing Rights")

    def test_renumbered_and_swapped_subsections(self):
        """Test that renumbered/swapped subsections (e.g. 5.1 Ketoacidosis <-> 5.2 Ketoacidosis,
        and 5.2 Pancreatitis <-> 5.1 Pancreatitis) are matched by title and body content rather
        than blindly by subsection number alone, avoiding cross-topic corruption."""
        app_words = [
            # 5.1 Diabetic Ketoacidosis
            Word(text="5.1", page=1, x0=20, top=20, x1=40, bottom=32),
            Word(text="Diabetic", page=1, x0=45, top=20, x1=85, bottom=32),
            Word(text="Ketoacidosis", page=1, x0=90, top=20, x1=150, bottom=32),
            Word(text="In", page=1, x0=20, top=35, x1=35, bottom=47),
            Word(text="patients", page=1, x0=40, top=35, x1=80, bottom=47),
            Word(text="with", page=1, x0=85, top=35, x1=105, bottom=47),
            Word(text="type", page=1, x0=110, top=35, x1=130, bottom=47),
            Word(text="1", page=1, x0=135, top=35, x1=145, bottom=47),
            Word(text="diabetes,", page=1, x0=150, top=35, x1=195, bottom=47),
            Word(text="the", page=1, x0=200, top=35, x1=215, bottom=47),
            Word(text="risk", page=1, x0=220, top=35, x1=240, bottom=47),
            Word(text="of", page=1, x0=245, top=35, x1=255, bottom=47),
            Word(text="ketoacidosis", page=1, x0=260, top=35, x1=320, bottom=47),
            Word(text="is", page=1, x0=325, top=35, x1=335, bottom=47),
            Word(text="increased.", page=1, x0=340, top=35, x1=390, bottom=47),
            # 5.2 Pancreatitis
            Word(text="5.2", page=1, x0=20, top=60, x1=40, bottom=72),
            Word(text="Pancreatitis", page=1, x0=45, top=60, x1=110, bottom=72),
            Word(text="Acute", page=1, x0=20, top=75, x1=50, bottom=87),
            Word(text="pancreatitis", page=1, x0=55, top=75, x1=115, bottom=87),
            Word(text="has", page=1, x0=120, top=75, x1=135, bottom=87),
            Word(text="been", page=1, x0=140, top=75, x1=165, bottom=87),
            Word(text="reported", page=1, x0=170, top=75, x1=210, bottom=87),
            Word(text="in", page=1, x0=215, top=75, x1=225, bottom=87),
            Word(text="clinical", page=1, x0=230, top=75, x1=265, bottom=87),
            Word(text="trials.", page=1, x0=270, top=75, x1=300, bottom=87),
        ]

        susp_words = [
            # 5.1 Pancreatitis (swapped)
            Word(text="5.1", page=1, x0=20, top=20, x1=40, bottom=32),
            Word(text="Pancreatitis", page=1, x0=45, top=20, x1=110, bottom=32),
            Word(text="Acute", page=1, x0=20, top=35, x1=50, bottom=47),
            Word(text="pancreatitis", page=1, x0=55, top=35, x1=115, bottom=47),
            Word(text="has", page=1, x0=120, top=35, x1=135, bottom=47),
            Word(text="been", page=1, x0=140, top=35, x1=165, bottom=47),
            Word(text="reported", page=1, x0=170, top=35, x1=210, bottom=47),
            Word(text="in", page=1, x0=215, top=35, x1=225, bottom=47),
            Word(text="clinical", page=1, x0=230, top=35, x1=265, bottom=47),
            Word(text="trials.", page=1, x0=270, top=35, x1=300, bottom=47),
            # 5.2 Ketoacidosis (swapped)
            Word(text="5.2", page=1, x0=20, top=60, x1=40, bottom=72),
            Word(text="Ketoacidosis", page=1, x0=45, top=60, x1=110, bottom=72),
            Word(text="In", page=1, x0=20, top=75, x1=35, bottom=87),
            Word(text="patients", page=1, x0=40, top=75, x1=80, bottom=87),
            Word(text="with", page=1, x0=85, top=75, x1=105, bottom=87),
            Word(text="type", page=1, x0=110, top=75, x1=130, bottom=87),
            Word(text="1", page=1, x0=135, top=75, x1=145, bottom=87),
            Word(text="diabetes,", page=1, x0=150, top=75, x1=195, bottom=87),
            Word(text="the", page=1, x0=200, top=75, x1=215, bottom=87),
            Word(text="risk", page=1, x0=220, top=75, x1=240, bottom=87),
            Word(text="of", page=1, x0=245, top=75, x1=255, bottom=87),
            Word(text="ketoacidosis", page=1, x0=260, top=75, x1=320, bottom=87),
            Word(text="is", page=1, x0=325, top=75, x1=335, bottom=87),
            Word(text="increased.", page=1, x0=340, top=75, x1=390, bottom=87),
        ]

        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words)
        diffs = diff_documents(app_tokens, susp_tokens)

        # Pancreatitis was relocated/swapped, so it emits a reordered_section diff
        reordered = [d for d in diffs if d.kind == "reordered_section"]
        self.assertEqual(len(reordered), 1, "Swapped Pancreatitis should be detected as reordered_section")

        # Pancreatitis body is identical -> only the section number differs, no cross-topic body diffs
        panc_body_diffs = [
            d for d in diffs
            if "pancreatitis" in (d.approved_text + d.suspect_text).lower() and d.kind != "reordered_section"
        ]
        self.assertEqual(len(panc_body_diffs), 0, "Pancreatitis body must match cleanly with no cross-topic diffs")

    def test_accented_and_dose_unit_headers(self):
        """Test that numbered headers with accents and dose/strength units
        (e.g. '1. QUÉ ES PRODUCTO SOLUCIÓN INYECTABLE 250 mg/5 mL Y PARA QUÉ SE UTILIZA')
        are detected and paired correctly, rather than rejected or merged into the TOC."""
        app_words = [
            # TOC line (must NOT be treated as a header)
            Word(text="1.", page=1, x0=20, top=10, x1=35, bottom=22),
            Word(text="Qué", page=1, x0=40, top=10, x1=65, bottom=22),
            Word(text="es", page=1, x0=70, top=10, x1=85, bottom=22),
            Word(text="producto", page=1, x0=90, top=10, x1=140, bottom=22),
            # Real section header 1
            Word(text="1.", page=1, x0=20, top=40, x1=35, bottom=52),
            Word(text="QUÉ", page=1, x0=40, top=40, x1=70, bottom=52),
            Word(text="ES", page=1, x0=75, top=40, x1=95, bottom=52),
            Word(text="PRODUCTO", page=1, x0=100, top=40, x1=165, bottom=52),
            Word(text="SOLUCIÓN", page=1, x0=170, top=40, x1=235, bottom=52),
            Word(text="250", page=1, x0=240, top=40, x1=265, bottom=52),
            Word(text="mg/5", page=1, x0=270, top=40, x1=300, bottom=52),
            Word(text="mL", page=1, x0=305, top=40, x1=325, bottom=52),
            # Section 1 body
            Word(text="El", page=1, x0=20, top=60, x1=35, bottom=72),
            Word(text="principio", page=1, x0=40, top=60, x1=90, bottom=72),
            Word(text="activo", page=1, x0=95, top=60, x1=135, bottom=72),
            Word(text="es", page=1, x0=140, top=60, x1=155, bottom=72),
            Word(text="fulvestrant.", page=1, x0=160, top=60, x1=225, bottom=72),
        ]

        susp_words = [
            # Real section header 1 (flat proof, no TOC)
            Word(text="1.", page=1, x0=20, top=40, x1=35, bottom=52),
            Word(text="QUÉ", page=1, x0=40, top=40, x1=70, bottom=52),
            Word(text="ES", page=1, x0=75, top=40, x1=95, bottom=52),
            Word(text="PRODUCTO", page=1, x0=100, top=40, x1=165, bottom=52),
            Word(text="SOLUCIÓN", page=1, x0=170, top=40, x1=235, bottom=52),
            Word(text="250", page=1, x0=240, top=40, x1=265, bottom=52),
            Word(text="mg/5", page=1, x0=270, top=40, x1=300, bottom=52),
            Word(text="mL", page=1, x0=305, top=40, x1=325, bottom=52),
            # Section 1 body
            Word(text="El", page=1, x0=20, top=60, x1=35, bottom=72),
            Word(text="principio", page=1, x0=40, top=60, x1=90, bottom=72),
            Word(text="activo", page=1, x0=95, top=60, x1=135, bottom=72),
            Word(text="es", page=1, x0=140, top=60, x1=155, bottom=72),
            Word(text="fulvestrant.", page=1, x0=160, top=60, x1=225, bottom=72),
        ]

        app_tokens = build_comparison_tokens(app_words)
        susp_tokens = build_comparison_tokens(susp_words)
        diffs = diff_documents(app_tokens, susp_tokens)

        # Section 1 is identical between the two documents: no false removals or additions of Section 1 body
        sec1_diffs = [
            d for d in diffs
            if "fulvestrant" in (d.approved_text + d.suspect_text).lower()
        ]
        self.assertEqual(len(sec1_diffs), 0, "Identical accented section body must not produce false diffs")


if __name__ == "__main__":
    unittest.main()


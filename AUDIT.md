# DocForensics Analyzer — risk audit

## Status (2026-10-04, MVP-38)

Every item below has been dealt with. Details of each fix are in README
section 39; each is locked in by a test that fails on the old code
(smoke TESTs 29–32, GUI step 24). Smoke suite: all checks pass (was 7
failures). GUI suite: 29/29.

| item | status |
|---|---|
| A1 artwork filter swallows dose text | **fixed** — narrowed; label content never demoted |
| A2 artwork outranks the digit rule | **fixed** — digit rule first; both sides must be artwork |
| A3 own test failing | **fixed** — passes |
| A4 deletions relabelled "Moved" | **fixed** — verbatim (or verbatim fragments) only; both sides for replacements; moved rows show both positions |
| A5 spacing differences dropped | **fixed** — spacing next to a digit reported as minor |
| A6 meaning changes as "typo" | **fixed** — negations/opposite prefixes critical; typo = noise shape only; misspellings major |
| A7 decimal separator swaps minor | **fixed** — critical, category Number |
| A8 near-empty suspect compared silently | **fixed** — banner + dialog + report |
| A9 partly scanned documents | **fixed** — per-page banner + report |
| A10 PPTX groups / notes | **fixed** |
| A11 DOCX headers/footers/notes/text boxes | **fixed** (comments deliberately not compared: review annotations, not document content) |
| A12 XLSX formulas | **fixed** — compared by formula when no cached value |
| B1 lost word boundaries | **fixed** — MuPDF word boxes add breaks |
| B2 two-column reading order | **not a defect** — see correction below; test fixture rewritten |
| B3 colon headers | **decided** — code behaviour kept; test now checks the business risk |
| B4 pairing failures | **not a defect** — see correction below; stale tests updated |
| B5 comparator marks whole words | **fixed** — caret ‸ marks where the other side has text |
| C1 decisions wiped after streaming | **fixed** |
| C2 misleading tooltip | **fixed** |
| C3 no export / persistence | **fixed** — File › Export Report (HTML/CSV), review saved per file pair (SHA-256) |
| C4 rematch removals unrecorded | **fixed** — rematch log, saved and exported |
| C5 severity-only ordering | **fixed** — page-order sort |
| C6 performance / housekeeping | **fixed** — extraction 2.6× less CPU with identical output (README section 40); housekeeping was not a risk (see below) |

### Corrections to this audit

* **A4, second example was wrong.** `Effective date: 05-SEP-2021` and
  `Approved date: 22-JUL-2021` exist once in BOTH documents — they
  genuinely moved. The real defect there was only that the row showed an
  empty Original side; it now shows both positions. The alcohol-warning
  example stands, and the real `S Rx Only POM` case (0 copies in the
  original, 2 in the suspect) was found while fixing it.
* **B2 was not an engine defect.** With realistic geometry (a gutter at
  least 3.5× the word spacing) columns read column-major correctly. The
  failing fixture used one 3pt token per "column" per row — the shape of
  a label/value table, which is deliberately read row-major.
* **B4 was not an engine defect.** The pairings were exactly what the
  tests demand; they failed only because a move is now reported once
  with both positions instead of as two half-rows.
* **C6.** The `.bak` files and `scratch/` are git-ignored and were never
  committed or packaged, and `.bak` files cannot be imported — no risk;
  left in place. The 68.6s on the ESMOLOC pair was not caused by glued
  tokens, as guessed: table detection dominates extraction. Not changed
  here; extraction already runs in the background when a file opens.
  The word-boundary fix adds ~20% on that one dense leaflet.

---

Date: 2026-10-03. Every point below was **reproduced by running the code**,
not inferred from reading it. Evidence and the exact `file:line` are given
for each. Ordered by business loss, worst first.

Real files used as evidence (from `C:\Users\aaqib\Downloads`):
`01-124R6 _d4.pdf`/`_d5.pdf`, `3037613.pdf`/`3037718.pdf`,
`6,7.pdf`/`6,7 major.pdf`, `3038574 - Proof Scanpdf*.pdf`.

---

## Part A — False negatives: real deviations the tool hides

These are the ones that can ship a wrong label. A deviation that is reported
at `minor`/`typo`/`reorder` is sorted to the bottom of the list and is
**removed from view entirely** by the "Ignore minor and below" tick box.

### A1. The chart-artwork filter swallows ordinary dosage text — WORST

`_is_figure_artwork_text` (`core/diff_engine.py:1966`) decides a passage is a
plot axis or diagram callout and demotes it to `minor`. It fires on ordinary
label text. Measured: **11 of 22 ordinary label phrases** are treated as
artwork.

| phrase | treated as artwork? |
|---|---|
| `Each tablet contains 20 mg` | **yes → minor** |
| `Dose 5 mg twice daily` | **yes → minor** |
| `Contains 100 mg per 5 ml` | **yes → minor** |
| `Inject 0.5 ml subcutaneously` | **yes → minor** |
| `Dilute with 5 ml water` | **yes → minor** |
| `Batch 1234 Exp 06 2027` | **yes → minor** |
| `Take 2 tablets 3 times` | **yes → minor** |
| `10 tablets of 20 mg` | **yes → minor** |
| `2 x 10 tablets` | **yes → minor** |
| `response rate 50 percent` | **yes → minor** |
| `4:26 pm, Sep 05, 2023` | **yes → minor** |

End-to-end proof, a carton panel with four lines deleted from the suspect:

```
approved: ESMOLOC 20 / Each tablet contains 20 mg / Dose 5 mg twice daily
          / Contains 100 mg per 5 ml / Batch 1234 Exp 06 2027
          / Keep out of reach of children
suspect:  ESMOLOC 20 / Keep out of reach of children

result:   minor  removed_in_suspect
          'Each tablet contains 20 mg Dose 5 mg twice daily Contains 100 mg
           per 5 ml Batch 1234 Exp 06 2027'
```

Strength, dose, concentration, batch and expiry all gone from the carton →
**one `minor` row**. Tick "Ignore minor and below" and the reviewer sees
**nothing**.

Three separate sub-rules are too loose:

* **Rule 4, chart keywords** (`:2008`). Keywords are matched as *substrings*
  (`k in t.lower()`), and the list contains plain pharma prose — `rate`,
  `response`, `severe`, `moderate`, `mild`, `impairment`, `normal`, `drug`,
  `inhibitor`, `population`, `description`, `weeks`, `change`. Any number or
  unit token also counts. Threshold is only **0.4 of tokens** at **≥4
  tokens**, so a 5-word line with 2 numbers qualifies.
* **Rule 5, device callouts** (`:2015`). Any run of **1–3 tokens** containing
  one of `adaptor plunger syringe bottle cap slow blister vial needle pouch`
  → `minor`. `Discard needle`, `single-use vial`, `Replace the cap`, `SLOW!`
  all demote.
* **Rule 1, atom symbols** (`:1973`). Any token whose letters are all drawn
  from `H C N O F P S` counts as a chemical symbol. So `no`, `NO`, `off`,
  `of`, `soon` → `minor`. A deleted `no` is a meaning inversion.

### A2. One short artwork-looking side downgrades an unlimited change on the other

`_classify_severity` (`core/diff_engine.py:2093`, `:2107`) runs the artwork
check **before** the digit/dosage rule, and joins the two sides with `or`:

```python
if _is_figure_artwork_text(approved_text) or _is_figure_artwork_text(suspect_text):
    return "minor"
```

So the artwork filter **outranks the "digit changes are always critical"
rule** the module docstring promises. Reproduced on your real pair
`3037613.pdf` vs `3037718.pdf`:

```
minor  text_changed
  approved side: 80,561 characters (the entire document body)
  suspect side:  '4:26 pm, Sep 05, 2023'   <- 5 tokens, 2 numeric = 0.4 share
```

The whole body of the approved document is missing, and it is reported as
**one `minor` row**, because the date stamp it got paired with looks like a
chart axis.

### A3. Your own test suite already fails on this

`dev_tests/test_extraction_diff_smoke.py:1981` —
`FAIL: removed prose with rising numbers was downgraded.` The suite asserts
a removed prose sentence containing rising dosages must be `critical`; it
comes back `minor`. This has been failing, so the guard is known-broken.

### A4. Genuine deletions are relabelled "Moved"

`_downgrade_relocated_runs` (`core/diff_engine.py:1925-1933`) has a
bag-of-words branch with **no minimum run length and no contiguity
requirement**: if every word of a deleted passage (≥3 chars) appears at
least as often somewhere in the other document, the passage is demoted to
`reorder` / "Moved".

Reproduced:

```
approved: "Patients should not take this medicine with alcohol." (+3 more lines)
suspect:  alcohol line deleted, a filler line added that reuses the same
          common words

result:   reorder  reordered
          A='Patients should not take this medicine with alcohol.'  S=''
```

The sentence is **gone** from the suspect, and the tool calls it "Moved"
with an empty Suspect side. Untick the "Moved" chip, or tick "Ignore minor
and below", and a deleted contraindication is invisible.

Also reproduced on your real pair `01-124R6 _d4` vs `_d5` — two stamps that
exist **only in the suspect** are reported as "Moved":

```
reorder  reordered  A=''  S='Effective date: 05-SEP-2021'
reorder  reordered  A=''  S='Approved date: 22-JUL-2021'
```

A changed effective/approval date on a controlled document is exactly what
this tool exists to catch.

### A5. Spacing differences are dropped completely — no row at all

`_is_whitespace_artifact` (`core/diff_engine.py:546`) deletes any
`text_changed` whose two sides are equal once whitespace is removed.
Verified: `'100 mg'` → `'100mg'` and `'50mg'` → `'50 mg'` produce **no
finding whatsoever**. In regulated labelling the space between value and
unit is itself controlled. The category list advertises a
`Punctuation/spacing` bucket (`core/diff_engine.py:2144`) that these can
never reach.

### A6. Meaning-reversing word pairs are classified as "typo"

`_looks_like_typo` (`core/diff_engine.py:2203`) accepts any two strings
whose letters are within 2 characters and ≥0.82 similar. Verified results,
all landing in the lowest tier:

| approved | suspect | severity |
|---|---|---|
| `hypertension` | `hypotension` | **typo** |
| `hyperglycemia` | `hypoglycemia` | **typo** |
| `hyperkalemia` | `hypokalemia` | **typo** |
| `10 mg/kg` | `10 mg/g` | **typo** (1000×) |
| `should` | `shouldn't` | **typo** (inversion) |
| `intravenous` | `intravenously` | **typo** |

The docstring claims it "never fires on numbers or mixed alphanumeric
content" and that both sides "must be purely alphabetic" — the code never
checks that. It strips non-letters and compares what is left, which is how
`mg/kg` vs `mg/g` passes.

### A7. Decimal-separator swaps are "minor punctuation"

`core/diff_engine.py:2118` — once digit runs match, a punctuation-only
difference is `minor`. Verified: `'5.000'` → `'5,000'` and `'1.5'` → `'1,5'`
are both `minor`. Between EU and US conventions that is a 1000×
difference in strength.

### A8. A near-empty suspect is compared instead of refused

`is_scanned_pdf` (`core/pdf_extractor.py:2842`) asks whether **any** page
has ≥20 non-whitespace characters. A document whose body is image-only but
carries one digital stamp passes as "has real text". Verified on
`3037718.pdf`:

```
3037613.pdf: 4 pages, chars per page = [37881, 37172, 426, 0]
3037718.pdf: 4 pages, chars per page = [41, 0, 0, 0]

extraction: 6653 tokens  vs  9 tokens
suspect text in full: ['By','Packaging','Development','at','4:26','pm,','Sep','05,','2023']
diff_documents -> 2 rows   (1 critical, 1 minor)
```

A suspect that extracted **9 words against 6,653** is reported as
"2 differences". Nothing anywhere checks for a gross token-count or
page-count mismatch, and nothing warns the reviewer.

### A9. Mixed digital/scanned documents are silently partial

`is_scanned_pdf`'s docstring states the check is deliberately
whole-document, so a file with some scanned pages is reported as *not*
scanned. Those pages contribute zero text and are never compared. There is
no per-page warning in the UI (`page_has_text_layer` exists at
`core/pdf_extractor.py:2832` but is **never called** outside tests).

### A10. PPTX: grouped shapes extract as nothing — verified 0 tokens

`core/document_extractor.py:438` iterates `slide.shapes` and handles only
`has_table` / `has_text_frame`. A group shape has neither, and the code
never recurses into it. Built and ran a real deck:

```
slide with two text boxes grouped together
top-level shapes: type=GROUP (6)  has_table=False  has_text_frame=False
extract_document_tokens -> 0 tokens: []
```

Both text boxes — including `Each tablet contains 20 mg` — are invisible to
the comparison, with no warning. Artwork decks are routinely grouped.
Speaker notes (`notes_slide`) are also never read.

### A11. DOCX approximate path reads the body only

`_docx_blocks` (`core/document_extractor.py:289`) walks
`document.element.body.iterchildren()`. **Headers, footers, footnotes,
endnotes, text boxes and comments are never read** — i.e. document code,
version and revision date, the controlled part of a regulated document.
This path is used only when no Word/LibreOffice converter is installed; the
panel does label it "approximate layout" (`ui/document_panel.py:948`), which
is good, but the label does not say what is missing.

### A12. XLSX: formula cells vanish from the comparison — verified

`core/document_extractor.py:515` loads with `data_only=True`, which returns
the *cached* value and `None` when the workbook has not been recalculated by
Excel. Verified:

```
A1 Strength 20 | A2 Units 3 | A3 'Total mg' =B1*B2
tokens: ['Strength','20','Units','3','Total','mg', ...]
the computed total dose cell is absent entirely
```

A whole column of computed values can silently drop out. (Hidden sheets *are*
compared — verified — which is defensible, but worth knowing.)

---

## Part B — False positives: noise that buries the real findings

### B1. Word boundaries are lost on small type — 12% of a real document

`core/pdf_extractor.py:966` calls `extract_words(x_tolerance=2, ...)`. The
tolerance is a **fixed 2 points** regardless of font size. Your documents are
set at 6.3pt, where the inter-word space is under 2pt — so word boundaries
are swallowed and whole lines become single tokens.

Measured on `3037613.pdf`:

```
body font size: 6.3pt (35,544 of 38,185 glyphs)
inter-glyph gap distribution: 2,595 gaps fall in the 1-2pt band
   -> every one of those is a real word space treated as INSIDE a word

822 of 6,653 tokens (12%) are longer than 22 characters; longest 126
'Esomeprazolebelongstoaclassofantisecretorycompounds'
'patientswhohavenothealedafter4to8weeksoftreatment,'
'studiesdonotextendbeyond6months.'
```

Both engines do it (`mupdf` default and `pdfplumber`: 1,026 long tokens,
longest 413). Sweeping the tolerance on page 1:

| `x_tolerance` | words | tokens > 22 chars |
|---|---|---|
| **2.0 (shipped)** | 3,886 | **353** |
| 1.5 | 4,849 | 239 |
| 1.2 | 5,607 | 150 |
| **1.0** | **6,484** | **11** |

PyMuPDF's own `get_text("words")` finds 6,475 on that page — so at
`x_tolerance=1.0` extraction is correct and **the shipped setting is losing
about 40% of word boundaries**.

Consequences: a change inside a glued line is reported as one unreadable
50–126 character blob; and when the counterpart PDF is typeset slightly
differently, the two token streams stop aligning and the whole region turns
into false positives. Fix direction: scale the tolerance with font size
(pdfplumber convention ≈ 0.25 × size) instead of a fixed 2pt.

### B2. Two-column reading order is row-major, not column-major

`dev_tests/test_extraction_diff_smoke.py:1523` is failing:

```
FAIL: expected zone 2 to read ['A0','A1','A2','A3','A4','B0','B1','B2','B3','B4'],
      got ['A0','B0','A1','B1','A2','B2','A3','B3','A4','B4']
```

A genuine narrow (8pt) gutter is detected but the words are emitted
interleaved across the two columns. Two identical layouts scramble
identically and still match, so this hides until the suspect is typeset
differently (full vs concise, different panel widths) — then a whole section
reads in a different order and every line of it is reported.

### B3. Colon headers no longer start a section, and the suite disagrees

`core/section_diff.py:778` contains
`not (has_numbered and line_text.strip().endswith(":"))` — in a numbered
document, a colon header is refused. So `Manufactured by:`,
`Distributed by:`, `Revised:` do not start their own section and get
absorbed into whatever numbered section precedes them. These blocks move
between panels constantly, so their content ends up compared against
unrelated text.

The suite still expects the old behaviour and fails:

```
FAIL: repeated header sections: ['', '1 INDICATIONS AND USAGE']
      expected ['Manufactured by:', '1 INDICATIONS AND USAGE', 'Manufactured by:']
```

The code and the test contradict each other, so **nobody currently knows
which is intended**. This one needs your decision, not just a fix.

### B4. Two more engine failures in your own suite

```
FAIL: moved 'epsilon' was not paired as two reorder items:
      [('reordered','epsilon','epsilon')]
FAIL: interleave: matches={0:0, 1:1, 4:3, 3:5, 2:2, 5:4} criticals=[]
      reorders=[('eta theta iota kappa','eta theta iota kappa')]
```

The second is a mis-pairing in section matching (`4→3` and `3→5` crossing),
which is the mechanism behind the paragraph-swap false positives.

### B5. The comparator marks whole words that did not change

`_refine_chars` (`core/inline_diff.py:73-78`) forces at least one marked run
per side. When the change is a pure insertion or deletion inside a word, the
counterpart side has nothing to mark, so **the entire word is highlighted**:

```
FAIL: 'Ropinirole 5 mg' vs 'Ropinirol 50 mg'
      marked ['e','5'] / ['Ropinirol','0']   expected ['e'] / ['0']
FAIL: 'take one tablet' vs 'take two tablets'
      marked ['one','tablet'] / ['two','s']  expected ['one'] / ['two','s']
```

The reviewer is told `tablet` changed when only an `s` was added. It wastes
review time on the one surface where precision matters most.

---

## Part C — Workflow and record-keeping risks

### C1. Review decisions taken while results stream are silently wiped

Differences now appear as they are found, so a reviewer can start working
immediately — but `set_results` (`ui/results_panel.py:319`) resets
`_decisions` and `_viewed_items`, and `main.py:880` calls it when the
comparison finishes. Verified headless:

```
during streaming:  decision on row 0 = Decision(status='accepted', reviewer='aaqib', ...)
                   viewed = True
after set_results: decision on row 0 = None
                   viewed = False
```

Everything accepted or rejected during the run is lost, with no notice.

### C2. The "Ignore minor and below" tooltip misdescribes what it hides

`ui/results_panel.py:180` promises it hides "punctuation-only changes,
character-level typos, and moved text". Because of A1/A2/A4/A6 it also hides
deleted dosage blocks, changed strengths and added effective dates.
Verified:

```
rows: [minor] deleted 'Each tablet contains 20 mg Dose 5 mg twice daily Batch 1234 Exp 06 2027'
      [reorder] added 'Effective date: 05-SEP-2021'
with 'Ignore minor and below' ticked -> table shows 0 of 2 rows
```

A reviewer ticks it on the tooltip's promise and silently loses both.

### C3. No export, no report, no persistence — no audit trail

There is no export, report, save or print anywhere in `main.py` (File menu:
Open, Merge, Compare, Crop, Exit). Nothing writes the findings or the
decisions to disk. `Decision` captures reviewer name and a UTC timestamp —
and discards them when the app closes. For regulated proofing that means no
evidence pack to attach to a batch record, no reviewer/time record, and a
full re-review if the app is closed. This is a commercial blocker as much as
a technical one.

### C4. A rematch can delete a true finding, unrecorded

`replace_item` (`ui/results_panel.py:375`) can return an empty replacement
list, removing the row. Only an in-session Ctrl+Shift+Z brings it back, and
nothing records that a row was removed or why.

### C5. The list is severity-ordered only

`_finalize_items` sorts by severity (`core/diff_engine.py:568`) and the table
has no sorting controls (`setSortingEnabled` is never called). A reviewer
cannot walk the documents in page order, so anything scored `minor` sits
below hundreds of rows and is never reached in practice. This is what turns
every Part A downgrade into an actual miss.

### C6. Performance and housekeeping

* `3037613.pdf`/`3037718.pdf` took **68.6s** to extract and compare — 4
  pages. The glued mega-tokens (B1) are the likely cause.
* `core/` contains `diff_engine.py.bak`, `document_extractor.py.bak`,
  `pdf_extractor.py.bak`, `section_diff.py.bak`, and a `scratch/` directory
  of one-off patch scripts is committed. A stale `.bak` imported by mistake
  would be very hard to spot.
* `_MAX_MOVE_CANDIDATES = 25` (`core/diff_engine.py:1708`) caps moved-block
  candidates; defensible, but it is a silent cap.

---

## Suggested order of work

1. **A1 + A2** — never let the artwork filter outrank the digit rule, never
   let a short artwork-looking side demote a long real side, and tighten
   rules 1/4/5. This is the one that can ship a wrong dose.
2. **A4** — require a minimum length and contiguity before calling a
   deletion "Moved".
3. **B1** — make `x_tolerance` scale with font size. Fixes both the
   unreadable blobs and the cross-typesetting false positives.
4. **C2 + C5** — correct the tooltip and let the list sort by page, so a
   `minor` row is at least reachable while A1–A7 are being fixed.
5. **C3** — export + decision persistence. Needed to sell into a regulated
   workflow at all.
6. **A5, A6, A7** — give spacing its own visible category; stop the
   `hyper`/`hypo` class of change reaching `typo`; treat a decimal-separator
   swap as a number change.
7. **A8 + A9** — refuse or loudly warn on a gross token-count mismatch and
   on pages with no text layer.
8. **A10, A11, A12** — recurse into PPTX groups, read DOCX headers/footers,
   and handle uncached XLSX formulas.
9. **B3** — decide whether colon headers start a section, then make the code
   and the test agree.
10. **B2, B4, B5** — reading order, section mis-pairing, comparator marking.

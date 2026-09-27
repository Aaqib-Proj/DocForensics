import fitz
import pdfplumber
import unicodedata

def make_pdf(path, text):
    doc = fitz.open()
    page = doc.new_page(width=500, height=200)
    # Using a standard font that might not support Arabic perfectly,
    # but fitz handles it or we can just pass characters.
    # PyMuPDF has a built in font for this? 
    # Actually, we can just insert English and Arabic characters.
    # We will just write visual strings if fitz doesn't shape Arabic.
    # But let's try just inserting text.
    page.insert_textbox(fitz.Rect(50, 50, 450, 150), text, fontsize=20, fontname="helv", align=fitz.TEXT_ALIGN_RIGHT)
    doc.save(path)
    doc.close()

def test_pdf():
    text = "أبديل-أتش (شراب السعال العشبي) H-1"
    make_pdf("test_rtl.pdf", text)
    
    with pdfplumber.open("test_rtl.pdf") as pdf:
        page = pdf.pages[0]
        words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False, use_text_flow=False)
        print("use_text_flow=False:")
        for w in words:
            print(f"  x0={w['x0']:.1f} text={w['text']}")
            
        words2 = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False, use_text_flow=True)
        print("\nuse_text_flow=True:")
        for w in words2:
            print(f"  x0={w['x0']:.1f} text={w['text']}")

test_pdf()

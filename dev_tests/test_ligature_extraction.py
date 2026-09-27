import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from core.pdf_extractor import extract_words

SAMPLE_PDF = r'C:\Documents\samples proof\sample 2\100 pages diff.pdf'

@pytest.mark.skipif(not os.path.exists(SAMPLE_PDF), reason='Sample PDF not present')
def test_ligature_words_not_split():
    words = extract_words(SAMPLE_PDF, first_page=18, last_page=18)
    texts = [w.text for w in words]
    
    # 'pharmacokinetic' must be intact, not split into 'pharmacokineti' and 'c'
    assert 'pharmacokinetic' in texts
    assert 'pharmacokineti' not in texts
    
    # 'tizanidine' must be intact, not split into 'ti' and 'zanidine'
    assert any('tizanidine' in t.lower() for t in texts)
    assert not any(t == 'zanidine' for t in texts)
    
    # 'Populations' must be intact, not split into 'Populati' and 'ons'
    assert 'Populations' in texts
    assert 'Populati' not in texts

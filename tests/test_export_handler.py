"""
tests/test_export_handler.py — Unit tests for PDF & Word DOCX document generation.
"""

from export_handler import generate_pdf_bytes, generate_docx_bytes

SAMPLE_SUMMARY = """# Lecture Summary: Machine Learning Basics

## Key Concepts
- **Supervised Learning**: Training models on labeled data (e.g. classification).
- **Unsupervised Learning**: Discovering hidden patterns in unlabeled data.
- **Overfitting**: Model memorizing noise rather than general patterns.

### Action Items
1. Complete assignment 3 by Friday.
2. Review cross-validation techniques.
"""

def test_generate_pdf_bytes():
    pdf_bytes = generate_pdf_bytes(
        title="Machine Learning 101",
        summary_text=SAMPLE_SUMMARY,
        metadata={"duration": "45m 20s", "language": "English"},
    )
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 500
    assert pdf_bytes.startswith(b"%PDF")

def test_generate_docx_bytes():
    docx_bytes = generate_docx_bytes(
        title="Machine Learning 101",
        summary_text=SAMPLE_SUMMARY,
        metadata={"duration": "45m 20s", "language": "English"},
    )
    assert isinstance(docx_bytes, bytes)
    assert len(docx_bytes) > 500
    assert docx_bytes.startswith(b"PK")  # docx is a zip file

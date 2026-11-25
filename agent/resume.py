"""Extract plain text from an uploaded resume (PDF, DOCX or TXT)."""
import io
import re

from agent import config


class ResumeError(ValueError):
    pass


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages[:10])
        except Exception as e:
            raise ResumeError("Couldn't read that PDF. Try saving it again or upload a DOCX.") from e
    elif name.endswith(".docx"):
        from docx import Document
        try:
            doc = Document(io.BytesIO(data))
        except Exception as e:
            raise ResumeError("Couldn't read that Word file. Try a PDF instead.") from e
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        text = "\n".join(parts)
    elif name.endswith(".txt"):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ResumeError("Please upload a PDF, DOCX or TXT file.")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) < 100:
        raise ResumeError("Couldn't find enough text in that file. If it's a scanned image, "
                          "upload a text-based PDF or DOCX.")
    return text[:config.MAX_RESUME_CHARS]

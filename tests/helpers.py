"""Test helpers: temporary projects, a fake HTTP response and a tiny PDF writer. No network."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

RESUME_TEXT = (
    "John Doe — New Grad / SDE-1\n"
    "Email: john.doe@example.com | linkedin.com/in/john-doe\n\n"
    "EXPERIENCE\n"
    "Software Engineer, Acme (2023-2025): built REST APIs in Java and Spring Boot with PostgreSQL,\n"
    "Docker and AWS. Wrote React and TypeScript dashboards used by 5,000 people.\n\n"
    "PROJECTS\n"
    "Realtime chat on Redis pub/sub; a FastAPI service with pgvector search.\n"
)


def write_project(tmp: Path, settings_yaml: str, resumes: dict[str, str] | None = None,
                  plugins: dict[str, str] | None = None) -> Path:
    """Create settings, resume and plugin files under tmp; returns the settings path."""
    (tmp / "resumes").mkdir(parents=True, exist_ok=True)
    for name, text in (resumes if resumes is not None else {"backend.txt": RESUME_TEXT}).items():
        (tmp / "resumes" / name).write_text(text, encoding="utf-8")
    if plugins:
        (tmp / "plugins").mkdir(exist_ok=True)
        for name, code in plugins.items():
            (tmp / "plugins" / name).write_text(textwrap.dedent(code), encoding="utf-8")
    path = tmp / "jobhunter.yaml"
    path.write_text(textwrap.dedent(settings_yaml), encoding="utf-8")
    return path


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else (json.dumps(json_data) if json_data is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            return json.loads(self.text)
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def make_pdf(path: Path, lines: list[str]) -> None:
    """Write a one-page PDF with plain Helvetica text that pypdf can extract."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = "BT /F1 12 Tf 14 TL 72 720 Td " + " ".join(f"({esc(l)}) '" for l in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(path).write_bytes(out)

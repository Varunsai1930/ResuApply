"""Attachment headers shared by current and frozen resume downloads."""

import re
import unicodedata
from urllib.parse import quote

from fastapi.responses import Response

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_response(content: bytes, name: str, company: str, kind: str = "resume") -> Response:
    title = " - ".join(part for part in (name, company, kind) if part)
    title = re.sub(r'[\\/<>:"|?*\x00-\x1f\x7f]', " ", title).strip(" .")[:160] or "resume"
    filename = title + ".docx"
    fallback = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    # Compatibility normalization can introduce quotes and path separators that
    # were not present in the original Unicode title (for example, fullwidth forms).
    fallback = re.sub(r'[\\/<>:"|?*\x00-\x1f\x7f]', " ", fallback).strip(" .") or "resume"
    return Response(content, media_type=DOCX_TYPE, headers={
        "Content-Disposition": f'attachment; filename="{fallback}.docx"; filename*=UTF-8\'\'{quote(filename, safe="")}',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })

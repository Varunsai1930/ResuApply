"""Attachment headers shared by current and frozen resume downloads."""

import re
import unicodedata
from urllib.parse import quote, urlencode

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_response(content: bytes, name: str, company: str, kind: str = "resume") -> Response:
    return _attachment(content, name, company, kind, "docx", DOCX_TYPE)


def pdf_response(content: bytes, name: str, company: str) -> Response:
    return _attachment(content, name, company, "resume", "pdf", "application/pdf")


def _attachment(content: bytes, name: str, company: str, kind: str, extension: str, media_type: str) -> Response:
    title = " - ".join(part for part in (name, company, kind) if part)
    title = re.sub(r'[\\/<>:"|?*\x00-\x1f\x7f]', " ", title).strip(" .")[:160] or "resume"
    filename = title + "." + extension
    fallback = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    # Compatibility normalization can introduce quotes and path separators that
    # were not present in the original Unicode title (for example, fullwidth forms).
    fallback = re.sub(r'[\\/<>:"|?*\x00-\x1f\x7f]', " ", fallback).strip(" .") or "resume"
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{fallback}.{extension}"; filename*=UTF-8\'\'{quote(filename, safe="")}',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


def preview_response(document, base_url: str, layout: str = "") -> JSONResponse:
    query = urlencode({"version": document.version, **({"layout": layout} if layout else {})})
    return JSONResponse({"page_count": document.pages, "version": document.version,
                         "pages": [f"{base_url}/{page}.png?{query}" for page in range(1, document.pages + 1)]},
                        headers={"Cache-Control": "no-store"})


def preview_image(document, page_number: int, version: str) -> Response:
    if version != document.version:
        raise HTTPException(409, "The resume changed. Reload its preview.")
    try:
        content = document.image(page_number)
    except IndexError as exc:
        raise HTTPException(404, "Preview page not found.") from exc
    return Response(content, media_type="image/png", headers={"Cache-Control": "no-store",
                                                             "X-Content-Type-Options": "nosniff"})

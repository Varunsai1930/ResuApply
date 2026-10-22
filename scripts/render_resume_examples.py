"""Regenerate the original gallery previews after changing a layout or demo text.

Run from the repository root: python -m scripts.render_resume_examples
Requires the optional local browser add-on. Gallery visitors need no renderer.
"""

from io import BytesIO

from PIL import Image

from app.services.resume_examples import SAMPLE_RESUME
from app.services.resume_layouts import LAYOUTS
from app.services.resume_pdf import export_resume
from app.templating import STATIC_DIR


def main():
    destination = STATIC_DIR / "resume-examples"
    destination.mkdir(exist_ok=True)
    for layout in LAYOUTS:
        document = export_resume(SAMPLE_RESUME, layout.key)
        if document.pages != 1:
            raise ValueError(f"{layout.label} demo must fit on one page; got {document.pages}.")
        with Image.open(BytesIO(document.image(1))) as image:
            image.convert("RGB").save(destination / f"{layout.key}.webp", quality=88, method=6)
        print(f"Rendered {layout.label}: one A4 page")


if __name__ == "__main__":
    main()

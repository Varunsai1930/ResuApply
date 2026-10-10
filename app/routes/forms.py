"""Reading the editors' forms with limits that fit them.

Starlette refuses a form with more than 1,000 fields or a field over 1 MB, and the refusal is a
bare 400 that discards everything the user entered. Every requirement row in the editor submits
21 fields, so about 47 requirements already hit the default, and a long profile's reviewed
payload can pass 1 MB. The editors read their forms through ``read_form`` instead; the services
keep their own, friendlier limits (for example ``requirements.MAX_REQUIREMENTS``).
"""

from __future__ import annotations

from fastapi import Request
from starlette.datastructures import FormData

MAX_FIELDS = 20_000
MAX_PART_SIZE = 16 * 1024 * 1024  # 16 MB for one field, such as the reviewed profile payload


async def read_form(request: Request) -> FormData:
    return await request.form(max_fields=MAX_FIELDS, max_part_size=MAX_PART_SIZE)

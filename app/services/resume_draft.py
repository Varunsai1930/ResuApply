"""A draft profile from a resume's text, read locally by layout rules. Nothing is sent anywhere.

The draft only fills the profile form: the user checks every field and the usual review step
shows each change before anything is saved. The rules are deliberately plain:

- contact details come from the lines before the first section heading (at most ``HEADER_LINES``);
- known headings (Summary, Education, Experience, Projects, Skills, Certifications) split the rest;
- in Education, Experience and Projects, a line starting with a bullet mark is a bullet, and other
  lines are an entry's heading, from which dates and the parts between separators are read;
- skills are read as "Category: a, b" lines or plain lists.

A heading for a section the profile doesn't hold (Awards, Volunteering) ends the section before
it, so its lines are left out rather than put in the wrong place; the extracted text stays on
screen for the user to copy from. Only listed headings count: an ALL-CAPS name or company is
not taken for one.
An existing profile is never overwritten: ``merge`` only fills empty fields and adds entries,
skills and certifications it doesn't already have.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .profile import LINK_KEYS
from .skills import canon

# ---------------------------------------------------------------- headings

_HEADINGS = {
    "summary": ("summary", "professional summary", "profile", "objective", "about me", "about"),
    "education": ("education", "academic background", "education and training"),
    "experience": ("experience", "work experience", "professional experience", "employment",
                   "employment history", "work history", "internships", "relevant experience"),
    "projects": ("projects", "personal projects", "selected projects", "academic projects", "technical projects"),
    "skills": ("skills", "technical skills", "core skills", "skills and tools", "technologies",
               "tools and technologies", "skills and technologies", "programming languages", "tools"),
    "certifications": ("certifications", "certificates", "licenses and certifications", "certifications and licenses"),
}
# Sections the profile doesn't hold: their lines are left out.
_OTHER_HEADINGS = (
    "awards", "honors", "honors and awards", "awards and honors", "achievements", "activities",
    "extracurricular activities", "leadership", "leadership and activities", "volunteering",
    "volunteer experience", "volunteer work", "community service", "publications", "presentations",
    "research", "languages", "interests", "hobbies", "hobbies and interests", "references",
    "affiliations", "memberships", "professional memberships", "additional information", "coursework",
    "relevant coursework",
)
_HEADING_OF = {name: section for section, names in _HEADINGS.items() for name in names}
_HEADING_OF.update({name: "other" for name in _OTHER_HEADINGS})


_COMPACT = {name.replace(" ", ""): section for name, section in _HEADING_OF.items()}
# Words a heading puts before a section's name: "Research Experience", "Software Projects".
_QUALIFIERS = {"relevant", "selected", "professional", "technical", "work", "academic", "key", "additional",
               "research", "industry", "internship", "leadership", "teaching", "software", "engineering",
               "programming", "computer", "course", "personal", "notable", "recent", "core"}


def _named(key: str) -> str | None:
    """The section a heading's words name, with a qualifying word before it allowed."""
    if key in _HEADING_OF:
        return _HEADING_OF[key]
    first, _, rest = key.partition(" ")
    if first in _QUALIFIERS and rest in _HEADING_OF and _HEADING_OF[rest] != "other":
        return _HEADING_OF[rest]
    return None


_WITH_CERTIFICATIONS = "+certifications"  # "skills+certifications": a line is one or the other
_CERTIFICATE = re.compile(r"(?i)certif|licen[cs]e|credential")


def heading(line: str) -> str | None:
    """The section a heading line starts, or None: for the import, which finds a page's columns by their headings."""
    return _heading(line)


def _heading(line: str) -> str | None:
    """The section a heading line starts ("other" for one the profile doesn't hold), or None for any other line.

    Besides the listed headings, this reads a qualified one ("Research Experience"), a combined one
    ("Skills & Interests" is Skills, "Leadership & Activities" is left out) when every part is a
    heading, and a letter-spaced one ("E X P E R I E N C E"). A combined heading of two sections the
    profile holds is left out ("Experience & Education"), except Certifications with Skills or with
    Education, whose lines are each read as a certification or as the other section's.
    """
    if len(line) > 60:
        return None
    key = re.sub(r"\s+", " ", line.strip().rstrip(":").replace("&", " and ")).lower().strip()
    key = re.sub(r"\s+", " ", key)
    if (section := _named(key)) is not None:
        return section
    words = key.split()
    if len(words) >= 4 and all(len(word) == 1 for word in words):
        return _COMPACT.get("".join(words))
    parts = [part.strip() for part in re.split(r" and | / |/", key)]
    if len(parts) > 1:
        sections = [_named(part) for part in parts]
        if all(sections):
            held = {section for section in sections if section != "other"}
            if held in ({"skills", "certifications"}, {"education", "certifications"}):
                return (held - {"certifications"}).pop() + _WITH_CERTIFICATIONS  # read line by line
            # Other sections the profile holds can't be told apart line by line: left out, not misfiled.
            return held.pop() if len(held) == 1 else "other"
    return None


# ---------------------------------------------------------------- contact details

# Bounded as addresses are (64 characters before the @, 63 per domain label): unbounded, a long line
# without an @ took quadratic time, about 17 seconds for 100,000 characters.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
_PHONE = re.compile(r"\+?\(?\d[\d\s().-]{7,}\d")
_LINK = re.compile(r"(?:https?://)?(?:www\.)?(linkedin\.com|github\.com)/[^\s|,•·]+", re.I)
_URL = re.compile(r"(?:https?://|www\.)[^\s|,•·]+", re.I)
# "Austin, TX", "Toronto, Canada", or a work mode. Only the work modes ignore case: "Python, SQL" is no place.
_LOCATION = re.compile(r"[A-Z][A-Za-z .'-]+, (?:[A-Z]{2}|[A-Z][a-z]+(?: [A-Z][a-z]+)*)|(?i:remote|hybrid|on-?site)")
_YEARS = re.compile(r"\d{4}\s*[-–—]\s*\d{4}")  # a date range, not a phone number
HEADER_LINES = 30  # contact details are read from at most this many lines before the first heading
_SEPARATORS = re.compile(r"\s*(?:\||•|·|–|—|\s-\s)\s*")


def _bare(url: str) -> str:
    """A web address without its scheme or "www.", as the profile keeps links."""
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", url, flags=re.I)


def has_contact(line: str) -> bool:
    """Whether a line holds a contact detail: an email, a phone number, a web link or a place."""
    if _EMAIL.search(line) or _LINK.search(line) or _URL.search(line):
        return True
    phone = _PHONE.search(line)
    if phone and 8 <= len(re.sub(r"\D", "", phone.group(0))) <= 15 and not _YEARS.fullmatch(phone.group(0).strip()):
        return True
    return any(_LOCATION.fullmatch(part.strip(" ,")) for part in _SEPARATORS.split(line))


def _contact(lines: list[str]) -> dict:
    contact: dict = {"name": "", "email": "", "phone": "", "location": "", "links": {k: "" for k in LINK_KEYS}}
    for line in lines:
        rest = line
        if not contact["email"] and (m := _EMAIL.search(rest)):
            contact["email"], rest = m.group(0), rest.replace(m.group(0), " ")
        for m in _LINK.finditer(rest):
            key = "linkedin" if m.group(1).lower().startswith("linkedin") else "github"
            contact["links"][key] = contact["links"][key] or _bare(m.group(0))
        rest = _LINK.sub(" ", rest)
        if not contact["links"]["portfolio"] and (m := _URL.search(rest)):
            contact["links"]["portfolio"], rest = m.group(0), rest.replace(m.group(0), " ")
        if (not contact["phone"] and (m := _PHONE.search(rest)) and not _YEARS.fullmatch(m.group(0).strip())
                and 8 <= len(re.sub(r"\D", "", m.group(0))) <= 15):
            contact["phone"], rest = m.group(0).strip(), rest.replace(m.group(0), " ")
        for part in _SEPARATORS.split(rest):
            part = part.strip(" ,")
            if not part:
                continue
            if not contact["location"] and _LOCATION.fullmatch(part):
                contact["location"] = part
            elif (not contact["name"] and re.fullmatch(r"[A-Za-zÀ-ÿ.'-]+(?: [A-Za-zÀ-ÿ.'-]+){1,3}", part)
                  and part[0].isupper()):
                contact["name"] = part
    return contact


# ---------------------------------------------------------------- dates

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_SEASON = r"(?:spring|summer|fall|autumn|winter)"
# A month name starts a word and is at most "sep" and six more letters ("september"): unbounded, or
# matched inside a word, a long run such as "decdecdec..." took quadratic time (17 s for 60,000 letters).
_DATE = (r"(?:\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]{0,6}\.?\s+(?:\d{4}|['’]\d{2}\b)"
         rf"|{_SEASON}\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}|\d{{4}})")
_RANGE = re.compile(rf"({_DATE})\s*(?:-|–|—|to)\s*({_DATE}|present|current|now|ongoing)", re.I)
_SHORT_RANGE = re.compile(r"\b((?:19|20)\d{2})(\s*[-–—]\s*)(\d{2})\b(?![/-])")  # "2021–22"
_SINGLE = re.compile(rf"(?:expected\s+|anticipated\s+)?({_DATE})", re.I)


def _date(text: str) -> str:
    """A date as the profile stores it: YYYY-MM or YYYY, or "present"."""
    text = text.strip().lower()
    if text in ("present", "current", "now", "ongoing"):
        return "present"
    if m := re.fullmatch(r"([a-z]+)\.?\s+['’](\d{2})", text):  # "May '24": this century's
        text = f"{m.group(1)} 20{m.group(2)}"
    if m := re.fullmatch(r"([a-z]+)\.?\s+(\d{4})", text):
        month = _MONTHS.get(m.group(1)[:3])  # a season names no month: only its year is kept
        return f"{m.group(2)}-{month:02d}" if month else m.group(2)
    if m := re.fullmatch(r"(\d{1,2})/(\d{4})", text):
        return f"{m.group(2)}-{int(m.group(1)):02d}" if 1 <= int(m.group(1)) <= 12 else m.group(2)
    return text


def _short_end(start: int, dash: str, short: int) -> int | None:
    """The year a short range "2021–22" ends in, or None when it isn't one.

    "2024-05", written with a plain hyphen and a month number, is a year and month. Otherwise the
    range ends in the first year with those last two digits after the start, within ten years:
    "1999–00" ends in 2000, and "2024–05" ends in no year at all.
    """
    if dash == "-" and 1 <= short <= 12:
        return None
    century = start // 100 * 100
    return next((end for end in (century + short, century + 100 + short) if start < end <= start + 10), None)


def _dates(text: str, single_is_end: bool) -> tuple[str, str, str]:
    """(start, end, text without the dates)."""
    if m := _RANGE.search(text):
        return _date(m.group(1)), _date(m.group(2)), text[:m.start()] + " " + text[m.end():]
    for m in _SHORT_RANGE.finditer(text):
        if (end := _short_end(int(m.group(1)), m.group(2), int(m.group(3)))) is not None:
            return m.group(1), str(end), text[:m.start()] + " " + text[m.end():]
    if m := _SINGLE.search(text):
        value = _date(m.group(1))
        rest = text[:m.start()] + " " + text[m.end():]
        return ("", value, rest) if single_is_end else (value, "", rest)
    return "", "", text


def _parts(text: str) -> list[str]:
    text = re.sub(r"\(\s*\)", " ", text)
    return [p.strip(" ,()") for p in _SEPARATORS.split(text) if p.strip(" ,()")]


# ---------------------------------------------------------------- entries

# Bullet marks as resumes' text gives them: dots, dashes, squares, arrows and ticks, Word's Symbol and
# Wingdings bullets (private-use characters such as U+F0B7 when a PDF has no Unicode for them), and the
# "o" and ">" of a second-level list. Each must be followed by a space; an "o" only before a capital.
_BULLET = re.compile(r"^(?:[•\-*–—▪●◦‣·■□◆◇○►▶▸➢➤→✓✔❖\uf0a7\uf0b7\uf076\uf0d8\uf0fc\uf0a8>]|o(?= +[A-Z]))\s+")
_ROLE = re.compile(r"\b(intern|engineer|developer|analyst|assistant|manager|researcher|scientist|designer|"
                   r"consultant|lead|associate|fellow|coordinator|specialist|tutor|teaching|technician|founder)\b", re.I)
_SCHOOL = re.compile(r"\b(university|college|institute|school|academy|polytechnic)\b", re.I)
# Long forms first, and an abbreviation must end the word: "Bachelor" is not "B.A." followed by "chelor".
_DEGREE = re.compile(r"\b(Bachelor(?:'s)?(?: of [A-Z][a-z]+)?|Master(?:'s)?(?: of [A-Z][a-z]+)?|Associate(?:'s)?|"
                     r"Ph\.?D\.?|MBA|B\.?Sc\.?|B\.?Eng\.?|M\.?Sc\.?|M\.?Eng\.?|B\.?S\.?|B\.?A\.?|M\.?S\.?|M\.?A\.?)"
                     r"(?![A-Za-z])", re.I)
_GPA = re.compile(r"\bGPA:?\s*(\d\.\d{1,2})(?:\s*/\s*\d\.\d{1,2})?", re.I)


@dataclass
class _Entry:
    heading: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)


def _starts_entry(line: str) -> bool:
    """Whether a line reads as an entry's heading: it has separators or dates, which a sentence rarely has."""
    return bool(_SEPARATORS.search(line) or _RANGE.search(line)
                or re.search(rf"(?i)\b(?:{_MONTH}[a-z]{{0,6}}\.?\s+\d{{4}}|present)\b", line))


WRAP = 40  # a bullet line this long may have wrapped onto the next line
# Words a phrase doesn't end on: a bullet ending with one goes on in the next line.
_DANGLING = {"a", "an", "the", "to", "and", "or", "of", "for", "with", "using", "in", "on", "by", "from", "that",
             "which", "into", "across", "via", "as", "at", "while", "including", "through", "over", "per"}


def _dangling(text: str) -> bool:
    """Whether a bullet stops mid-phrase: on a comma, a dash or a word like "using" or "to"."""
    words = text.rstrip().split()
    return bool(words) and (words[-1][-1] in ",-–&/" or words[-1].lower() in _DANGLING)


def _fragment(line: str) -> bool:
    """Whether a line reads as part of a sentence rather than a title: it has a lower-case word longer
    than three letters ("Flask and PostgreSQL for orders"), which a name such as "University of Texas
    at Austin" or "Budget Tracker" doesn't have."""
    return any(len(word) > 3 and word[0].islower() for word in re.findall(r"[A-Za-z][\w'-]*", line))


def _entries(lines: list[str], section: str = "") -> list[_Entry]:
    """Entries of a section, from its lines with "" where the text had a blank line.

    A line after a bullet continues it (the bullet wrapped) when it starts in lower case, or when
    the bullet didn't end a sentence, neither this line nor the next reads as an entry's heading (a
    two-line heading), and either the bullet's last line was long enough to wrap or this line reads
    as part of a sentence rather than a title. A bullet that stopped mid-phrase ("... reports to")
    always goes on, unless the line is an entry's heading. A blank line ends a bullet, and the entry
    too unless a bullet comes next.
    """
    entries: list[_Entry] = []
    blank, last = False, 0  # a blank line came before this one; the length of the bullet's last line
    for i, line in enumerate(lines):
        if not line:
            blank = True
            continue
        after = lines[i + 1] if i + 1 < len(lines) else ""
        if m := _BULLET.match(line):
            if not entries:  # after a blank line too: spacing between bullets doesn't end the entry
                entries.append(_Entry())
            entries[-1].bullets.append(line[m.end():].strip())
            last = len(entries[-1].bullets[-1])
        elif entries and entries[-1].bullets and not blank and (
                line[:1].islower()
                or (not entries[-1].bullets[-1].endswith((".", "!", "?")) and not _starts_entry(line)
                    and (_dangling(entries[-1].bullets[-1])
                         or ((last >= WRAP or _fragment(line))
                             and not (after and not _BULLET.match(after) and _starts_entry(after)))))):
            entries[-1].bullets[-1] += " " + line  # a bullet that wrapped onto the next line
            last = len(line)
        elif not entries or entries[-1].bullets or blank or _another_entry(entries[-1].heading, line, section):
            entries.append(_Entry([line]))
        else:
            entries[-1].heading.append(line)
        blank = False
    return entries


# A year or a month and year in a heading line: a date, not a count ("1,200 users").
_DATED = re.compile(rf"(?i)\b(?:(?:{_MONTH}[a-z]{{0,6}}|{_SEASON})\.?\s+)?(?:19|20)\d{{2}}\b|\bpresent\b"
                    rf"|\b{_MONTH}[a-z]{{0,6}}\.?\s+['’]\d{{2}}\b")


def _another_entry(heading: list[str], line: str, section: str) -> bool:
    """Whether a heading line starts the next entry of a section whose entries have no bullets.

    Entries are usually told apart by their bullets; an education section rarely has any. A line
    that brings a second set of dates belongs to the next entry. In Education, that takes a second
    school, or dates with a degree: a dated line of honours ("Dean's List, Fall 2018") stays.
    """
    dated = bool(_DATED.search(line)) and any(_DATED.search(h) for h in heading)
    if section == "education":
        return bool(_SCHOOL.search(line) and any(_SCHOOL.search(h) for h in heading)
                    or dated and _DEGREE.search(line))
    return dated


def _bullets(entry: _Entry) -> list[dict]:
    return [{"id": "", "text": text} for text in entry.bullets if text]


def _experience(entry: _Entry) -> dict:
    start, end, rest = _dates(" | ".join(entry.heading), single_is_end=False)
    parts = _parts(rest)
    location = next((p for p in parts if _LOCATION.fullmatch(p)), "")
    parts = [p for p in parts if p != location]
    title = next((p for p in parts if _ROLE.search(p)), "")
    others = [p for p in parts if p != title]
    if not title and len(others) > 1:
        title = others.pop(1)
    return {"id": "", "organization": others[0] if others else title, "title": title if others else "",
            "location": location, "start": start, "end": end, "technologies": [], "bullets": _bullets(entry)}


def _school_apart(part: str) -> list[str]:
    """A comma-separated "University, B.S. in Computer Science" as the school and the degree with its field."""
    pieces = [p.strip() for p in part.split(",") if p.strip()]
    if len(pieces) < 2 or not _DEGREE.search(part):
        return [part]
    schools = [p for p in pieces if _SCHOOL.search(p) and not _DEGREE.search(p)]
    rest = ", ".join(p for p in pieces if p not in schools)
    return [*schools, rest] if schools and rest else [part]


_MINOR = re.compile(r"[,;|]?\s*\b(minor|concentration|specialization)\b.*$", re.I)


def _education(entry: _Entry) -> dict:
    """An education entry. Heading lines that name no school, degree or (new) dates (honours, the
    Dean's List, coursework) and a minor are kept as the entry's bullets rather than dropped."""
    gpa = next((m.group(1) for line in entry.heading if (m := _GPA.search(line))), "")
    lines, extras = [], []
    for index, line in enumerate(entry.heading):
        line = _GPA.sub(" ", line)
        new_dates = _DATED.search(line) and not any(_DATED.search(earlier) for earlier in lines)
        if index and not (_SCHOOL.search(line) or _DEGREE.search(line) or new_dates):
            extras.append(line)  # honours, coursework: dated ones too once the entry has its dates
        else:
            lines.append(line)
    start, end, rest = _dates(" | ".join(lines), single_is_end=True)
    parts = [piece for part in _parts(rest) for piece in _school_apart(part)]
    institution = next((p for p in parts if _SCHOOL.search(p)), parts[0] if parts else "")
    degree, field_ = "", ""
    for part in parts:
        if part != institution and (m := _DEGREE.search(part)):
            degree = m.group(0)
            field_ = re.sub(r"^\s*(?:in|of)\s+|^[,\s]+", "", part[m.end():]).strip(" ,")
            if minor := _MINOR.search(field_):
                extras.insert(0, minor.group(0).strip(" ,;|"))
                field_ = field_[:minor.start()].strip(" ,")
            break
    bullets = [{"id": "", "text": text} for line in extras
               if (text := " ".join(p.strip(" ,") for p in _SEPARATORS.split(line) if p.strip(" ,")))]
    return {"id": "", "institution": institution, "degree": degree, "field": field_, "start": start, "end": end,
            "gpa": gpa, "bullets": bullets + _bullets(entry)}


def _project(entry: _Entry) -> dict:
    start, end, rest = _dates(" | ".join(entry.heading), single_is_end=False)
    link = m.group(0) if (m := _URL.search(rest) or _LINK.search(rest)) else ""
    parts = [p for p in _parts(rest.replace(link, " ") if link else rest) if p]  # replacing "" spaces out every letter
    name, others = (parts[0], parts[1:]) if parts else ("", [])
    # A comma list after the name is the project's tech stack ("TaskBot | Python, SQLite"), not a role.
    stack = next((p for p in others if "," in p), "")
    technologies = [t for t in _split_list(stack) if t and len(t) <= 60]
    role = next((p for p in others if p != stack), "")
    return {"id": "", "name": name, "role": role, "link": _bare(link), "start": start, "end": end,
            "technologies": technologies, "bullets": _bullets(entry)}


def _sentence(text: str) -> bool:
    """Whether a skills item reads as a sentence ("I pick up new tools quickly.") rather than a skill's name."""
    words = text.split()
    return len(words) > 5 or (len(words) >= 3 and text.rstrip().endswith((".", "!", "?")))


# A category label starting a part of a skills line: "Tools: Git" in "Frameworks: React; Tools: Git".
_NEXT_CATEGORY = re.compile(r"\s*[;|]\s*(?=[A-Z][\w &/+.-]{0,30}:)")


def _split_list(text: str) -> list[str]:
    """A list split at commas, semicolons, bars and dots, but not inside brackets: "Python (Pandas, NumPy)" stays whole.

    A bracket group too long to be one skill's name is read as its head and its items, and a bracket
    that never closes doesn't hold back the rest of the line.
    """
    items, depth, current = [], 0, []
    for ch in text:
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(ch, 0)
        if depth <= 0 and ch in ",;|•·":
            items.append("".join(current))
            current, depth = [], 0
        else:
            current.append(ch)
    items.append("".join(current))
    if depth > 0:
        return [item.strip() for item in re.split(r"[,;|•·(\[]", re.sub(r"[)\]]", "", text))]
    out = []
    for item in (item.strip() for item in items):
        head, bracket, inner = item.partition("(")
        if bracket and len(item) > 60:
            out += [head.strip(), *_split_list(inner.rsplit(")", 1)[0])]
        else:
            out.append(item)
    return out


def _categorized(line: str) -> list[tuple[str, str]]:
    """(category, list) parts of a skills line: "Category: a, b", a table row "Category | a, b", or a plain list."""
    parts = []
    for part in _NEXT_CATEGORY.split(line):
        category, sep, names = part.partition(":")
        if sep and len(category.split()) <= 4:
            parts.append((category.strip(), names))
            continue
        first, bar, rest = part.partition("|")
        if bar and "," not in first and len(first.split()) <= 3 and "," in rest:
            parts.append((first.strip(), rest))  # a table row: the first cell names the category
        else:
            parts.append(("Skills", part))
    return parts


def _joined(lines: list[str]) -> list[str]:
    """Skills lines without their bullets, a line whose bracket wraps onto the next joined to it."""
    joined: list[str] = []
    wrapped = False  # the last line is already two lines: a bracket that never closes takes no more
    for line in (_BULLET.sub("", line) for line in lines):
        if joined and not wrapped and joined[-1].count("(") > joined[-1].count(")"):
            joined[-1] += " " + line
            wrapped = True
        else:
            joined.append(line)
            wrapped = False
    return joined


def _skills(lines: list[str]) -> list[dict]:
    skills, seen = [], set()
    for line in _joined(lines):
        for category, names in _categorized(line):
            for name in _split_list(names):
                if _sentence(name):
                    continue
                name = name.strip(" .")
                if name and len(name) <= 60 and canon(name) not in seen:
                    seen.add(canon(name))
                    skills.append({"name": name, "category": category or "Skills"})
    return skills


# ---------------------------------------------------------------- the draft

def sections(text: str, blanks: bool = False) -> dict[str, list[str]]:
    """The non-empty lines of each section; "header" holds the lines before the first heading.

    With ``blanks``, a blank line between two lines of a section is kept as "", where it ends an entry.
    """
    found: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if blanks and found.get(current) and found[current][-1]:
                found[current].append("")
            continue
        if (section := _heading(line)) is not None:
            current = section
            found.setdefault(section, [])
        elif current.endswith(_WITH_CERTIFICATIONS):  # each line where it belongs
            other = current.removesuffix(_WITH_CERTIFICATIONS)
            found.setdefault("certifications" if _CERTIFICATE.search(line) else other, []).append(line)
        else:
            found.setdefault(current, []).append(line)
    for name in [name for name in found if name == "other" or name.endswith(_WITH_CERTIFICATIONS)]:
        found.pop(name)
    return {name: lines[:-1] if lines and not lines[-1] else lines for name, lines in found.items()}


def draft_profile(text: str) -> dict:
    """A profile in the shape the profile form shows, read from a resume's text."""
    with_blanks = sections(text, blanks=True)
    parts = {name: [line for line in lines if line] for name, lines in with_blanks.items()}
    certifications = [{"id": "", "name": _BULLET.sub("", line), "issuer": "", "date": ""}
                      for line in parts.get("certifications", [])]
    return {
        "contact": _contact(parts.get("header", [])[:HEADER_LINES]),
        "summary": " ".join(parts.get("summary", [])),
        "education": [_education(e) for e in _entries(with_blanks.get("education", []), "education")],
        "experience": [_experience(e) for e in _entries(with_blanks.get("experience", []), "experience")],
        "projects": [_project(e) for e in _entries(with_blanks.get("projects", []), "projects")],
        "skills": _skills(parts.get("skills", [])),
        "skills_absent": [],
        "certifications": certifications,
        "authorization": [],  # never read from a resume: always the user's own answer
        "preferences": {},
        "availability": {},
    }


# ---------------------------------------------------------------- with an existing profile

@dataclass(frozen=True)
class Merged:
    data: dict
    added: list[str]  # what the import adds, for the notice: "2 experience entries", "Phone"


# What tells two entries apart: a second role at the same organization is a new entry, and so is
# one whose name the draft couldn't read.
_ENTRY_KEY = {"education": ("institution", "degree"), "experience": ("organization", "title"),
              "projects": ("name", "role")}


def _key(value: str) -> str:
    return re.sub(r"\W+", " ", (value or "").casefold()).strip()


def _entry_key(entry: dict, fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_key(entry.get(name, "")) for name in fields)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# How the notice counts what was added to each list.
_NOUNS = {"education": ("education entry", "education entries"),
          "experience": ("experience entry", "experience entries"), "projects": ("project", "projects"), "skills": ("skill", "skills"),
          "certifications": ("certification", "certifications")}
_LINK_LABELS = {"linkedin": "LinkedIn", "github": "GitHub", "portfolio": "Portfolio"}


def merge(current: dict, draft: dict) -> Merged:
    """The current profile with the draft's new facts added; nothing it already holds is changed."""
    data = copy.deepcopy(current)
    added = []
    contact = data.setdefault("contact", {})
    for name, label in (("name", "Name"), ("email", "Email"), ("phone", "Phone"), ("location", "Location")):
        if not contact.get(name) and draft["contact"].get(name):
            contact[name] = draft["contact"][name]
            added.append(label)
    links = contact.setdefault("links", {})
    for name in LINK_KEYS:
        if not links.get(name) and draft["contact"]["links"].get(name):
            links[name] = draft["contact"]["links"][name]
            added.append(_LINK_LABELS[name])
    if not data.get("summary") and draft["summary"]:
        data["summary"] = draft["summary"]
        added.append("Summary")

    def add(section: str, new: list[dict]) -> None:
        data[section] = [*(data.get(section) or []), *new]
        if new:
            added.append(_count(len(new), *_NOUNS[section]))

    for section, fields in _ENTRY_KEY.items():
        have = {_entry_key(e, fields) for e in data.get(section) or []}
        new = []
        for entry in draft[section]:
            key = _entry_key(entry, fields)
            if (any(key) or entry.get("bullets")) and key not in have:
                have.add(key)
                new.append(entry)
        add(section, new)
    have_skills = {canon(s["name"]) for s in data.get("skills") or []}
    add("skills", [s for s in draft["skills"] if canon(s["name"]) not in have_skills])
    have_certs = {_key(c.get("name", "")) for c in data.get("certifications") or []}
    add("certifications", [c for c in draft["certifications"] if _key(c["name"]) and _key(c["name"]) not in have_certs])
    return Merged(data, added)

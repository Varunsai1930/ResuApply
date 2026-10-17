"""Compare ResuApply's rules with ResuSkill's on the same inputs, so drift between the two copies shows.

    .venv/bin/python -m scripts.rules_parity [path/to/ResuSkill]

ResuApply keeps its own port of ResuSkill's rules (question categories, claim checks, skill names).
The two copies are meant to agree; this runs both on one fixed set of synthetic inputs and lists
every difference. It reads the ResuSkill checkout (by default ``../ResuSkill``) without changing it.
Exit status: 0 when the rules agree, 1 when they differ, 2 when ResuSkill isn't found.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from app.services import claims, questions, skills

DEFAULT = Path(__file__).resolve().parents[2] / "ResuSkill"
CORE = Path("skills/resuskill/scripts")

QUESTIONS = [
    "What is your email address?", "Phone number", "Full name", "First Name", "Last Name", "LinkedIn Profile",
    "GitHub URL", "Website", "Portfolio", "Current location", "City, State", "Are you legally authorized to work in "
    "the United States?", "Will you now or in the future require visa sponsorship?", "What is your gender?",
    "Race / ethnicity", "Veteran status", "Disability status", "What are your salary expectations?",
    "Why do you want to work here?", "Describe a project you are proud of", "Are you willing to relocate?",
    "When can you start?", "What is your expected graduation date?", "Graduation year", "University",
    "Degree", "Major or field of study", "GPA", "How did you hear about us?", "Pronouns", "Sexual orientation",
    "Do you have a criminal record?", "Date of birth", "Earliest start date", "Cover letter",
]
CLAIMS = [
    "Built a Flask REST API serving 1,200 internal users", "Cut report time by 35% in 2024",
    "Led a team of twelve engineers", "Doubled throughput with 3.5k requests per second", "Served 2M users",
    "Handled one hundred tickets a week", "Earned a B.S. in Computer Science", "AWS Certified Solutions Architect",
    "Holds a PMP and a CPA license", "Published two peer-reviewed papers", "Dean's List, cum laude",
    "Passed CFA Level II", "Pursuing an MBA", "Patented a caching scheme", "Won the 2023 hackathon award",
]
SKILL_TEXTS = [
    "Python, SQL and PostgreSQL", "Node.js and React", "C++ and C#", "Go and Golang microservices",
    "Kubernetes on AWS", "JS/TS front ends", "scikit-learn and PyTorch", "ML pipelines with Spark",
]


def _load(root: Path):
    path = root / CORE
    if not (path / "resuskill_core").is_dir():
        return None
    sys.path.insert(0, str(path))
    return {name: importlib.import_module(f"resuskill_core.{name}") for name in ("questions", "validate", "skills")}


_PROFESSIONAL = ("CPA", "PMP", "CFA", "CISSP", "CCNA")


def _same_names(found: set[str]) -> set[str]:
    """Credential names in one vocabulary, for either project: ResuSkill's "CPA" is ResuApply's "CPA credential",
    and ResuApply's umbrella "degree" beside a named degree is dropped. Only differences in meaning remain."""
    named = {"bachelor's degree", "master's degree", "associate's degree", "doctorate", "mba"}
    same = set()
    for name in found:
        if name == "degree" and found & named:
            continue
        base, _, rest = name.partition(" ")
        if base in _PROFESSIONAL and not rest.startswith(("credential", "Level")):
            name = f"{base} credential{(' ' + rest) if rest else ''}"
        same.add(name)
    return same


def compare(theirs: dict) -> list[str]:
    differences = []
    for text in QUESTIONS:
        ours, other = questions.classify(text), theirs["questions"].classify(text)
        if tuple(ours) != tuple(other):
            differences.append(f"classify({text!r}): ResuApply {ours} vs ResuSkill {tuple(other)}")
    for text in CLAIMS:
        for name, ours_fn, their_fn in (("numbers", claims.numbers, theirs["validate"].numbers),
                                        ("credentials", claims.credentials, theirs["validate"].credentials)):
            ours, other = set(ours_fn(text)), set(their_fn(text))
            if name == "credentials":
                ours, other = _same_names(ours), _same_names(other)
            if ours != other:
                differences.append(f"{name}({text!r}): ResuApply {sorted(ours)} vs ResuSkill {sorted(other)}")
    for text in SKILL_TEXTS:
        ours, other = set(skills.find_terms(text)), set(theirs["skills"].find_terms(text))
        if ours != other:
            differences.append(f"find_terms({text!r}): ResuApply {sorted(ours)} vs ResuSkill {sorted(other)}")
    return differences


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]) if argv else DEFAULT
    theirs = _load(root)
    if theirs is None:
        print(f"ResuSkill wasn't found at {root} (expected {CORE}/resuskill_core).")
        return 2
    differences = compare(theirs)
    checked = len(QUESTIONS) + 2 * len(CLAIMS) + len(SKILL_TEXTS)
    if not differences:
        print(f"The rules agree on all {checked} checks.")
        return 0
    print(f"{len(differences)} of {checked} checks differ:")
    for line in differences:
        print(f"- {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

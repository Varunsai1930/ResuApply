"""Skill normalization and the alias map, ported from ResuSkill's ``resuskill_core.skills``.

Matching is deliberately simple and explicit: case folding plus a small alias map.
Keep the lists identical to ResuSkill.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .text import norm_text

# canonical name -> extra spellings. The canonical name is also a spelling.
ALIASES: dict[str, list[str]] = {
    "JavaScript": ["js", "javascript", "ecmascript", "es6"],
    "TypeScript": ["ts", "typescript"],
    "Python": ["python3"],
    "Go": ["golang"],
    "C++": ["cpp", "c plus plus"],
    "C#": ["c sharp", "csharp"],
    "Node.js": ["nodejs", "node js"],
    "React": ["react.js", "reactjs", "react js"],
    "React Native": ["react-native"],
    "Next.js": ["nextjs", "next js"],
    "Vue": ["vue.js", "vuejs"],
    "Angular": ["angularjs", "angular.js"],
    "Express": ["express.js", "expressjs"],
    "PostgreSQL": ["postgres", "postgre", "psql"],
    "MongoDB": ["mongo"],
    "Kubernetes": ["k8s"],
    "Amazon Web Services": ["aws"],
    "Google Cloud": ["gcp", "google cloud platform"],
    "Microsoft Azure": ["azure"],
    "scikit-learn": ["sklearn", "scikit learn"],
    "TensorFlow": ["tensorflow", "tf2"],
    "PyTorch": ["torch"],
    "Machine Learning": ["ml"],
    "Natural Language Processing": ["nlp"],
    "Continuous Integration": ["ci/cd", "ci", "cicd"],
    "Power BI": ["powerbi"],
    "GitHub Actions": ["gh actions"],
    "Hugging Face": ["huggingface", "hugging face transformers"],
    ".NET": ["dotnet", "dot net"],
    "Objective-C": ["objective c", "objc"],
    "Elasticsearch": ["elastic search"],
    "REST": ["rest api", "restful", "rest apis", "restful apis"],
    "SQL": ["structured query language"],
    "NoSQL": ["no-sql"],
    "Ruby on Rails": ["Rails"],
    "Spring Boot": ["springboot"],
}

# Technologies recognised by name even when absent from the profile.
LEXICON: list[str] = [
    "Python", "Java", "JavaScript", "TypeScript", "Go", "Rust", "C", "C++", "C#", "R",
    "Ruby", "PHP", "Perl", "Kotlin", "Swift", "Objective-C", "Scala", "Dart", "Julia",
    "MATLAB", "Haskell", "Elixir", "Erlang", "Clojure", "Lua", "Solidity", "Bash",
    "PowerShell", "SQL", "NoSQL", "HTML", "CSS", "Sass", "GraphQL", "REST", "gRPC",
    "React", "React Native", "Next.js", "Vue", "Angular", "Svelte", "Redux", "jQuery",
    "Tailwind", "Bootstrap", "Node.js", "Express", "Django", "Flask", "FastAPI",
    "Spring", "Spring Boot", ".NET", "ASP.NET", "Laravel", "Ruby on Rails", "Flutter",
    "Android", "iOS", "Unity", "Unreal", "PostgreSQL", "MySQL", "SQLite", "MongoDB",
    "Redis", "DynamoDB", "Cassandra", "Elasticsearch", "Snowflake", "BigQuery",
    "Firebase", "Supabase", "Kafka", "RabbitMQ", "Celery", "Spark", "Hadoop",
    "Airflow", "dbt", "Tableau", "Power BI", "Looker", "Excel", "Docker", "Kubernetes",
    "Helm", "Terraform", "Ansible", "Jenkins", "GitHub Actions", "GitLab", "CircleCI",
    "Prometheus", "Grafana", "Nginx", "Linux", "Git", "Amazon Web Services",
    "Google Cloud", "Microsoft Azure", "Lambda", "S3", "EC2", "Heroku", "Vercel",
    "Netlify", "TensorFlow", "PyTorch", "Keras", "scikit-learn", "pandas", "NumPy",
    "SciPy", "OpenCV", "Hugging Face", "LangChain", "Selenium", "Playwright",
    "Cypress", "Jest", "pytest", "JUnit", "Webpack", "Vite", "Figma", "Jira",
    "Postman", "Machine Learning", "Natural Language Processing", "Computer Vision",
    "Continuous Integration",
]


def norm_skill(name: str) -> str:
    text = norm_text(name).rstrip(".")
    return re.sub(r"\s+", " ", text)


@lru_cache(maxsize=None)
def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for canon_name, spellings in ALIASES.items():
        index[norm_skill(canon_name)] = canon_name
        for spelling in spellings:
            index[norm_skill(spelling)] = canon_name
    for term in LEXICON:
        index.setdefault(norm_skill(term), term)
    return index


def canon(name: str) -> str:
    """Return the canonical comparison key for a skill name (case-folded)."""
    key = norm_skill(name)
    return norm_skill(_alias_index().get(key, name))


def display(name: str) -> str:
    """The preferred spelling of a skill: the canonical name for known aliases, else as typed."""
    return _alias_index().get(norm_skill(name), name.strip())


# These spellings also occur as ordinary words and require their technology case.
CASE_SENSITIVE: set[str] = {
    "Go", "R", "C", "Rust", "Swift", "Spring", "React", "Express", "Spark", "Lambda",
    "Unity", "Excel", "Dart", "Julia", "Lua", "Helm", "Snowflake", "Looker", "Vite",
    "Jest", "REST", "Rails", "CV", "CI", "TS", "JS", "ML", "Unreal", "Git", "Celery",
    "Angular", "Flutter", "Keras", "Bootstrap", "Tailwind", "Svelte", "Redux",
}


@lru_cache(maxsize=128)
def _term_patterns(extra_terms: tuple[str, ...]) -> tuple[tuple[str, re.Pattern], ...]:
    entries: dict[str, set[str]] = {}
    for term in [*LEXICON, *extra_terms]:
        spellings = entries.setdefault(canon(term), set())
        spellings.update([display(term), term.strip(), *ALIASES.get(display(term), [])])
    patterns = []
    for key, spellings in entries.items():
        for spelling in sorted(spellings, key=len, reverse=True):
            if not spelling:
                continue
            sensitive = spelling in CASE_SENSITIVE or len(spelling) <= 2
            if sensitive and spelling.islower():
                spelling = spelling.upper()
            body = re.escape(spelling).replace(r"\ ", r"[\s-]+")
            pattern = rf"(?<![\w+#.&-]){body}(?![\w+#&]|-\w)"
            patterns.append((key, re.compile(pattern, 0 if sensitive else re.IGNORECASE)))
    return tuple(patterns)


def find_terms(text: str, extra_terms=()) -> set[str]:
    """Canonical keys of known technologies, respecting boundaries and common words."""
    extras = tuple(sorted({t for t in extra_terms if isinstance(t, str) and t.strip()}))
    return {key for key, pattern in _term_patterns(extras) if pattern.search(text or "")}

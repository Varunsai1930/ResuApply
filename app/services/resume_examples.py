"""Original, fictional layout demos and links to publisher-reported hiring examples.

The public resumes remain at their sources. No applicant content is imported into
the demos, a candidate profile, or an accepted application.
"""

EXAMPLES = {
    "classic": {
        "audience": "A balanced first impression",
        "description": "A centered name, clear section rules and comfortable spacing. Let your experience lead the page.",
        "best_for": "A straightforward career story",
        "notes": (
            ("A clear introduction", "Keep your summary to a few specific lines about the work you do and the value you bring."),
            ("Experience in focus", "Lead each role with your most relevant achievement, then add the context behind it."),
            ("Room to read", "Consistent section rules and generous spacing give each part of your story a clear place."),
        ),
    },
    "modern": {
        "audience": "Structure with a little character",
        "description": "A split header, navy accents and side labels create a distinct rhythm while keeping the reading order clear.",
        "best_for": "Projects and technical experience",
        "notes": (
            ("Easy to navigate", "Section labels sit beside your work, helping a reader find experience, projects and skills."),
            ("Projects with purpose", "Explain what you built, the tools you used and what the project accomplished."),
            ("A restrained accent", "A single navy tone adds hierarchy without competing with the words on the page."),
        ),
    },
    "compact": {
        "audience": "More experience, considered space",
        "description": "An efficient header, inline dates and closer spacing make room for a longer story with a clear hierarchy.",
        "best_for": "A fuller work history",
        "notes": (
            ("Make every line count", "Choose the achievements most relevant to the role and remove repeated responsibilities."),
            ("Dates stay in view", "Inline dates make the timeline easy to scan without adding another line to each entry."),
            ("Keep the evidence", "Use the extra space for concrete contributions and relevant skills, rather than more filler."),
        ),
    },
}

# These outcome statements belong to the publisher, not to ResuApply or the employers.
# Checked 2026-10-22. The demo layouts above are independent original designs.
HIRING_EXAMPLES = (
    {"company": "Google", "role": "Software Engineering Intern", "mark": "G",
     "group": "software", "level": "intern", "focus": "Look for projects and contributions that demonstrate practical experience.",
     "url": "https://www.kickresume.com/en/help-center/software-engineer-intern-google-resume-sample/"},
    {"company": "Amazon", "role": "Software Development Engineer", "mark": "a",
     "group": "software", "level": "professional", "focus": "Look for the connection between technical work and its impact.",
     "url": "https://www.kickresume.com/en/help-center/software-development-engineer-amazon-resume-sample/"},
    {"company": "IBM", "role": "Software Engineer", "mark": "IBM",
     "group": "software", "level": "professional", "focus": "Look for a clear progression of relevant engineering experience.",
     "url": "https://www.kickresume.com/en/help-center/ibm-software-engineer-resume-sample/"},
    {"company": "Vacasa", "role": "Data Analyst", "mark": "V", "group": "data", "level": "professional",
     "focus": "Look for how analytical tools support a business question or decision.",
     "url": "https://www.kickresume.com/en/help-center/data-analyst-3-resume-sample/"},
    {"company": "Cactus communication", "role": "Lead Data Analyst", "mark": "C", "group": "data", "level": "lead",
     "focus": "Look for ownership, team scope and the decisions behind the analysis.",
     "url": "https://www.kickresume.com/en/help-center/lead-data-analyst-resume-sample/"},
    {"company": "RS Express", "role": "Marketing Manager", "mark": "RS", "group": "marketing", "level": "lead",
     "focus": "Look for campaign ownership, channels and clearly described responsibilities.",
     "url": "https://www.kickresume.com/en/help-center/marketing-manager-resume-sample/"},
)

ROLE_GROUPS = {"all": "All roles", "software": "Software engineering", "data": "Data analysis", "marketing": "Marketing"}
ROLE_LEVELS = {"all": "All levels", "intern": "Internship", "professional": "Professional", "lead": "Lead / management"}

# One fictional career across all three layouts makes comparison meaningful.
SAMPLE_RESUME = {
    "contact": {"name": "Avery Morgan", "email": "avery@example.com", "phone": "",
                "location": "Portland, OR", "links": {"Portfolio": "avery.example.com", "GitHub": "github.com/avery-example"}},
    "summary": {"text": "Software engineer building dependable services and thoughtful developer tools. Experienced in Python, TypeScript and the work of turning a useful idea into a product people rely on."},
    "experience": [
        {"title": "Software Engineer", "organization": "Northstar Studio", "location": "Portland, OR",
         "start": "2022", "end": "present", "technologies": ["Python", "PostgreSQL", "AWS"],
         "bullets": [
             {"text": "Built an event processing service handling 2 million daily updates; reduced processing delays from 12 minutes to under 1 minute."},
             {"text": "Redesigned database queries and indexes, cutting the reporting dashboard's median load time by 38%."},
             {"text": "Introduced deployment checks and service runbooks that helped a team of six ship weekly releases with fewer rollbacks."},
         ]},
        {"title": "Junior Software Engineer", "organization": "Fieldwork Labs", "location": "Remote",
         "start": "2020", "end": "2022", "technologies": ["TypeScript", "React", "Node.js"],
         "bullets": [
             {"text": "Delivered a reusable component library used across three internal products, with keyboard navigation and documented examples."},
             {"text": "Worked with support to resolve recurring onboarding issues, reducing setup requests by 25%."},
         ]},
    ],
    "projects": [
        {"name": "Open Trail", "role": "Creator", "start": "2023", "end": "2024",
         "link": "github.com/avery-example/open-trail", "technologies": ["FastAPI", "SQLite"],
         "bullets": [
             {"text": "Created a local planning tool that turns trail notes into searchable routes and offline trip checklists."},
             {"text": "Added automated import validation and documented a contribution workflow for first-time contributors."},
         ]},
    ],
    "education": [{"institution": "Cascadia Institute", "degree": "B.S.", "field": "Computer Science",
                   "start": "2016", "end": "2020", "bullets": []}],
    "skills": ["Python", "TypeScript", "SQL", "React", "AWS", "Docker", "Testing", "Accessibility"],
    "certifications": [],
}

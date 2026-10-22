from app.services.profile import normalize
from app.services.resume import profile_draft
from app.services.resume_quality import quality_notes
from tests.test_resume_routes import setup, manual_proposal, accept


def notes(data, *, reversed_dates=False):
    profile = normalize(data).profile
    if reversed_dates:
        profile.experience[0].start, profile.experience[0].end = '2025-02', '2024-12'
    content = profile_draft(profile)
    return quality_notes(profile, content)


def test_quality_checks_find_repetition_vague_openings_and_dates(sample_profile):
    entry = sample_profile['experience'][0]
    entry['bullets'] = [{'text': 'Responsible for the API.'}, {'text': 'responsible for the API!'}]
    found = notes(sample_profile, reversed_dates=True)
    assert any('end date' in n.message for n in found)
    assert any('repeats wording' in n.message for n in found)
    assert sum('starts broadly' in n.message for n in found) == 2


def test_quality_checks_only_selected_content_and_do_not_mutate(sample_profile):
    profile = normalize(sample_profile).profile
    before = profile.model_dump()
    from app.schemas.resume import ResumeContent
    assert quality_notes(profile, ResumeContent()) == []
    assert profile.model_dump() == before


def test_suggestions_do_not_block_resume_acceptance(client, sample_profile):
    sample_profile['experience'][0]['bullets'][0] = 'Worked on the API.'
    job = setup(client, sample_profile)
    review = manual_proposal(client, job)
    assert 'starts broadly' in review.text
    assert '/profile/edit?section=experience' in review.text
    assert accept(client, job, review).status_code == 303

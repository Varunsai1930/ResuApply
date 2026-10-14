"""Damaged Word uploads return useful errors and leave the saved profile intact."""

import pytest

from app.models import Candidate
from tests.test_resume_import import _encrypted_member, _missing_body, _missing_table_span, _unexpected_root
from tests.test_routes import profile_form, review_and_save


def _profiles(client):
    with client.app.state.session_factory() as session:
        return [(candidate.id, candidate.profile.model_dump(), candidate.revision,
                 candidate.id_counters, candidate.created_at, candidate.updated_at, candidate.sample)
                for candidate in session.query(Candidate).all()]


@pytest.mark.parametrize("saved_profile", [False, True], ids=["new-profile", "saved-profile"])
@pytest.mark.parametrize("make, message", [
    (_missing_body, "This Word file couldn&#39;t be read. Try saving it again as .docx."),
    (_unexpected_root, "This Word file couldn&#39;t be read. Try saving it again as .docx."),
    (_missing_table_span, "This Word file couldn&#39;t be read. Try saving it again as .docx."),
    (_encrypted_member, "This Word file is password-protected. Save a copy without a password"),
])
def test_damaged_word_uploads_are_recoverable(client, sample_profile, saved_profile, make, message):
    if saved_profile:
        _, saved = review_and_save(client, profile_form(sample_profile))
        assert saved.status_code == 303
    before = _profiles(client)

    response = client.post("/profile/import", files={"file": (
        "resume.docx", make(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )})

    assert response.status_code == 422
    assert message in response.text
    assert 'name="file"' in response.text  # the user can choose another file after the error
    assert _profiles(client) == before
    for path in ("/", "/jobs", "/profile"):
        assert client.get(path).status_code == 200

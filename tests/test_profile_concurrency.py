"""Overlapping profile saves must retain the winner's facts, revisions and IDs."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Candidate
from app.services import jobs, profile
from app.services.profile import StaleReview


@pytest.mark.parametrize("base_revision", [1, None])
def test_cached_sessions_cannot_overwrite_a_newer_revision(session, sample_profile, base_revision):
    original = profile.save(session, sample_profile).candidate
    original_id = original.id
    with Session(session.get_bind(), expire_on_commit=False) as stale_session:
        stale_candidate = profile.get_candidate(stale_session)
        stale_data = stale_candidate.profile.model_dump()
        winning_data = original.profile.model_dump()
        winning_data["experience"][0]["bullets"].append({"text": "Won the first save"})
        winner = profile.save(session, winning_data, base_revision=1)
        assert winner.candidate is original

        stale_data["experience"][0]["bullets"].append({"text": "Must not overwrite the first save"})
        with pytest.raises(StaleReview):
            profile.save(stale_session, stale_data, base_revision=base_revision)

        # Rollback also refreshes the cached object rather than leaving a phantom revision.
        assert stale_candidate.revision == 2
        assert stale_candidate.profile.experience[0].bullets[-1].text == "Won the first save"

    session.expire_all()
    stored = profile.get_candidate(session)
    assert stored.id == original_id and stored.revision == 2
    assert stored.profile.experience[0].bullets[-1].id == "exp-1-b4"
    assert stored.id_counters["exp-1-b"] == 4


def test_cached_noop_save_still_refuses_a_stale_review(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    with Session(session.get_bind(), expire_on_commit=False) as stale_session:
        stale_candidate = profile.get_candidate(stale_session)
        stale_data = stale_candidate.profile.model_dump()
        changed = candidate.profile.model_dump() | {"summary": "A newer summary"}
        profile.save(session, changed, base_revision=1)
        with pytest.raises(StaleReview):
            profile.save(stale_session, stale_data, base_revision=1)
    assert profile.get_candidate(session).profile.summary == "A newer summary"


@pytest.mark.parametrize("is_new", [False, True])
def test_simultaneous_saves_allow_exactly_one_winner(session, sample_profile, monkeypatch, is_new):
    pending_job = jobs.create(session, jobs.clean_input(title="Intern", company="Example", description="Python"))
    if not is_new:
        profile.save(session, sample_profile)
    initial_revision = 0 if is_new else 1
    barrier = Barrier(2)
    normalize = profile.normalize

    def normalized_together(*args, **kwargs):
        result = normalize(*args, **kwargs)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(profile, "normalize", normalized_together)

    def attempt(summary):
        data = copy.deepcopy(sample_profile) | {"summary": summary}
        with Session(session.get_bind(), expire_on_commit=False) as request_session:
            try:
                result = profile.save(request_session, data, base_revision=initial_revision)
            except StaleReview:
                return "stale", summary
            return "saved", result.candidate.profile.summary

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["First simultaneous proposal", "Second simultaneous proposal"]))

    assert sorted(state for state, _ in outcomes) == ["saved", "stale"]
    winning_summary = next(summary for state, summary in outcomes if state == "saved")
    session.expire_all()
    candidate = profile.get_candidate(session)
    assert candidate.profile.summary == winning_summary
    assert candidate.revision == initial_revision + 1
    assert session.scalar(select(func.count()).select_from(Candidate)) == 1
    assert pending_job.application.candidate_id == candidate.id

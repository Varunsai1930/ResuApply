"""The ResuSkill parity report: differences in meaning only, and a plain exit when ResuSkill is absent."""

from __future__ import annotations

from app.services import claims, questions, skills
from scripts import rules_parity


def test_identical_rules_report_no_difference():
    ours = {"questions": questions, "validate": claims, "skills": skills}
    assert rules_parity.compare(ours) == []


def test_naming_differences_are_not_reported():
    assert rules_parity._same_names({"CPA (in progress)", "CFA Level II"}) == {
        "CPA credential (in progress)", "CFA Level II"}
    assert rules_parity._same_names({"CPA credential", "PMP"}) == {"CPA credential", "PMP credential"}
    assert rules_parity._same_names({"bachelor's degree", "degree"}) == {"bachelor's degree"}


def test_a_missing_resuskill_checkout_is_reported_not_raised(tmp_path, capsys):
    assert rules_parity.main([str(tmp_path / "nowhere")]) == 2
    assert "ResuSkill wasn't found" in capsys.readouterr().out

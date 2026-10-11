"""The search shared by the jobs board and the answer bank (services/search.py)."""

from __future__ import annotations

from app.services import search


def test_clean_query_joins_spaces_and_cuts_to_the_limit():
    assert search.clean_query("  data   engineering \n") == "data engineering"
    assert search.clean_query(None) == ""
    assert len(search.clean_query("x" * 5000)) == search.SEARCH_LIMIT


def test_matches_needs_every_word_in_any_case():
    assert search.matches("Backend Intern at Example Corp", "example BACKEND")
    assert not search.matches("Backend Intern", "backend data")
    assert search.matches("anything", "")


def test_the_board_and_the_answer_bank_use_the_same_search(client):
    from app.routes import answers as answers_route
    from app.services import answers, board

    assert board.clean_query is search.clean_query and answers_route.clean_query is search.clean_query
    assert answers.matches is search.matches and board._matches is search.matches
    long = client.get("/answers", params={"q": "y" * 5000})
    assert long.status_code == 200

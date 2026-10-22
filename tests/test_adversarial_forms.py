"""Malformed request metadata must not crash the upload routes."""

import pytest


@pytest.mark.parametrize("length", ["9" * 5000, "0" * 5000 + "999999999"])
def test_an_enormous_announced_upload_is_refused(client, length):
    response = client.post("/profile/import", content=b"", headers={"content-length": length})

    assert response.status_code == 413
    assert "over 10 MB" in response.text


def test_a_small_upload_with_many_leading_zeroes_is_not_a_server_error(client):
    response = client.post("/profile/import", content=b"", headers={"content-length": "0" * 5000 + "1"})

    assert response.status_code == 422
    assert "Choose" in response.text

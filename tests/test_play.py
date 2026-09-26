"""Google Play purchases of cloud caption hours: the Play build of Subrep."""

from __future__ import annotations

from backend.settings import settings


def test_the_play_build_can_sell_only_when_the_server_can_check_purchases(
        client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "caption_api_key", "dg_test")
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "play_service_account_file", "")
    assert client.get("/api/captions").json()["play_available"] is False

    key = tmp_path / "play-key.json"
    monkeypatch.setattr(settings, "play_service_account_file", str(key))
    assert client.get("/api/captions").json()["play_available"] is False  # no file yet

    key.write_text("{}")
    state = client.get("/api/captions").json()
    # The Play build does not need Stripe, and the web build does not need Google.
    assert state["play_available"] is True
    assert state["available"] is False

    monkeypatch.setattr(settings, "caption_api_key", "")
    assert client.get("/api/captions").json()["play_available"] is False

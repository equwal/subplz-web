"""The headers on the static files: cross-origin isolation, and how long a
browser may keep each file."""

from __future__ import annotations


def test_page_is_cross_origin_isolated(client):
    r = client.get("/")
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert r.headers["cross-origin-embedder-policy"] == "credentialless"


def test_engine_modules_are_revalidated_every_time(client):
    # engine/job.js imports ./asr.js by bare path. A kept copy of one with a
    # new copy of the other is a broken engine, so each must ask the server.
    for path in ("/", "/app.js", "/engine/asr.js", "/engine/job.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-cache", path


def test_vendor_files_may_be_kept(client):
    r = client.get("/vendor/versions.json")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=86400"

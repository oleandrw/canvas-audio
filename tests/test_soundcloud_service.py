"""SoundCloud: PKCE, single-use refresh tokens, upload and playlist calls.

These tests pin the request shapes to what the official API documents, so a
change here is a deliberate change, not a guess.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from app.security import pkce_pair
from app.services import soundcloud_service as sc
from app.services.errors import AuthenticationError, SoundCloudNotConnected


class TestPKCE:
    def test_challenge_is_the_url_safe_sha256_of_the_verifier(self) -> None:
        verifier, challenge = pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert challenge == expected
        assert "=" not in challenge
        assert 43 <= len(verifier) <= 128

    def test_verifiers_are_unique(self) -> None:
        assert pkce_pair()[0] != pkce_pair()[0]

    def test_authorize_url_carries_every_required_parameter(self) -> None:
        url = sc.build_authorize_url(
            client_id="abc",
            redirect_uri="http://localhost:8000/cb",
            state="state123",
            code_challenge="chal",
        )
        assert url.startswith("https://secure.soundcloud.com/authorize?")
        for fragment in (
            "client_id=abc",
            "response_type=code",
            "state=state123",
            "code_challenge=chal",
            "code_challenge_method=S256",
        ):
            assert fragment in url


class TestTokenStorage:
    def test_tokens_are_encrypted_at_rest(self, db: Session) -> None:
        from app.models.core import OAuthToken

        bundle = sc.TokenBundle(
            access_token="plain-access-token",
            refresh_token="plain-refresh-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        sc.save_token(db, bundle, account_name="ola")
        db.commit()

        row = db.get(OAuthToken, "soundcloud")
        assert "plain-access-token" not in row.access_token_enc
        assert "plain-refresh-token" not in row.refresh_token_enc

        loaded = sc.load_token(db)
        assert loaded.access_token == "plain-access-token"
        assert loaded.refresh_token == "plain-refresh-token"
        assert sc.stored_account_name(db) == "ola"

    def test_no_token_means_not_connected(self, db: Session) -> None:
        assert sc.load_token(db) is None
        assert not sc.is_connected(db)

    def test_clear_token_disconnects(self, db: Session) -> None:
        sc.save_token(db, sc.TokenBundle("a", "b", None))
        db.commit()
        sc.clear_token(db)
        db.commit()
        assert not sc.is_connected(db)


class TestOAuthFlow:
    async def test_code_exchange_posts_the_documented_form(self, env: Path) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.read().decode()
            return httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 3600,
                    "scope": "non-expiring",
                },
            )

        bundle = await sc.exchange_code(
            client_id="cid",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
            code="the-code",
            code_verifier="the-verifier",
            transport=httpx.MockTransport(handler),
        )

        assert captured["url"] == "https://secure.soundcloud.com/oauth/token"
        assert "grant_type=authorization_code" in captured["body"]
        assert "code_verifier=the-verifier" in captured["body"]
        assert bundle.access_token == "at"
        assert bundle.expires_at is not None

    async def test_refresh_rotates_and_persists_the_new_refresh_token(self, db: Session) -> None:
        sc.save_token(
            db,
            sc.TokenBundle(
                access_token="old-access",
                refresh_token="old-refresh",
                expires_at=datetime.now(UTC) - timedelta(minutes=1),  # already expired
            ),
        )
        db.commit()

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            assert "grant_type=refresh_token" in body
            assert "refresh_token=old-refresh" in body
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            )

        token = await sc.get_access_token(
            client_id="cid", client_secret="secret", transport=httpx.MockTransport(handler)
        )
        assert token == "new-access"

        db.expire_all()
        stored = sc.load_token(db)
        assert stored.access_token == "new-access"
        assert stored.refresh_token == "new-refresh", "single-use refresh token must be replaced"

    async def test_valid_token_is_not_refreshed(self, db: Session) -> None:
        sc.save_token(
            db,
            sc.TokenBundle("still-good", "rt", datetime.now(UTC) + timedelta(hours=2)),
        )
        db.commit()

        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not refresh a token that is still valid")

        token = await sc.get_access_token(
            client_id="cid", client_secret="secret", transport=httpx.MockTransport(handler)
        )
        assert token == "still-good"

    async def test_missing_token_raises_not_connected(self, env: Path) -> None:
        with pytest.raises(SoundCloudNotConnected):
            await sc.get_access_token(client_id="cid", client_secret="secret")


class TestApiCalls:
    def build(self, handler) -> sc.SoundCloudClient:
        return sc.SoundCloudClient("token", transport=httpx.MockTransport(handler))

    async def test_uses_the_oauth_authorization_scheme(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json={"username": "ola"})

        async with self.build(handler) as client:
            await client.me()
        assert seen["authorization"] == "OAuth token"

    async def test_upload_sends_multipart_track_fields(self, tmp_path: Path) -> None:
        mp3 = tmp_path / "01-velkommen.mp3"
        mp3.write_bytes(b"ID3-fake")
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.read().decode("utf-8", errors="replace")
            return httpx.Response(
                201,
                json={"id": 12345, "title": "Velkommen", "permalink_url": "https://snd.sc/x"},
            )

        async with self.build(handler) as client:
            track = await client.upload_track(
                file_path=mp3, title="Velkommen", description="Emne — Modul 1", sharing="private"
            )

        assert captured["url"] == "https://api.soundcloud.com/tracks"
        assert 'name="track[title]"' in captured["body"]
        assert 'name="track[asset_data]"' in captured["body"]
        assert 'name="track[sharing]"' in captured["body"]
        assert track.track_id == "12345"
        assert track.permalink_url == "https://snd.sc/x"

    async def test_upload_of_a_missing_file_fails_clearly(self, tmp_path: Path) -> None:
        async with self.build(lambda _r: httpx.Response(201, json={"id": 1})) as client:
            with pytest.raises(Exception, match="finnes ikke"):
                await client.upload_track(file_path=tmp_path / "nope.mp3", title="X")

    async def test_playlist_creation_uses_the_documented_body(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.read())
            return httpx.Response(
                201,
                json={
                    "id": 999,
                    "title": "Modul 1",
                    "permalink_url": "https://snd.sc/p",
                    "tracks": [{"id": 1}, {"id": 2}],
                },
            )

        async with self.build(handler) as client:
            info = await client.create_playlist(
                title="Modul 1", track_ids=["1", "2"], description="Mitt emne"
            )

        assert captured["url"] == "https://api.soundcloud.com/playlists"
        assert captured["json"]["playlist"]["tracks"] == [{"id": 1}, {"id": 2}]
        assert captured["json"]["playlist"]["title"] == "Modul 1"
        assert info.playlist_id == "999"
        assert info.track_count == 2

    async def test_playlist_update_replaces_the_full_track_list(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.read())
            return httpx.Response(200, json={"id": 999, "title": "Modul 1", "tracks": [{"id": 3}]})

        async with self.build(handler) as client:
            await client.update_playlist("999", track_ids=["3"])

        assert captured["method"] == "PUT"
        assert captured["url"] == "https://api.soundcloud.com/playlists/999"
        assert captured["json"]["playlist"]["tracks"] == [{"id": 3}]

    async def test_non_numeric_track_ids_are_dropped(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["json"] = json.loads(request.read())
            return httpx.Response(201, json={"id": 1, "tracks": []})

        async with self.build(handler) as client:
            await client.create_playlist(title="M", track_ids=["12", "", "abc"])
        assert captured["json"]["playlist"]["tracks"] == [{"id": 12}]

    async def test_forbidden_explains_the_artist_pro_requirement(self) -> None:
        async with self.build(lambda _r: httpx.Response(403, json={"error": "no"})) as client:
            with pytest.raises(AuthenticationError, match="Artist Pro"):
                await client.me()


class TestOptionalIntegration:
    async def test_unconfigured_reports_gracefully_instead_of_raising(self, env: Path) -> None:
        result = await sc.check_connection(client_id="", client_secret="")
        assert result["ok"] is False
        assert "valgfri" in result["detail"].lower()

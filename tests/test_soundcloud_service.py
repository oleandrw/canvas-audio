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

    async def test_upload_reads_the_track_urn(self, tmp_path: Path) -> None:
        mp3 = tmp_path / "01-velkommen.mp3"
        mp3.write_bytes(b"ID3-fake")
        response = {"urn": "soundcloud:tracks:2401076190", "title": "Velkommen"}
        async with self.build(lambda _r: httpx.Response(201, json=response)) as client:
            track = await client.upload_track(file_path=mp3, title="Velkommen")
        assert track.track_id == "2401076190"

    async def test_playlist_creation_uses_the_documented_body(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.read())
            return httpx.Response(
                201,
                json={
                    "urn": "soundcloud:playlists:999",
                    "title": "Modul 1",
                    "permalink_url": "https://snd.sc/p",
                    "tracks": [{"urn": "soundcloud:tracks:1"}, {"urn": "soundcloud:tracks:2"}],
                },
            )

        async with self.build(handler) as client:
            info = await client.create_playlist(
                title="Modul 1", track_ids=["1", "2"], description="Mitt emne"
            )

        assert captured["url"] == "https://api.soundcloud.com/playlists"
        assert captured["json"]["playlist"]["tracks"] == [
            {"urn": "soundcloud:tracks:1"},
            {"urn": "soundcloud:tracks:2"},
        ]
        assert captured["json"]["playlist"]["title"] == "Modul 1"
        assert info.playlist_id == "999"
        assert info.track_ids == ["1", "2"]
        assert info.track_count == 2

    async def test_track_ids_beyond_32_bits_are_sent_as_urn_strings(self) -> None:
        """SoundCloud cannot parse ``{"id": 2401076190}``: numeric ids overflow 32 bits."""
        captured: dict[str, bytes] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read()
            return httpx.Response(201, json={"urn": "soundcloud:playlists:1"})

        async with self.build(handler) as client:
            await client.create_playlist(title="M", track_ids=["2401076190"])

        assert b'"urn":"soundcloud:tracks:2401076190"' in captured["body"]
        assert b'"id"' not in captured["body"]

    async def test_playlist_update_replaces_the_full_track_list(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["json"] = json.loads(request.read())
            return httpx.Response(
                200,
                json={
                    "urn": "soundcloud:playlists:999",
                    "title": "Modul 1",
                    "tracks": [{"urn": "soundcloud:tracks:3"}],
                },
            )

        async with self.build(handler) as client:
            await client.update_playlist("999", track_ids=["3"])

        assert captured["method"] == "PUT"
        assert captured["url"] == "https://api.soundcloud.com/playlists/soundcloud:playlists:999"
        assert captured["json"]["playlist"]["tracks"] == [{"urn": "soundcloud:tracks:3"}]

    async def test_a_playlist_is_fetched_by_urn(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"urn": "soundcloud:playlists:2401", "title": "M"})

        async with self.build(handler) as client:
            info = await client.get_playlist("2401")

        url = httpx.URL(captured["url"])
        assert url.path == "/playlists/soundcloud:playlists:2401"
        assert url.params["access"] == "playable,preview,blocked"
        assert info.playlist_id == "2401"

    async def test_non_numeric_track_ids_are_dropped(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["json"] = json.loads(request.read())
            return httpx.Response(201, json={"id": 1, "tracks": []})

        async with self.build(handler) as client:
            await client.create_playlist(title="M", track_ids=["12", "", "abc"])
        assert captured["json"]["playlist"]["tracks"] == [{"urn": "soundcloud:tracks:12"}]

    async def test_forbidden_explains_the_artist_pro_requirement(self) -> None:
        async with self.build(lambda _r: httpx.Response(403, json={"error": "no"})) as client:
            with pytest.raises(AuthenticationError, match="Artist Pro"):
                await client.me()


class TestOptionalIntegration:
    async def test_unconfigured_reports_gracefully_instead_of_raising(self, env: Path) -> None:
        result = await sc.check_connection(client_id="", client_secret="")
        assert result["ok"] is False
        assert "valgfri" in result["detail"].lower()


class TestPlaylistListing:
    async def test_lists_every_page_of_the_users_playlists(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "cursor=2" in str(request.url):
                return httpx.Response(200, json={"collection": [{"id": 3, "title": "C"}]})
            return httpx.Response(
                200,
                json={
                    "collection": [
                        {"id": 1, "title": "A", "track_count": 5, "sharing": "private"},
                        {"id": 2, "title": "B", "track_count": 0},
                    ],
                    "next_href": "https://api.soundcloud.com/me/playlists?cursor=2&show_tracks=false",
                },
            )

        async with sc.SoundCloudClient("token", transport=httpx.MockTransport(handler)) as client:
            playlists = await client.list_my_playlists()

        assert [p.playlist_id for p in playlists] == ["1", "2", "3"]
        assert playlists[0].public_track_count == 5
        assert playlists[0].sharing == "private"
        assert "show_tracks=false" in calls[0]
        assert "linked_partitioning=true" in calls[0]
        assert len(calls) == 2

    async def test_an_empty_page_ends_the_listing(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                200,
                json={"collection": [], "next_href": "https://api.soundcloud.com/me/playlists?c=x"},
            )

        async with sc.SoundCloudClient("token", transport=httpx.MockTransport(handler)) as client:
            assert await client.list_my_playlists() == []
        assert len(calls) == 1

    async def test_a_playlist_reports_its_track_ids_in_order(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"id": 9, "title": "M", "tracks": [{"id": 3}, {"id": 1}, {"id": 2}]}
            )

        async with sc.SoundCloudClient("token", transport=httpx.MockTransport(handler)) as client:
            info = await client.get_playlist("9")

        assert info.track_ids == ["3", "1", "2"]
        assert info.track_count == 3

    async def test_soundcloud_track_count_is_kept_as_the_public_count(self) -> None:
        """A private playlist reports ``track_count: 0`` however many tracks it holds."""
        payload = {
            "urn": "soundcloud:playlists:9",
            "track_count": 0,
            "tracks": [{"urn": "soundcloud:tracks:1"}, {"urn": "soundcloud:tracks:2"}],
        }
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        async with sc.SoundCloudClient("token", transport=transport) as client:
            info = await client.get_playlist("9")
        assert info.track_count == 2
        assert info.public_track_count == 0


TRACKS_URL = "https://api.soundcloud.com/playlists/soundcloud:playlists:9/tracks"


class TestPlaylistTracks:
    async def test_every_page_is_read_and_blocked_tracks_are_included(self) -> None:
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            if request.url.params.get("cursor") == "2":
                return httpx.Response(200, json={"collection": [{"urn": "soundcloud:tracks:3"}]})
            return httpx.Response(
                200,
                json={
                    "collection": [{"urn": "soundcloud:tracks:1"}, {"urn": "soundcloud:tracks:2"}],
                    "next_href": f"{TRACKS_URL}?cursor=2",
                },
            )

        async with sc.SoundCloudClient("token", transport=httpx.MockTransport(handler)) as client:
            track_ids = await client.list_playlist_track_ids("9")

        assert track_ids == ["1", "2", "3"]
        assert seen[0].path == "/playlists/soundcloud:playlists:9/tracks"
        assert seen[0].params["linked_partitioning"] == "true"
        assert [url.params["access"] for url in seen] == ["playable,preview,blocked"] * 2

    async def test_a_track_that_cannot_be_identified_stops_the_read(self) -> None:
        from app.services.errors import UpstreamError

        payload = {"collection": [{"urn": "soundcloud:tracks:1"}, {"title": "uten id"}]}
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        async with sc.SoundCloudClient("token", transport=transport) as client:
            with pytest.raises(UpstreamError, match="ikke kan lese"):
                await client.list_playlist_track_ids("9")

    async def test_an_endless_track_list_is_refused(self) -> None:
        from app.services.errors import UpstreamError

        payload = {
            "collection": [{"urn": "soundcloud:tracks:1"}],
            "next_href": f"{TRACKS_URL}?cursor=n&access=playable,preview,blocked",
        }
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        async with sc.SoundCloudClient("token", transport=transport) as client:
            with pytest.raises(UpstreamError, match="endrer den ikke"):
                await client.list_playlist_track_ids("9", max_pages=3)


ME_TRACKS_URL = "https://api.soundcloud.com/me/tracks"


class TestTrackLookup:
    async def test_the_accounts_own_tracks_are_listed_across_pages(self) -> None:
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            if request.url.params.get("cursor") == "2":
                # SoundCloud ends with an empty page that still has a next_href.
                return httpx.Response(
                    200, json={"collection": [], "next_href": f"{ME_TRACKS_URL}?cursor=3"}
                )
            return httpx.Response(
                200,
                json={
                    "collection": [{"urn": "soundcloud:tracks:1"}, {"urn": "soundcloud:tracks:2"}],
                    "next_href": f"{ME_TRACKS_URL}?cursor=2",
                },
            )

        async with sc.SoundCloudClient("token", transport=httpx.MockTransport(handler)) as client:
            assert await client.list_my_track_ids() == {"1", "2"}
        assert seen[0].path == "/me/tracks"
        assert seen[0].params["limit"] == "200"
        assert len(seen) == 2

    async def test_a_track_is_missing_only_when_soundcloud_answers_404(self) -> None:
        from app.services.errors import ServiceError

        statuses = {"1": 200, "2": 404, "3": 500}

        def handler(request: httpx.Request) -> httpx.Response:
            track_id = request.url.path.rsplit(":", 1)[-1]
            return httpx.Response(statuses[track_id], json={"urn": f"soundcloud:tracks:{track_id}"})

        async with sc.SoundCloudClient("token", transport=httpx.MockTransport(handler)) as client:
            assert await client.track_exists("1") is True
            assert await client.track_exists("2") is False
            with pytest.raises(ServiceError):
                await client.track_exists("3")

"""Publishing to SoundCloud: merging, playlists, and automatic publishing after a job.

A small in-memory stand-in for the SoundCloud API is used, so nothing leaves the
machine. The central promise under test: a playlist never loses a track.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import session_scope
from app.models.core import GenerationJob, ItemStatus, JobItem, JobStatus, Page, SoundCloudPlaylist
from app.services import soundcloud_publish as pub
from app.services import soundcloud_service as sc
from app.services.app_settings import load_preferences, set_value
from tests.conftest import attach_audio, make_course, make_module, make_page

TRACK_URN = "soundcloud:tracks:"
PLAYLIST_URN = "soundcloud:playlists:"
UNPARSEABLE = httpx.Response(422, json={"message": "Could not parse JSON request body."})


def urn_tail(track: dict[str, Any]) -> str:
    return track["urn"].removeprefix(TRACK_URN) if "urn" in track else str(track["id"])


class FakeSoundCloud:
    """The parts of the SoundCloud API the app uses, kept in memory.

    Like the real API it identifies tracks and playlists by URN, refuses playlist
    bodies that reference tracks by the deprecated numeric ``id``, reports
    ``track_count`` as the number of *public* tracks, and pages ``/tracks``.

    Knobs for the ways SoundCloud's answers can disagree: ``embedded_limit`` cuts
    the tracks embedded in a playlist, ``paged_limit`` cuts the paged list,
    ``blocked`` tracks never appear embedded and only appear paged when ``access``
    asks for them, ``reverse_pages`` reorders the paged list, and ``drop_on_write``
    makes create and update lose the last track.
    """

    def __init__(
        self,
        *,
        page_size: int = 2,
        embedded_limit: int | None = None,
        paged_limit: int | None = None,
        public_count: int = 0,
        blocked: frozenset[str] = frozenset(),
        reverse_pages: bool = False,
        drop_on_write: bool = False,
        ignore_put: bool = False,
        me_tracks_error: bool = False,
        track_lookup_error: bool = False,
    ) -> None:
        self.next_track = 2_401_076_100  # beyond 32 bits, like real track ids today
        self.next_playlist = 900
        self.tracks: set[str] = set()  # tracks that exist on the account
        self.hidden_from_listing: set[str] = set()  # exist, but /me/tracks leaves them out
        self.playlists: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.put_bodies: list[list[str]] = []
        self.page_size = page_size
        self.embedded_limit = embedded_limit
        self.paged_limit = paged_limit
        self.public_count = public_count
        self.blocked = blocked
        self.reverse_pages = reverse_pages
        self.drop_on_write = drop_on_write
        self.ignore_put = ignore_put
        self.me_tracks_error = me_tracks_error
        self.track_lookup_error = track_lookup_error
        self.status_override: int | None = None

    def add_playlist(self, playlist_id: str, title: str, track_ids: list[int | str]) -> None:
        self.playlists[playlist_id] = {
            "urn": f"{PLAYLIST_URN}{playlist_id}",
            "title": title,
            "permalink_url": f"https://snd.sc/p{playlist_id}",
            "tracks": [{"urn": f"{TRACK_URN}{track_id}"} for track_id in track_ids],
        }

    def track_ids(self, playlist_id: str) -> list[str]:
        return [urn_tail(track) for track in self.playlists[playlist_id]["tracks"]]

    def delete_track(self, track_id: str) -> None:
        """Like deleting a track on SoundCloud: it also disappears from every playlist."""
        self.tracks.discard(track_id)
        for playlist in self.playlists.values():
            playlist["tracks"] = [t for t in playlist["tracks"] if urn_tail(t) != track_id]

    def delete_playlist(self, playlist_id: str, *, with_tracks: bool) -> None:
        playlist = self.playlists.pop(playlist_id)
        if with_tracks:
            for track in playlist["tracks"]:
                self.delete_track(urn_tail(track))

    def my_tracks_page(self, request: httpx.Request) -> httpx.Response:
        if self.me_tracks_error:
            return httpx.Response(500, json={"error": "down"})
        listed = sorted(self.tracks - self.hidden_from_listing)
        start = int(request.url.params.get("cursor", "0"))
        end = start + self.page_size
        return httpx.Response(
            200,
            json={
                "collection": [{"urn": f"{TRACK_URN}{t}"} for t in listed[start:end]],
                # Like SoundCloud, the last page still points at a next, empty page.
                "next_href": f"https://api.soundcloud.com/me/tracks?cursor={end}",
            },
        )

    @staticmethod
    def body_tracks(request: httpx.Request) -> list[dict[str, Any]] | None:
        tracks = json.loads(request.read())["playlist"]["tracks"]
        if not all(isinstance(t.get("urn"), str) and "id" not in t for t in tracks):
            return None
        return tracks

    def written(self, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tracks[:-1] if self.drop_on_write and tracks else tracks

    def playlist_payload(self, playlist_id: str) -> dict[str, Any]:
        stored = self.playlists[playlist_id]
        embedded = [t for t in stored["tracks"] if urn_tail(t) not in self.blocked]
        if self.embedded_limit is not None:
            embedded = embedded[: self.embedded_limit]
        return {**stored, "tracks": embedded, "track_count": self.public_count}

    def tracks_page(self, playlist_id: str, request: httpx.Request) -> httpx.Response:
        access = request.url.params.get("access", "playable,preview").split(",")
        tracks = [
            t
            for t in self.playlists[playlist_id]["tracks"]
            if "blocked" in access or urn_tail(t) not in self.blocked
        ]
        if self.reverse_pages:
            tracks = tracks[::-1]
        if self.paged_limit is not None:
            tracks = tracks[: self.paged_limit]
        start = int(request.url.params.get("cursor", "0"))
        end = start + self.page_size
        payload: dict[str, Any] = {"collection": tracks[start:end]}
        if end < len(tracks):
            # No access parameter here: the client must keep asking for blocked tracks.
            payload["next_href"] = (
                f"https://api.soundcloud.com/playlists/{PLAYLIST_URN}{playlist_id}/tracks"
                f"?linked_partitioning=true&cursor={end}"
            )
        return httpx.Response(200, json=payload)

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"error": "down"})

        if method == "POST" and path == "/tracks":
            self.next_track += 1
            self.tracks.add(str(self.next_track))
            return httpx.Response(
                201,
                json={
                    "urn": f"{TRACK_URN}{self.next_track}",
                    "title": "spor",
                    "permalink_url": f"https://snd.sc/t{self.next_track}",
                },
            )
        if method == "GET" and path == "/me/tracks":
            return self.my_tracks_page(request)
        if method == "GET" and path.startswith(f"/tracks/{TRACK_URN}"):
            if self.track_lookup_error:
                return httpx.Response(500, json={"error": "down"})
            found = path.removeprefix(f"/tracks/{TRACK_URN}") in self.tracks
            return httpx.Response(200 if found else 404, json={"urn": path.rsplit("/", 1)[-1]})
        if method == "POST" and path == "/playlists":
            tracks = self.body_tracks(request)
            if tracks is None:
                return UNPARSEABLE
            title = json.loads(request.read())["playlist"]["title"]
            self.next_playlist += 1
            playlist_id = str(self.next_playlist)
            self.add_playlist(playlist_id, title, [urn_tail(t) for t in self.written(tracks)])
            return httpx.Response(201, json=self.playlist_payload(playlist_id))
        if not path.startswith(f"/playlists/{PLAYLIST_URN}"):
            return httpx.Response(404, json={"error": f"unexpected {method} {path}"})

        playlist_id, _, sub = path.removeprefix(f"/playlists/{PLAYLIST_URN}").partition("/")
        if playlist_id not in self.playlists:
            return httpx.Response(404, json={"error": "not found"})
        if method == "GET" and sub == "tracks":
            return self.tracks_page(playlist_id, request)
        if method == "PUT" and not sub:
            tracks = self.body_tracks(request)
            if tracks is None:
                return UNPARSEABLE
            self.put_bodies.append([urn_tail(t) for t in tracks])
            if not self.ignore_put:
                self.playlists[playlist_id]["tracks"] = self.written(tracks)
            return httpx.Response(200, json=self.playlist_payload(playlist_id))
        if method == "GET" and not sub:
            return httpx.Response(200, json=self.playlist_payload(playlist_id))
        return httpx.Response(404, json={"error": f"unexpected {method} {path}"})

    def client(self) -> sc.SoundCloudClient:
        return sc.SoundCloudClient("token", transport=httpx.MockTransport(self.handler))


@pytest.fixture()
def module(db: Session, env: Path) -> dict[str, Any]:
    """One module, three pages, all with current audio and files on disk."""
    course = make_course(db)
    mod = make_module(db, course, "Modul 1 – Introduksjon", 1)
    pages = [
        make_page(db, course, mod, f"Side {i}", position=i, text=f"Tekst {i}.") for i in (1, 2, 3)
    ]
    for page in pages:
        audio = attach_audio(db, page)
        target = env / "output" / audio.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ID3")
    db.commit()
    return {"module_id": mod.id, "page_ids": [p.id for p in pages]}


def link(module_id: int, playlist_id: str, title: str = "Mine favoritter") -> None:
    with session_scope() as session:
        info = sc.PlaylistInfo(
            playlist_id=playlist_id, title=title, permalink_url="", track_count=0
        )
        pub.link_playlist(session, module_id, info)


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


class TestMerge:
    def test_a_new_playlist_gets_the_module_in_canvas_order(self) -> None:
        assert pub.merge_track_ids([], ["1", "2", "3"]) == ["1", "2", "3"]

    def test_tracks_already_in_the_playlist_are_never_removed(self) -> None:
        assert pub.merge_track_ids(["90", "91"], ["1", "2"]) == ["90", "91", "1", "2"]

    def test_nothing_to_add_leaves_the_list_as_it_was(self) -> None:
        assert pub.merge_track_ids(["90", "1", "2"], ["1", "2"]) == ["90", "1", "2"]

    def test_a_missing_page_is_inserted_next_to_its_canvas_neighbour(self) -> None:
        current = ["90", "1", "3", "91"]
        assert pub.merge_track_ids(current, ["1", "2", "3"]) == ["90", "1", "2", "3", "91"]

    def test_a_missing_first_page_goes_before_the_next_one(self) -> None:
        assert pub.merge_track_ids(["90", "2", "3"], ["1", "2", "3"]) == ["90", "1", "2", "3"]

    def test_a_reuploaded_track_is_swapped_in_place(self) -> None:
        merged = pub.merge_track_ids(["90", "old1", "2"], ["new1", "2"], {"old1": "new1"})
        assert merged == ["90", "new1", "2"]

    def test_an_old_version_is_dropped_when_the_new_one_is_already_listed(self) -> None:
        assert pub.merge_track_ids(["new1", "old1"], ["new1"], {"old1": "new1"}) == ["new1"]

    def test_the_users_own_duplicates_are_kept(self) -> None:
        assert pub.merge_track_ids(["90", "90"], ["1"]) == ["90", "90", "1"]


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #


class TestSummary:
    def test_says_whether_a_track_or_a_playlist_failed(self) -> None:
        uploads = [
            pub.UploadOutcome(1, "A", "reused"),
            pub.UploadOutcome(2, "B", "failed", message="x"),
        ]
        playlists = [pub.PlaylistOutcome(1, "Modul", "failed", message="SoundCloud nektet (403).")]
        ok, message = pub.summarize(uploads, playlists)
        assert ok is False
        assert message == (
            "0 spor lastet opp, 1 gjenbrukt, 1 spor feilet, 1 spilleliste feilet: x."
        )

    def test_a_detail_ending_in_a_period_is_not_doubled(self) -> None:
        playlists = [pub.PlaylistOutcome(1, "Modul", "failed", message="Krever Artist Pro.")]
        _ok, message = pub.summarize([], playlists)
        assert message == "0 spor lastet opp, 0 gjenbrukt, 1 spilleliste feilet: Krever Artist Pro."

    def test_all_good(self) -> None:
        uploads = [pub.UploadOutcome(1, "A", "uploaded")]
        playlists = [pub.PlaylistOutcome(1, "Modul", "created")]
        assert pub.summarize(uploads, playlists) == (
            True,
            "1 spor lastet opp, 0 gjenbrukt, 1 spilleliste opprettet.",
        )


class TestPublishing:
    async def test_first_publish_uploads_and_creates_a_playlist_in_canvas_order(
        self, module: dict[str, Any]
    ) -> None:
        fake = FakeSoundCloud()
        async with fake.client() as client:
            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [u.status for u in uploads] == ["uploaded"] * 3
        assert [p.status for p in playlists] == ["created"]
        (playlist_id,) = fake.playlists
        assert fake.track_ids(playlist_id) == [u.track_id for u in uploads]
        with session_scope() as session:
            stored = session.scalar(select(SoundCloudPlaylist))
            assert stored.playlist_id == playlist_id
            assert stored.module_id == module["module_id"]

    async def test_publishing_again_reuses_tracks_and_changes_nothing(
        self, module: dict[str, Any]
    ) -> None:
        fake = FakeSoundCloud()
        async with fake.client() as client:
            await pub.publish_modules(client, [module["module_id"]])
            fake.requests.clear()
            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [u.status for u in uploads] == ["reused"] * 3
        assert [p.status for p in playlists] == ["unchanged"]
        assert not any(method in ("POST", "PUT") for method, _path in fake.requests)

    async def test_an_existing_playlist_keeps_its_own_tracks(self, module: dict[str, Any]) -> None:
        fake = FakeSoundCloud()
        fake.add_playlist("555", "Mine favoritter", [7, 8])
        link(module["module_id"], "555")

        async with fake.client() as client:
            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [p.status for p in playlists] == ["updated"]
        assert fake.track_ids("555") == ["7", "8", *[u.track_id for u in uploads]]
        assert list(fake.playlists) == ["555"], "no new playlist may be created"

    async def test_a_regenerated_page_is_swapped_in_place(self, module: dict[str, Any]) -> None:
        fake = FakeSoundCloud()
        async with fake.client() as client:
            first, _ = await pub.publish_modules(client, [module["module_id"]])
            (playlist_id,) = fake.playlists
            # The user adds a track of their own to the playlist.
            fake.playlists[playlist_id]["tracks"].append({"urn": f"{TRACK_URN}42"})

            with session_scope() as session:  # page 2's audio is regenerated
                session.get(Page, module["page_ids"][1]).audio.file_hash = "new-audio"
            second, playlists = await pub.publish_modules(client, [module["module_id"]])

        new_track = next(u.track_id for u in second if u.status == "uploaded")
        assert fake.track_ids(playlist_id) == [
            first[0].track_id,
            new_track,
            first[2].track_id,
            "42",
        ]
        assert [p.status for p in playlists] == ["updated"]

    async def test_blocked_tracks_in_an_existing_playlist_are_kept(
        self, module: dict[str, Any]
    ) -> None:
        # Tracks 8 and 10 are missing from the embedded list and from any page that does
        # not ask for blocked tracks. A PUT built without them would delete them.
        fake = FakeSoundCloud(blocked=frozenset({"8", "10"}))
        fake.add_playlist("555", "Mine favoritter", [7, 8, 9, 10])
        link(module["module_id"], "555")

        async with fake.client() as client:
            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [p.status for p in playlists] == ["updated"]
        assert playlists[0].message == ""
        assert fake.track_ids("555") == ["7", "8", "9", "10", *[u.track_id for u in uploads]]

    async def test_a_long_playlist_is_read_across_pages(self, module: dict[str, Any]) -> None:
        fake = FakeSoundCloud(page_size=2, embedded_limit=3)
        fake.add_playlist("555", "Lang liste", [1, 2, 3, 4, 5])
        link(module["module_id"], "555")

        async with fake.client() as client:
            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [p.status for p in playlists] == ["updated"]
        assert playlists[0].message == ""
        assert fake.track_ids("555") == ["1", "2", "3", "4", "5", *[u.track_id for u in uploads]]

    @pytest.mark.parametrize(
        "fake",
        [
            pytest.param(FakeSoundCloud(paged_limit=1), id="paged-list-shorter"),
            pytest.param(FakeSoundCloud(public_count=5), id="public-count-higher"),
            pytest.param(FakeSoundCloud(reverse_pages=True), id="different-order"),
        ],
    )
    async def test_a_track_list_that_does_not_add_up_is_left_alone(
        self, module: dict[str, Any], fake: FakeSoundCloud
    ) -> None:
        fake.add_playlist("555", "Mine favoritter", [7, 8])
        link(module["module_id"], "555")

        async with fake.client() as client:
            _, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [p.status for p in playlists] == ["failed"]
        assert "endrer den ikke" in playlists[0].message
        assert fake.put_bodies == []
        assert fake.track_ids("555") == ["7", "8"]

    async def test_a_created_playlist_that_lost_a_track_is_reported(
        self, module: dict[str, Any]
    ) -> None:
        fake = FakeSoundCloud(drop_on_write=True)
        async with fake.client() as client:
            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [p.status for p in playlists] == ["created"]
        assert "Sjekk spillelisten" in playlists[0].message
        assert pub.summarize(uploads, playlists)[0] is False

    async def test_a_deleted_linked_playlist_is_reported_clearly(
        self, module: dict[str, Any]
    ) -> None:
        fake = FakeSoundCloud()
        link(module["module_id"], "404404")
        async with fake.client() as client:
            _, playlists = await pub.publish_modules(client, [module["module_id"]])
        assert [p.status for p in playlists] == ["failed"]
        assert "finnes ikke lenger" in playlists[0].message

    async def test_a_playlist_that_did_not_take_the_update_is_reported(
        self, module: dict[str, Any]
    ) -> None:
        fake = FakeSoundCloud(ignore_put=True)
        fake.add_playlist("555", "Mine favoritter", [7])
        link(module["module_id"], "555")

        async with fake.client() as client:
            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert playlists[0].status == "updated"
        assert "Sjekk spillelisten" in playlists[0].message
        ok, _message = pub.summarize(uploads, playlists)
        assert ok is False


class TestDeletedTracks:
    """Tracks deleted on SoundCloud, for example together with a playlist."""

    async def test_tracks_deleted_with_a_playlist_are_uploaded_again_into_a_new_one(
        self, module: dict[str, Any]
    ) -> None:
        fake = FakeSoundCloud()
        async with fake.client() as client:
            first, _ = await pub.publish_modules(client, [module["module_id"]])
            (old_playlist,) = fake.playlists
            fake.delete_playlist(old_playlist, with_tracks=True)
            with session_scope() as session:  # the user picks «Ny spilleliste»
                pub.link_playlist(session, module["module_id"], None)

            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [u.status for u in uploads] == ["uploaded"] * 3
        assert all("slettet" in u.message for u in uploads)
        assert {u.track_id for u in uploads}.isdisjoint(u.track_id for u in first)
        assert [p.status for p in playlists] == ["created"]
        assert playlists[0].message == ""
        (new_playlist,) = fake.playlists
        assert fake.track_ids(new_playlist) == [u.track_id for u in uploads]

    async def test_an_emptied_linked_playlist_is_filled_again(self, module: dict[str, Any]) -> None:
        fake = FakeSoundCloud()
        async with fake.client() as client:
            first, _ = await pub.publish_modules(client, [module["module_id"]])
            (playlist_id,) = fake.playlists
            for upload in first:
                fake.delete_track(upload.track_id)

            uploads, playlists = await pub.publish_modules(client, [module["module_id"]])

        assert [u.status for u in uploads] == ["uploaded"] * 3
        assert [p.status for p in playlists] == ["updated"]
        assert fake.track_ids(playlist_id) == [u.track_id for u in uploads]

    async def test_tracks_that_still_exist_are_confirmed_by_the_account_listing(
        self, module: dict[str, Any]
    ) -> None:
        fake = FakeSoundCloud()
        async with fake.client() as client:
            await pub.publish_modules(client, [module["module_id"]])
            fake.requests.clear()
            uploads, _ = await pub.publish_modules(client, [module["module_id"]])

        assert [u.status for u in uploads] == ["reused"] * 3
        assert ("GET", "/me/tracks") in fake.requests
        assert not any(path.startswith("/tracks") for _method, path in fake.requests)

    async def test_a_track_left_out_of_the_listing_is_checked_before_uploading(
        self, module: dict[str, Any]
    ) -> None:
        fake = FakeSoundCloud()
        async with fake.client() as client:
            first, _ = await pub.publish_modules(client, [module["module_id"]])
            fake.hidden_from_listing = {first[0].track_id}
            fake.requests.clear()
            uploads, _ = await pub.publish_modules(client, [module["module_id"]])

        assert [u.status for u in uploads] == ["reused"] * 3
        assert ("GET", f"/tracks/{TRACK_URN}{first[0].track_id}") in fake.requests
        assert ("POST", "/tracks") not in fake.requests

    @pytest.mark.parametrize(
        ("me_tracks_error", "track_lookup_error"),
        [(True, False), (True, True)],
        ids=["listing-fails", "listing-and-lookup-fail"],
    )
    async def test_errors_while_checking_never_cause_a_duplicate_upload(
        self, module: dict[str, Any], me_tracks_error: bool, track_lookup_error: bool
    ) -> None:
        fake = FakeSoundCloud()
        async with fake.client() as client:
            await pub.publish_modules(client, [module["module_id"]])
            fake.me_tracks_error = me_tracks_error
            fake.track_lookup_error = track_lookup_error
            fake.requests.clear()
            uploads, _ = await pub.publish_modules(client, [module["module_id"]])

        assert [u.status for u in uploads] == ["reused"] * 3
        assert ("POST", "/tracks") not in fake.requests


# --------------------------------------------------------------------------- #
# Which modules count as complete
# --------------------------------------------------------------------------- #


class TestCompleteModules:
    def test_every_page_with_text_needs_current_audio(self, db: Session, env: Path) -> None:
        course = make_course(db)
        mod = make_module(db, course, "Modul", 1)
        first = make_page(db, course, mod, "A", position=1, text="Tekst.")
        second = make_page(db, course, mod, "B", position=2, text="Mer tekst.")
        make_page(db, course, mod, "Tom", position=3, text="")
        attach_audio(db, first)
        db.commit()
        assert pub.complete_module_ids(db, [mod.id]) == []

        attach_audio(db, second)
        db.commit()
        db.expire_all()
        assert pub.complete_module_ids(db, [mod.id]) == [mod.id], "an empty page must not block"

    def test_stale_audio_does_not_count(self, db: Session, env: Path) -> None:
        course = make_course(db)
        mod = make_module(db, course, "Modul", 1)
        page = make_page(db, course, mod, "A", position=1, text="Tekst.")
        attach_audio(db, page, stale=True)
        db.commit()
        assert pub.complete_module_ids(db, [mod.id]) == []

    def test_a_module_without_any_text_is_not_complete(self, db: Session, env: Path) -> None:
        course = make_course(db)
        mod = make_module(db, course, "Modul", 1)
        make_page(db, course, mod, "Tom", position=1, text="")
        db.commit()
        assert pub.complete_module_ids(db, [mod.id]) == []


# --------------------------------------------------------------------------- #
# Automatic publishing after a job
# --------------------------------------------------------------------------- #


def make_job(db: Session, page_ids: list[int]) -> int:
    job = GenerationJob(status=JobStatus.COMPLETED, total_items=len(page_ids))
    db.add(job)
    db.flush()
    for page_id in page_ids:
        db.add(JobItem(job_id=job.id, page_id=page_id, status=ItemStatus.DONE))
    db.commit()
    return job.id


def connect_soundcloud(db: Session) -> None:
    expires = datetime.now(UTC) + timedelta(hours=2)
    sc.save_token(db, sc.TokenBundle("access-token", "refresh-token", expires))
    db.commit()


def switch_on(db: Session) -> None:
    set_value(db, "soundcloud_auto_publish", "1")
    db.commit()


class TestPublishAfterJob:
    async def test_is_off_by_default(self, db: Session, module: dict[str, Any]) -> None:
        assert load_preferences(db).soundcloud_auto_publish is False
        connect_soundcloud(db)
        job_id = make_job(db, module["page_ids"])
        fake = FakeSoundCloud()

        assert (
            await pub.publish_after_job(job_id, transport=httpx.MockTransport(fake.handler)) is None
        )
        assert fake.requests == []

    async def test_publishes_a_complete_module_when_switched_on(
        self, db: Session, module: dict[str, Any]
    ) -> None:
        connect_soundcloud(db)
        switch_on(db)
        # The job only touched one page, but that completed the module.
        job_id = make_job(db, module["page_ids"][:1])
        fake = FakeSoundCloud()

        message = await pub.publish_after_job(job_id, transport=httpx.MockTransport(fake.handler))

        assert message is not None and "3 spor lastet opp" in message
        assert len(fake.playlists) == 1
        with session_scope() as session:
            record = pub.last_publish(session)
        assert record["ok"] is True
        assert record["job_id"] == job_id
        assert record["modules"] == ["Modul 1 – Introduksjon"]
        assert record["at_label"]

    async def test_skips_a_module_that_is_not_complete(self, db: Session, env: Path) -> None:
        course = make_course(db)
        mod = make_module(db, course, "Modul", 1)
        done = make_page(db, course, mod, "A", position=1, text="Tekst.")
        make_page(db, course, mod, "B", position=2, text="Ikke generert ennå.")
        attach_audio(db, done)
        db.commit()
        connect_soundcloud(db)
        switch_on(db)
        job_id = make_job(db, [done.id])
        fake = FakeSoundCloud()

        assert (
            await pub.publish_after_job(job_id, transport=httpx.MockTransport(fake.handler)) is None
        )
        assert fake.requests == []

    async def test_skips_when_soundcloud_is_not_connected(
        self, db: Session, module: dict[str, Any]
    ) -> None:
        switch_on(db)
        job_id = make_job(db, module["page_ids"])
        fake = FakeSoundCloud()
        assert (
            await pub.publish_after_job(job_id, transport=httpx.MockTransport(fake.handler)) is None
        )
        assert fake.requests == []

    async def test_soundcloud_errors_are_reported_not_raised(
        self, db: Session, module: dict[str, Any]
    ) -> None:
        connect_soundcloud(db)
        switch_on(db)
        job_id = make_job(db, module["page_ids"])
        fake = FakeSoundCloud()
        fake.status_override = 503

        message = await pub.publish_after_job(job_id, transport=httpx.MockTransport(fake.handler))

        assert message is not None and "feilet" in message
        with session_scope() as session:
            assert pub.last_publish(session)["ok"] is False

    async def test_an_unexpected_error_is_recorded_not_raised(
        self, db: Session, module: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connect_soundcloud(db)
        switch_on(db)
        job_id = make_job(db, module["page_ids"])

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(pub, "publish_modules", boom)
        message = await pub.publish_after_job(job_id)

        assert message is not None and "boom" in message
        with session_scope() as session:
            assert pub.last_publish(session)["ok"] is False


class TestJobHook:
    async def test_runs_after_a_finished_job_but_not_a_cancelled_one(
        self, db: Session, module: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.jobs.pipeline import run_job
        from tests.test_pipeline import FakeElevenLabs, build_job

        seen: list[int] = []

        async def fake_publish(job_id: int, **_kwargs: Any) -> None:
            seen.append(job_id)

        monkeypatch.setattr(pub, "publish_after_job", fake_publish)
        pages = [db.get(Page, page_id) for page_id in module["page_ids"]]

        finished = build_job(db, pages)
        await run_job(finished, tts_client=FakeElevenLabs())
        cancelled = build_job(db, pages)
        await run_job(cancelled, is_cancelled=lambda: True, tts_client=FakeElevenLabs())

        assert seen == [finished]

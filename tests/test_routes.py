"""FastAPI routes: JSON API and the rendered GUI."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.core import GenerationJob, JobStatus, Page
from app.services.app_settings import set_many


class TestHealth:
    def test_health_is_open_and_reports_version(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"]


class TestSettingsApi:
    def test_status_masks_every_secret(self, client: TestClient) -> None:
        response = client.get("/api/settings/status")
        assert response.status_code == 200
        body = response.json()

        assert body["canvas_url"] == "https://canvas.example.test"
        for secret in body["secrets"]:
            assert "dummy" not in secret["masked"].lower()
            assert len(secret["masked"]) <= 12
        payload = response.text
        for value in ("dummy-canvas-token", "sk-dummy-elevenlabs-key-for-tests"):
            assert value not in payload

    def test_environment_managed_keys_are_marked_locked(self, client: TestClient) -> None:
        body = client.get("/api/settings/status").json()
        canvas_token = next(s for s in body["secrets"] if s["key"] == "CANVAS_TOKEN")
        assert canvas_token["configured"]
        assert canvas_token["source"] == "env"
        assert canvas_token["locked"]

    def test_saving_preferences_persists_them(self, client: TestClient) -> None:
        response = client.post(
            "/api/settings",
            json={"monthly_budget_usd": 42, "filename_style": "title", "ai_provider": "openai"},
        )
        assert response.status_code == 200

        prefs = client.get("/api/settings/status").json()["preferences"]
        assert prefs["monthly_budget_usd"] == "42.0"
        assert prefs["filename_style"] == "title"
        assert prefs["ai_provider"] == "openai"

    def test_env_managed_secret_is_reported_as_skipped(self, client: TestClient) -> None:
        response = client.post("/api/settings", json={"canvas_token": "nytt-token"})
        assert response.status_code == 200
        assert response.json()["skipped_env_managed"] == ["CANVAS_TOKEN"]

    def test_invalid_filename_style_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/settings", json={"filename_style": "kaos"})
        assert response.status_code == 422
        assert "filnavnstil" in response.json()["detail"].lower()

    def test_unknown_provider_test_is_a_404(self, client: TestClient) -> None:
        assert client.post("/api/settings/test/facebook").status_code == 404

    def _capture_canvas_check(self, monkeypatch) -> dict[str, str]:
        from app.services import canvas_service

        seen: dict[str, str] = {}

        async def fake_check(url: str, token: str, **_kwargs: Any) -> dict[str, Any]:
            seen["url"], seen["token"] = url, token
            return {"ok": True, "detail": "Tilkoblet som Test."}

        monkeypatch.setattr(canvas_service, "check_canvas_connection", fake_check)
        return seen

    def test_connection_test_uses_values_typed_in_the_form(
        self, client: TestClient, monkeypatch, env
    ) -> None:
        seen = self._capture_canvas_check(monkeypatch)
        response = client.post(
            "/api/settings/test/canvas",
            json={"canvas_url": "https://hiof.instructure.com/", "canvas_token": "typed-token"},
        )
        body = response.json()
        assert body["ok"] is True
        assert seen == {"url": "https://hiof.instructure.com", "token": "typed-token"}
        assert "ikke lagret" in body["detail"]
        assert not (env / ".env").exists(), "testing a connection must never save anything"

    def test_connection_test_falls_back_to_saved_values(
        self, client: TestClient, monkeypatch
    ) -> None:
        seen = self._capture_canvas_check(monkeypatch)
        body = client.post("/api/settings/test/canvas").json()
        assert body["ok"] is True
        assert seen == {
            "url": "https://canvas.example.test",
            "token": "12345~dummy-canvas-token-for-tests",
        }
        assert "ikke lagret" not in body["detail"]

    def test_connection_test_ignores_fields_of_other_providers(
        self, client: TestClient, monkeypatch
    ) -> None:
        seen = self._capture_canvas_check(monkeypatch)
        client.post(
            "/api/settings/test/canvas",
            json={"elevenlabs_api_key": "belongs-to-another-provider"},
        )
        assert seen["token"] == "12345~dummy-canvas-token-for-tests"


class TestPagesApi:
    def test_lists_pages_of_the_selected_course(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        response = client.get("/api/pages")
        assert response.status_code == 200
        titles = [p["title"] for p in response.json()]
        assert titles == ["Velkommen", "Hva skal vi lære?", "Introduksjon", "Oppsummering"]

    def test_page_status_reflects_change_detection(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        by_title = {p["title"]: p for p in client.get("/api/pages").json()}
        assert by_title["Velkommen"]["status"] == "up_to_date"
        assert by_title["Velkommen"]["status_label"] == "Oppdatert"
        assert by_title["Oppsummering"]["status"] == "not_generated"

    def test_detail_returns_both_texts(self, client: TestClient, seeded: dict[str, Any]) -> None:
        page_id = seeded["pages"][0].id
        body = client.get(f"/api/pages/{page_id}").json()
        assert body["original_text"] == "Velkommen til emnet."
        assert body["processed_text"] == ""
        assert body["module_name"] == "Modul 1 – Introduksjon"

    def test_missing_page_is_a_404(self, client: TestClient, seeded: dict[str, Any]) -> None:
        assert client.get("/api/pages/99999").status_code == 404

    def test_editing_text_marks_it_as_manual(
        self, client: TestClient, seeded: dict[str, Any], db: Session
    ) -> None:
        page_id = seeded["pages"][1].id
        response = client.put(
            f"/api/pages/{page_id}/text", json={"processed_text": "Min egen versjon."}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["processed_text"] == "Min egen versjon."
        assert body["manually_edited"] is True

        db.expire_all()
        assert db.get(Page, page_id).tts_text == "Min egen versjon."

    def test_editing_text_invalidates_existing_audio(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page_id = seeded["pages"][0].id  # has up-to-date audio
        client.put(f"/api/pages/{page_id}/text", json={"processed_text": "Endret tekst."})
        assert client.get(f"/api/pages/{page_id}").json()["status"] == "text_changed"

    def test_reset_text_falls_back_to_the_original(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page_id = seeded["pages"][1].id
        client.put(f"/api/pages/{page_id}/text", json={"processed_text": "Noe annet."})
        body = client.post(f"/api/pages/{page_id}/reset-text").json()
        assert body["processed_text"] == ""
        assert body["manually_edited"] is False

    def test_recleaning_recomputes_text_from_stored_html(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page_id = seeded["pages"][1].id
        body = client.post(f"/api/pages/{page_id}/clean").json()
        assert body["original_text"] == "Vi skal lære mye."

    def test_process_without_a_provider_is_rejected(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page_id = seeded["pages"][1].id
        response = client.post(
            f"/api/pages/{page_id}/process", json={"ai_provider": "none", "ai_model": ""}
        )
        assert response.status_code == 400


class TestCostApi:
    def test_estimate_reports_characters_and_cost(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page_ids = [p.id for p in seeded["pages"]]
        response = client.post(
            "/api/costs/estimate",
            json={
                "page_ids": page_ids,
                "mode": "selected",
                "tts_model": "eleven_multilingual_v2",
            },
        )
        assert response.status_code == 200
        body = response.json()

        assert body["page_count"] == 4
        assert body["tts_page_count"] == 3  # one page already has current audio
        assert body["total_characters"] > 0
        assert body["elevenlabs_usd"] > 0
        assert body["currency_code"] == "NOK"
        assert len(body["pages"]) == 4

    def test_estimate_shows_the_saving_from_change_detection(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page_ids = [p.id for p in seeded["pages"]]
        body = client.post(
            "/api/costs/estimate",
            json={"page_ids": page_ids, "mode": "selected", "tts_model": "eleven_flash_v2_5"},
        ).json()
        assert body["full_regeneration_usd"] > body["total_usd"]
        assert body["savings_usd"] > 0

    def test_changed_mode_selects_only_changed_pages(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        client.put(f"/api/pages/{seeded['pages'][0].id}/text", json={"processed_text": "Endret."})
        body = client.post(
            "/api/costs/estimate",
            json={"mode": "changed", "tts_model": "eleven_multilingual_v2"},
        ).json()
        assert body["page_count"] == 1
        assert body["pages"][0]["title"] == "Velkommen"

    def test_missing_mode_selects_pages_without_audio(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        body = client.post(
            "/api/costs/estimate",
            json={"mode": "missing", "tts_model": "eleven_multilingual_v2"},
        ).json()
        assert body["page_count"] == 3

    def test_usage_endpoint_returns_all_scopes(self, client: TestClient) -> None:
        body = client.get("/api/costs/usage").json()
        assert body["this_month"]["scope"] == "month"
        assert body["all_time"]["scope"] == "all"


class TestGenerationApi:
    def _choose_voice(self, client: TestClient) -> None:
        client.post(
            "/api/settings",
            json={
                "elevenlabs_voice_id": "v1",
                "elevenlabs_voice_name": "Testrøst",
                "elevenlabs_model_id": "eleven_multilingual_v2",
            },
        )

    def test_generation_requires_voice_and_model(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        response = client.post(
            "/api/generation", json={"page_ids": [seeded["pages"][1].id], "mode": "selected"}
        )
        assert response.status_code == 400
        assert "stemme" in response.json()["detail"]

    def test_empty_selection_is_rejected(self, client: TestClient, seeded: dict[str, Any]) -> None:
        self._choose_voice(client)
        response = client.post("/api/generation", json={"page_ids": [], "mode": "selected"})
        assert response.status_code == 400

    def test_job_is_created_and_queued(
        self, client: TestClient, seeded: dict[str, Any], recorded_jobs: list[int], db: Session
    ) -> None:
        self._choose_voice(client)
        page_ids = [p.id for p in seeded["pages"][1:]]

        response = client.post("/api/generation", json={"page_ids": page_ids, "mode": "selected"})
        assert response.status_code == 200
        body = response.json()
        assert body["total_items"] == 3

        db.expire_all()
        job = db.get(GenerationJob, body["job_id"])
        assert job.voice_name == "Testrøst"
        assert job.total_items == 3
        assert len(job.items) == 3

    def test_expensive_job_requires_confirmation(
        self, client: TestClient, db: Session, seeded: dict[str, Any]
    ) -> None:
        self._choose_voice(client)
        # Make one page huge and set a low threshold.
        page = db.get(Page, seeded["pages"][1].id)
        page.original_text = "x" * 200_000
        from app.services.hashing import text_hash

        page.original_hash = text_hash(page.original_text)
        set_many(db, {"cost_confirm_threshold_usd": "5"})
        db.commit()

        response = client.post("/api/generation", json={"page_ids": [page.id], "mode": "selected"})
        assert response.status_code == 409
        body = response.json()
        assert body["requires_confirmation"] is True
        assert "koste" in body["message"]

        confirmed = client.post(
            "/api/generation",
            json={"page_ids": [page.id], "mode": "selected", "confirmed": True},
        )
        assert confirmed.status_code == 200

    def test_job_status_can_be_polled(self, client: TestClient, seeded: dict[str, Any]) -> None:
        self._choose_voice(client)
        job_id = client.post(
            "/api/generation",
            json={"page_ids": [seeded["pages"][1].id], "mode": "selected"},
        ).json()["job_id"]

        body = client.get(f"/api/generation/{job_id}").json()
        assert body["id"] == job_id
        assert body["status_label"] in {"I kø", "Kjører", "Ferdig"}
        assert len(body["items"]) == 1
        assert body["items"][0]["status_label"] == "Venter"

    def test_unknown_job_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/generation/12345").status_code == 404

    def test_cancel_marks_the_job(self, client: TestClient, seeded: dict[str, Any]) -> None:
        self._choose_voice(client)
        job_id = client.post(
            "/api/generation",
            json={"page_ids": [seeded["pages"][1].id], "mode": "selected"},
        ).json()["job_id"]
        assert client.post(f"/api/generation/{job_id}/cancel").json()["ok"] is True

    def test_cancelling_a_finished_job_is_reported(
        self, client: TestClient, seeded: dict[str, Any], db: Session
    ) -> None:
        self._choose_voice(client)
        job_id = client.post(
            "/api/generation",
            json={"page_ids": [seeded["pages"][1].id], "mode": "selected"},
        ).json()["job_id"]

        job = db.get(GenerationJob, job_id)
        job.status = JobStatus.COMPLETED
        db.commit()

        assert client.post(f"/api/generation/{job_id}/cancel").json()["ok"] is False


class TestAudioApi:
    def test_missing_audio_is_a_404(self, client: TestClient, seeded: dict[str, Any]) -> None:
        assert client.get(f"/api/audio/{seeded['pages'][1].id}").status_code == 404

    def test_registered_but_absent_file_is_reported(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        response = client.get(f"/api/audio/{seeded['pages'][0].id}")
        assert response.status_code == 404
        assert "borte fra disk" in response.json()["detail"]

    def test_serves_an_existing_file(
        self, client: TestClient, seeded: dict[str, Any], env, db: Session
    ) -> None:
        page = seeded["pages"][0]
        target = env / "output" / page.audio.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ID3-fake-mp3")

        response = client.get(f"/api/audio/{page.id}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.content == b"ID3-fake-mp3"

    def test_download_sets_a_safe_filename(
        self, client: TestClient, seeded: dict[str, Any], env
    ) -> None:
        page = seeded["pages"][0]
        target = env / "output" / page.audio.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ID3")

        response = client.get(f"/api/audio/{page.id}?download=1")
        assert response.status_code == 200
        assert "velkommen.mp3" in response.headers["content-disposition"]

    def test_tampered_stored_path_is_rejected(
        self, client: TestClient, seeded: dict[str, Any], db: Session
    ) -> None:
        page = db.get(Page, seeded["pages"][0].id)
        page.audio.relative_path = "../../etc/passwd"
        db.commit()

        response = client.get(f"/api/audio/{page.id}")
        assert response.status_code == 400
        assert "Ugyldig filsti" in response.json()["detail"]


class TestSoundCloudApi:
    def test_status_reports_configured_but_not_connected(self, client: TestClient) -> None:
        body = client.get("/api/soundcloud/status").json()
        assert body["configured"] is True
        assert body["connected"] is False
        assert "ikke tilkoblet" in body["detail"].lower()

    def test_connect_returns_an_authorize_url_with_pkce(self, client: TestClient) -> None:
        body = client.post("/api/soundcloud/connect").json()
        assert body["authorize_url"].startswith("https://secure.soundcloud.com/authorize?")
        assert "code_challenge_method=S256" in body["authorize_url"]

    def test_callback_rejects_a_mismatched_state(self, client: TestClient) -> None:
        response = client.get(
            "/api/soundcloud/callback?code=abc&state=wrong", follow_redirects=False
        )
        assert response.status_code == 303
        assert "ugyldig-tilstand" in response.headers["location"]

    def test_upload_without_a_connection_fails_softly(
        self, client: TestClient, seeded: dict[str, Any], env
    ) -> None:
        page = seeded["pages"][0]
        target = env / "output" / page.audio.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ID3")

        response = client.post("/api/soundcloud/upload", json={"page_ids": [page.id]})
        assert response.status_code == 400
        assert "SoundCloud" in response.json()["detail"]


class TestGui:
    def test_dashboard_renders(self, client: TestClient, seeded: dict[str, Any]) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Oversikt" in response.text
        assert "Mitt emne" in response.text
        # Emoji navigation is part of the spec.
        for emoji in ("📊", "🎓", "🎧", "☁️", "⚙️"):
            assert emoji in response.text

    def test_canvas_view_lists_modules_and_pages(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        response = client.get("/canvas")
        assert response.status_code == 200
        assert "Modul 1 – Introduksjon" in response.text
        assert "Velkommen" in response.text
        assert "Oppdatert" in response.text

    def test_page_detail_shows_both_editors(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        response = client.get(f"/side/{seeded['pages'][0].id}")
        assert response.status_code == 200
        assert "Originaltekst" in response.text
        assert "Bearbeidet tekst" in response.text

    def test_generation_view_renders_the_table(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        response = client.get("/generering")
        assert response.status_code == 200
        assert "Generering" in response.text
        assert "Oppsummering" in response.text

    def test_soundcloud_view_documents_the_limitations(self, client: TestClient) -> None:
        response = client.get("/soundcloud")
        assert response.status_code == 200
        assert "Artist Pro" in response.text

    def test_settings_view_never_leaks_a_key(self, client: TestClient) -> None:
        response = client.get("/innstillinger")
        assert response.status_code == 200
        assert "Innstillinger" in response.text
        for secret in ("dummy-canvas-token", "sk-dummy-elevenlabs-key-for-tests"):
            assert secret not in response.text

    def test_progress_fragment_renders(self, client: TestClient, seeded: dict[str, Any]) -> None:
        response = client.get("/fragments/job-progress")
        assert response.status_code == 200
        assert "job-progress" in response.text

    def test_estimate_fragment_returns_html_and_flags(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        response = client.post(
            "/fragments/estimate",
            json={
                "page_ids": [p.id for p in seeded["pages"]],
                "mode": "selected",
                "tts_model": "eleven_multilingual_v2",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert "Estimert total" in body["html"]
        assert body["tts_ready"] is False  # no voice chosen yet


class TestAppLock:
    def test_login_is_required_when_a_password_is_set(self, env, monkeypatch) -> None:
        monkeypatch.setenv("APP_PASSWORD", "hemmelig")
        from app.config import reset_store
        from app.main import create_app

        reset_store(env_path=env / ".env")

        with TestClient(create_app()) as locked:
            assert locked.get("/api/health").status_code == 200  # probes stay open

            redirected = locked.get("/", follow_redirects=False)
            assert redirected.status_code == 303
            assert redirected.headers["location"] == "/logg-inn"

            assert locked.get("/api/pages").status_code == 401

            assert locked.post("/logg-inn", data={"password": "feil"}).status_code == 401
            ok = locked.post("/logg-inn", data={"password": "hemmelig"}, follow_redirects=False)
            assert ok.status_code == 303
            assert locked.get("/api/pages").status_code == 200


class TestErrorHandling:
    def test_canvas_failure_becomes_a_readable_error(self, client: TestClient, monkeypatch) -> None:
        from app.services import canvas_service

        real_client = canvas_service.CanvasClient

        def failing_client(*args: Any, **kwargs: Any) -> Any:
            kwargs["transport"] = httpx.MockTransport(
                lambda _r: httpx.Response(401, json={"errors": [{"message": "bad token"}]})
            )
            return real_client(*args, **kwargs)

        monkeypatch.setattr("app.routes.canvas.CanvasClient", failing_client)

        response = client.get("/api/canvas/courses")
        assert response.status_code == 502
        assert "401" in response.json()["detail"]
        assert response.json()["provider"] == "canvas"

    def test_validation_errors_are_readable(self, client: TestClient) -> None:
        response = client.post("/api/generation", json={"mode": "tull"})
        assert response.status_code == 422
        assert "utvalgsmodus" in response.json()["detail"].lower()


class TestSoundCloudPublishingApi:
    def test_listing_playlists_requires_a_connection(self, client: TestClient) -> None:
        response = client.get("/api/soundcloud/playlists")
        assert response.status_code == 400
        assert "SoundCloud" in response.json()["detail"]

    def test_linking_an_existing_playlist(
        self, client: TestClient, seeded: dict[str, Any], db: Session, monkeypatch
    ) -> None:
        from contextlib import asynccontextmanager

        from app.models.core import SoundCloudPlaylist
        from app.services import soundcloud_publish
        from app.services.soundcloud_service import SoundCloudClient

        def handler(_request: httpx.Request) -> httpx.Response:
            playlist = {"id": 555, "title": "Mine favoritter", "tracks": [{"id": 7}]}
            return httpx.Response(200, json={**playlist, "permalink_url": "https://snd.sc/p555"})

        @asynccontextmanager
        async def fake_client(transport=None):
            async with SoundCloudClient("token", transport=httpx.MockTransport(handler)) as c:
                yield c

        monkeypatch.setattr(soundcloud_publish, "connected_client", fake_client)
        module = seeded["modules"][0]

        response = client.put(
            f"/api/soundcloud/modules/{module.id}/playlist", json={"playlist_id": "555"}
        )
        assert response.status_code == 200
        assert "beholdes" in response.json()["message"]

        db.expire_all()
        stored = db.query(SoundCloudPlaylist).filter_by(module_id=module.id).one()
        assert stored.playlist_id == "555"
        assert stored.title == "Mine favoritter"

    def test_choosing_a_new_playlist_removes_the_link(
        self, client: TestClient, seeded: dict[str, Any], db: Session
    ) -> None:
        from app.models.core import SoundCloudPlaylist

        module = seeded["modules"][0]
        db.add(SoundCloudPlaylist(module_id=module.id, playlist_id="555", title="Gammel"))
        db.commit()

        response = client.put(
            f"/api/soundcloud/modules/{module.id}/playlist", json={"playlist_id": ""}
        )
        assert response.status_code == 200
        db.expire_all()
        assert db.query(SoundCloudPlaylist).count() == 0

    def test_an_invalid_playlist_id_is_rejected(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        module = seeded["modules"][0]
        response = client.put(
            f"/api/soundcloud/modules/{module.id}/playlist", json={"playlist_id": "abc"}
        )
        assert response.status_code == 422

    def test_an_unknown_module_is_a_404(self, client: TestClient) -> None:
        response = client.put("/api/soundcloud/modules/99999/playlist", json={"playlist_id": ""})
        assert response.status_code == 404

    def test_the_auto_publish_setting_is_saved(self, client: TestClient) -> None:
        assert (
            client.post("/api/settings", json={"soundcloud_auto_publish": True}).status_code == 200
        )
        preferences = client.get("/api/settings/status").json()["preferences"]
        assert preferences["soundcloud_auto_publish"] == "True"

    def test_publishing_now_requires_a_connection(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        module = seeded["modules"][0]
        response = client.post("/api/soundcloud/publish", json={"module_ids": [module.id]})
        assert response.status_code == 400

    def test_the_soundcloud_page_lists_modules(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        response = client.get("/soundcloud")
        assert response.status_code == 200
        assert "Moduler og spillelister" in response.text
        assert "Modul 1 – Introduksjon" in response.text


def _client_without(env, monkeypatch, *keys: str) -> TestClient:
    """A fresh app where the given credentials are unset and `.env` is writable."""
    from app.config import reset_store
    from app.main import create_app

    for key in keys:
        monkeypatch.delenv(key, raising=False)
    reset_store(env_path=env / ".env")
    return TestClient(create_app())


class TestApiKeysPage:
    """Keys live on their own page; Innstillinger keeps only the choices."""

    def test_the_page_shows_every_credential(self, client: TestClient) -> None:
        body = client.get("/api-nokler").text
        for field in (
            "canvas_url",
            "canvas_token",
            "elevenlabs_api_key",
            "openai_api_key",
            "anthropic_api_key",
            "soundcloud_client_id",
            "soundcloud_client_secret",
            "soundcloud_redirect_uri",
            "app_password",
        ):
            assert f'id="{field}"' in body
        for provider in ("canvas", "elevenlabs", "openai", "anthropic", "soundcloud"):
            assert f'id="test-{provider}-result"' in body

    def test_the_page_never_leaks_a_key(self, client: TestClient) -> None:
        from tests.conftest import DUMMY_ENV

        response = client.get("/api-nokler")
        assert response.status_code == 200
        for key in ("CANVAS_TOKEN", "ELEVENLABS_API_KEY", "SOUNDCLOUD_CLIENT_SECRET"):
            assert DUMMY_ENV[key] not in response.text

    def test_environment_managed_keys_are_locked(self, client: TestClient) -> None:
        body = client.get("/api-nokler").text
        assert "Fra miljøvariabel" in body
        assert "disabled" in body

    def test_settings_keeps_the_choices_and_drops_the_keys(self, client: TestClient) -> None:
        body = client.get("/innstillinger").text
        for gone in (
            "canvas_token",
            "elevenlabs_api_key",
            "soundcloud_client_secret",
            "app_password",
        ):
            assert f'id="{gone}"' not in body
        for kept in ("ai_provider", "voice_stability", "monthly_budget_usd", "soundcloud_sharing"):
            assert f'id="{kept}"' in body
        assert "/api-nokler" in body, "the page must say where the keys went"

    def test_the_api_page_comes_before_innstillinger_in_the_menu(self, client: TestClient) -> None:
        body = client.get("/").text
        assert body.index('href="/api-nokler"') < body.index('href="/innstillinger"')
        assert "🔑" in body

    def test_saving_only_keys_leaves_the_preferences_alone(self, client: TestClient) -> None:
        client.post("/api/settings", json={"monthly_budget_usd": 42, "filename_style": "title"})
        before = client.get("/api/settings/status").json()["preferences"]

        response = client.post("/api/settings", json={"canvas_url": "https://ny.test"})

        assert response.status_code == 200
        assert client.get("/api/settings/status").json()["preferences"] == before

    def test_a_key_can_be_removed_from_env(self, env, monkeypatch) -> None:
        with _client_without(env, monkeypatch, "OPENAI_API_KEY") as client:
            client.post("/api/settings", json={"openai_api_key": "sk-skal-slettes"})
            assert "OPENAI_API_KEY" in (env / ".env").read_text(encoding="utf-8")

            response = client.post("/api/settings", json={"openai_api_key": ""})

            assert response.status_code == 200
            assert "OPENAI_API_KEY" not in (env / ".env").read_text(encoding="utf-8")

    def test_the_voice_button_waits_for_a_saved_key(self, env, monkeypatch) -> None:
        with _client_without(env, monkeypatch, "ELEVENLABS_API_KEY") as client:
            body = client.get("/innstillinger").text
        assert "Legg inn ElevenLabs-nøkkelen" in body
        assert "/api-nokler" in body

    def test_the_dashboard_sends_you_to_the_api_page(self, env, monkeypatch) -> None:
        with _client_without(env, monkeypatch, "ELEVENLABS_API_KEY") as client:
            body = client.get("/").text
        assert "Mangler oppsett" in body
        assert 'href="/api-nokler"' in body

    def test_the_page_is_locked_behind_the_password(self, env, monkeypatch) -> None:
        monkeypatch.setenv("APP_PASSWORD", "hemmelig")
        from app.config import reset_store
        from app.main import create_app

        reset_store(env_path=env / ".env")
        with TestClient(create_app()) as locked:
            assert locked.get("/api-nokler", follow_redirects=False).status_code == 303


class TestUpdateApi:
    """The update page and its endpoints. Git is faked; see tests/test_update_service.py."""

    def test_status_reports_version_git_and_schema(self, client: TestClient, monkeypatch) -> None:
        from tests.test_update_service import fake_git, repo

        fake_git(monkeypatch, repo())

        body = client.get("/api/update/status").json()

        assert body["running"]["version"]
        assert body["running"]["restart_pending"] is False
        assert body["git"]["branch"] == "main"
        assert body["git"]["can_update"] is False
        assert body["db"]["expected_version"] == body["db"]["file_version"]

    def test_check_fetches_and_reports_what_is_new(self, client: TestClient, monkeypatch) -> None:
        from tests.test_update_service import behind_by, fake_git

        calls: list[list[str]] = []
        fake_git(monkeypatch, behind_by(2), calls)

        body = client.post("/api/update/check").json()

        assert body["ok"] is True
        assert body["behind"] == 2
        assert len(body["incoming"]) == 2
        assert any(call[0] == "fetch" for call in calls)

    def test_apply_refuses_a_dirty_tree(self, client: TestClient, monkeypatch) -> None:
        from tests.test_update_service import behind_by, fake_git, ok

        calls: list[list[str]] = []
        fake_git(
            monkeypatch, behind_by(1, {("status", "--porcelain"): ok(" M app/main.py")}), calls
        )

        body = client.post("/api/update/apply").json()

        assert body["ok"] is False
        assert "lokale endringer" in body["message"]
        assert not any("merge" in call for call in calls)

    def test_the_page_shows_the_cards(self, client: TestClient, monkeypatch) -> None:
        from tests.test_update_service import fake_git, repo

        fake_git(monkeypatch, repo())

        text = client.get("/oppdatering").text

        assert "Versjon" in text
        assert "Du har siste versjon" in text
        assert "Etter en oppdatering" in text
        assert "install.bat" in text

    def test_the_page_degrades_without_git(self, client: TestClient, monkeypatch) -> None:
        from app.services import update_service
        from tests.test_update_service import fake_git

        fake_git(monkeypatch, {("rev-parse",): update_service.GitRun(False, "", "", -1)})

        response = client.get("/oppdatering")

        assert response.status_code == 200
        assert "Fant ikke git" in response.text
        assert "Oppdater nå" not in response.text

    def test_the_menu_ends_with_the_update_page(self, client: TestClient) -> None:
        body = client.get("/").text
        assert body.index('href="/oppdatering"') > body.index('href="/innstillinger"')

    def test_the_endpoints_need_the_password(self, env, monkeypatch) -> None:
        monkeypatch.setenv("APP_PASSWORD", "hemmelig")
        from app.config import reset_store
        from app.main import create_app

        reset_store(env_path=env / ".env")
        with TestClient(create_app()) as locked:
            assert locked.get("/api/update/status").status_code == 401
            assert locked.get("/oppdatering", follow_redirects=False).status_code == 303

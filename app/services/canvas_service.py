"""Canvas LMS REST client.

What the application needs: courses, modules with their items, pages and a
single page's body, plus the two writes used by the Canvas player: uploading a
course file and replacing a page's body. Canvas paginates with ``Link`` headers,
which this client follows transparently.

Module items can be quizzes, assignments, files or external links. Those are
deliberately ignored — the app only synthesises Canvas Pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.logging_config import get_logger
from app.services.errors import ConfigurationError, NotFoundError, ServiceError, UpstreamError
from app.services.http import build_client, raise_for_status, with_retries

logger = get_logger("app.services.canvas")

PROVIDER = "canvas"
API_PREFIX = "/api/v1"
PER_PAGE = 100
MAX_PAGES = 100  # hard stop so a broken Link header cannot loop forever

_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

ERROR_MESSAGES = {
    401: "Canvas avviste tokenet (401). Sjekk at Canvas-tokenet er gyldig.",
    403: "Canvas nektet tilgang (403). Tokenet mangler rettigheter til denne ressursen.",
    404: "Fant ikke ressursen i Canvas (404). Sjekk Canvas-URL og emne-ID.",
    429: "Canvas begrenser antall forespørsler (429). Prøver igjen om litt.",
}


def _parse_timestamp(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(slots=True)
class CanvasCourse:
    id: str
    name: str
    course_code: str = ""


@dataclass(slots=True)
class CanvasModuleItem:
    title: str
    type: str
    page_url: str
    position: int
    published: bool = True

    @property
    def is_page(self) -> bool:
        return self.type == "Page" and bool(self.page_url)


@dataclass(slots=True)
class CanvasModule:
    id: str
    name: str
    position: int
    items: list[CanvasModuleItem] = field(default_factory=list)


@dataclass(slots=True)
class CanvasPageSummary:
    page_id: str
    url: str
    title: str
    updated_at: datetime | None = None
    published: bool = True


@dataclass(slots=True)
class CanvasPageBody(CanvasPageSummary):
    body: str = ""


@dataclass(slots=True)
class CanvasFile:
    id: str
    display_name: str = ""
    folder_id: str = ""
    size: int = 0


def _page_body(data: dict[str, Any], page_url: str) -> CanvasPageBody:
    return CanvasPageBody(
        page_id=str(data.get("page_id") or page_url),
        url=data.get("url") or page_url,
        title=data.get("title") or "Uten tittel",
        updated_at=_parse_timestamp(data.get("updated_at")),
        published=bool(data.get("published", True)),
        body=data.get("body") or "",
    )


def _file(data: dict[str, Any]) -> CanvasFile:
    if data.get("id") is None:
        raise UpstreamError("Canvas bekreftet ikke opplastingen av filen.", provider=PROVIDER)
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return CanvasFile(
        id=str(data["id"]),
        display_name=data.get("display_name") or data.get("filename") or "",
        folder_id=str(data.get("folder_id") or ""),
        size=size,
    )


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


class CanvasClient:
    """Thin async wrapper over the Canvas REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        if not base_url:
            raise ConfigurationError("Canvas-URL er ikke satt.", provider=PROVIDER)
        if not token:
            raise ConfigurationError("Canvas-token er ikke satt.", provider=PROVIDER)
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout
        self._client = build_client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> CanvasClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- low level -------------------------------------------------------

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def _call() -> httpx.Response:
            response = await self._client.request(method, url, **kwargs)
            raise_for_status(response, provider=PROVIDER, messages=ERROR_MESSAGES)
            return response

        return await with_retries(_call, provider=PROVIDER, description=f"Canvas {method} {url}")

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return await self._send("GET", url, params=params)

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._get(url, params)
        return response.json()

    async def _get_paginated(
        self, url: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        merged = {"per_page": PER_PAGE, **(params or {})}
        results: list[dict[str, Any]] = []
        next_url: str | None = url
        page_count = 0

        while next_url and page_count < MAX_PAGES:
            response = await self._get(next_url, merged if page_count == 0 else None)
            payload = response.json()
            if isinstance(payload, list):
                results.extend(item for item in payload if isinstance(item, dict))
            next_url = self._next_link(response)
            page_count += 1

        return results

    @staticmethod
    def _next_link(response: httpx.Response) -> str | None:
        link_header = response.headers.get("Link") or response.headers.get("link")
        if not link_header:
            return None
        match = _LINK_NEXT.search(link_header)
        return match.group(1) if match else None

    def _is_canvas_host(self, url: str) -> bool:
        return httpx.URL(url).host == httpx.URL(self.base_url).host

    # -- public API ------------------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
        """Verify the token by asking Canvas who we are."""
        data = await self._get_json(f"{API_PREFIX}/users/self")
        name = data.get("name") or data.get("short_name") or "ukjent bruker"
        logger.info("Canvas connection successful user=%s", name)
        return {"name": name, "id": str(data.get("id", ""))}

    async def list_courses(self) -> list[CanvasCourse]:
        raw = await self._get_paginated(
            f"{API_PREFIX}/courses",
            {"enrollment_state": "active", "state[]": "available", "include[]": "term"},
        )
        courses = [
            CanvasCourse(
                id=str(item["id"]),
                name=item.get("name") or f"Emne {item['id']}",
                course_code=item.get("course_code") or "",
            )
            for item in raw
            if item.get("id") is not None
        ]
        logger.info("Fetched %d courses", len(courses))
        return courses

    async def get_course(self, course_id: str) -> CanvasCourse:
        data = await self._get_json(f"{API_PREFIX}/courses/{course_id}")
        return CanvasCourse(
            id=str(data.get("id", course_id)),
            name=data.get("name") or f"Emne {course_id}",
            course_code=data.get("course_code") or "",
        )

    async def list_modules(self, course_id: str) -> list[CanvasModule]:
        raw = await self._get_paginated(
            f"{API_PREFIX}/courses/{course_id}/modules", {"include[]": "items"}
        )
        modules: list[CanvasModule] = []
        for index, item in enumerate(raw, start=1):
            items = [
                CanvasModuleItem(
                    title=entry.get("title") or "",
                    type=entry.get("type") or "",
                    page_url=entry.get("page_url") or "",
                    position=int(entry.get("position") or 0),
                    published=bool(entry.get("published", True)),
                )
                for entry in (item.get("items") or [])
                if isinstance(entry, dict)
            ]
            modules.append(
                CanvasModule(
                    id=str(item.get("id", index)),
                    name=item.get("name") or f"Modul {index}",
                    position=int(item.get("position") or index),
                    items=items,
                )
            )
        modules.sort(key=lambda m: m.position)
        logger.info("Fetched %d modules", len(modules))
        return modules

    async def list_pages(self, course_id: str) -> list[CanvasPageSummary]:
        raw = await self._get_paginated(f"{API_PREFIX}/courses/{course_id}/pages")
        pages = [
            CanvasPageSummary(
                page_id=str(item.get("page_id") or item.get("url") or ""),
                url=item.get("url") or "",
                title=item.get("title") or "Uten tittel",
                updated_at=_parse_timestamp(item.get("updated_at")),
                published=bool(item.get("published", True)),
            )
            for item in raw
            if item.get("url") or item.get("page_id")
        ]
        logger.info("Fetched %d pages", len(pages))
        return pages

    async def get_page(self, course_id: str, page_url: str) -> CanvasPageBody:
        data = await self._get_json(f"{API_PREFIX}/courses/{course_id}/pages/{page_url}")
        return _page_body(data, page_url)

    # -- writes used by the Canvas player --------------------------------

    async def update_page_body(self, course_id: str, page_url: str, body: str) -> CanvasPageBody:
        """Replace a page's HTML. Canvas keeps the previous version in the page history."""
        response = await self._send(
            "PUT",
            f"{API_PREFIX}/courses/{course_id}/pages/{page_url}",
            json={"wiki_page": {"body": body, "notify_of_update": False}},
        )
        return _page_body(response.json(), page_url)

    async def get_file(self, file_id: str) -> CanvasFile | None:
        """The file, or ``None`` when Canvas no longer has it."""
        try:
            data = await self._get_json(f"{API_PREFIX}/files/{file_id}")
        except NotFoundError:
            return None
        return _file(data)

    async def upload_file(
        self,
        course_id: str,
        *,
        path: Path,
        name: str,
        folder_path: str,
        content_type: str = "audio/mpeg",
    ) -> CanvasFile:
        """Upload a course file with Canvas' documented three-step flow.

        1. Tell Canvas about the file. It answers with an upload URL and parameters.
        2. POST those parameters, then the file last, to the upload URL. The Canvas
           token is not sent there: the URL may belong to a storage service.
        3. Confirm through the redirect or ``Location`` Canvas returns. The token is
           only sent when that address is on the Canvas host itself.

        ``on_duplicate=overwrite`` replaces an earlier upload with the same name in
        the same folder instead of piling up copies.
        """
        content = path.read_bytes()
        ticket = (
            await self._send(
                "POST",
                f"{API_PREFIX}/courses/{course_id}/files",
                data={
                    "name": name,
                    "size": str(len(content)),
                    "content_type": content_type,
                    "parent_folder_path": folder_path,
                    "on_duplicate": "overwrite",
                },
            )
        ).json()
        upload_url = ticket.get("upload_url") if isinstance(ticket, dict) else None
        if not upload_url:
            raise UpstreamError("Canvas ga ingen opplastingsadresse for filen.", provider=PROVIDER)
        params = {str(k): str(v) for k, v in (ticket.get("upload_params") or {}).items()}
        file_param = ticket.get("file_param") or "file"

        async with build_client(
            timeout=self._timeout, transport=self._transport, follow_redirects=False
        ) as uploader:

            async def _upload() -> httpx.Response:
                response = await uploader.post(
                    upload_url, data=params, files={file_param: (name, content, content_type)}
                )
                if not response.is_redirect:
                    raise_for_status(response, provider=PROVIDER, messages=ERROR_MESSAGES)
                return response

            response = await with_retries(
                _upload, provider=PROVIDER, description=f"Canvas opplasting av {name}"
            )

            location = response.headers.get("Location") or ""
            if response.is_success:
                payload = _json_or_empty(response)
                if payload.get("id") is not None:
                    return _file(payload)
                location = payload.get("location") or location
            if not location:
                raise UpstreamError(
                    "Canvas bekreftet ikke opplastingen av filen.", provider=PROVIDER
                )

            confirm_url = str(httpx.URL(self.base_url).join(location))
            if self._is_canvas_host(confirm_url):
                return _file(await self._get_json(confirm_url))

            async def _confirm() -> httpx.Response:
                confirmed = await uploader.get(confirm_url)
                raise_for_status(confirmed, provider=PROVIDER, messages=ERROR_MESSAGES)
                return confirmed

            confirmed = await with_retries(
                _confirm, provider=PROVIDER, description=f"Canvas bekreftelse av {name}"
            )
            return _file(_json_or_empty(confirmed))


async def check_canvas_connection(
    base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Used by the Settings page's "Test Canvas" button."""
    try:
        async with CanvasClient(base_url, token, transport=transport) as client:
            info = await client.test_connection()
    except ServiceError:
        raise
    return {"ok": True, "detail": f"Tilkoblet som {info['name']}."}

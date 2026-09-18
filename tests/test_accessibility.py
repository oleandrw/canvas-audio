"""Accessibility rules that can be checked without a browser.

Three kinds of check: contrast ratios computed from the stylesheet's own tokens,
CSS rules that must exist (or must not), and the rendered HTML parsed the way a
screen reader would read it. What is left — that Tab actually reaches things in a
sensible order, and that a screen reader says something sensible — is in the
manual checklist in README.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

CSS = (Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "app.css").read_text(
    encoding="utf-8"
)
JS = (Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "app.js").read_text(
    encoding="utf-8"
)

#: WCAG 2.1: normal text needs 4.5:1, and a focus indicator 3:1.
TEXT_MINIMUM = 4.5
NON_TEXT_MINIMUM = 3.0

SURFACES = ("bg-base", "bg-surface", "bg-elevated", "bg-hover", "bg-inset")


def token(name: str) -> str:
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert match, f"the token --{name} is gone from app.css"
    return match.group(1)


def _luminance(color: str) -> float:
    parts = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(first: str, second: str) -> float:
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class TestContrast:
    def test_the_helper_agrees_with_the_published_examples(self) -> None:
        """Black on white is 21:1 — if this drifts, the numbers below mean nothing."""
        assert round(contrast("#000000", "#ffffff"), 1) == 21.0
        assert round(contrast("#777777", "#ffffff"), 1) == 4.5

    @pytest.mark.parametrize("name", ["text-muted", "text-secondary"])
    def test_quiet_text_is_still_readable(self, name: str) -> None:
        for surface in SURFACES:
            ratio = contrast(token(name), token(surface))
            assert ratio >= TEXT_MINIMUM, f"--{name} on --{surface} is only {ratio:.2f}:1"

    def test_the_focus_ring_stands_out(self) -> None:
        for surface in SURFACES:
            ratio = contrast(token("brand-light"), token(surface))
            assert ratio >= NON_TEXT_MINIMUM, f"the ring on --{surface} is only {ratio:.2f}:1"


class TestStylesheet:
    def test_the_focus_outline_is_never_removed(self) -> None:
        assert "outline: none" not in CSS, "this is what hid the keyboard focus"

    def test_form_fields_show_keyboard_focus(self) -> None:
        assert "input:focus-visible" in CSS

    def test_motion_can_be_turned_down(self) -> None:
        assert "@media (prefers-reduced-motion: reduce)" in CSS

    def test_a_scrolling_table_has_a_height_to_scroll_within(self) -> None:
        block = CSS[CSS.index(".table-wrap.is-scrollable") :][:200]
        assert "max-height" in block and "overflow: auto" in block

    def test_small_controls_are_big_enough_to_hit(self) -> None:
        assert re.search(r"\.btn-sm \{[^}]*min-height: 3[2-9]px", CSS)
        assert re.search(r'input\[type="checkbox"\] \{[^}]*width: 1[89]px', CSS)
        assert re.search(r"\.page-row input\[type=\"checkbox\"\][^{]*\{[^}]*min-width: 24px", CSS)

    def test_the_mobile_table_is_labelled(self) -> None:
        assert ".table-stack td::before" in CSS
        assert "content: attr(data-label)" in CSS

    def test_the_menu_labels_survive_on_a_narrow_screen(self) -> None:
        narrow = CSS[CSS.index("@media (max-width: 520px)") :][:900]
        assert "clip-path: inset(50%)" in narrow, "the labels must be hidden, not removed"
        assert "display: none" not in narrow.split(".nav a span")[1][:200]


class TestJavaScript:
    """Crude on purpose: these guard against deletion, not against wrong behaviour."""

    def test_errors_are_announced_immediately(self) -> None:
        assert 'setAttribute("role", "alert")' in JS

    def test_an_error_message_does_not_disappear_by_itself(self) -> None:
        assert 'if (kind === "error") return;' in JS

    def test_the_dialog_traps_and_returns_focus(self) -> None:
        assert "function trapFocus" in JS
        assert "function restoreFocus" in JS
        assert "aria-labelledby" in JS


def soup(client: TestClient, url: str) -> BeautifulSoup:
    response = client.get(url)
    assert response.status_code == 200, url
    return BeautifulSoup(response.text, "html.parser")


PAGES = ["/", "/canvas", "/generering", "/publiser-canvas", "/soundcloud", "/innstillinger"]


class TestRenderedPages:
    @pytest.mark.parametrize("url", PAGES)
    def test_every_page_can_be_skipped_into(
        self, client: TestClient, seeded: dict[str, Any], url: str
    ) -> None:
        page = soup(client, url)
        link = page.select_one('a.skip-link[href="#main"]')
        assert link is not None and link.get_text(strip=True)
        assert page.select_one("#main") is not None

    @pytest.mark.parametrize("url", PAGES)
    def test_every_menu_link_has_a_name(
        self, client: TestClient, seeded: dict[str, Any], url: str
    ) -> None:
        page = soup(client, url)
        nav = page.select_one("nav.nav")
        assert nav.get("aria-label")
        links = nav.select("a")
        assert len(links) >= 7
        for link in links:
            assert link.get("aria-label"), f"{link.get('href')} has no name"
        assert len(nav.select('a[aria-current="page"]')) == 1

    def test_the_toast_area_is_announced(self, client: TestClient) -> None:
        page = soup(client, "/logg-inn")
        # The login page has no toast stack; the application pages do.
        page = soup(client, "/")
        stack = page.select_one(".toast-stack")
        assert stack.get("role") == "status"
        assert stack.get("aria-live") == "polite"

    def test_modules_open_with_the_keyboard(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page = soup(client, "/canvas")
        toggles = page.select("button.module-toggle")
        assert toggles, "the module heads must be real buttons"
        for toggle in toggles:
            assert toggle.get("aria-expanded") in ("true", "false")
            assert page.select_one(f"#{toggle['aria-controls']}") is not None

    def test_every_checkbox_says_what_it_selects(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page = soup(client, "/canvas")
        names = [box.get("aria-label") for box in page.select("input.module-check")]
        assert all(names), "a module checkbox without a name is 35 identical checkboxes"
        assert len(set(names)) == len(names), "each module must be told apart"

        module_id = seeded["modules"][0].id
        rows = soup(client, f"/fragments/canvas-sider?modul={module_id}")
        boxes = rows.select("input.page-check")
        assert boxes
        for box in boxes:
            target = box.get("aria-labelledby")
            assert target and rows.select_one(f"#{target}") is not None

    def test_the_generation_table_is_reachable_and_labelled(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page = soup(client, "/generering")
        wrap = page.select_one("#generation-table")
        assert wrap.get("tabindex") == "0"
        assert wrap.get("role") == "region"
        assert wrap.get("aria-label")

    def test_every_cell_is_labelled_for_the_mobile_layout(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page = soup(client, "/generering")
        cells = page.select("table.table-stack tbody td")
        assert cells
        for cell in cells:
            assert cell.get("data-label"), "stacked cells lose their column header"

    def test_decorative_emoji_are_not_read_out(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page = soup(client, "/canvas")
        for element in page.select(".nav-emoji"):
            assert element.get("aria-hidden") == "true"
        for element in page.select(".chevron"):
            assert element.get("aria-hidden") == "true"

    def test_the_download_button_says_what_it_downloads(
        self, client: TestClient, seeded: dict[str, Any]
    ) -> None:
        page = soup(client, "/generering")
        buttons = page.select('a[href*="download=1"]')
        assert buttons, "the fixture must have a page with audio"
        for button in buttons:
            label = button.get("aria-label") or ""
            assert "Last ned" in label and len(label) > len("Last ned")


class TestAssetFreshness:
    """An update that the browser does not fetch looks like an update that did nothing."""

    def test_the_stylesheet_url_carries_a_version(self, client: TestClient) -> None:
        page = soup(client, "/")
        link = page.select_one('link[rel="stylesheet"]')
        assert "?v=" in link["href"]

    def test_the_version_follows_the_files(self) -> None:
        from app.routes.ui import _asset_version

        before = _asset_version()
        css = Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "app.css"
        css.touch()
        assert _asset_version() != before

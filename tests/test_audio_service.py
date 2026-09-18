"""Joining the parts of a long page back into one MP3.

A page too long for one ElevenLabs request is read in several takes. The takes
share voice, model and output format, so their frames belong together — but each
one may arrive with its own ID3 tag, and a tag in the middle of the stream is not
audio.
"""

from __future__ import annotations

from app.services.audio_service import join_mp3

FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 100  # enough to look like an MP3 frame


def id3v2(payload: bytes, *, size: int = 20) -> bytes:
    """An ID3v2 tag of ``size`` bytes, written the way a real one is."""
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + b"\x00" * size + payload


def id3v1(payload: bytes) -> bytes:
    return payload + b"TAG" + b"\x00" * 125


class TestJoiningParts:
    def test_a_single_part_is_returned_untouched(self) -> None:
        part = id3v2(FRAME)
        assert join_mp3([part]) == part

    def test_only_the_first_part_keeps_its_header(self) -> None:
        joined = join_mp3([id3v2(FRAME + b"a"), id3v2(FRAME + b"b"), id3v2(FRAME + b"c")])

        assert joined.startswith(b"ID3")
        assert joined.count(b"ID3") == 1
        assert joined.endswith(FRAME + b"c")

    def test_only_the_last_part_keeps_its_trailer(self) -> None:
        joined = join_mp3([id3v1(FRAME + b"a"), id3v1(FRAME + b"b")])

        assert joined.count(b"TAG") == 1
        assert joined.endswith(b"TAG" + b"\x00" * 125)

    def test_the_audio_itself_is_never_dropped(self) -> None:
        parts = [FRAME + bytes([i]) for i in range(5)]
        assert join_mp3(parts) == b"".join(parts)

    def test_bytes_that_only_look_like_a_tag_are_left_alone(self) -> None:
        """A syncsafe size never has the high bit set, so this is not a tag."""
        impostor = b"ID3\x03\x00\x00\xff\xff\xff\xff" + FRAME
        assert join_mp3([FRAME, impostor]) == FRAME + impostor

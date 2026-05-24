"""Tests for /api/v1/media + /api/v2/media + post_status media_ids."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import MediaAttachment

_AUTH = {"Authorization": "Bearer raw-token-abc"}
_BOB_TOKEN = "bob-token"
_BOB_AUTH = {"Authorization": f"Bearer {_BOB_TOKEN}"}


def _make_png(width: int = 32, height: int = 32, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """Generate a real PNG so PIL can decode it and produce variants."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


_PNG_1x1 = _make_png()  # name kept for compat with existing test bodies


@pytest.fixture(autouse=True)
def _media_root_tmpdir(monkeypatch: pytest.MonkeyPatch):
    """Redirect media storage to a per-test tempdir so uploads don't leak."""
    from app.python.settings import get_settings

    with tempfile.TemporaryDirectory() as d:
        get_settings.cache_clear()
        monkeypatch.setenv("MEDIA_ROOT", d)
        yield d
        get_settings.cache_clear()


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_user"](id_=2, account_id=2, email="bob@example.com"),
                seed_data["make_application"](),
                seed_data["make_token"](id_=1, token="raw-token-abc", resource_owner_id=1),
                seed_data["make_token"](id_=2, token=_BOB_TOKEN, resource_owner_id=2),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_upload_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media",
        files={"file": ("x.png", _PNG_1x1, "image/png")},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_upload_creates_row_and_writes_file(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("hello.png", _PNG_1x1, "image/png")},
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "image"
    assert body["url"].endswith("/hello.png")

    async with session_factory() as s:
        rows = (await s.execute(select(MediaAttachment))).scalars().all()
        assert len(rows) == 1
        assert rows[0].account_id == 1
        assert rows[0].status_id is None
        # PNG is re-encoded to strip metadata, so the byte count won't
        # match the upload exactly — just verify the column tracks the
        # stored original.
        assert rows[0].file_file_size > 0
        # The file landed under the tempdir.
        found = False
        for _root, _dirs, files in os.walk(_media_root_tmpdir):
            if "hello.png" in files:
                found = True
                break
        assert found


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_mime(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("a.zip", b"not an image", "application/zip")},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_upload_with_description(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("a.png", _PNG_1x1, "image/png")},
        data={"description": "alt text here"},
        headers=_AUTH,
    )
    assert response.json()["description"] == "alt text here"


@pytest.mark.asyncio
async def test_show_returns_owner_only(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("a.png", _PNG_1x1, "image/png")},
        headers=_AUTH,
    )
    mid = upload.json()["id"]

    # Owner reads back: 200
    owner = await client.get(f"/api/v1/media/{mid}", headers=_AUTH)
    assert owner.status_code == 200

    # Bob (not the owner): 404
    other = await client.get(f"/api/v1/media/{mid}", headers=_BOB_AUTH)
    assert other.status_code == 404


@pytest.mark.asyncio
async def test_put_updates_description(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("a.png", _PNG_1x1, "image/png")},
        headers=_AUTH,
    )
    mid = upload.json()["id"]

    response = await client.put(
        f"/api/v1/media/{mid}",
        json={"description": "updated alt"},
        headers=_AUTH,
    )
    assert response.json()["description"] == "updated alt"


@pytest.mark.asyncio
async def test_post_status_with_media_ids_attaches(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("photo.png", _PNG_1x1, "image/png")},
        headers=_AUTH,
    )
    mid = int(upload.json()["id"])

    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "with media", "media_ids": [mid]},
        headers=_AUTH,
    )
    assert posted.status_code == 200
    body = posted.json()
    assert len(body["media_attachments"]) == 1
    assert body["media_attachments"][0]["id"] == str(mid)

    async with session_factory() as s:
        row = (
            await s.execute(
                select(MediaAttachment).where(MediaAttachment.id == mid)
            )
        ).scalar_one()
        assert row.status_id == int(body["id"])


@pytest.mark.asyncio
async def test_post_status_rejects_non_owner_media(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Bob uploads; alice tries to attach bob's media to her post."""
    await _seed(session_factory, seed_data)
    bob_upload = await client.post(
        "/api/v1/media",
        files={"file": ("bob.png", _PNG_1x1, "image/png")},
        headers=_BOB_AUTH,
    )
    bob_mid = int(bob_upload.json()["id"])

    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "stealing media", "media_ids": [bob_mid]},
        headers=_AUTH,
    )
    assert posted.status_code == 422


@pytest.mark.asyncio
async def test_post_status_rejects_already_attached_media(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("a.png", _PNG_1x1, "image/png")},
        headers=_AUTH,
    )
    mid = int(upload.json()["id"])

    first = await client.post(
        "/api/v1/statuses",
        json={"status": "first", "media_ids": [mid]},
        headers=_AUTH,
    )
    assert first.status_code == 200

    second = await client.post(
        "/api/v1/statuses",
        json={"status": "second", "media_ids": [mid]},
        headers=_AUTH,
    )
    assert second.status_code == 422


@pytest.mark.asyncio
async def test_post_status_text_only_still_works(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Sanity: posting without media still requires non-empty text."""
    await _seed(session_factory, seed_data)
    blank = await client.post(
        "/api/v1/statuses", json={"status": ""}, headers=_AUTH
    )
    assert blank.status_code == 422

    ok = await client.post(
        "/api/v1/statuses", json={"status": "hi"}, headers=_AUTH
    )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_upload_generates_small_variant_and_meta(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    await _seed(session_factory, seed_data)
    # 800×600 PNG so the small variant must actually shrink.
    big_png = _make_png(width=800, height=600, color=(0, 128, 255))
    response = await client.post(
        "/api/v1/media",
        files={"file": ("big.png", big_png, "image/png")},
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # url -> original, preview_url -> small.
    assert body["url"].endswith("/original/big.png")
    assert body["preview_url"].endswith("/small/big.png")
    assert body["url"] != body["preview_url"]
    # Blurhash populated.
    assert body["blurhash"]
    assert isinstance(body["blurhash"], str)
    # Meta has original + small dimensions; small respects max 400 on the long edge.
    meta = body["meta"]
    assert meta["original"]["width"] == 800
    assert meta["original"]["height"] == 600
    assert meta["small"]["width"] <= 400
    assert meta["small"]["height"] <= 400
    # Both files actually written.
    files_on_disk: list[str] = []
    for _root, _dirs, files in os.walk(_media_root_tmpdir):
        files_on_disk.extend(files)
    assert files_on_disk.count("big.png") == 2


@pytest.mark.asyncio
async def test_status_with_media_carries_preview_and_blurhash(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """When a status response embeds a MediaAttachment, the preview_url +
    blurhash + meta come through to clients."""
    await _seed(session_factory, seed_data)
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("p.png", _make_png(200, 100), "image/png")},
        headers=_AUTH,
    )
    mid = int(upload.json()["id"])
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "look", "media_ids": [mid]},
        headers=_AUTH,
    )
    attachment = posted.json()["media_attachments"][0]
    assert attachment["preview_url"].endswith("/small/p.png")
    assert attachment["url"].endswith("/original/p.png")
    assert attachment["blurhash"]
    assert attachment["meta"]["original"]["width"] == 200


def _make_wav(*, duration_s: float = 1.0, sample_rate: int = 22050) -> bytes:
    """Generate a silent mono 16-bit WAV via the stdlib `wave` module."""
    import wave

    buf = io.BytesIO()
    n_frames = int(duration_s * sample_rate)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


def _make_mp4(*, duration_s: int = 1, size: int = 64, rate: int = 10) -> bytes:
    """Generate a short MP4 via ffmpeg's lavfi testsrc. Skips the whole
    test if ffmpeg is missing on this host."""
    import shutil
    import subprocess
    import tempfile

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        out = f.name
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f", "lavfi",
                "-i", f"testsrc=duration={duration_s}:size={size}x{size}:rate={rate}",
                "-pix_fmt", "yuv420p",
                "-loglevel", "error",
                out,
            ],
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0:
            pytest.skip(f"ffmpeg failed to generate fixture: {proc.stderr!r}")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(out)


def _make_animated_gif(*, frames: int = 4, size: int = 64, duration_ms: int = 100) -> bytes:
    """Multi-frame GIF with per-frame durations populated."""
    from PIL import Image

    palette = [
        Image.new("P", (size, size))
        for _ in range(frames)
    ]
    images: list[Image.Image] = []
    for i in range(frames):
        img = Image.new("RGB", (size, size), (i * 60 % 256, 100, 200))
        images.append(img.convert("P"))
    buf = io.BytesIO()
    images[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=duration_ms,
        loop=0,
    )
    return buf.getvalue()


@pytest.mark.asyncio
async def test_upload_animated_gif_preserves_frames_and_meta(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    """Animated GIF: original keeps its frames; small is a static frame;
    meta.original carries frames/duration/frame_rate."""
    from PIL import Image

    # GIF stores durations as centiseconds, so use a multiple of 10ms.
    gif_bytes = _make_animated_gif(frames=4, duration_ms=100)
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("anim.gif", gif_bytes, "image/gif")},
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    meta = body["meta"]
    assert meta["original"]["frames"] == 4
    assert meta["original"]["duration"] == pytest.approx(0.4, abs=1e-3)
    assert meta["original"]["frame_rate"]  # non-empty string
    # Original on disk still has 4 frames (passthrough — we don't re-encode animated yet).
    saved_original: bytes | None = None
    saved_small: bytes | None = None
    for root, _dirs, files in os.walk(_media_root_tmpdir):
        for name in files:
            if name != "anim.gif":
                continue
            full = os.path.join(root, name)
            with open(full, "rb") as fh:  # noqa: ASYNC230
                blob = fh.read()
            if "/original/" in full:
                saved_original = blob
            elif "/small/" in full:
                saved_small = blob
    assert saved_original is not None and saved_small is not None
    assert Image.open(io.BytesIO(saved_original)).n_frames == 4
    # Small is single-frame (JPEG or PNG, never animated).
    small_img = Image.open(io.BytesIO(saved_small))
    assert getattr(small_img, "is_animated", False) is False


def _make_jpeg_with_exif() -> bytes:
    """JPEG carrying GPS EXIF — the data we explicitly want stripped."""
    import piexif
    from PIL import Image

    img = Image.new("RGB", (64, 64), (200, 100, 50))
    # ((deg, 1), (min, 1), (sec, 1)) — SF: 37° 46' 30" N, 122° 25' 10" W
    exif_dict = {
        "0th": {piexif.ImageIFD.Make: b"Anthropic"},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: ((37, 1), (46, 1), (30, 1)),
            piexif.GPSIFD.GPSLongitudeRef: b"W",
            piexif.GPSIFD.GPSLongitude: ((122, 1), (25, 1), (10, 1)),
        },
    }
    exif_bytes = piexif.dump(exif_dict)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif_bytes, quality=90)
    return buf.getvalue()


def _make_animated_gif_with_comment(*, comment: bytes = b"secret-watermark") -> bytes:
    """4-frame GIF carrying a Comment-extension block — the metadata we
    want stripped on upload. Distinct frame colors so PIL doesn't
    silently de-dupe."""
    from PIL import Image

    frames = [
        Image.new("RGB", (32, 32), (255, 0, 0)),
        Image.new("RGB", (32, 32), (0, 255, 0)),
        Image.new("RGB", (32, 32), (0, 0, 255)),
        Image.new("RGB", (32, 32), (255, 255, 0)),
    ]
    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
        comment=comment,
    )
    return buf.getvalue()


@pytest.mark.asyncio
async def test_upload_animated_gif_strips_comment_metadata(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    """Animated GIF with a Comment-extension block: post-upload the
    on-disk original re-opens without that comment. Frame count + per-
    frame duration survive the re-encode."""
    from PIL import Image

    gif_bytes = _make_animated_gif_with_comment(comment=b"private-watermark")
    # Sanity: input does carry the comment before upload.
    pre = Image.open(io.BytesIO(gif_bytes))
    assert pre.info.get("comment") == b"private-watermark"

    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("anim.gif", gif_bytes, "image/gif")},
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Frame count + animated-meta preserved through the re-encode.
    assert body["meta"]["original"]["frames"] == 4

    # Find the original on disk and re-open it.
    saved: bytes | None = None
    for root, _dirs, files in os.walk(_media_root_tmpdir):
        for name in files:
            full = os.path.join(root, name)
            if name == "anim.gif" and os.path.basename(root) == "original":
                with open(full, "rb") as fh:  # noqa: ASYNC230
                    saved = fh.read()
                break
        if saved:
            break
    assert saved is not None
    post = Image.open(io.BytesIO(saved))
    post.load()
    assert post.is_animated
    assert post.n_frames == 4
    # The Comment-extension block is gone.
    assert post.info.get("comment") is None


@pytest.mark.asyncio
async def test_upload_strips_exif_from_jpeg_original(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    """The original on disk must not carry EXIF — Mastodon's privacy default."""
    from PIL import Image

    pytest.importorskip("piexif")
    jpeg_bytes = _make_jpeg_with_exif()
    # Sanity: input does carry EXIF before upload.
    assert Image.open(io.BytesIO(jpeg_bytes))._getexif() is not None  # type: ignore[attr-defined]

    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("with_gps.jpg", jpeg_bytes, "image/jpeg")},
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text

    saved: bytes | None = None
    for root, _dirs, files in os.walk(_media_root_tmpdir):
        for name in files:
            full = os.path.join(root, name)
            if name == "with_gps.jpg" and "/original/" in full:
                with open(full, "rb") as fh:  # noqa: ASYNC230
                    saved = fh.read()
                break
        if saved is not None:
            break
    assert saved is not None, "original/with_gps.jpg not written"
    # EXIF segment is gone after re-encode.
    assert Image.open(io.BytesIO(saved))._getexif() is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upload_rejects_non_image_bytes_for_image_mime(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Sending bogus bytes with an image MIME type → 422 (Pillow rejects)."""
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("fake.png", b"not really a png", "image/png")},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_upload_mp4_extracts_meta_and_poster(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    """Video upload: type=video, meta has dimensions/duration/frame_rate,
    blurhash + small variant come from the poster frame."""
    mp4_bytes = _make_mp4(duration_s=1, size=64, rate=10)
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("clip.mp4", mp4_bytes, "video/mp4")},
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "video"
    meta = body["meta"]["original"]
    assert meta["width"] == 64
    assert meta["height"] == 64
    assert meta["duration"] == pytest.approx(1.0, abs=0.2)
    assert meta["frame_rate"]
    # Poster frame is what fed the small variant + blurhash.
    assert body["preview_url"].endswith("/small/clip.mp4")
    assert body["blurhash"]
    # Both files (original mp4 + small jpeg poster) landed on disk.
    on_disk: list[str] = []
    for _root, _dirs, files in os.walk(_media_root_tmpdir):
        on_disk.extend(files)
    assert on_disk.count("clip.mp4") == 2


@pytest.mark.asyncio
async def test_upload_rejects_garbage_video_bytes(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Garbage bytes with a video MIME → 422 (ffprobe rejects)."""
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("fake.mp4", b"not a real mp4", "video/mp4")},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_status_with_video_attaches(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Video media_id can be attached to a status, same as an image."""
    mp4_bytes = _make_mp4(duration_s=1, size=64, rate=10)
    await _seed(session_factory, seed_data)
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("clip.mp4", mp4_bytes, "video/mp4")},
        headers=_AUTH,
    )
    mid = int(upload.json()["id"])
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "video post", "media_ids": [mid]},
        headers=_AUTH,
    )
    assert posted.status_code == 200
    attachment = posted.json()["media_attachments"][0]
    assert attachment["type"] == "video"
    assert attachment["preview_url"].endswith("/small/clip.mp4")


@pytest.mark.asyncio
async def test_upload_wav_extracts_duration_and_waveform_preview(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    """Audio upload: type=audio, meta has duration+bitrate, preview_url
    points to a waveform PNG (rendered via ffmpeg showwavespic).
    Blurhash stays null — a waveform's information density is too low
    for a meaningful hash."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe not installed")
    wav_bytes = _make_wav(duration_s=1.0)
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("voice.wav", wav_bytes, "audio/wav")},
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "audio"
    meta = body["meta"]["original"]
    assert meta["duration"] == pytest.approx(1.0, abs=0.05)
    # WAV is uncompressed PCM; ffprobe still reports a bitrate.
    assert meta["bitrate"]
    # Preview = waveform PNG; filename gets the .png suffix so mime
    # sniffing on serve identifies it as image/png.
    assert body["preview_url"].endswith("/small/voice.wav.png")
    assert body["blurhash"] is None
    # small dims match the showwavespic output (720x120).
    assert body["meta"]["small"]["width"] == 720
    assert body["meta"]["small"]["height"] == 120
    # Original .wav and small .png both on disk.
    found_wav = False
    found_png = False
    for root, _dirs, files in os.walk(_media_root_tmpdir):
        bucket = os.path.basename(root)
        for name in files:
            if bucket == "original" and name == "voice.wav":
                found_wav = True
            if bucket == "small" and name == "voice.wav.png":
                found_png = True
    assert found_wav and found_png


@pytest.mark.asyncio
async def test_upload_rejects_garbage_audio_bytes(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Garbage bytes with an audio MIME → 422 (ffprobe finds no stream)."""
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/media",
        files={"file": ("fake.wav", b"definitely not wav", "audio/wav")},
        headers=_AUTH,
    )
    assert response.status_code == 422


def _make_mpeg4_video(*, duration_s: int = 1, size: int = 64, rate: int = 10) -> bytes:
    """Generate a video encoded with the (older) mpeg4 codec, so we can
    verify a transcode actually swapped the codec to h264."""
    import shutil as _sh
    import subprocess
    import tempfile

    if _sh.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        out = f.name
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f", "lavfi",
                "-i", f"testsrc=duration={duration_s}:size={size}x{size}:rate={rate}",
                "-c:v", "mpeg4",
                "-loglevel", "error",
                out,
            ],
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0:
            pytest.skip(f"ffmpeg failed to encode mpeg4 fixture: {proc.stderr!r}")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(out)


def _probe_codec(path: str) -> str:
    """Return the video stream codec_name reported by ffprobe."""
    import json as _json
    import subprocess

    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "json",
            path,
        ],
        capture_output=True,
        timeout=10,
    )
    return _json.loads(proc.stdout)["streams"][0]["codec_name"]


@pytest.mark.asyncio
async def test_upload_video_transcodes_to_h264(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    """Source uses the mpeg4 codec; after the worker runs, the on-disk
    original is H.264 — proof the transcode took effect."""
    mpeg4_bytes = _make_mpeg4_video()
    # Sanity: source really is mpeg4.
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(mpeg4_bytes)
        src_path = f.name
    try:
        assert _probe_codec(src_path) == "mpeg4"
    finally:
        os.unlink(src_path)

    await _seed(session_factory, seed_data)
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("clip.mp4", mpeg4_bytes, "video/mp4")},
        headers=_AUTH,
    )
    assert upload.status_code == 200, upload.text

    # The transcoded original is on disk under the variant tree.
    original_path: str | None = None
    for root, _dirs, files in os.walk(_media_root_tmpdir):
        for name in files:
            full = os.path.join(root, name)
            if name == "clip.mp4" and "/original/" in full:
                original_path = full
                break
        if original_path:
            break
    assert original_path is not None
    assert _probe_codec(original_path) == "h264"


@pytest.mark.asyncio
async def test_v2_upload_returns_202_with_processing_state(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    """v2 contract: write original, queue job, return 202. Body has null
    `url`/`preview_url`/`blurhash`/`meta` until the worker fires."""
    from tests.conftest import FakeEnqueuer

    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v2/media",
        files={"file": ("hello.png", _PNG_1x1, "image/png")},
        headers=_AUTH,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["type"] == "image"
    assert body["url"] is None  # processing != READY → asset_url returns None
    assert body["preview_url"] is None
    assert body["blurhash"] is None
    assert body["meta"] is None

    # Row exists in PROCESSING state.
    async with session_factory() as s:
        rows = (await s.execute(select(MediaAttachment))).scalars().all()
        assert len(rows) == 1
        assert rows[0].processing == 0  # MediaProcessing.IN_PROGRESS

    # The job was enqueued with the new attachment_id.
    fake = client._transport.app.dependency_overrides  # type: ignore[attr-defined]
    from app.python.queue import get_enqueuer
    enqueuer: FakeEnqueuer = fake[get_enqueuer]()  # type: ignore[assignment]
    assert len(enqueuer.calls) == 1
    name, args = enqueuer.calls[0]
    assert name == "prepare_media_attachment"
    assert args == (int(body["id"]),)


@pytest.mark.asyncio
async def test_v2_upload_then_worker_run_flips_to_ready(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    _media_root_tmpdir: str,
) -> None:
    """End-to-end: v2 upload + manually running the worker function
    transitions the row to READY and populates url/preview_url/meta."""
    from app.python.storage import get_storage
    from app.python.workers.media import _run

    await _seed(session_factory, seed_data)
    big_png = _make_png(width=200, height=100, color=(40, 90, 180))
    response = await client.post(
        "/api/v2/media",
        files={"file": ("p.png", big_png, "image/png")},
        headers=_AUTH,
    )
    mid = int(response.json()["id"])

    # Drive the worker inline.
    async with session_factory() as s:
        await _run(s, get_storage(), mid)
        await s.commit()

    # GET reflects READY state.
    polled = await client.get(f"/api/v1/media/{mid}", headers=_AUTH)
    assert polled.status_code == 200
    body = polled.json()
    assert body["url"].endswith("/original/p.png")
    assert body["preview_url"].endswith("/small/p.png")
    assert body["blurhash"]
    assert body["meta"]["original"]["width"] == 200


@pytest.mark.asyncio
async def test_v2_rejects_unsupported_mime_with_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v2/media",
        files={"file": ("a.zip", b"nope", "application/zip")},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_status_media_only_allowed(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """An image-only post (empty text + media_ids) should succeed."""
    await _seed(session_factory, seed_data)
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("p.png", _PNG_1x1, "image/png")},
        headers=_AUTH,
    )
    mid = int(upload.json()["id"])

    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "", "media_ids": [mid]},
        headers=_AUTH,
    )
    assert posted.status_code == 200
    assert len(posted.json()["media_attachments"]) == 1

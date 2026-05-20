"""Media upload: write a file to local storage, create the row, generate
the `small` preview variant + blurhash + dimensional meta for images,
and a poster-frame preview for video.

EXIF and other metadata are stripped from JPEG/PNG originals by
re-encoding through PIL. Mastodon's default is metadata-off — leaving
GPS/camera info on uploaded photos would be a privacy leak. Animated
GIF/WebP pass through untouched for now; a multi-frame re-encoder is
its own slice.

Video uploads run two shells: ffprobe to pull
width/height/duration/frame_rate/bitrate, and ffmpeg to extract a
single poster frame which then goes through the same image pipeline
(small variant + blurhash). Video originals are NOT transcoded yet —
clients receive the uploaded bytes as-is.

What this module does NOT do yet:

  - The `static` variant (animated→still) — needs a multi-frame-aware
    decoder; we skip it for now.
  - Re-encoding animated GIF/WebP to strip their metadata.
  - Video transcoding (re-encode to widely playable H.264/AAC) and
    audio support.
  - Remote-URL caching (federation phase).
  - The async PrepareMediaAttachmentJob — uploads are processed
    synchronously, marked `READY` on completion.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import now_id
from app.python.models import (
    Account,
    MediaAttachment,
    MediaProcessing,
    MediaType,
)
from app.python.storage import get_storage

if TYPE_CHECKING:
    from app.python.queue import Enqueuer


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 80 * 1024 * 1024
MAX_AUDIO_BYTES = 40 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/ogg",
}
ALLOWED_AUDIO_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/flac",
}
SMALL_MAX_DIMENSION = 400  # matches Mastodon's `small` style for images.

Variant = Literal["original", "small"]


class MediaValidationError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _storage_key(attachment_id: int, variant: Variant, file_name: str) -> str:
    """Paperclip-style relative key for one variant of an attachment.

    Splits the id into 4-char chunks so a single directory never holds
    too many entries. Backends (local, S3, Azure) all key off this
    string — they're free to translate it into a path, an object key,
    a blob name, etc.
    """
    bucket = f"{attachment_id:013d}"
    chunks = [bucket[i : i + 4] for i in range(0, len(bucket), 4)]
    return "/".join(["media_attachments", "files", *chunks, variant, file_name])


def _small_variant_file_name(media_type: MediaType, file_name: str) -> str:
    """The small/thumbnail variant's on-disk filename.

    Audio's preview is a waveform PNG, so we append `.png` to the
    original name. That keeps Content-Type sniffing honest (S3 and the
    reverse proxy both mime-detect from extension). Image and video
    small variants keep the source filename — their content-types stay
    image-ish either way.
    """
    if media_type is MediaType.AUDIO:
        return f"{file_name}.png"
    return file_name


def _detect_type(content_type: str) -> MediaType:
    if content_type in ALLOWED_IMAGE_TYPES:
        return MediaType.IMAGE
    if content_type in ALLOWED_VIDEO_TYPES:
        return MediaType.VIDEO
    if content_type in ALLOWED_AUDIO_TYPES:
        return MediaType.AUDIO
    return MediaType.UNKNOWN


WAVEFORM_SIZE = "720x120"


def _generate_waveform(src_path: Path) -> bytes | None:
    """Render a static waveform PNG via ffmpeg's `showwavespic` filter.

    `aformat=channel_layouts=mono` upstream normalizes the input so the
    filter doesn't choke on stereo/multi-channel sources. Returns None
    if ffmpeg can't decode the audio (silent files still render as a
    flat line, so a None means actual failure).
    """
    if shutil.which("ffmpeg") is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as out:
        out_path = Path(out.name)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src_path),
                "-filter_complex",
                f"aformat=channel_layouts=mono,showwavespic=s={WAVEFORM_SIZE}",
                "-frames:v",
                "1",
                "-loglevel",
                "error",
                str(out_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            return None
        return out_path.read_bytes()
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        out_path.unlink(missing_ok=True)


def _process_audio(
    data: bytes,
) -> tuple[bytes, dict[str, object], None, bytes | None, dict[str, int] | None] | None:
    """ffprobe + showwavespic-based audio processing.

    Probes for duration/bitrate and renders a static waveform PNG as
    the `small` preview. Returns None for blurhash because the
    waveform's information density is too low for a meaningful hash.
    """
    if shutil.which("ffprobe") is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as src:
        src.write(data)
        src_path = Path(src.name)
    try:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(src_path),
                ],
                capture_output=True,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            info = json.loads(result.stdout.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None
        audio_stream = next(
            (s for s in info.get("streams", []) if s.get("codec_type") == "audio"),
            None,
        )
        if audio_stream is None:
            return None
        fmt = info.get("format", {}) or {}
        duration_raw = fmt.get("duration") or audio_stream.get("duration")
        try:
            duration = float(duration_raw) if duration_raw is not None else None
        except (TypeError, ValueError):
            duration = None
        bitrate_raw = fmt.get("bit_rate") or audio_stream.get("bit_rate")
        try:
            bitrate = int(bitrate_raw) if bitrate_raw is not None else None
        except (TypeError, ValueError):
            bitrate = None
        meta = {"duration": duration, "bitrate": bitrate}

        # Render the waveform from the same source file the prober just
        # accepted. Failure here is non-fatal — we'd rather serve the
        # audio without a preview than reject the upload.
        waveform_bytes = _generate_waveform(src_path)
    finally:
        src_path.unlink(missing_ok=True)

    if waveform_bytes is None:
        return data, meta, None, None, None  # type: ignore[return-value]
    try:
        from PIL import Image

        with Image.open(io.BytesIO(waveform_bytes)) as wf:
            small_dims = {
                "width": wf.width,
                "height": wf.height,
                "size": f"{wf.width}x{wf.height}",
                "aspect": wf.width / wf.height if wf.height else 0.0,
            }
    except Exception:
        return data, meta, None, None, None  # type: ignore[return-value]
    return data, meta, None, waveform_bytes, small_dims  # type: ignore[return-value]


def _ffprobe_metadata(path: Path) -> dict[str, object] | None:
    """Run ffprobe on a file and return Mastodon-shaped metadata.

    Returns None if ffprobe is missing or the file isn't a parseable
    media container. The caller turns that into a 422.
    """
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    video_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        return None
    fmt = info.get("format", {}) or {}
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    duration_raw = fmt.get("duration") or video_stream.get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None
    bitrate_raw = fmt.get("bit_rate") or video_stream.get("bit_rate")
    try:
        bitrate = int(bitrate_raw) if bitrate_raw is not None else None
    except (TypeError, ValueError):
        bitrate = None
    return {
        "width": width,
        "height": height,
        "size": f"{width}x{height}",
        "aspect": width / height if height else 0.0,
        "frame_rate": video_stream.get("avg_frame_rate") or None,
        "duration": duration,
        "bitrate": bitrate,
    }


def _extract_video_poster(path: Path) -> bytes | None:
    """ffmpeg → 1-frame JPEG bytes from the start of the video."""
    if shutil.which("ffmpeg") is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as out:
        out_path = Path(out.name)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "image2",
                "-loglevel",
                "error",
                str(out_path),
            ],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0 or not out_path.exists():
            return None
        return out_path.read_bytes()
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        out_path.unlink(missing_ok=True)


VIDEO_MAX_WIDTH = 1280  # `-vf scale='min(W,iw)':-2`, even-height kept by -2.
VIDEO_AUDIO_BITRATE = "192k"
VIDEO_CRF = "23"  # libx264 quality target — Mastodon-equivalent.
VIDEO_PRESET = "veryfast"  # Speed/size tradeoff. Worker-bound, not request.


def _transcode_video(src_path: Path) -> bytes | None:
    """Re-encode to H.264/AAC MP4 with faststart and a ≤1280px width.

    Mirrors Mastodon's standard video output: portable codecs, scaled
    down to a sane resolution, moov atom up front so streaming clients
    can begin playback before the whole file arrives.

    Audio is included only when the source has it; ffmpeg's `-c:a aac`
    is harmless on audio-less inputs but the `-map` selection makes the
    intent explicit.
    """
    if shutil.which("ffmpeg") is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as out:
        out_path = Path(out.name)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src_path),
                "-c:v",
                "libx264",
                "-preset",
                VIDEO_PRESET,
                "-crf",
                VIDEO_CRF,
                "-pix_fmt",
                "yuv420p",
                "-vf",
                f"scale='min({VIDEO_MAX_WIDTH},iw)':-2",
                "-c:a",
                "aac",
                "-b:a",
                VIDEO_AUDIO_BITRATE,
                "-movflags",
                "+faststart",
                "-loglevel",
                "error",
                str(out_path),
            ],
            capture_output=True,
            timeout=180,
        )
        if result.returncode != 0 or not out_path.exists():
            return None
        return out_path.read_bytes()
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        out_path.unlink(missing_ok=True)


def _process_video(
    data: bytes,
) -> tuple[bytes, dict[str, object], str, bytes, dict[str, int]] | None:
    """ffprobe + ffmpeg-based video processing.

    Re-encodes the upload to H.264/AAC MP4, then probes the transcoded
    output for metadata and extracts a poster frame for the small
    variant. Returning the transcoded bytes from this function means
    callers transparently store the re-encoded version as `original`
    (the source bytes never hit `original/`).

    Returns None if the bytes aren't a valid video container, ffmpeg is
    missing, or transcoding fails for any reason.
    """
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as src:
        src.write(data)
        src_path = Path(src.name)
    try:
        transcoded = _transcode_video(src_path)
    finally:
        src_path.unlink(missing_ok=True)
    if transcoded is None:
        return None

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tx:
        tx.write(transcoded)
        tx_path = Path(tx.name)
    try:
        meta = _ffprobe_metadata(tx_path)
        if meta is None:
            return None
        poster = _extract_video_poster(tx_path)
        if poster is None:
            return None
    finally:
        tx_path.unlink(missing_ok=True)

    poster_processed = _process_image(poster, target_format="JPEG")
    if poster_processed is None:
        return None
    # Discard the re-encoded poster bytes (they'd shadow the video
    # original); keep the small variant + blurhash derived from them.
    _, _, blurhash_str, small_bytes, small_dims = poster_processed
    return transcoded, meta, blurhash_str, small_bytes, small_dims


def _reencode_animated_gif(img: object) -> bytes | None:
    """Re-encode an open animated GIF to drop comment/XMP/extension
    blocks while preserving frame durations and loop count.

    Conservative: on any PIL encoder hiccup return None and let the
    caller fall back to passing the original bytes through. Animated
    GIFs in the wild have palette quirks PIL handles imperfectly —
    silently dropping privacy metadata is better than corrupting the
    image.
    """
    try:
        from PIL import Image, ImageSequence
    except ImportError:
        return None

    try:
        # PIL's GIF encoder re-emits `image.info['comment']` (and other
        # auxiliary blocks) on save. To actually drop them we build
        # fresh Image instances and paste the pixel data over — the
        # new image's info dict starts empty.
        frames: list[Image.Image] = []
        durations: list[int] = []
        for frame in ImageSequence.Iterator(img):  # type: ignore[arg-type]
            fresh = Image.new(frame.mode, frame.size)
            fresh.paste(frame)
            frames.append(fresh)
            durations.append(int(frame.info.get("duration", 100) or 100))
        if not frames:
            return None
        loop = int(img.info.get("loop", 0) or 0)  # type: ignore[attr-defined]
        buf = io.BytesIO()
        frames[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            optimize=False,
            disposal=2,
        )
        return buf.getvalue()
    except Exception:
        return None


def _process_image(
    data: bytes, *, target_format: str
) -> tuple[bytes, dict[str, int], str, bytes, dict[str, int]] | None:
    """Synchronous image processing. Returns
    `(original_bytes, original_dims, blurhash, small_bytes, small_dims)`
    or None if the bytes don't decode as an image.

    `target_format` is the Pillow format string (`PNG`/`JPEG`/`WEBP`/`GIF`).
    Non-animated JPEG/PNG originals are re-encoded through PIL with no
    metadata kwargs to drop EXIF, IPTC, XMP, and PNG text chunks (the
    privacy-relevant payloads). Animated and exotic formats keep their
    original bytes — the small variant is always re-encoded so previews
    can never leak metadata.
    """
    import blurhash
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None

    is_animated = getattr(img, "is_animated", False)
    if img.format in {"JPEG", "PNG"} and not is_animated:
        clean_buf = io.BytesIO()
        save_kwargs: dict[str, object] = {"format": img.format}
        if img.format == "JPEG":
            save_kwargs["quality"] = 90
            save_kwargs["progressive"] = True
        if img.format == "PNG":
            save_kwargs["optimize"] = True
        img.save(clean_buf, **save_kwargs)  # type: ignore[arg-type]
        data = clean_buf.getvalue()
    elif img.format == "GIF" and is_animated:
        cleaned = _reencode_animated_gif(img)
        if cleaned is not None:
            data = cleaned
            # Re-open the cleaned bytes so the rest of this function
            # walks the new frames (PIL caches some state on the old
            # `img` that the re-encode invalidates).
            img = Image.open(io.BytesIO(data))
            img.load()

    original_dims: dict[str, object] = {
        "width": img.width,
        "height": img.height,
        "size": f"{img.width}x{img.height}",
        "aspect": img.width / img.height if img.height else 0.0,
    }
    if is_animated:
        # Per-frame durations are in ms; sum to total duration in seconds.
        # Some encoders omit the `duration` key on individual frames — fall
        # back to a sane 100ms default that matches most viewers.
        n_frames = getattr(img, "n_frames", 1)
        total_ms = 0
        for i in range(n_frames):
            img.seek(i)
            total_ms += int(img.info.get("duration", 100) or 100)
        img.seek(0)
        duration_s = total_ms / 1000.0
        original_dims["frames"] = n_frames
        original_dims["duration"] = duration_s
        original_dims["frame_rate"] = f"{n_frames}/{int(total_ms / 1000)}" if total_ms >= 1000 else f"{n_frames}/1"

    # Small variant: thumbnail in-place (preserves aspect, caps at the max).
    small = img.copy()
    if small.mode not in ("RGB", "RGBA"):
        small = small.convert("RGB")
    small.thumbnail((SMALL_MAX_DIMENSION, SMALL_MAX_DIMENSION))
    small_buf = io.BytesIO()
    small_fmt = "JPEG" if small.mode == "RGB" else "PNG"
    small.save(small_buf, format=small_fmt, quality=85)
    small_bytes = small_buf.getvalue()
    small_dims = {
        "width": small.width,
        "height": small.height,
        "size": f"{small.width}x{small.height}",
        "aspect": small.width / small.height if small.height else 0.0,
    }

    # Blurhash from a downsampled grid. `blurhash-python` calls Pillow's
    # `Image.getdata()` internally, which is deprecated; suppress that one
    # specific warning rather than letting the strict test filter convert
    # it to a failure.
    import warnings

    blur_src = img.convert("RGB")
    blur_src.thumbnail((64, 64))
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Image.Image.getdata is deprecated.*",
            category=DeprecationWarning,
        )
        hash_str = blurhash.encode(blur_src, x_components=4, y_components=4)

    return data, original_dims, hash_str, small_bytes, small_dims  # type: ignore[return-value]


async def upload_media(
    session: AsyncSession,
    *,
    author: Account,
    file_name: str,
    content_type: str,
    file_obj: IO[bytes],
    description: str | None = None,
) -> MediaAttachment:
    media_type = _detect_type(content_type)
    if media_type is MediaType.UNKNOWN:
        raise MediaValidationError(f"Unsupported media type: {content_type!r}")

    data = await asyncio.to_thread(file_obj.read)
    max_bytes = {
        MediaType.VIDEO: MAX_VIDEO_BYTES,
        MediaType.AUDIO: MAX_AUDIO_BYTES,
    }.get(media_type, MAX_IMAGE_BYTES)
    if len(data) > max_bytes:
        kind = media_type.name.title()
        raise MediaValidationError(f"{kind} too large (max {max_bytes // (1024 * 1024)}MB)")

    if media_type is MediaType.VIDEO:
        processed = await asyncio.to_thread(_process_video, data)
        if processed is None:
            raise MediaValidationError("Video could not be decoded (ffmpeg/ffprobe missing or unsupported codec)")
    elif media_type is MediaType.AUDIO:
        processed = await asyncio.to_thread(_process_audio, data)  # type: ignore[arg-type]
        if processed is None:
            raise MediaValidationError("Audio could not be decoded (ffprobe missing or unsupported codec)")
    else:
        processed = await asyncio.to_thread(_process_image, data, target_format="PNG")  # type: ignore[arg-type]
        if processed is None:
            raise MediaValidationError("File does not appear to be a valid image")
    original_bytes, original_dims, blurhash_str, small_bytes, small_dims = processed

    attachment_id = now_id()
    storage = get_storage()
    await storage.write(_storage_key(attachment_id, "original", file_name), original_bytes)

    # `small` is JPEG/PNG (image/video) or PNG (audio waveform).
    small_file_name: str | None = None
    if small_bytes is not None:
        small_file_name = _small_variant_file_name(media_type, file_name)
        await storage.write(_storage_key(attachment_id, "small", small_file_name), small_bytes)

    file_meta: dict[str, object] = {
        # `original_dims` already carries frame_rate/duration when the
        # source is animated; static images leave them absent so clients
        # can distinguish.
        "original": original_dims,
    }
    if small_dims is not None:
        file_meta["small"] = small_dims

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = MediaAttachment(
        id=attachment_id,
        account_id=author.id,
        status_id=None,
        scheduled_status_id=None,
        type=media_type.value,
        processing=MediaProcessing.READY.value,
        file_file_name=file_name,
        file_content_type=content_type,
        file_file_size=len(original_bytes),
        file_meta=file_meta,
        file_updated_at=now,
        thumbnail_file_name=(small_file_name if small_file_name and small_file_name != file_name else None),
        thumbnail_content_type="image/png" if media_type is MediaType.AUDIO and small_bytes else None,
        thumbnail_file_size=len(small_bytes) if small_bytes is not None else None,
        remote_url="",
        thumbnail_remote_url=None,
        description=description,
        blurhash=blurhash_str,
        shortcode=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def upload_media_async(
    session: AsyncSession,
    *,
    author: Account,
    file_name: str,
    content_type: str,
    file_obj: IO[bytes],
    description: str | None = None,
    enqueuer: Enqueuer,
) -> MediaAttachment:
    """v2 contract: write original, insert PROCESSING row, enqueue the
    job. Returns immediately; clients poll `/api/v1/media/{id}` for the
    READY state.

    Size + MIME validation still happens up-front so obvious garbage
    gets a 422 instead of a 202-then-FAILED. Anything deeper (decoder
    rejection, oversize after a sneaky content-type) becomes a row
    transition to FAILED in the job.
    """
    media_type = _detect_type(content_type)
    if media_type is MediaType.UNKNOWN:
        raise MediaValidationError(f"Unsupported media type: {content_type!r}")

    data = await asyncio.to_thread(file_obj.read)
    max_bytes = {
        MediaType.VIDEO: MAX_VIDEO_BYTES,
        MediaType.AUDIO: MAX_AUDIO_BYTES,
    }.get(media_type, MAX_IMAGE_BYTES)
    if len(data) > max_bytes:
        kind = media_type.name.title()
        raise MediaValidationError(f"{kind} too large (max {max_bytes // (1024 * 1024)}MB)")

    attachment_id = now_id()
    storage = get_storage()
    await storage.write(_storage_key(attachment_id, "original", file_name), data)

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = MediaAttachment(
        id=attachment_id,
        account_id=author.id,
        status_id=None,
        scheduled_status_id=None,
        type=media_type.value,
        processing=MediaProcessing.IN_PROGRESS.value,
        file_file_name=file_name,
        file_content_type=content_type,
        file_file_size=len(data),
        file_meta=None,
        file_updated_at=now,
        thumbnail_file_name=None,
        thumbnail_content_type=None,
        thumbnail_file_size=None,
        remote_url="",
        thumbnail_remote_url=None,
        description=description,
        blurhash=None,
        shortcode=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()

    # Enqueue *after* commit so the worker can find the row by id.
    await enqueuer.enqueue("prepare_media_attachment", attachment_id)
    return row


async def update_media(
    session: AsyncSession,
    *,
    author: Account,
    attachment: MediaAttachment,
    description: str | None,
) -> MediaAttachment:
    if attachment.account_id != author.id:
        raise MediaValidationError("not your attachment")
    if description is not None:
        attachment.description = description
    attachment.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)
    await session.commit()
    return attachment


def asset_url(attachment: MediaAttachment, variant: Variant = "original") -> str | None:
    """Public URL for one variant of the attachment.

    Local files served via the reverse proxy at `/system/...`. Returns
    None for not-yet-ready uploads so clients poll. When the requested
    `small` variant doesn't exist on this row (audio uploads, legacy
    pre-pipeline rows), return None rather than aliasing `original` —
    a client asking for a preview wants a still image, not the source
    audio/video bytes back as a "preview".
    """
    if attachment.processing != MediaProcessing.READY.value:
        return None
    if attachment.remote_url:
        return attachment.remote_url
    if not attachment.file_file_name:
        return None
    if variant == "small" and not (attachment.file_meta or {}).get("small"):
        return None
    name = attachment.file_file_name
    if variant == "small" and attachment.thumbnail_file_name:
        name = attachment.thumbnail_file_name
    return get_storage().url(_storage_key(attachment.id, variant, name))

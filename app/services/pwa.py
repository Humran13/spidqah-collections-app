"""PWA settings, logo handling and icon rendering.

Settings live in the existing ``app_settings`` key/value table, so no schema
change is needed. The logo itself is stored on the persistent upload volume
(``UPLOAD_DIR``), never in the git repository. Because the uploaded file is
decoded and re-encoded here, the bytes served to browsers are always a PNG
we generated - a crafted upload cannot smuggle non-image content through.

Icons are rendered on demand from the stored master and cached in memory,
keyed by the settings version, so a logo or name change is picked up as soon
as the version bumps (URLs carry ``?v=<version>``).
"""
import hashlib
import io
import os
import secrets
from functools import lru_cache

from flask import current_app
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from app.services.settings import get_setting, set_setting

APP_NAME_KEY = "pwa_app_name"
SHORT_NAME_KEY = "pwa_short_name"
VERSION_KEY = "pwa_version"
LOGO_SHA_KEY = "pwa_logo_sha256"

DEFAULT_APP_NAME = "SPIDQAH Collections"
DEFAULT_SHORT_NAME = "SPIDQAH"
THEME_COLOR = "#1f4e3d"
BACKGROUND_COLOR = "#ffffff"

MAX_LOGO_BYTES = 2 * 1024 * 1024
MIN_LOGO_SIDE = 128
MAX_LOGO_SIDE = 4000
MAX_LOGO_PIXELS = 4000 * 4000
MASTER_SIDE = 1024
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}

# Only these generated icon names are ever served. No user-supplied path
# segment reaches the filesystem.
ICON_SPECS = {
    "icon-192.png": (192, False),
    "icon-512.png": (512, False),
    "icon-maskable-192.png": (192, True),
    "icon-maskable-512.png": (512, True),
    "apple-touch-icon.png": (180, False),
    "favicon-32.png": (32, False),
}


class LogoError(ValueError):
    """The uploaded file is not an acceptable logo. Message is safe to show."""


def upload_root() -> str:
    return current_app.config["UPLOAD_DIR"]


def _logo_path() -> str:
    return os.path.join(upload_root(), "pwa", "logo-master.png")


def get_pwa_settings() -> dict:
    version = get_setting(VERSION_KEY, "1")
    has_logo = os.path.isfile(_logo_path())
    return {
        "app_name": get_setting(APP_NAME_KEY, DEFAULT_APP_NAME),
        "short_name": get_setting(SHORT_NAME_KEY, DEFAULT_SHORT_NAME),
        "version": version,
        "has_logo": has_logo,
    }


def _bump_version(user_id=None):
    set_setting(VERSION_KEY, secrets.token_hex(6), user_id=user_id)


def validate_logo_bytes(data: bytes) -> Image.Image:
    """Verify real image content and return a normalized RGBA master.

    Rejects: oversized files, non-image content, formats other than
    PNG/JPEG/WEBP (so SVG, GIF, HTML, scripts etc. never pass), decompression
    bombs, and dimensions outside the supported range. The filename and the
    browser-supplied MIME type are deliberately not consulted.
    """
    if not data:
        raise LogoError("The logo file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise LogoError("The logo is larger than 2 MB. Please upload a smaller image.")

    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format
        width, height = probe.size
        probe.verify()  # structural check; invalidates `probe`
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise LogoError("That file is not a valid image. Upload a PNG, JPEG or WEBP logo.")

    if fmt not in ALLOWED_FORMATS:
        raise LogoError("Unsupported image type. Upload a PNG, JPEG or WEBP logo.")
    if min(width, height) < MIN_LOGO_SIDE or max(width, height) > MAX_LOGO_SIDE:
        raise LogoError(
            f"Logo dimensions must be between {MIN_LOGO_SIDE} and {MAX_LOGO_SIDE} pixels on each side."
        )
    if width * height > MAX_LOGO_PIXELS:
        raise LogoError("The logo has too many pixels. Please upload a smaller image.")
    if max(width, height) / min(width, height) > 4:
        raise LogoError("The logo is too narrow or too wide. Use an image no more than 4:1 in either direction.")

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise LogoError("That image could not be read. Try exporting it again as PNG.")

    img = ImageOps.exif_transpose(img).convert("RGBA")
    img.thumbnail((MASTER_SIDE, MASTER_SIDE), Image.Resampling.LANCZOS)
    return img


def encode_master(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def store_logo(master_png: bytes, user_id=None):
    """Atomically replace the stored logo master and bump the PWA version."""
    path = _logo_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(master_png)
    os.replace(tmp, path)
    set_setting(LOGO_SHA_KEY, hashlib.sha256(master_png).hexdigest(), user_id=user_id)
    _bump_version(user_id)


def remove_logo(user_id=None):
    path = _logo_path()
    if os.path.isfile(path):
        os.remove(path)
    set_setting(LOGO_SHA_KEY, "", user_id=user_id)
    _bump_version(user_id)


def bump_version(user_id=None):
    """Public hook: call after any PWA name/short-name change."""
    _bump_version(user_id)


def _default_master(short_name: str) -> Image.Image:
    side = 512
    img = Image.new("RGBA", (side, side), THEME_COLOR)
    letter = (short_name or DEFAULT_SHORT_NAME).strip()[:1].upper() or "S"
    try:
        font = ImageFont.load_default(size=int(side * 0.6))
    except TypeError:  # very old Pillow without sized default font
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(img)
    left, top, right, bottom = draw.textbbox((0, 0), letter, font=font)
    draw.text(((side - (right - left)) / 2 - left, (side - (bottom - top)) / 2 - top), letter, font=font, fill="white")
    return img


def _compose(master: Image.Image, size: int, maskable: bool) -> Image.Image:
    # Maskable icons keep the logo inside the 80% central safe zone so that
    # Android launchers can crop them into circles/squircles safely.
    fraction = 0.6 if maskable else 0.82
    area = max(1, int(round(size * fraction)))
    fitted = ImageOps.contain(master, (area, area), Image.Resampling.LANCZOS)  # keeps aspect ratio
    canvas = Image.new("RGBA", (size, size), BACKGROUND_COLOR)
    offset = ((size - fitted.width) // 2, (size - fitted.height) // 2)
    canvas.alpha_composite(fitted, dest=offset)
    return canvas


@lru_cache(maxsize=48)
def _render_cached(version: str, short_name: str, has_logo: bool, name: str) -> bytes:
    if has_logo:
        with open(_logo_path(), "rb") as fh:
            master = Image.open(io.BytesIO(fh.read())).convert("RGBA")
    else:
        master = _default_master(short_name)

    size, maskable = ICON_SPECS[name]
    icon = _compose(master, size, maskable)
    if name == "apple-touch-icon.png":
        # iOS ignores transparency in touch icons; flatten onto white.
        icon = icon.convert("RGB")
    buf = io.BytesIO()
    icon.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_icon(name: str) -> bytes:
    if name not in ICON_SPECS:
        raise KeyError(name)
    settings = get_pwa_settings()
    return _render_cached(settings["version"], settings["short_name"], settings["has_logo"], name)


def settings_snapshot() -> dict:
    s = get_pwa_settings()
    return {"app_name": s["app_name"], "short_name": s["short_name"], "has_logo": s["has_logo"]}

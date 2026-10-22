"""Image pipeline: decode (incl. HEIC), EXIF-rotate, blur-fill to the frame size, encode JPEG."""

import io

import pillow_heif
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

pillow_heif.register_heif_opener()

# Modern phone sensors (50–200 MP) trip Pillow's default decompression-bomb guard.
Image.MAX_IMAGE_PIXELS = 250_000_000

ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "HEIF"}

BLUR_RADIUS = 40
BACKGROUND_BRIGHTNESS = 0.7
ASPECT_TOLERANCE = 0.01


class ProcessingError(Exception):
    pass


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (0, 0, 0))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def load_image(data: bytes, size: tuple[int, int]) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as exc:
        raise ProcessingError("Not a recognised image file") from exc
    if img.format not in ALLOWED_FORMATS:
        raise ProcessingError(f"Unsupported format: {img.format}")

    # Let the JPEG decoder downscale by a power of two while staying at least frame-sized
    # on both axes (a square request so a later 90° rotation still has enough pixels).
    if img.format in ("JPEG", "MPO"):
        edge = max(size)
        img.draft("RGB", (edge, edge))

    try:
        img = ImageOps.exif_transpose(img)
        return _to_rgb(img)
    except Exception as exc:
        raise ProcessingError("Could not decode image") from exc


def center_crop(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(img, size, Image.Resampling.LANCZOS)


def blur_fill(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Fit the whole photo on the canvas; fill the leftover space with a blurred, darkened copy."""
    frame_ratio = size[0] / size[1]
    img_ratio = img.width / img.height
    if abs(img_ratio - frame_ratio) / frame_ratio < ASPECT_TOLERANCE:
        return center_crop(img, size)

    background = ImageOps.fit(img, size, Image.Resampling.BILINEAR)
    background = background.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS))
    background = ImageEnhance.Brightness(background).enhance(BACKGROUND_BRIGHTNESS)

    foreground = ImageOps.contain(img, size, Image.Resampling.LANCZOS)
    offset = ((size[0] - foreground.width) // 2, (size[1] - foreground.height) // 2)
    background.paste(foreground, offset)
    return background


def render(data: bytes, width: int, height: int, quality: int = 88) -> bytes:
    """Return an optimised, EXIF-free JPEG of exactly width x height."""
    size = (width, height)
    out = blur_fill(load_image(data, size), size)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()

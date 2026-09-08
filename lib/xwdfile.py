"""Read the raw window dumps `xwd` writes.

`xwd` hands over the server's bytes with a 100-byte header in front of them
and does no encoding, which is why it is worth a reader of our own: a grab
loop that has to keep up with a moving screen cannot afford a PNG encode per
frame, and a trim pass that throws most of the frames away should not pay to
decode the ones it discards at full size.
"""
from __future__ import annotations

import struct
from PIL import Image

# The fields of the version 7 header we care about, by index into the run of
# big-endian uint32s that opens the file.
_H = ("header_size file_version pixmap_format pixmap_depth pixmap_width "
      "pixmap_height xoffset byte_order bitmap_unit bitmap_bit_order "
      "bitmap_pad bits_per_pixel bytes_per_line visual_class red_mask "
      "green_mask blue_mask bits_per_rgb colormap_entries ncolors "
      "window_width window_height").split()


class XwdError(Exception):
    """the dump is not one we can read; the caller should shell out instead"""


def header(buf: bytes) -> dict:
    if len(buf) < 100:
        raise XwdError("short file")
    vals = struct.unpack(">22I", buf[:88])
    h = dict(zip(_H, vals))
    if h["file_version"] != 7:
        raise XwdError(f"xwd version {h['file_version']}, expected 7")
    if h["pixmap_format"] != 2:
        raise XwdError(f"pixmap format {h['pixmap_format']}, expected ZPixmap")
    if h["bits_per_pixel"] not in (24, 32):
        raise XwdError(f"{h['bits_per_pixel']} bits per pixel")
    return h


def _raw_mode(h: dict) -> str:
    """how PIL should read one of this dump's pixels.

    A 32-bit pixel on a little-endian server arrives with its bytes in the
    opposite order to the mask that names them, so the masks alone do not say
    which way round the channels are -- the byte order does.
    """
    msb = h["byte_order"] == 1
    if h["bits_per_pixel"] == 32:
        return "XRGB" if msb else "BGRX"
    return "RGB" if msb else "BGR"


def load(path: str) -> Image.Image:
    """the dump at `path` as an RGB image"""
    with open(path, "rb") as fh:
        buf = fh.read()
    h = header(buf)
    off = h["header_size"] + h["ncolors"] * 12
    w, hgt, stride = h["pixmap_width"], h["pixmap_height"], h["bytes_per_line"]
    if len(buf) - off < stride * hgt:
        raise XwdError("pixel data is short")
    return Image.frombuffer("RGB", (w, hgt), buf[off:off + stride * hgt],
                            "raw", _raw_mode(h), stride, 1)


def thumb(path: str, size=(240, 120)) -> Image.Image:
    """a small grayscale of the dump, for comparing one frame against another.

    Nothing downstream of a diff wants the pixels, only the size of the
    change, and a quarter-megapixel comparison costs more than the grab did.
    """
    return load(path).convert("L").resize(size, Image.BILINEAR)

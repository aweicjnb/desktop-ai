from dataclasses import dataclass
import ctypes as ct
from ctypes import wintypes as w
from pathlib import Path
import struct
import zlib


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CF_BITMAP, CF_HDROP = 2, 15

user = ct.WinDLL("user32", use_last_error=True)
kernel = ct.WinDLL("kernel32", use_last_error=True)
gdi = ct.WinDLL("gdi32", use_last_error=True)
shell = ct.WinDLL("shell32", use_last_error=True)

user.RegisterClipboardFormatW.argtypes = (w.LPCWSTR,)
user.RegisterClipboardFormatW.restype = w.UINT
user.IsClipboardFormatAvailable.argtypes = (w.UINT,)
user.OpenClipboard.argtypes = (w.HWND,)
user.CloseClipboard.argtypes = ()
user.GetClipboardData.argtypes = (w.UINT,)
user.GetClipboardData.restype = w.HANDLE
user.GetDC.argtypes = (w.HWND,)
user.GetDC.restype = w.HDC
user.ReleaseDC.argtypes = (w.HWND, w.HDC)
kernel.GlobalSize.argtypes = (w.HANDLE,)
kernel.GlobalSize.restype = ct.c_size_t
kernel.GlobalLock.argtypes = (w.HANDLE,)
kernel.GlobalLock.restype = ct.c_void_p
kernel.GlobalUnlock.argtypes = (w.HANDLE,)
gdi.GetDIBits.argtypes = (w.HDC, w.HBITMAP, w.UINT, w.UINT, ct.c_void_p, ct.c_void_p, w.UINT)
shell.DragQueryFileW.argtypes = (w.HANDLE, w.UINT, w.LPWSTR, w.UINT)
shell.DragQueryFileW.restype = w.UINT


class ClipboardImageError(Exception):
    pass


@dataclass(frozen=True)
class ClipboardImage:
    png: bytes
    width: int
    height: int


def check_dimensions(width, height):
    if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= MAX_PIXELS):
        raise ClipboardImageError("图片分辨率过大，请缩小或裁剪后重试。")


def read_png(data):
    if len(data) > MAX_IMAGE_BYTES:
        raise ClipboardImageError("单张图片最多 8 MiB，请缩小后重试。")
    if len(data) < 33 or not data.startswith(PNG_SIGNATURE + b"\x00\x00\x00\rIHDR"):
        raise ClipboardImageError("无法读取这张 PNG 图片。")
    width, height = struct.unpack_from(">II", data, 16)
    check_dimensions(width, height)
    offset = len(PNG_SIGNATURE)
    while offset + 12 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        offset += 12 + length
        if kind == b"IEND" and length == 0 and offset <= len(data):
            return ClipboardImage(data[:offset], width, height)
    raise ClipboardImageError("剪贴板中的 PNG 图片不完整。")


def clipboard_png(handle):
    size = kernel.GlobalSize(handle)
    if size > MAX_IMAGE_BYTES:
        raise ClipboardImageError("单张图片最多 8 MiB，请缩小后重试。")
    pointer = kernel.GlobalLock(handle)
    if not pointer:
        raise ClipboardImageError("无法读取剪贴板图片。")
    try:
        return ct.string_at(pointer, size)
    finally:
        kernel.GlobalUnlock(handle)


def clipboard_png_path(handle):
    if shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0) != 1:
        return None
    size = shell.DragQueryFileW(handle, 0, None, 0) + 1
    path = ct.create_unicode_buffer(size)
    if shell.DragQueryFileW(handle, 0, path, size) and path.value.lower().endswith(".png"):
        return Path(path.value)
    return None


def bitmap_pixels(handle, hwnd):
    dc = user.GetDC(hwnd)
    if not dc:
        raise ClipboardImageError("无法读取剪贴板位图。")
    try:
        # A compatible bitmap is first queried for its dimensions, then read as top-down BGRX.
        info = ct.create_string_buffer(124)
        struct.pack_into("<I", info, 0, 40)
        if not gdi.GetDIBits(dc, handle, 0, 0, None, info, 0):
            raise ClipboardImageError("无法读取剪贴板位图。")
        width, height = struct.unpack_from("<ii", info, 4)
        height = abs(height)
        check_dimensions(width, height)
        struct.pack_into("<IiiHHIIiiII", info, 0, 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ct.create_string_buffer(width * height * 4)
        if gdi.GetDIBits(dc, handle, 0, height, pixels, info, 0) != height:
            raise ClipboardImageError("无法读取剪贴板位图。")
        return pixels, width, height
    finally:
        user.ReleaseDC(hwnd, dc)


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(data, zlib.crc32(kind)))


def bitmap_to_png(pixels, width, height):
    source = memoryview(pixels).cast("B")
    stride = width * 4
    scanline = bytearray(1 + width * 3)
    compressor = zlib.compressobj(3)
    compressed = bytearray()
    for offset in range(0, stride * height, stride):
        row = source[offset:offset + stride]
        scanline[1::3], scanline[2::3], scanline[3::3] = bytes(row[2::4]), bytes(row[1::4]), bytes(row[0::4])
        compressed.extend(compressor.compress(scanline))
        if len(compressed) > MAX_IMAGE_BYTES:
            raise ClipboardImageError("单张图片最多 8 MiB，请缩小后重试。")
    compressed.extend(compressor.flush())
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return read_png(PNG_SIGNATURE + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", compressed) + png_chunk(b"IEND", b""))


def read_clipboard_image(hwnd):
    png_format = user.RegisterClipboardFormatW("PNG")
    formats = [number for number in (png_format, CF_BITMAP, CF_HDROP)
               if number and user.IsClipboardFormatAvailable(number)]
    if not formats:
        return None
    if not user.OpenClipboard(hwnd):
        raise ClipboardImageError("剪贴板暂时被占用，请再粘贴一次。")
    data = pixels = path = None
    try:
        for number in formats:
            handle = user.GetClipboardData(number)
            if not handle:
                continue
            if number == png_format:
                data = clipboard_png(handle)
                break
            if number == CF_BITMAP:
                pixels = bitmap_pixels(handle, hwnd)
                break
            path = clipboard_png_path(handle)
    finally:
        user.CloseClipboard()
    if data is not None:
        return read_png(data)
    if pixels is not None:
        return bitmap_to_png(*pixels)
    if path is not None:
        try:
            with path.open("rb") as image_file:
                return read_png(image_file.read(MAX_IMAGE_BYTES + 1))
        except OSError:
            raise ClipboardImageError("无法读取复制的 PNG 文件。") from None
    return None

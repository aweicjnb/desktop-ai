"""Decode bounded image data off the Tk thread; create Tk images on demand."""
from dataclasses import dataclass
import importlib.util
import io
from pathlib import Path
import sys
import threading


PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / '.runtime/images'
MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 20_000_000
_lock = threading.Lock()
_pillow = None


class ImageLoadError(Exception):
    pass


def pillow():
    global _pillow
    with _lock:
        if _pillow is None:
            if not (RUNTIME / 'version.txt').is_file() or not (RUNTIME / 'PIL/Image.py').is_file() or not list((RUNTIME / 'PIL').glob('_imaging.cp313-*.pyd')):
                spec = importlib.util.spec_from_file_location('setup_image_runtime', PROJECT / 'scripts/setup_image_runtime.py')
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.prepare()
            if str(RUNTIME) not in sys.path:
                sys.path.insert(0, str(RUNTIME))
            from PIL import Image, ImageOps, ImageTk
            Image.MAX_IMAGE_PIXELS = MAX_PIXELS
            _pillow = Image, ImageOps, ImageTk
        return _pillow


@dataclass(repr=False)
class LoadedImage:
    raw: bytes
    image: object
    width: int
    height: int
    extension: str


def decode_image(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ImageLoadError('图片过大或内容为空，请在网页查看。')
    try:
        Image, ImageOps, _ = pillow()
        with Image.open(io.BytesIO(raw), formats=['PNG','JPEG','WEBP','GIF','BMP','AVIF']) as source:
            width, height = source.size
            if not (0 < width <= 16384 and 0 < height <= 16384 and width*height <= MAX_PIXELS):
                raise ImageLoadError('图片分辨率过大，请在网页查看。')
            extension = {'JPEG':'.jpg','PNG':'.png','WEBP':'.webp','GIF':'.gif','BMP':'.bmp','AVIF':'.avif'}.get(source.format,'.png')
            source.load()
            image = ImageOps.exif_transpose(source).convert('RGBA' if source.mode in ('RGBA','P','LA') else 'RGB')
            width, height = image.size
            image.thumbnail((1600,1200), Image.Resampling.LANCZOS)
            return LoadedImage(raw, image, width, height, extension)
    except ImageLoadError:
        raise
    except Exception:
        raise ImageLoadError('这张图片暂时无法读取，请重试或在网页查看。') from None


def photo_image(loaded, width, height, master):
    Image, _, ImageTk = pillow()
    image = loaded.image.copy()
    image.thumbnail((max(1,width),max(1,height)), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(image, master=master)

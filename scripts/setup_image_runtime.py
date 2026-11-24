"""Prepare the pinned Windows image decoder without changing system Python."""
from hashlib import sha256
import io
from pathlib import Path
import sys
from urllib.request import urlopen
from zipfile import ZipFile


PROJECT = Path(__file__).resolve().parents[1]
DESTINATION = PROJECT / '.runtime/images'
VERSION = '13.0.0'
WHEEL_URL = 'https://files.pythonhosted.org/packages/d7/ed/1b16fb66a37e1b9e41370b93b2c614c7da761a5b2a41dc333f7af4639962/pillow-13.0.0-cp313-cp313-win_amd64.whl'
WHEEL_SHA256 = '9dda0788a9932776a75d8bcc020076edc308ce2508b51f6739a45e3158b3fc42'


def prepare():
    if sys.version_info[:2] != (3, 13) or sys.maxsize < 2**32:
        raise RuntimeError('The bundled image decoder requires Python 3.13 x64.')
    marker = DESTINATION / 'version.txt'
    if marker.is_file() and marker.read_text(encoding='ascii') == VERSION+'\n'+WHEEL_SHA256+'\n' and (DESTINATION / 'PIL/Image.py').is_file() and list((DESTINATION / 'PIL').glob('_imaging.cp313-*.pyd')):
        return DESTINATION
    with urlopen(WHEEL_URL, timeout=40) as response:
        data = response.read(12 * 1024 * 1024)
    if sha256(data).hexdigest() != WHEEL_SHA256:
        raise RuntimeError('Image decoder wheel checksum mismatch.')
    DESTINATION.mkdir(parents=True, exist_ok=True)
    with ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if name.endswith('/'):
                continue
            parts = Path(name).parts
            if '..' in parts:
                raise RuntimeError('Unexpected wheel path.')
            if parts[0] == 'PIL':
                target = DESTINATION / name
            elif len(parts) > 2 and parts[1] == 'licenses':
                target = DESTINATION / 'licenses' / Path(*parts[2:])
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    (DESTINATION / 'version.txt').write_text(VERSION+'\n'+WHEEL_SHA256+'\n', encoding='ascii')
    return DESTINATION


if __name__ == '__main__':
    print(prepare())

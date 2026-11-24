"""Prepare the private local runtime from checksum-verified public packages."""
import base64
from hashlib import sha256, sha512
import io
import json
from pathlib import Path
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZipFile


PROJECT = Path(__file__).resolve().parents[1]
DESTINATION = PROJECT / '.runtime/chatgpt'
NODE_VERSION = '24.22.3'
NODE_ZIP_SHA256 = 'e612af99f626ab415ea225af6bce5c971feb448cb5cae250bb1c3fa50c5f7d9f'
MODULE_URL = 'https://chatgpt.com/cdn/assets/646436.8f73ec6517.js'


def download(url):
    with urlopen(url, timeout=60) as response:
        return response.read(80 * 1024 * 1024)


def prepare():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    if not (DESTINATION / 'node.exe').is_file():
        data = download(f'https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip')
        if sha256(data).hexdigest() != NODE_ZIP_SHA256:
            raise RuntimeError('Node archive checksum mismatch')
        with ZipFile(io.BytesIO(data)) as archive:
            for name in ('node.exe', 'LICENSE'):
                (DESTINATION / name).write_bytes(archive.read(f'node-v{NODE_VERSION}-win-x64/{name}'))
    for name in ('package.json', 'package-lock.json'):
        source = PROJECT / 'assets/chatgpt-runtime' / name
        if source.is_file():
            (DESTINATION / name).write_bytes(source.read_bytes())
    if not (DESTINATION / 'node_modules/jsdom/lib/api.js').is_file():
        # npm is a build dependency, not part of the shipped runtime.
        with TemporaryDirectory(prefix='npm-build-', dir=DESTINATION) as temporary:
            directory = Path(temporary).resolve()
            assert directory.is_relative_to(DESTINATION.resolve())
            metadata = json.loads(download('https://registry.npmjs.org/npm/12.2.3'))
            data = download(metadata['dist']['tarball'])
            expected = metadata['dist']['integrity'].split('-', 1)
            if expected[0] != 'sha512' or base64.b64encode(sha512(data).digest()).decode() != expected[1]:
                raise RuntimeError('npm package checksum mismatch')
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
                archive.extractall(directory, filter='data')
            method = 'ci' if (DESTINATION / 'package-lock.json').is_file() else 'install'
            subprocess.run([str(DESTINATION / 'node.exe'), str(directory / 'package/bin/npm-cli.js'),
                            method, '--ignore-scripts', '--no-audit', '--no-fund'], cwd=DESTINATION,
                           check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if not (DESTINATION / 'client-module.js').is_file():
        source = download(MODULE_URL).decode('utf-8')
        start = source.index('lXEa:(function') + len('lXEa:')
        end = source.index('}),ydaf:', start) + 2
        module = source[start:end]
        # Do not accept a changed webpage module without another verification.
        expected = (PROJECT / 'assets/chatgpt-runtime/module.sha256').read_text().strip()
        if sha256(module.encode()).hexdigest() != expected:
            raise RuntimeError('ChatGPT client module has changed; update and verify the integration')
        (DESTINATION / 'client-module.js').write_text(module, encoding='utf-8')
    return DESTINATION


if __name__ == '__main__':
    print(prepare())

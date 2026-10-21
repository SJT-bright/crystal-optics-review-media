"""Publish exact original media, using the SHA-256 inventory as the boundary.

Manual GitHub Actions maintenance job. This never creates a downloadable archive.
The online H5 fetches only a selected image, adjacent rotation frames or video.
"""
from pathlib import Path, PurePosixPath
import concurrent.futures
import hashlib
import http.cookiejar
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://crystal-optics-review.app.workbuddy.host/'
QUERY = '?sharecode=A64ZUFQANV'
TAG = 'v1.0.0'
MANIFEST = json.loads((ROOT / 'media-manifest.json').read_text())
LOCAL = threading.local()
RATE_LOCK = threading.Lock()
LAST_REQUEST = 0.0
STOP = threading.Event()


def pace():
    global LAST_REQUEST
    with RATE_LOCK:
        delay = 0.125 - (time.monotonic() - LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        LAST_REQUEST = time.monotonic()


def digest(file):
    h = hashlib.sha256()
    with file.open('rb') as stream:
        for block in iter(lambda: stream.read(256 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def valid(name):
    file = ROOT / name
    return file.is_file() and file.stat().st_size == MANIFEST[name]['bytes'] and digest(file) == MANIFEST[name]['sha256']


def opener():
    if not hasattr(LOCAL, 'opener'):
        jar = http.cookiejar.CookieJar()
        LOCAL.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with LOCAL.opener.open(BASE + QUERY, timeout=45) as response:
            if response.status != 200:
                raise RuntimeError('Source website unavailable')
    return LOCAL.opener


def fetch(name):
    if STOP.is_set():
        raise RuntimeError('Media mirror stopped')
    if valid(name):
        return name
    dest = ROOT / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + '.transfer-tmp')
    failures = 0
    for attempt in range(180):
        if STOP.is_set():
            raise RuntimeError('Media mirror stopped')
        try:
            h = hashlib.sha256()
            size = 0
            url = BASE + urllib.parse.quote(name, safe='/') + QUERY
            pace()
            with opener().open(url, timeout=120) as response, temporary.open('wb') as stream:
                if response.status != 200:
                    raise RuntimeError('Unexpected HTTP status')
                for block in iter(lambda: response.read(256 * 1024), b''):
                    stream.write(block)
                    h.update(block)
                    size += len(block)
                    if size > MANIFEST[name]['bytes']:
                        raise RuntimeError('Unexpected media length')
            if size != MANIFEST[name]['bytes'] or h.hexdigest() != MANIFEST[name]['sha256']:
                raise RuntimeError('Original media SHA-256 mismatch')
            temporary.replace(dest)
            return name
        except urllib.error.HTTPError as error:
            temporary.unlink(missing_ok=True)
            # The producer is uploading originals in the same order. Wait for a
            # registered file that has not arrived yet; never alter gateway access.
            if error.code == 404 and attempt < 179:
                STOP.wait(5)
                continue
            failures += 1
            if error.code in (401, 403) or failures >= 4 or attempt == 179:
                STOP.set()
                raise RuntimeError('Source HTTP ' + str(error.code) + ' for ' + name) from None
            STOP.wait(2 ** failures)
        except Exception:
            temporary.unlink(missing_ok=True)
            failures += 1
            if failures >= 4 or attempt == 179:
                STOP.set()
                raise RuntimeError('Cannot verify ' + name) from None
            STOP.wait(2 ** failures)


def git(*args, **kwargs):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, **kwargs)


def publish(names, sequence):
    git('add', '--pathspec-from-file=-', '--pathspec-file-nul', input=b''.join(name.encode() + b'\0' for name in names))
    changed = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=ROOT).returncode
    if changed:
        git('commit', '-m', f'Publish SHA-256 verified original media batch {sequence}')
        git('push', 'origin', 'HEAD:main')


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('Run this maintenance job in GitHub Actions.')
    for name, meta in MANIFEST.items():
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or p.parts[0] not in ('img', 'video', 'quiz_assets') or not isinstance(meta['bytes'], int) or len(meta['sha256']) != 64:
            raise SystemExit('Invalid media inventory')
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    priority = lambda n: (0 if n.startswith('img/seq/863_0/') else 1 if n.startswith('img/hand/') else 2 if n.startswith('img/seq/') else 3, n)
    names = sorted(MANIFEST, key=priority)
    verified = 0
    sequence = 0
    while names:
        batch, size = [], 0
        while names and (size < 160_000_000 or not batch):
            name = names.pop(0)
            batch.append(name)
            size += MANIFEST[name]['bytes']
        sequence += 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for _ in pool.map(fetch, batch):
                verified += 1
        publish(batch, sequence)
        print(json.dumps({'verified': verified, 'total': len(MANIFEST), 'batch': sequence}), flush=True)
    # Recheck the entire working tree, rather than trusting individual receipts.
    bad = [name for name in MANIFEST if not valid(name)]
    if bad:
        raise SystemExit('Final integrity verification failed: ' + str(len(bad)))
    tag = subprocess.run(['git', 'ls-remote', '--tags', 'origin', 'refs/tags/' + TAG], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    if tag:
        raise SystemExit('Release tag already exists; inspect it before publishing a new tag.')
    git('tag', '-a', TAG, '-m', '10,992 original media files; exact bytes and SHA-256 verified')
    git('push', 'origin', TAG)
    print(json.dumps({'complete': True, 'files': len(MANIFEST), 'bytes': sum(m['bytes'] for m in MANIFEST.values()), 'tag': TAG}), flush=True)


if __name__ == '__main__':
    main()

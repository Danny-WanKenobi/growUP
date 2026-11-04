"""Bounded private photo cache. Photos never authenticate property claims or reuse rights."""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import warnings
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo

from app.workflow import digest_payload
from app.workflow_db import WorkflowError

MAX_IMAGES = 10
MAX_INPUT_BYTES = 8_000_000
MAX_OUTPUT_BYTES = 1_000_000
MAX_PIXELS = 24_000_000


def root():
    configured = os.getenv('GROWUP_MEDIA_DIR', '')
    database = os.getenv('DATABASE_NAME', '')
    if not configured or not Path(configured).is_absolute() or not re.fullmatch(r'growup_[a-z0-9_]+', database):
        raise WorkflowError('absolute private media directory and explicit Grow Up database required')
    return Path(configured).resolve() / database


def _write_once(path, body):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(path.parent).free < 100_000_000 + len(body):
        raise WorkflowError('private media store has insufficient free space')
    # Exclusive creation never overwrites another retained object. Incomplete writes
    # fail hash validation on replay rather than silently replacing audit material.
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(body)
            handle.flush(); os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_bytes() != body:
            raise WorkflowError('retained media integrity failure')


def compress_photo(body):
    from PIL import Image, ImageOps, UnidentifiedImageError
    if not isinstance(body, bytes) or not body or len(body) > MAX_INPUT_BYTES:
        raise WorkflowError('photo input exceeds byte limit')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
                    raise WorkflowError('single-frame JPEG, PNG or WebP required')
                if image.width * image.height > MAX_PIXELS:
                    raise WorkflowError('photo exceeds decoded pixel limit')
                image = ImageOps.exif_transpose(image)
                image.thumbnail((1600, 1600))
                # A fresh RGB image strips EXIF/GPS, comments and source metadata.
                clean = Image.new('RGB', image.size, 'white')
                if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                    rgba = image.convert('RGBA'); clean.paste(rgba, mask=rgba.getchannel('A'))
                else:
                    clean.paste(image.convert('RGB'))
                output = BytesIO(); clean.save(output, 'WEBP', quality=78, method=6)
                compressed = output.getvalue()
                if len(compressed) > MAX_OUTPUT_BYTES:
                    raise WorkflowError('compressed photo exceeds output limit')
                return compressed, clean.size
    except WorkflowError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise WorkflowError('invalid or unsafe raster image') from exc


def retain_photo(body, *, page_url, image_url, retrieved_at):
    from urllib.parse import urlsplit
    for value in (page_url, image_url):
        parsed=urlsplit(value)
        if (len(value)>4096 or parsed.scheme not in ('http','https') or not parsed.hostname
                or parsed.username or parsed.password or any(ord(c)<32 for c in value)):
            raise WorkflowError('credential-free source/image URL required')
    compressed, (width, height) = compress_photo(body)
    digest = sha256(compressed).hexdigest()
    _write_once(root() / 'images' / (digest + '.webp'), compressed)
    return {'asset_id': digest, 'sha256': digest, 'format': 'webp', 'bytes': len(compressed),
            'input_sha256': sha256(body).hexdigest(), 'input_bytes': len(body),
            'width': width, 'height': height, 'page_url': page_url, 'image_url': image_url,
            'retrieved_at': retrieved_at, 'kind': 'source_photo', 'cached_by': 'gabriel',
            'property_identity': 'unverified_source_association', 'reuse_permission': 'unknown',
            'outreach_approved': False, 'metadata_stripped': True}


def read_photo(entry):
    digest = entry.get('sha256', '')
    if not re.fullmatch('[0-9a-f]{64}', digest) or entry.get('asset_id') != digest:
        raise WorkflowError('invalid media content address')
    path = root() / 'images' / (digest + '.webp')
    if path.stat().st_size > MAX_OUTPUT_BYTES:
        raise WorkflowError('retained photo exceeds limit')
    body = path.read_bytes()
    if sha256(body).hexdigest() != digest:
        raise WorkflowError('retained photo digest mismatch')
    return body


def archive_photos(entries):
    """Compress a bounded, shared-folder selection; retain real byte statistics."""
    if len(entries) > MAX_IMAGES:
        raise WorkflowError('dossier image count exceeds limit')
    manifest = {'schema': 'dossier-photos-1', 'images': entries,
                'property_claims_authenticated': False, 'publication_rights_established': False}
    buffer = BytesIO(); source_bytes = 0
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        files = {'manifest.json': json.dumps(manifest, sort_keys=True).encode()}
        for entry in entries:
            content = read_photo(entry)
            name='images/' + entry['sha256'] + '.webp'
            if name not in files:source_bytes += len(content)
            files[name] = content
        for name, content in sorted(files.items()):
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)); info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)
    body = buffer.getvalue(); digest = sha256(body).hexdigest()
    _write_once(root() / 'archives' / (digest + '.zip'), body)
    return {**manifest, 'archive_sha256': digest, 'archive_bytes': len(body),
            'compressed_image_bytes': source_bytes, 'additional_zip_savings_bytes': source_bytes - len(body)}


def collect_search_images(run_id, results, *, config=None, transport=None, resolver=None):
    """Opt-in additional GETs, each claimed/charged durably before transmission.

    No gallery crawl, redirects, retries, inferred rights or fabricated association.
    Unknown/uncertain attempts never retransmit automatically, including after crash.
    """
    if os.getenv('GABRIEL_IMAGE_CACHE_ENABLED', '0') != '1':
        return
    root()  # Validate isolated storage before reserving any attempt.
    existing=next(p for p in (root(),*root().parents) if p.exists())
    if shutil.disk_usage(existing).free < 120_000_000:
        raise WorkflowError('image acquisition paused for low disk space')
    from urllib.parse import urlsplit
    from app.agents.gabriel.sources.http_policy import fetch_bytes, HttpPolicyError
    from app.properties_db import get_connection
    from app.workflow_db import (get_run_in_transaction, get_record_in_transaction,
                                 put_record_in_transaction, add_usage_in_transaction)
    hosts = {host.strip().lower() for host in os.getenv('GABRIEL_IMAGE_HOSTS', '').split(',')} - {''}
    for result in results:
        url = result.get('image_url')
        if not url or urlsplit(url).hostname not in hosts:
            continue
        key = 'photo:' + digest_payload({'page': result['url'], 'image': url})
        with get_connection(config) as conn:
            run = get_run_in_transaction(conn, run_id, for_update=True)
            if run is None or run.status == 'stopped' or os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP') == '1':
                return
            record = get_record_in_transaction(conn, run_id, 'research_artifact', key)
            if record:
                if record.digest != digest_payload(record.payload):
                    raise WorkflowError('image claim integrity failure')
                if record.payload.get('image'):
                    result['images'] = [record.payload['image']]
                continue
            if int((run.usage or {}).get('image_attempts', 0)) >= MAX_IMAGES:
                return
            claim = {'status': 'started', 'page_url': result['url'], 'image_url': url}
            put_record_in_transaction(conn, run_id, kind='research_artifact', record_key=key,
                                     version=1, payload=claim, digest=digest_payload(claim))
            add_usage_in_transaction(conn, run_id, 'image_attempts', 1); conn.commit()
        try:
            body = fetch_bytes(url, timeout=12, max_bytes=MAX_INPUT_BYTES, transport=transport,
                               resolver=resolver, name='property image')
            image = retain_photo(body, page_url=result['url'], image_url=url,
                                 retrieved_at=datetime.now(timezone.utc).isoformat())
            outcome = {**claim, 'status': 'cached', 'image': image}; result['images'] = [image]
        except (WorkflowError, HttpPolicyError, OSError):
            outcome = {**claim, 'status': 'unavailable', 'image': None}
        with get_connection(config) as conn:
            put_record_in_transaction(conn, run_id, kind='research_artifact', record_key=key,
                                     version=2, payload=outcome, digest=digest_payload(outcome))
            conn.commit()

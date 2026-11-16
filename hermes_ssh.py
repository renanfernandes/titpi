"""Bounded SSH/SFTP image transport; no credentials are copied to the server."""

import base64
import ipaddress
import json
import logging
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
import uuid

from bird_id import DEFAULT_PROMPT, encode_image, _parse_remote_result

log = logging.getLogger('titpi.identification')
DEFAULT_SETTINGS = {
    'host': '10.0.0.3', 'user': 'renanfernandes',
    'key_path': '/home/titpi/.ssh/id_ed25519_macmini',
    'remote_temp_dir': '/tmp',
    'hermes_path': '/Users/renanfernandes/.local/bin/hermes',
    'connect_timeout': 10, 'timeout': 60, 'max_attempts': 1, 'min_confidence': 0.7,
    'notify_all': True,
}


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) != set(DEFAULT_SETTINGS):
        raise ValueError('Invalid Hermes SSH fields')
    if type(settings['notify_all']) is not bool:
        raise ValueError('Notify every result must be a boolean')
    for field in ('key_path', 'remote_temp_dir', 'hermes_path'):
        value = settings[field]
        if not isinstance(value, str) or not value.startswith('/') or len(value) > 512 or any(ord(char) < 32 for char in value):
            raise ValueError('Paths must be absolute and contain no control characters')
        if '..' in value.split('/'):
            raise ValueError('Parent path traversal is not allowed')
    if not isinstance(settings['user'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', settings['user']):
        raise ValueError('Invalid SSH user')
    host = settings['host']
    if not isinstance(host, str) or len(host) > 253:
        raise ValueError('Invalid SSH host')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in host.split('.')):
            raise ValueError('Invalid SSH host')
    for field, ceiling in (('connect_timeout', 30), ('timeout', 300), ('max_attempts', 3)):
        if type(settings[field]) is not int or not 1 <= settings[field] <= ceiling:
            raise ValueError('Invalid timeout or attempts')
    if settings['connect_timeout'] > settings['timeout']:
        raise ValueError('Connection timeout exceeds total timeout')
    if type(settings['min_confidence']) not in (int, float) or not 0.01 <= settings['min_confidence'] <= 1:
        raise ValueError('Invalid remote confidence threshold')
    return settings


_CLEANUP = """
import os, re, shutil, sys
path = sys.argv[1]
if not re.fullmatch(r'titpi-hermes-[a-f0-9]{32}', os.path.basename(path)) or os.path.islink(path):
    raise SystemExit(1)
if os.path.isdir(path):
    shutil.rmtree(path)
"""

_RUN_REMOTE = """
import json, os, re, shutil, signal, subprocess, sys, tempfile
data = json.load(sys.stdin)
folder = data['folder']
if not re.fullmatch(r'titpi-hermes-[a-f0-9]{32}', os.path.basename(folder)) or os.path.islink(folder):
    raise SystemExit(1)
process = None
def stop(signum, frame):
    raise SystemExit(124)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGHUP, stop)
try:
    command = [data['hermes_path'], 'chat', '--cli', '--oneshot', '--format', 'stream-json',
               '--toolsets', 'bot_room', '--ignore-rules', '--max-turns', '1',
               '--run-budget', str(data['budget']), '--source', 'tool', '--in', folder,
               '--image', os.path.join(folder, 'visitor.jpg'), '--query-file', '-']
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                   start_new_session=True)
        try:
            process.communicate(data['prompt'].encode(), timeout=data['budget'])
        except subprocess.TimeoutExpired:
            raise SystemExit(124)
        if process.returncode != 0:
            raise SystemExit(1)
        output.seek(0)
        body = output.read(262145)
        if len(body) > 262144:
            raise SystemExit(1)
        result = None
        model = None
        for line in body.decode().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or str(event.get('type', '')).startswith('tool'):
                raise SystemExit(1)
            if event.get('type') == 'system':
                model = event.get('model')
            if event.get('type') == 'result':
                if result is not None or event.get('exit_code') != 0 or not isinstance(event.get('text'), str):
                    raise SystemExit(1)
                result = event['text']
        if result is None:
            raise SystemExit(1)
        print(json.dumps({'text': result, 'model': model}))
finally:
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    shutil.rmtree(folder)
"""


def _options(settings):
    return ['-F', '/dev/null', '-i', settings['key_path'],
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no',
            '-o', 'ClearAllForwardings=yes', '-o', 'ServerAliveInterval=5',
            '-o', 'ServerAliveCountMax=2', '-o', f"ConnectTimeout={settings['connect_timeout']}"]


def _ssh(settings, command, timeout, *, input=None):
    return subprocess.run(['ssh', *_options(settings), '-l', settings['user'], settings['host'], command],
                          input=input, capture_output=True, timeout=timeout, check=True)


def identify_remote(image_path, settings, prompt):
    settings = validate_settings({**DEFAULT_SETTINGS, **settings})
    key = os.stat(settings['key_path'])
    if not stat.S_ISREG(key.st_mode) or key.st_mode & 0o077 or key.st_uid != os.getuid():
        raise ValueError('SSH private key must be a private file owned by this user')
    deadline = time.monotonic() + settings['timeout']
    if prompt != DEFAULT_PROMPT:
        prompt += '\n' + DEFAULT_PROMPT
    with tempfile.TemporaryDirectory(prefix='titpi-hermes-') as local:
        photo = os.path.join(local, 'visitor.jpg')
        with open(photo, 'wb') as output:
            output.write(base64.b64decode(encode_image(image_path)))
        os.chmod(photo, 0o600)
        for attempt in range(settings['max_attempts']):
            folder = settings['remote_temp_dir'].rstrip('/') + '/titpi-hermes-' + uuid.uuid4().hex
            created = False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 5:
                    raise TimeoutError('Hermes SSH budget exhausted')
                _ssh(settings, 'mkdir -m 700 ' + shlex.quote(folder), remaining)
                created = True
                host = settings['host']
                destination = f"{settings['user']}@{'[' + host + ']' if ':' in host else host}:{folder}/visitor.jpg"
                subprocess.run(['scp', '-q', *_options(settings), photo, destination], capture_output=True,
                               timeout=max(1, deadline - time.monotonic()), check=True)
                budget = int(deadline - time.monotonic() - 3)
                if budget < 1:
                    raise TimeoutError('Hermes SSH budget exhausted')
                command = 'exec python3 -c ' + shlex.quote(_RUN_REMOTE)
                response = _ssh(settings, command, budget + 3, input=json.dumps({
                    'folder': folder, 'hermes_path': settings['hermes_path'],
                    'prompt': prompt, 'budget': budget,
                }).encode())
                if len(response.stdout) > 65536:
                    raise ValueError('Hermes result too large')
                data = json.loads(response.stdout)
                result = _parse_remote_result(data['text'], 'hermes_ssh')
                log.info('Hermes SSH result=%s model=%s', result['status'],
                         re.sub(r'[^A-Za-z0-9_./:-]', '', str(data.get('model', 'unknown')))[:128])
                return result
            except (subprocess.SubprocessError, OSError, TimeoutError) as error:
                log.warning('Hermes SSH attempt=%d failed (%s)', attempt + 1, type(error).__name__)
                if attempt + 1 == settings['max_attempts']:
                    raise
            finally:
                if created:
                    try:
                        command = 'python3 -c ' + shlex.quote(_CLEANUP) + ' ' + shlex.quote(folder)
                        _ssh(settings, command, max(1, min(3, deadline - time.monotonic())))
                    except (subprocess.SubprocessError, OSError):
                        log.warning('Remote temporary cleanup unavailable; only this job directory may remain.')
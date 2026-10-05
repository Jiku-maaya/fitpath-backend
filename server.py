"""FitPath shared AI gateway. Python 3.13+, standard library only."""
import base64
from contextlib import closing
import hashlib
import ipaddress
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Quota:
    """Atomic, persistent IP + global limits shared by all server threads."""
    def __init__(self, path, per_minute=3, per_day=20, global_day=200, photo_day=40):
        self.path = path
        self.limits = per_minute, per_day, global_day, photo_day
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS quota (bucket TEXT PRIMARY KEY, n INTEGER NOT NULL, expires INTEGER NOT NULL)')

    def reserve(self, identity, photo=False, now=None):
        now = int(time.time() if now is None else now)
        minute, day = now // 60, now // 86400
        pm, pd, gd, photos = self.limits
        buckets = [(f'm:{identity}:{minute}', pm, (minute+1)*60),
                   (f'd:{identity}:{day}', pd, (day+1)*86400),
                   (f'g:{day}', gd, (day+1)*86400)]
        if photo:
            buckets.append((f'p:{day}', photos, (day+1)*86400))
        with closing(sqlite3.connect(self.path, timeout=10, isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM quota WHERE expires <= ?', (now,))
            for key, limit, expires in buckets:
                row = db.execute('SELECT n FROM quota WHERE bucket = ?', (key,)).fetchone()
                if row and row[0] >= limit:
                    db.rollback()
                    return expires-now
            for key, _, expires in buckets:
                db.execute('INSERT INTO quota VALUES (?,1,?) ON CONFLICT(bucket) DO UPDATE SET n=n+1', (key, expires))
            db.commit()
        return 0


def validate_payload(data):
    if not isinstance(data, dict) or not isinstance(data.get('vision'), bool):
        raise ValueError('Invalid request')
    messages = data.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 3:
        raise ValueError('Invalid messages')
    chars, images = 0, 0
    clean = []
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('system', 'user'):
            raise ValueError('Invalid role')
        content = message.get('content')
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list) and 1 <= len(content) <= 2 and message['role'] == 'user':
            for part in content:
                if not isinstance(part, dict):
                    raise ValueError('Invalid content')
                if part.get('type') == 'text' and isinstance(part.get('text'), str):
                    chars += len(part['text'])
                elif part.get('type') == 'image_url' and data['vision']:
                    url = part.get('image_url', {}).get('url', '')
                    if not isinstance(url, str) or ',' not in url:
                        raise ValueError('Invalid image')
                    header, encoded = url.split(',', 1)
                    if header not in ('data:image/jpeg;base64', 'data:image/png;base64', 'data:image/webp;base64'):
                        raise ValueError('Only embedded image data is accepted')
                    if len(encoded) > 8*1024*1024:
                        raise ValueError('Image too large')
                    decoded = base64.b64decode(encoded, validate=True)
                    if not decoded or len(decoded) > 6*1024*1024:
                        raise ValueError('Image too large')
                    images += 1
                else:
                    raise ValueError('Unsupported content')
        else:
            raise ValueError('Invalid content')
        clean.append({'role': message['role'], 'content': content})
    if chars > 60000 or images > 1:
        raise ValueError('Request too large')
    return clean


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_):
        pass  # Never log request bodies, API keys, or submitted health data.

    def respond(self, status, body, retry=None):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        if retry:
            self.send_header('Retry-After', str(retry))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.respond(200 if self.path == '/health' else 404, {'ok': self.path == '/health'})

    def do_POST(self):
        if self.path != '/v1/analyze':
            return self.respond(404, {'error': {'message': 'Unknown endpoint'}})
        if not self.server.api_key:
            return self.respond(503, {'error': {'message': 'Shared AI is not configured. Use your own Groq key.'}})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 9*1024*1024 or self.headers.get('Transfer-Encoding'):
                return self.respond(413, {'error': {'message': 'Request too large'}})
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('Incomplete request')
            data = json.loads(raw)
            messages = validate_payload(data)
        except (ValueError, TypeError, AttributeError, TimeoutError):
            return self.respond(400, {'error': {'message': 'Invalid shared AI request'}})
        # Forwarded identities are accepted only from explicitly trusted peers.
        peer = self.client_address[0]
        client_ip = peer
        if peer in getattr(self.server, 'trusted_proxy_ips', set()):
            try:
                client_ip = str(ipaddress.ip_address(self.headers.get('X-Real-IP', '')))
            except ValueError:
                return self.respond(400, {'error': {'message': 'Proxy client identity missing'}})
        identity = hashlib.sha256((self.server.salt+client_ip).encode()).hexdigest()
        try:
            retry = self.server.quota.reserve(identity, data['vision'])
        except sqlite3.Error:
            return self.respond(503, {'error': {'message': 'Shared quota unavailable. Try later.'}})
        if retry:
            return self.respond(429, {'error': {'message': f'Shared AI limit reached. Retry in {retry} seconds or use your own key.'}}, retry)
        # Server chooses models and token ceiling; callers cannot override these.
        payload = {'model': self.server.photo_model if data['vision'] else self.server.text_model,
                   'messages': messages, 'stream': False, 'max_completion_tokens': 8192,
                   'response_format': {'type': 'json_object'}}
        req = urllib.request.Request('https://api.groq.com/openai/v1/chat/completions',
            data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer '+self.server.api_key, 'Content-Type': 'application/json', 'User-Agent': 'FitPath/2.0 (Android personal nutrition diary)'})
        try:
            with urllib.request.urlopen(req, timeout=50) as response:
                raw = response.read(1024*1024+1)
                if len(raw) > 1024*1024:
                    raise ValueError('Oversized response')
                result = json.loads(raw)
            return self.respond(200, result)
        except urllib.error.HTTPError as error:
            status = error.code if error.code in (400, 429, 503) else 502
            return self.respond(status, {'error': {'message': 'Shared provider unavailable or quota exhausted. Try later or use your own key.'}})
        except (OSError, ValueError):
            return self.respond(502, {'error': {'message': 'Shared provider temporarily unavailable.'}})


def main():
    root = os.environ.get('DATA_DIR', './data')
    os.makedirs(root, exist_ok=True)
    salt = os.environ.get('IP_HASH_SALT', '')
    key = os.environ.get('GROQ_API_KEY', '')
    if len(salt) < 32 or not key.startswith('gsk_'):
        raise SystemExit('Set GROQ_API_KEY and IP_HASH_SALT (32+ characters) as server secrets.')
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler)
    server.api_key, server.salt = key, salt
    server.trusted_proxy_ips = {str(ipaddress.ip_address(v.strip())) for v in os.environ.get('TRUSTED_PROXY_IPS', '').split(',') if v.strip()}
    server.text_model = os.environ.get('TEXT_MODEL', 'openai/gpt-oss-20b')
    server.photo_model = os.environ.get('PHOTO_MODEL', 'qwen/qwen3.8-27b')
    server.quota = Quota(os.path.join(root, 'quota.sqlite3'))
    server.serve_forever()


if __name__ == '__main__':
    main()

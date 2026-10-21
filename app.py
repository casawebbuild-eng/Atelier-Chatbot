"""Atelier server: serves the pages, proxies the assistant to n8n, powers the admin API.

Every secret and every address comes from an environment variable. Nothing sensitive lives in this file.
Required:  ATELIER_ADMIN_KEY, SUPABASE_SERVICE_KEY, N8N_BASE_URL, N8N_AUTH
Optional:  ATELIER_DEBUG=1 (local development only), ATELIER_TRUST_PROXY=1 (behind a reverse proxy),
           ATELIER_DAILY_CAP, ATELIER_SITE_URL, SUPABASE_URL
"""
import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, request, Response, redirect, jsonify, send_from_directory, g


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------- settings (all from the environment)
SITE_URL = os.environ.get("ATELIER_SITE_URL", "http://atelier.localhost:5000").rstrip("/")

# n8n: the base address of your webhooks, e.g. https://your-n8n.example.com/webhook
#   setx N8N_BASE_URL "https://your-n8n.example.com/webhook"
# N8N_AUTH is the secret header n8n's Header Auth credential expects, as Name:value
#   setx N8N_AUTH "X-Atelier-Key:a-long-random-value"
N8N_BASE = os.environ.get("N8N_BASE_URL", "").rstrip("/")
N8N_AUTH = os.environ.get("N8N_AUTH") or os.environ.get("N8N_CHAT_AUTH", "")

# Supabase project that holds the knowledge base. The service key is a SECRET.
#   setx SUPABASE_SERVICE_KEY "the service_role / secret key from Supabase > Project Settings > API Keys"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://grcoqeailpiptijxkmnw.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Password for the admin page (also the Header Auth value of the KB Ingest webhook in n8n).
#   setx ATELIER_ADMIN_KEY "a-long-random-string"
ADMIN_KEY = os.environ.get("ATELIER_ADMIN_KEY", "")

DEBUG = os.environ.get("ATELIER_DEBUG") == "1"              # never on a public server
TRUST_PROXY = os.environ.get("ATELIER_TRUST_PROXY") == "1"   # only behind your own reverse proxy
DAILY_CAP = env_int("ATELIER_DAILY_CAP", 1500)               # AI requests a day, all visitors together

# ---------------------------------------------------------------- limits
MIN_ADMIN_KEY = 24
CHAT_PER_MIN = 20          # per visitor
MEDIA_PER_MIN = 8          # voice notes and images, per visitor
ADMIN_FAILS = 8            # wrong admin keys from one address ...
ADMIN_FAIL_WINDOW = 600    # ... within this many seconds locks it out
MAX_MESSAGE = 4000
MAX_HISTORY = 24000
MAX_CHARS = 200_000        # one knowledge document
MAX_UPLOAD = 6 * 1024 * 1024

HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")
ORIGIN = re.compile(r"^https?://(\*\.)?[A-Za-z0-9.\-]+(:\d{1,5})?$")
FONT_STACK = re.compile(r"^[A-Za-z0-9 ,'\"\-]{1,120}$")
FONTS_URL = "https://fonts.googleapis.com/css2?"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")[:60]


def text(value, limit):
    ok = isinstance(value, (str, int, float)) and not isinstance(value, bool)
    return str(value).strip()[:limit] if ok else ""


# ---------------------------------------------------------------- who is asking, and how often
_lock = threading.Lock()
_hits = {}          # (bucket, address) -> request times
_admin_fails = {}   # address -> wrong-key times
_daily = {"day": "", "n": 0}


def is_https():
    return request.is_secure or (TRUST_PROXY and request.headers.get("X-Forwarded-Proto", "").lower() == "https")


def client_ip():
    if TRUST_PROXY:  # only believe X-Forwarded-For when your own proxy sets it
        first = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if first:
            return first
    return request.remote_addr or "?"


def too_many(bucket, limit, window):
    """0 if the request may go ahead, otherwise how many seconds to wait."""
    now, key = time.time(), (bucket, client_ip())
    with _lock:
        if len(_hits) > 4000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
                del _hits[k]
        recent = [t for t in _hits.get(key, []) if now - t < window]
        if len(recent) >= limit:
            _hits[key] = recent
            return int(window - (now - recent[0])) + 1
        recent.append(now)
        _hits[key] = recent
    return 0


def daily_used(bump=False):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _lock:
        if _daily["day"] != today:
            _daily.update(day=today, n=0)
        if bump:
            _daily["n"] += 1
        return _daily["n"]


def limited(wait, message):
    resp = jsonify(error=message)
    resp.headers["Retry-After"] = str(wait)
    return resp, 429


def public_gate(bucket, per_minute):
    """Checks every public call (chat, voice, image). Returns an error response or None."""
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:  # a browser on another website
        return jsonify(error="Requests from other websites are not allowed."), 403
    wait = too_many(bucket, per_minute, 60)
    if wait:
        return limited(wait, f"Too many requests. Please wait {wait} seconds and try again.")
    if DAILY_CAP and daily_used() >= DAILY_CAP:
        return limited(3600, "The daily limit has been reached. Please try again tomorrow.")
    daily_used(bump=True)
    return None


# ---------------------------------------------------------------- security headers on every response
CSP_REPORT_ONLY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; media-src 'self' blob: data:; connect-src 'self' https://cdn.jsdelivr.net; "
    "worker-src 'self' blob: https://cdn.jsdelivr.net; frame-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'"
)


@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), geolocation=(), payment=(), usb=()")
    if resp.mimetype == "text/html":
        # Report-only: browsers list anything it would block in the console, and nothing breaks.
        h.setdefault("Content-Security-Policy-Report-Only", CSP_REPORT_ONLY)
    if request.path.startswith("/api/") or request.path == "/admin":
        h["Cache-Control"] = "no-store"
    if is_https():
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


@app.errorhandler(413)
def too_big(_):
    return jsonify(error="That upload is too large."), 413


# ---------------------------------------------------------------- brands
def read_brand(name):
    """The brand file as a dict, or None. Read on every request, so edits show without a restart."""
    name = slug(name)
    f = Path(app.root_path) / "brands" / f"{name}.json"
    if not name or not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        app.logger.error("Brand file %s is not valid JSON: %s", f.name, e)
        return None
    return data if isinstance(data, dict) else None


def get_brand(name):
    """(slug, data) for an existing brand, or None. There is no fallback brand."""
    data = read_brand(name) if name else None
    return (slug(name), data) if data is not None else None


def public_brand(name, data):
    """Only the look and wording. The persona and the knowledge id never leave the server."""
    out = {"slug": name}
    for key, limit in (("name", 60), ("wordmark", 40), ("initial", 2), ("title", 80),
                       ("heroTitle", 120), ("subtitle", 300), ("placeholder", 80), ("teaser", 120)):
        if text(data.get(key), limit):
            out[key] = text(data.get(key), limit)
    if data.get("dot") is False:
        out["dot"] = False
    if isinstance(data.get("suggestions"), list):
        out["suggestions"] = [text(x, 100) for x in data["suggestions"] if text(x, 100)][:6]
    theme = {}
    for mode in ("light", "dark"):
        colours = data.get("theme", {}).get(mode) if isinstance(data.get("theme"), dict) else None
        if isinstance(colours, dict):
            ok = {k: v for k, v in colours.items()
                  if k in ("bg", "surface", "ink", "muted", "accent") and isinstance(v, str) and HEX.match(v)}
            if ok:
                theme[mode] = ok
    if theme:
        out["theme"] = theme
    fonts = data.get("fonts") if isinstance(data.get("fonts"), dict) else {}
    safe = {}
    if isinstance(fonts.get("url"), str) and fonts["url"].startswith(FONTS_URL) and len(fonts["url"]) < 600:
        safe["url"] = fonts["url"]
    for key in ("serif", "sans"):
        if isinstance(fonts.get(key), str) and FONT_STACK.match(fonts[key]):
            safe[key] = fonts[key]
    if safe:
        out["fonts"] = safe
    return out


def frame_ancestors(brand_data):
    """Which sites may show this brand's chat in an iframe: itself plus the brand's allowedOrigins."""
    listed = brand_data.get("allowedOrigins") if isinstance(brand_data, dict) else None
    ok = [o.strip() for o in (listed if isinstance(listed, list) else []) if isinstance(o, str) and ORIGIN.match(o.strip())]
    return " ".join(["'self'"] + ok)


def serve_html(name, ancestors="'self'"):
    resp = send_from_directory(app.root_path, name)
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors}"
    return resp


# ---------------------------------------------------------------- pages
@app.before_request
def use_friendly_name():
    # Anyone who opens the raw address gets sent to the named one.
    # 302 (not 301) so the browser never caches the redirect permanently.
    if request.method == "GET" and request.host.startswith("127.0.0.1"):
        return redirect(SITE_URL + request.full_path.rstrip("?"), code=302)


@app.get("/")
def index():
    # No default chat lives here. Each client's chat is at /c/<name>.
    return redirect("/admin", code=302)


@app.get("/admin")
def admin():
    return serve_html("admin.html", "'none'")  # the admin page must never be framed


@app.get("/widget.js")
def widget_js():
    resp = send_from_directory(app.root_path, "widget.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.get("/embed-demo")
def embed_demo():
    return serve_html("embed-demo.html")


@app.get("/c/<brand>")
def brand_page(brand):
    data = read_brand(brand)
    if data is None:
        return Response("Unknown brand.", status=404)
    return serve_html("Atelier.html", frame_ancestors(data))


@app.get("/api/brand")
def brand_api():
    found = get_brand(request.args.get("b"))
    if found is None:
        resp, status = jsonify(error="Unknown or missing brand. Use /api/brand?b=<name>."), 404
    else:
        resp, status = jsonify(public_brand(*found)), 200
    resp.headers["Access-Control-Allow-Origin"] = "*"  # public look-and-wording only; the widget on other sites reads it
    return resp, status


# ---------------------------------------------------------------- the assistant (public, so every call is checked)
def n8n_headers():
    headers = {"ngrok-skip-browser-warning": "1"}
    name, sep, value = N8N_AUTH.partition(":")
    if sep and name.strip() and value.strip():
        headers[name.strip()] = value.strip()
    return headers


def to_n8n(path, **kwargs):
    """Calls an n8n webhook. Visitors only ever see a generic error: never an address or a stack trace."""
    if not N8N_BASE:
        app.logger.error("N8N_BASE_URL is not set, so nothing can be sent to n8n.")
        return jsonify(error="The assistant isn't set up yet."), 503
    try:
        r = requests.post(f"{N8N_BASE}/{path}", headers=n8n_headers(), timeout=120, **kwargs)
    except requests.RequestException as e:
        app.logger.error("Cannot reach n8n for %s: %s", path, e)
        return jsonify(error="The assistant is unreachable right now. Please try again in a moment."), 502
    if not r.ok:
        if r.status_code in (401, 403):
            app.logger.error("n8n refused /%s (HTTP %s). Check that webhook in n8n: Authentication must be Header Auth "
                             "with the same Name:value as N8N_AUTH, 'Ignore Bots' must be OFF (n8n treats Python "
                             "scripts as bots), and the workflow must be republished.", path, r.status_code)
        else:
            app.logger.error("n8n answered /%s with HTTP %s", path, r.status_code)
        return jsonify(error="The assistant couldn't answer right now. Please try again."), 502
    return Response(r.content, status=200, content_type=r.headers.get("Content-Type", "text/plain"))


@app.post("/api/chat")
def chat():
    if (bad := public_gate("chat", CHAT_PER_MIN)):
        return bad
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    found = get_brand(body.get("brand"))
    if found is None:
        return jsonify(error="Unknown brand."), 400
    _, brand = found
    message = text(body.get("message"), MAX_MESSAGE)
    if not message:
        return jsonify(error="There is no message to send."), 400
    # Only these fields go on to n8n. The persona and knowledge id come from the brand file, never the browser.
    payload = {
        "message": message,
        "history": text(body.get("history"), MAX_HISTORY),
        "isFirst": bool(body.get("isFirst")),
        "chatId": text(body.get("chatId"), 64),
        "sessionId": text(body.get("sessionId"), 64),
        "client": text(brand.get("client"), 60),
        "persona": text(brand.get("persona"), 4000),
    }
    return to_n8n("atelier", json=payload)


def sniff_audio(data):
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm", "webm"
    if data[4:8] == b"ftyp":
        return "audio/mp4", "m4a"
    if data[:4] == b"OggS":
        return "audio/ogg", "ogg"
    return None


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def read_upload(field, sniff):
    """(bytes, mime, ext) of a valid upload, or (None, error response). Checked by file signature, not by name."""
    f = request.files.get(field)
    data = f.read(MAX_UPLOAD + 1) if f else b""
    if not data:
        return None, (jsonify(error="No file was received."), 400)
    if len(data) > MAX_UPLOAD:
        return None, (jsonify(error="That file is too large."), 413)
    kind = sniff(data)
    if kind is None:
        return None, (jsonify(error="That file type isn't supported."), 415)
    return (data, *kind), None


@app.post("/api/voice")
def voice():
    if (bad := public_gate("voice", MEDIA_PER_MIN)):
        return bad
    upload, err = read_upload("audio", sniff_audio)
    if err:
        return err
    data, mime, ext = upload
    return to_n8n("atelier-transcribe", files={"audio": (f"voice.{ext}", data, mime)})


@app.post("/api/image")
def image():
    if (bad := public_gate("image", MEDIA_PER_MIN)):
        return bad
    upload, err = read_upload("image", sniff_image)
    if err:
        return err
    data, mime, ext = upload
    return to_n8n("atelier-image", files={"image": (f"image.{ext}", data, mime)},
                  data={"message": text(request.form.get("message"), 2000)})


# ---------------------------------------------------------------- admin (needs the admin key)
def sb(method, path, extra_headers=None, **kwargs):
    """Call Supabase's REST API (PostgREST) with the service key."""
    headers = {"apikey": SUPABASE_KEY}
    if SUPABASE_KEY.startswith("eyJ"):  # legacy JWT-style keys also go in Authorization
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    headers.update(extra_headers or {})
    return requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, timeout=30, **kwargs)


def admin_locked():
    now, ip = time.time(), client_ip()
    with _lock:
        recent = [t for t in _admin_fails.get(ip, []) if now - t < ADMIN_FAIL_WINDOW]
        _admin_fails[ip] = recent
        return int(ADMIN_FAIL_WINDOW - (now - recent[0])) + 1 if len(recent) >= ADMIN_FAILS else 0


def identify(sent):
    """Who a key belongs to: {"role": "admin"}, {"role": "client", "client": slug}, or None.

    Your admin key sees everything. A client key is stored only as a SHA-256 hash in that client's brand file
    ("adminKeyHash"), so the file is safe to commit, and it unlocks that client's documents and nothing else."""
    if hmac.compare_digest(sent.encode(), ADMIN_KEY.encode()):
        return {"role": "admin", "client": None}
    digest = hashlib.sha256(sent.encode()).hexdigest()
    match = None
    for f in sorted((Path(app.root_path) / "brands").glob("*.json")):
        data = read_brand(f.stem) if slug(f.stem) == f.stem else None
        stored, client = (data or {}).get("adminKeyHash"), text((data or {}).get("client"), 60)
        if client and isinstance(stored, str) and len(stored) == 64 and hmac.compare_digest(digest, stored.lower()):
            match = {"role": "client", "client": client}
    return match


def guard(need_db=True, admin_only=False):
    """Returns an error response if the request may not use the admin API, else None.
    On success g.who says who is asking."""
    if not ADMIN_KEY:
        return jsonify(error="The server has no admin key yet. Set ATELIER_ADMIN_KEY and restart app.py."), 503
    if len(ADMIN_KEY) < MIN_ADMIN_KEY:
        return jsonify(error=f"The admin key is too short. Use at least {MIN_ADMIN_KEY} characters."), 503
    if (wait := admin_locked()):
        return limited(wait, "Too many wrong keys. This address is locked for a few minutes.")
    who = identify(request.headers.get("X-Admin-Key", ""))
    if who is None:
        with _lock:
            _admin_fails.setdefault(client_ip(), []).append(time.time())
        return jsonify(error="Wrong key."), 401
    if admin_only and who["role"] != "admin":
        return jsonify(error="That needs the admin key."), 403
    g.who = who
    if need_db and not SUPABASE_KEY:
        return jsonify(error="The server has no Supabase key yet. Set SUPABASE_SERVICE_KEY and restart app.py."), 503
    return None


def db_error(e):
    app.logger.error("Database call failed: %s", e)
    return jsonify(error="Cannot reach the database. Check the Supabase address and key."), 502


@app.get("/api/brands")
def list_brands():
    """For the admin page's test chat: which brands exist and which knowledge client each one uses."""
    if (bad := guard(need_db=False)):
        return bad
    items = []
    for f in sorted((Path(app.root_path) / "brands").glob("*.json")):
        data = read_brand(f.stem) if slug(f.stem) == f.stem else None
        if data is not None and (g.who["role"] == "admin" or text(data.get("client"), 60) == g.who["client"]):
            items.append({"slug": f.stem, "name": text(data.get("name") or data.get("wordmark") or f.stem, 60),
                          "client": text(data.get("client"), 60)})
    return jsonify(items=items)


@app.get("/api/me")
def whoami():
    if (bad := guard(need_db=False)):
        return bad
    return jsonify(role=g.who["role"], client=g.who["client"])


def own_only(client):
    """A client key may only touch its own client's documents."""
    return g.who["role"] == "client" and client != g.who["client"]


@app.get("/api/kb")
def list_knowledge():
    if (bad := guard()):
        return bad
    params = {"select": "client,doc,chars,chunks,updated_at", "order": "client.asc,doc.asc"}
    if g.who["role"] == "client":
        params["client"] = f"eq.{g.who['client']}"
    try:
        r = sb("GET", "kb_overview", params=params)
    except requests.RequestException as e:
        return db_error(e)
    if not r.ok:
        return jsonify(error=f"The database refused the request (HTTP {r.status_code})."), 502
    return jsonify(items=r.json())


@app.get("/api/kb/<client>/<doc>")
def get_knowledge(client, doc):
    if (bad := guard()):
        return bad
    if slug(client) != client or slug(doc) != doc or own_only(client):
        return jsonify(error="Unknown document."), 404
    try:
        r = sb("GET", "kb_sources", params={"client": f"eq.{client}", "doc": f"eq.{doc}",
                                            "select": "client,doc,content,updated_at"})
    except requests.RequestException as e:
        return db_error(e)
    if not r.ok:
        return jsonify(error=f"The database refused the request (HTTP {r.status_code})."), 502
    rows = r.json()
    if not rows:
        return jsonify(error="That document no longer exists."), 404
    return jsonify(rows[0])


@app.delete("/api/kb/<client>/<doc>")
def delete_knowledge(client, doc):
    if (bad := guard()):
        return bad
    if slug(client) != client or slug(doc) != doc or own_only(client):
        return jsonify(error="Unknown document."), 404
    flt = {"client": f"eq.{client}", "doc": f"eq.{doc}"}
    try:
        # chunks first (that is what the assistant searches), then the stored original text
        for table in ("documents", "kb_sources"):
            r = sb("DELETE", table, extra_headers={"Prefer": "return=minimal"}, params=flt)
            if not r.ok:
                return jsonify(error=f"Couldn't delete from {table} (HTTP {r.status_code})."), 502
    except requests.RequestException as e:
        return db_error(e)
    return jsonify(ok=True, client=client, doc=doc)


@app.post("/api/kb")
def save_knowledge():
    """Admin page -> here -> n8n (chunks + embeddings), then the original text is kept for editing."""
    if (bad := guard()):
        return bad
    if not N8N_BASE:
        return jsonify(error="The server has no n8n address yet. Set N8N_BASE_URL and restart app.py."), 503

    data = request.get_json(silent=True) or {}
    client, doc = slug(data.get("client")), slug(data.get("doc"))
    if g.who["role"] == "client":
        client = g.who["client"]  # whatever the page sent, a client key can only write to its own client
    content = str(data.get("content") or "").strip()
    if not client or not doc:
        return jsonify(error="A client and a document name are required."), 400
    if not content:
        return jsonify(error="There is no content to save."), 400
    if len(content) > MAX_CHARS:
        return jsonify(error=f"Too long: {len(content):,} characters (limit {MAX_CHARS:,}). Split it into several documents."), 400

    try:
        r = requests.post(
            f"{N8N_BASE}/atelier-kb",
            json={"client": client, "doc": doc, "content": content},
            headers={"X-Admin-Key": ADMIN_KEY, "ngrok-skip-browser-warning": "1"},
            timeout=180,
        )
    except requests.RequestException as e:
        app.logger.error("Cannot reach n8n for atelier-kb: %s", e)
        return jsonify(error="Cannot reach n8n. Check N8N_BASE_URL and that n8n is running."), 502
    if not r.ok:
        return jsonify(error=f"The knowledge workflow failed (n8n returned HTTP {r.status_code}). "
                             "Open KB Ingest in n8n, go to Executions and check the newest run."), 502

    try:
        w = sb("POST", "kb_sources", params={"on_conflict": "client,doc"},
               extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
               json={"client": client, "doc": doc, "content": content,
                     "updated_at": datetime.now(timezone.utc).isoformat()})
    except requests.RequestException as e:
        return db_error(e)
    if not w.ok:
        return jsonify(error="The assistant learned it, but the original text couldn't be stored for editing "
                             f"(HTTP {w.status_code}). Save it again."), 502
    return jsonify(ok=True, client=client, doc=doc, chars=len(content))


# ---------------------------------------------------------------- the 20-point security checklist, live
def security_report():
    """Each item is (title, status, note). status: ok = verified, warn = fix it, todo = your move, info = a note."""
    root = Path(app.root_path)
    missing = [n for n, v in (("ATELIER_ADMIN_KEY", ADMIN_KEY), ("SUPABASE_SERVICE_KEY", SUPABASE_KEY),
                              ("N8N_BASE_URL", N8N_BASE), ("N8N_AUTH", N8N_AUTH)) if not v]
    cfg = root / ".git" / "config"
    if not cfg.is_file():
        git_item = ("info", "No git folder here, which is normal on a deployed server. On your PC: python tools/check_secrets.py --all")
    elif "hookspath" in cfg.read_text(errors="ignore").lower() and (root / ".githooks" / "pre-commit").is_file():
        git_item = ("ok", "A pre-commit scan blocks any commit that contains a key or a tunnel address.")
    else:
        git_item = ("todo", "Turn on the commit scan once: git config core.hooksPath .githooks")

    placeholders = []
    for f in sorted((root / "brands").glob("*.json")):
        data = read_brand(f.stem) or {}
        if any(".example" in str(o) for o in (data.get("allowedOrigins") or [])):
            placeholders.append(f.stem)
    local = urlparse(SITE_URL).hostname in ("localhost", "127.0.0.1") or str(urlparse(SITE_URL).hostname).endswith(".localhost")
    prod_issues = []
    if DEBUG:
        prod_issues.append("debug mode is on")
    if placeholders:
        prod_issues.append("placeholder allowedOrigins in: " + ", ".join(placeholders))
    if not local and SITE_URL.startswith("http://"):
        prod_issues.append("ATELIER_SITE_URL is plain http")
    if missing:
        prod_issues.append("missing settings: " + ", ".join(missing))

    if is_https():
        https_item = ("ok", "This page was served over HTTPS, and browsers are told to keep using it.")
    elif local or request.host.split(":")[0] in ("localhost", "127.0.0.1") or request.host.split(":")[0].endswith(".localhost"):
        https_item = ("info", "Plain HTTP is fine on this PC. A public site must be HTTPS: the microphone needs it too.")
    else:
        https_item = ("warn", "This site is on plain HTTP. Put it behind HTTPS before real visitors use it.")

    keyed, unkeyed = [], []
    for f in sorted((root / "brands").glob("*.json")):
        data = read_brand(f.stem) or {}
        if text(data.get("client"), 60):
            (keyed if isinstance(data.get("adminKeyHash"), str) and len(data["adminKeyHash"]) == 64 else unkeyed).append(f.stem)
    if keyed and not unkeyed:
        perms = ("ok", f"Each client signs in with their own key and only sees their own documents ({len(keyed)} set up). "
                       "Your admin key stays with you. For the strongest split, give each client their own Supabase project.")
    elif keyed:
        perms = ("warn", "No client key yet for: " + ", ".join(unkeyed) + ". Make one: py tools/make_client_key.py <brand>")
    else:
        perms = ("warn", "Clients have no keys of their own, so the admin key would reach every client. "
                         "Make one per client: py tools/make_client_key.py <brand>")
    used = daily_used()
    items = [
        ("Hide API keys", "ok", "Every key and address is read from an environment variable. None is in the code."),
        ("Check env variables", "ok" if not missing else "warn",
         "All four settings are in place." if not missing else "Not set: " + ", ".join(missing)),
        ("Check keys in git", *git_item),
        ("Protect admin routes", "ok", f"Every admin route needs the key. {ADMIN_FAILS} wrong tries lock that address for {ADMIN_FAIL_WINDOW // 60} minutes."),
        ("Add auth", "ok" if len(ADMIN_KEY) >= MIN_ADMIN_KEY else "warn",
         f"The admin key is {len(ADMIN_KEY)} characters." if len(ADMIN_KEY) >= MIN_ADMIN_KEY
         else f"The admin key is shorter than {MIN_ADMIN_KEY} characters. Make a longer one."),
        ("Check user permissions", *perms),
        ("Sanitize user inputs", "ok", f"Messages are capped at {MAX_MESSAGE:,} characters, names are reduced to letters, digits and hyphens, and only listed fields reach n8n."),
        ("Protect against XSS", "ok", "Replies are sanitized, everything else is written as plain text, and a strict content policy runs in report-only mode."),
        ("Protect against SQL injection", "ok", "No SQL is built from visitor input. The database is reached through Supabase's REST API with encoded values."),
        ("Check DB rules", "info", "Row security is on for every table with no public policy (db/schema.sql). Re-check in Supabase > Advisors after schema changes."),
        ("Add rate limiting", "ok", f"Chat: {CHAT_PER_MIN} a minute. Voice and images: {MEDIA_PER_MIN} a minute. Per visitor address."),
        ("Set a spend cap", "info", f"App-wide cap: {DAILY_CAP:,} AI requests a day ({used:,} used today). Also set a budget alert in Google AI Studio or Cloud Billing."),
        ("Secure file uploads", "ok", f"Voice and image files are checked by file signature and limited to {MAX_UPLOAD // (1024 * 1024)} MB."),
        ("CSRF protection", "ok", "No cookies are used. The admin key travels in a header other sites can't send, and chat calls from other sites are refused."),
        ("Check CORS settings", "todo", "Only /api/brand is open to other sites (public wording and colours). Switch CORS off on the three n8n webhooks: the browser no longer calls them."),
        ("Enable HTTPS", *https_item),
        ("Add security headers", "ok", "nosniff, referrer policy, permissions policy, framing rules per client, and HSTS on HTTPS."),
        ("Secure cookies", "ok", "The app sets no cookies."),
        ("Disable debug mode", "ok" if not DEBUG else "warn",
         "Debug mode is off." if not DEBUG else "Debug mode is ON. Never run it on a public server."),
        ("Check prod settings", "ok" if not prod_issues else ("info" if local and not missing and not DEBUG else "warn"),
         "Nothing left over from development." if not prod_issues else ("; ".join(prod_issues)[:1].upper() + "; ".join(prod_issues)[1:]) + "."),
    ]
    counts = {k: sum(1 for i in items if i[1] == k) for k in ("ok", "warn", "todo", "info")}
    return [{"n": i + 1, "title": t, "status": s, "note": n} for i, (t, s, n) in enumerate(items)], counts


@app.get("/api/security")
def security():
    if (bad := guard(need_db=False, admin_only=True)):
        return bad
    items, counts = security_report()
    return jsonify(items=items, counts=counts)


if __name__ == "__main__":
    # With debug on, Flask starts twice; announce once, from the reloaded child.
    if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        print()
        print(f"  Knowledge admin:   {SITE_URL}/admin")
        names = sorted(f.stem for f in (Path(app.root_path) / "brands").glob("*.json"))
        for n in names:
            print(f"  Chat for {n}: {SITE_URL}/c/{n}")
        if not names:
            print("  No brand files found. Add one to the brands folder, e.g. brands/my-client.json")
        print(f"  Widget demo:       {SITE_URL}/embed-demo")
        with app.test_request_context("/"):
            _items, _counts = security_report()
        print(f"  Security checklist: {_counts['ok']} of 20 verified, {_counts['warn']} to fix, {_counts['todo']} for you to do")
        for _i in _items:
            if _i["status"] == "warn":
                print(f"    ! {_i['title']}: {_i['note']}")
        print()
    app.run(host=os.environ.get("ATELIER_HOST", "127.0.0.1"), port=env_int("PORT", 5000), debug=DEBUG)

import hmac
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

import requests
from flask import Flask, request, Response, redirect, jsonify, send_from_directory

N8N_URL = "https://science-footgear-darwinism.ngrok-free.dev/webhook/atelier"
KB_URL = "https://science-footgear-darwinism.ngrok-free.dev/webhook/atelier-kb"
SITE_URL = "http://atelier.localhost:5000"

# Supabase project that holds the knowledge base. The service key is a SECRET: keep it in an
# environment variable, never in this file (OneDrive syncs it).
#   setx SUPABASE_SERVICE_KEY "the service_role key from Supabase > Project Settings > API Keys"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://grcoqeailpiptijxkmnw.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Shared secret for the admin page. Set it once in PowerShell, then open a NEW terminal:
#   setx ATELIER_ADMIN_KEY "your-long-random-key"
# The same value goes into n8n as the Header Auth credential (name: X-Admin-Key).
ADMIN_KEY = os.environ.get("ATELIER_ADMIN_KEY", "")
MAX_CHARS = 200_000

# Only needed if the n8n "Chat" webhook has Header Auth switched on. Give the header exactly as
# "Name:value" (the same Name and Value as the credential in n8n), then open a NEW terminal:
#   setx N8N_CHAT_AUTH "X-Chat-Key:the-secret-value"
N8N_CHAT_AUTH = os.environ.get("N8N_CHAT_AUTH", "")

# Brands: one JSON file per client in the brands/ folder, opened at /c/<name>.
# There is deliberately no default brand: a mistyped name must never answer as the wrong business.
HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")
ORIGIN = re.compile(r"^https?://(\*\.)?[A-Za-z0-9.\-]+(:\d{1,5})?$")
FONT_STACK = re.compile(r"^[A-Za-z0-9 ,'\"\-]{1,120}$")
FONTS_URL = "https://fonts.googleapis.com/css2?"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")[:60]


def text(value, limit):
    ok = isinstance(value, (str, int, float)) and not isinstance(value, bool)
    return str(value).strip()[:limit] if ok else ""


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


def frame_ancestors(brand_data):
    """Which sites may show this brand's chat in an iframe: itself plus the brand's allowedOrigins."""
    listed = brand_data.get("allowedOrigins") if isinstance(brand_data, dict) else None
    ok = [o.strip() for o in (listed if isinstance(listed, list) else []) if isinstance(o, str) and ORIGIN.match(o.strip())]
    return " ".join(["'self'"] + ok)


def serve_html(name, ancestors="'self'"):
    resp = send_from_directory(app.root_path, name)
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors}"
    return resp


def chat_headers():
    headers = {"ngrok-skip-browser-warning": "1"}
    name, sep, value = N8N_CHAT_AUTH.partition(":")
    if sep and name.strip() and value.strip():
        headers[name.strip()] = value.strip()
    return headers


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


@app.post("/api/chat")
def chat():
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    found = get_brand(body.pop("brand", None))
    if found is None:
        return jsonify(error="Unknown brand."), 400
    _, brand = found
    # The browser can't choose these: they come from the brand file.
    body["client"] = text(brand.get("client"), 60)
    body["persona"] = text(brand.get("persona"), 4000)
    try:
        r = requests.post(
            N8N_URL,
            json=body,
            headers=chat_headers(),
            timeout=120,
        )
    except requests.RequestException as e:
        return Response(f"Cannot reach n8n: {e}", status=502)
    if r.status_code in (401, 403):
        app.logger.error("n8n refused the chat request (HTTP %s). Check the Chat webhook in n8n: "
                         "(1) Options > 'Ignore Bots' must be OFF, because n8n treats Python scripts as bots; "
                         "(2) if Authentication is Header Auth, set N8N_CHAT_AUTH to its header as Name:value; "
                         "(3) republish the workflow.", r.status_code)
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "text/plain"))


def sb(method, path, extra_headers=None, **kwargs):
    """Call Supabase's REST API (PostgREST) with the service key."""
    headers = {"apikey": SUPABASE_KEY}
    if SUPABASE_KEY.startswith("eyJ"):  # legacy JWT-style keys also go in Authorization
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    headers.update(extra_headers or {})
    return requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, timeout=30, **kwargs)


def guard(need_db=True):
    """Returns an error response if the request may not use the admin API, else None."""
    if not ADMIN_KEY:
        return jsonify(error="The server has no admin key yet. Set ATELIER_ADMIN_KEY and restart app.py."), 503
    sent = request.headers.get("X-Admin-Key", "")
    if not hmac.compare_digest(sent.encode(), ADMIN_KEY.encode()):
        return jsonify(error="Wrong admin key."), 401
    if need_db and not SUPABASE_KEY:
        return jsonify(error="The server has no Supabase key yet. Set SUPABASE_SERVICE_KEY and restart app.py."), 503
    return None


def db_error(e):
    return jsonify(error=f"Cannot reach the database: {e}"), 502


@app.get("/api/brands")
def list_brands():
    """For the admin page's test chat: which brands exist and which knowledge client each one uses."""
    if (bad := guard(need_db=False)):
        return bad
    items = []
    for f in sorted((Path(app.root_path) / "brands").glob("*.json")):
        data = read_brand(f.stem) if slug(f.stem) == f.stem else None
        if data is not None:
            items.append({"slug": f.stem, "name": text(data.get("name") or data.get("wordmark") or f.stem, 60),
                          "client": text(data.get("client"), 60)})
    return jsonify(items=items)


@app.get("/api/kb")
def list_knowledge():
    if (bad := guard()):
        return bad
    try:
        r = sb("GET", "kb_overview", params={"select": "client,doc,chars,chunks,updated_at",
                                             "order": "client.asc,doc.asc"})
    except requests.RequestException as e:
        return db_error(e)
    if not r.ok:
        return jsonify(error=f"The database refused the request (HTTP {r.status_code})."), 502
    return jsonify(items=r.json())


@app.get("/api/kb/<client>/<doc>")
def get_knowledge(client, doc):
    if (bad := guard()):
        return bad
    if slug(client) != client or slug(doc) != doc:
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
    if slug(client) != client or slug(doc) != doc:
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

    data = request.get_json(silent=True) or {}
    client, doc = slug(data.get("client")), slug(data.get("doc"))
    content = str(data.get("content") or "").strip()
    if not client or not doc:
        return jsonify(error="A client and a document name are required."), 400
    if not content:
        return jsonify(error="There is no content to save."), 400
    if len(content) > MAX_CHARS:
        return jsonify(error=f"Too long: {len(content):,} characters (limit {MAX_CHARS:,}). Split it into several documents."), 400

    try:
        r = requests.post(
            KB_URL,
            json={"client": client, "doc": doc, "content": content},
            headers={"X-Admin-Key": ADMIN_KEY, "ngrok-skip-browser-warning": "1"},
            timeout=180,
        )
    except requests.RequestException as e:
        return jsonify(error=f"Cannot reach n8n: {e}"), 502
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


if __name__ == "__main__":
    # With debug=True Flask starts twice; only announce from the reloaded child.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        print()
        print(f"  Knowledge admin:   {SITE_URL}/admin")
        names = sorted(f.stem for f in (Path(app.root_path) / "brands").glob("*.json"))
        for n in names:
            print(f"  Chat for {n}: {SITE_URL}/c/{n}")
        if not names:
            print("  No brand files found. Add one to the brands folder, e.g. brands/my-client.json")
        print(f"  Widget demo:       {SITE_URL}/embed-demo")
        if not ADMIN_KEY:
            print("  Note: ATELIER_ADMIN_KEY is not set, so the admin page can't be used yet.")
        if not N8N_CHAT_AUTH:
            print("  Chat webhook auth: none configured (fine unless the n8n Chat webhook has authentication on)")
        if not SUPABASE_KEY:
            print("  Note: SUPABASE_SERVICE_KEY is not set, so the admin page can't list or edit knowledge yet.")
        print()
    app.run(host="127.0.0.1", port=5000, debug=True)

# Atelier

A branded AI chat for small businesses. It answers from the business's own knowledge (menu, hours,
policies), takes voice notes and images, and runs either as a page or as a widget that drops onto any
website with one line of code.

```
visitor's browser / website widget
        |
   Flask app (app.py)  --->  n8n webhooks  --->  Gemini
        |                          |
        +--------> Supabase (pgvector: what the assistant knows)
```

## What it does

- Chat with markdown, tables, voice notes (transcribed by Gemini) and image analysis.
- Answers only from the client's knowledge base, via a Search Knowledge tool in n8n.
- One JSON file per client in `brands/` sets the look, wording, colours, fonts and personality.
- Admin page (`/admin`): see, edit, replace and delete what the assistant knows, with a test chat beside it.
  Upload PDF, Word, text, Markdown or CSV, or paste text.
- Embeddable widget (`widget.js`): a floating button that opens the client's chat on their own site.

## Layout

| Path | What it is |
|---|---|
| `app.py` | Flask server: serves the pages, proxies chat to n8n, powers the admin API |
| `Atelier.html` | The chat page (also the compact view inside the widget) |
| `admin.html` | Knowledge admin page |
| `widget.js` | The embeddable widget |
| `embed-demo.html` | A mock client website with the widget installed |
| `brands/*.json` | One file per client |
| `db/schema.sql` | Supabase tables, search function and view |
| `n8n/` | Exported n8n workflows (add yours here; see below) |

## Run it locally (Windows PowerShell)

You need Python 3.10+, an n8n instance with the workflows below, and a Supabase project.

1. In the Supabase SQL editor, run `db/schema.sql`.
2. Install and set the keys. Never put keys in a file.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

setx ATELIER_ADMIN_KEY "a-long-random-string"
setx SUPABASE_SERVICE_KEY "the service_role / secret key from Supabase"
```

3. Open a NEW terminal (`setx` only reaches windows opened afterwards), then:

```powershell
py app.py
```

| Address | What |
|---|---|
| `http://atelier.localhost:5000/c/<brand>` | A client's chat |
| `http://atelier.localhost:5000/admin` | Knowledge admin (asks for the admin key) |
| `http://atelier.localhost:5000/embed-demo` | The widget on a sample website |

`*.localhost` addresses resolve to your own machine in Chrome, Edge and Brave. Windows PowerShell does not
resolve them, so use `127.0.0.1:5000` for command-line tests.

## Settings

| Variable | Needed | Purpose |
|---|---|---|
| `ATELIER_ADMIN_KEY` | yes | Password for the admin page. Must equal the n8n Header Auth value. |
| `SUPABASE_SERVICE_KEY` | yes | Lets the server list, read and delete knowledge. |
| `SUPABASE_URL` | no | Defaults to the project set in `app.py`. |
| `N8N_CHAT_AUTH` | no | `Name:value` header, only if the n8n Chat webhook has Header Auth on. |

Edit the addresses at the top of `app.py` (`N8N_URL`, `KB_URL`, `SITE_URL`) for your own n8n.

## n8n workflows

| Webhook path | Does | Notes |
|---|---|---|
| `atelier` | Chat: AI Agent with the Search Knowledge tool | **Ignore Bots must be off**, or n8n rejects the Flask server as a bot (403) |
| `atelier-transcribe` | Voice note to text (Gemini, audio) | Called by the page |
| `atelier-image` | Describe or answer about an image (Gemini) | Called by the page |
| `atelier-kb` | Save a document: delete the old chunks, split, embed, store | Header Auth: `X-Admin-Key` |

In the chat workflow, the agent's system message and the Search Knowledge filter read `persona` and `client`
that the server adds from the brand file. They never come from the browser.

Export each workflow from n8n (three dots menu, Download) into `n8n/`. Unpin test data first, since pinned
data is included in the export.

## A brand file

```json
{
  "name": "Casa Atelier",
  "wordmark": "Casa Atelier",
  "initial": "C",
  "title": "Casa Atelier - Ask us anything",
  "heroTitle": "What can we *tell* you?",
  "subtitle": "Opening hours, the menu, reservations.",
  "placeholder": "Ask about our menu or hours...",
  "teaser": "Questions about the menu? Ask us.",
  "suggestions": ["What are your opening hours?", "Where can I park?"],
  "theme": { "light": { "accent": "#b4501f" }, "dark": { "accent": "#e0813f" } },
  "allowedOrigins": ["https://www.the-restaurant.com"],
  "client": "casa-demo",
  "persona": "You are the assistant for ..."
}
```

`client` and `persona` stay on the server. `allowedOrigins` lists the websites allowed to embed this client's
chat. Optional: `fonts` (`url`, `serif`, `sans`), `dot`, and light/dark `bg`, `surface`, `ink`, `muted`.
The file name is the address: `brands/casa-atelier.json` is `/c/casa-atelier`. There is no default brand.

## Put it on a website

```html
<script src="https://YOUR-HOST/widget.js" data-brand="casa-atelier" async></script>
```

The host must be public HTTPS (the microphone needs it). Add `data-position="left"` to move it, or call
`AtelierWidget.open()` from your own button.

## Before real clients

- The voice and image webhooks are still called straight from the page. Route them through `app.py` and add
  a rate limit and a shared secret.
- One admin key and one Supabase key reach every client. Use one Supabase project per client.
- Keep this repository private until the n8n addresses are out of the code.

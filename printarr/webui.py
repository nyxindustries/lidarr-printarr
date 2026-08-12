"""Review web UI: manually assign releases that automatic matching refused.

namer-style: every refused folder keeps a report of the candidates that were
considered; this UI lists stuck queue items and failed watch folders with
those candidates, and lets a human pick one — or paste any MusicBrainz
release / release-group URL — to force the match and trigger the import.

Implementation notes: standard-library HTTP server (no new dependencies),
single embedded HTML page, JSON API underneath. There is no authentication —
bind it to localhost or put it behind a reverse proxy.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from printarr import __version__
from printarr.config import Config
from printarr.log import get_logger
from printarr.musicbrainz import MusicBrainzError
from printarr.queueworker import QueueWorker

log = get_logger(__name__)

MBID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def parse_mb_reference(text: str) -> tuple[str | None, str]:
    """Extract (mbid, kind) from a raw MBID or MusicBrainz URL.

    kind is 'release', 'release-group', or 'unknown' (bare MBID with no
    context). Returns (None, '') when no MBID is found.
    """
    match = MBID_RE.search(text or "")
    if not match:
        return None, ""
    mbid = match.group(0).lower()
    if "release-group" in text:
        return mbid, "release-group"
    if "release" in text:
        return mbid, "release"
    return mbid, "unknown"


class WebUI:
    def __init__(self, config: Config, worker: QueueWorker):
        self.config = config
        self.worker = worker
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ API actions

    def list_items(self) -> dict:
        items = self.worker.list_review_items()
        items["version"] = __version__
        return items

    def assign(self, kind: str, item_id: str, release_text: str) -> tuple[bool, str]:
        mbid, ref_kind = parse_mb_reference(release_text)
        if mbid is None:
            return False, "no MusicBrainz ID found in the input"
        if ref_kind == "unknown":
            # A bare UUID could be either; try it as a release first
            try:
                self.worker.processor.mb.release(mbid)
                ref_kind = "release"
            except MusicBrainzError:
                ref_kind = "release-group"
        release_mbid = mbid if ref_kind == "release" else None
        group_mbid = mbid if ref_kind == "release-group" else None

        if kind == "queue":
            return self.worker.assign_queue_item(
                item_id, release_mbid=release_mbid, release_group_mbid=group_mbid)
        if kind == "folder":
            return self.worker.assign_folder(
                item_id, release_mbid=release_mbid, release_group_mbid=group_mbid)
        return False, f"unknown item kind: {kind}"

    def retry(self, kind: str, item_id: str) -> tuple[bool, str]:
        if kind == "queue":
            return self.worker.retry_queue_item(item_id)
        if kind == "folder":
            return self.worker.retry_folder(item_id)
        return False, f"unknown item kind: {kind}"

    # ------------------------------------------------------------- HTTP server

    def make_server(self) -> ThreadingHTTPServer:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"printarr/{__version__}"

            def log_message(self, fmt, *args):
                log.debug("web: " + fmt, *args)

            def _send_json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, html: str) -> None:
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send_html(PAGE)
                elif self.path == "/api/items":
                    self._send_json(200, app.list_items())
                elif self.path == "/api/health":
                    self._send_json(200, {"status": "ok", "version": __version__})
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self._send_json(400, {"error": "invalid JSON body"})
                    return
                kind = str(payload.get("kind", ""))
                item_id = str(payload.get("id", ""))
                if self.path == "/api/assign":
                    ok, reason = app.assign(kind, item_id,
                                            str(payload.get("release", "")))
                elif self.path == "/api/retry":
                    ok, reason = app.retry(kind, item_id)
                else:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(200 if ok else 422,
                                {"success": ok, "reason": reason})

        server = ThreadingHTTPServer(
            (self.config.web.host, self.config.web.port), Handler)
        self.server = server
        return server

    def start_background(self) -> None:
        server = self.make_server()
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="printarr-web", daemon=True)
        self._thread.start()
        log.info("web UI listening on http://%s:%d",
                 self.config.web.host, server.server_address[1])

    def serve_forever(self) -> None:
        server = self.make_server()
        log.info("web UI listening on http://%s:%d",
                 self.config.web.host, server.server_address[1])
        server.serve_forever()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server = None


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>printarr</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #14171c; color: #dbe1ea;
         font: 15px/1.5 system-ui, sans-serif; }
  header { padding: 14px 22px; background: #1b1f26; display: flex;
           align-items: baseline; gap: 12px; border-bottom: 1px solid #2a303a; }
  header h1 { margin: 0; font-size: 19px; letter-spacing: .3px; }
  header .sub { color: #8b95a5; font-size: 13px; }
  header button { margin-left: auto; }
  main { max-width: 980px; margin: 0 auto; padding: 18px 22px 60px; }
  h2 { font-size: 15px; color: #8b95a5; text-transform: uppercase;
       letter-spacing: .8px; margin: 26px 0 10px; }
  .item { background: #1b1f26; border: 1px solid #2a303a; border-radius: 8px;
          margin-bottom: 12px; padding: 14px 16px; }
  .item .title { font-weight: 600; }
  .meta { color: #8b95a5; font-size: 13px; margin-top: 2px; word-break: break-all; }
  .warn { color: #e0a34e; }
  .cands { margin: 10px 0 0; padding: 0; list-style: none; }
  .cands li { display: flex; align-items: center; gap: 10px; padding: 6px 8px;
              border-top: 1px solid #232933; font-size: 14px; flex-wrap: wrap; }
  .score { font-variant-numeric: tabular-nums; color: #8b95a5; width: 3.2em; }
  .cands a { color: #6aa1e8; text-decoration: none; }
  .cands a:hover { text-decoration: underline; }
  button { background: #2b6cb0; border: 0; border-radius: 6px; color: #fff;
           padding: 6px 12px; font-size: 13px; cursor: pointer; }
  button:hover { background: #3a7cc4; }
  button.ghost { background: #2a303a; color: #c7cfdb; }
  button.ghost:hover { background: #353d4a; }
  button:disabled { opacity: .5; cursor: wait; }
  .assign-row { display: flex; gap: 8px; margin-top: 12px; }
  .assign-row input { flex: 1; background: #12151a; color: #dbe1ea;
                      border: 1px solid #2a303a; border-radius: 6px;
                      padding: 6px 10px; font-size: 13px; }
  .status { margin-top: 8px; font-size: 13px; }
  .status.ok { color: #6fc287; }
  .status.err { color: #e07a6a; }
  .empty { color: #8b95a5; padding: 16px 4px; }
  #banner { display: none; margin: 14px 0; padding: 10px 14px; border-radius: 8px;
            background: #3a2a2a; color: #e0a34e; font-size: 14px; }
</style>
</head>
<body>
<header>
  <h1>printarr</h1>
  <span class="sub">manual review</span>
  <button class="ghost" onclick="load()">Refresh</button>
</header>
<main>
  <div id="banner"></div>
  <h2>Stuck Lidarr downloads</h2>
  <div id="queue"></div>
  <h2>Failed watch folders</h2>
  <div id="folders"></div>
</main>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function candidateRows(item, kind, id) {
  if (!item.candidates || !item.candidates.length) return "";
  const rows = item.candidates.map(c => `
    <li><span class="score">${(c.score ?? 0).toFixed(2)}</span>
      <span>${esc(c.artist)} — ${esc(c.title)}
        ${c.date ? "(" + esc(c.date) + ")" : ""}
        ${c.tracks ? " · " + c.tracks + " tracks" : ""}
        ${c.fingerprint_hits ? " · ♪" + c.fingerprint_hits : ""}</span>
      <a href="https://musicbrainz.org/release/${esc(c.release_id)}"
         target="_blank" rel="noopener">MB↗</a>
      <button onclick="assign('${kind}', this, '${esc(c.release_id)}')">Use this</button>
    </li>`).join("");
  return `<ul class="cands">${rows}</ul>`;
}

function itemCard(item, kind) {
  const id = kind === "queue" ? item.download_id : item.path;
  const missing = kind === "queue" && !item.exists
    ? `<div class="meta warn">path not accessible from printarr — check queue.path_mappings</div>` : "";
  const messages = (item.messages || []).map(m => esc(m)).join(" · ");
  return `<div class="item" data-id="${esc(id)}">
    <div class="title">${esc(item.title)}</div>
    <div class="meta">${esc(item.reason || item.state || "")}
      ${messages ? " · " + messages : ""}</div>
    <div class="meta">${esc(item.path)}</div>
    ${missing}
    ${candidateRows(item, kind, id)}
    <div class="assign-row">
      <input placeholder="MusicBrainz release or release-group URL / MBID"
             onkeydown="if(event.key==='Enter')assign('${kind}', this)">
      <button onclick="assign('${kind}', this)">Assign</button>
      <button class="ghost" onclick="retry('${kind}', this)">Retry auto</button>
    </div>
    <div class="status"></div>
  </div>`;
}

function render(el, items, kind) {
  el.innerHTML = items.length
    ? items.map(i => itemCard(i, kind)).join("")
    : `<div class="empty">Nothing waiting for review.</div>`;
}

async function load() {
  const res = await fetch("/api/items");
  const data = await res.json();
  const banner = document.getElementById("banner");
  banner.style.display = data.error ? "block" : "none";
  banner.textContent = data.error ? "Lidarr unreachable: " + data.error : "";
  render(document.getElementById("queue"), data.queue || [], "queue");
  render(document.getElementById("folders"), data.folders || [], "folder");
}

async function post(url, body, card) {
  const status = card.querySelector(".status");
  card.querySelectorAll("button, input").forEach(b => b.disabled = true);
  status.className = "status";
  status.textContent = "Working… (fingerprinting and tagging can take a while)";
  try {
    const res = await fetch(url, {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)});
    const data = await res.json();
    status.className = "status " + (data.success ? "ok" : "err");
    status.textContent = (data.success ? "✔ " : "✖ ") + data.reason;
    if (data.success) setTimeout(load, 2500);
  } catch (err) {
    status.className = "status err";
    status.textContent = "✖ " + err;
  } finally {
    card.querySelectorAll("button, input").forEach(b => b.disabled = false);
  }
}

function assign(kind, sourceEl, releaseId) {
  const card = sourceEl.closest(".item");
  const release = releaseId || card.querySelector("input").value.trim();
  if (!release) return;
  post("/api/assign", {kind, id: card.dataset.id, release}, card);
}

function retry(kind, sourceEl) {
  const card = sourceEl.closest(".item");
  post("/api/retry", {kind, id: card.dataset.id}, card);
}

load();
setInterval(load, 60000);
</script>
</body>
</html>
"""

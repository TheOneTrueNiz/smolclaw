#!/usr/bin/env python3
"""
SmolClaw Web UI v0.9.0 — Cluster control panel, chat, and observability.
Zero dependencies beyond Python stdlib.
"""

import http.server
import json
import os
import socketserver
import sys
import threading
import urllib.request
import urllib.error
import uuid
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CONVERSATIONS_DIR = BASE_DIR / "conversations"
CONVERSATIONS_DIR.mkdir(exist_ok=True)
MEMORY_FILE = BASE_DIR / "memory.md"
FLIGHT_LOG = BASE_DIR / "flight_recorder.jsonl"
STATE_FILE = BASE_DIR / "autonomy_state.json"
PORT = 8080

# ── Agent import ───────────────────────────────────────────────────────────

sys.path.insert(0, str(BASE_DIR))
_host = os.uname().nodename
if _host == "nizbot1":
    from agent import (run_agent_aot, LLAMA_URL, CRITIC_URL, MEMORY_URL,
                        DAILY_CALL_BUDGET, DAILY_TOKEN_BUDGET,
                        assemble_context, summarize_conversation)
    _agent_label = "agent.py (local)"
else:
    try:
        from agent_hackbook import (run_agent_aot, LLAMA_URL, CRITIC_URL, MEMORY_URL,
                                     DAILY_CALL_BUDGET, DAILY_TOKEN_BUDGET,
                                     assemble_context, summarize_conversation)
        _agent_label = "agent_hackbook.py (tailscale)"
    except ImportError:
        from agent import (run_agent_aot, LLAMA_URL, CRITIC_URL, MEMORY_URL,
                            DAILY_CALL_BUDGET, DAILY_TOKEN_BUDGET,
                            assemble_context, summarize_conversation)
        _agent_label = "agent.py (fallback)"


def _health_url(chat_url):
    return chat_url.rsplit('/v1/', 1)[0] + '/health'


NUC_HEALTH = [
    ("NUC1 / Actor",  _health_url(LLAMA_URL),  "actor"),
    ("NUC2 / Critic", _health_url(CRITIC_URL), "critic"),
    ("NUC3 / Memory", _health_url(MEMORY_URL), "memory"),
]

# ── Thread safety ──────────────────────────────────────────────────────────

_agent_lock = threading.Lock()
_agent_status = {"running": False, "query": ""}

# ── Conversations ──────────────────────────────────────────────────────────


def list_conversations():
    convos = []
    for f in CONVERSATIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            convos.append({
                "id": d["id"], "title": d.get("title", "Untitled"),
                "created": d.get("created", ""), "updated": d.get("updated", ""),
                "count": len(d.get("messages", []))
            })
        except Exception:
            continue
    convos.sort(key=lambda c: c.get("updated", ""), reverse=True)
    return convos


def create_conversation():
    cid = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()
    data = {"id": cid, "title": "New chat", "created": now, "updated": now,
            "summary": "", "summary_through": 0, "messages": []}
    (CONVERSATIONS_DIR / f"{cid}.json").write_text(json.dumps(data))
    return data


def get_conversation(cid):
    p = CONVERSATIONS_DIR / f"{cid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def save_conversation(data):
    data["updated"] = datetime.now().isoformat()
    (CONVERSATIONS_DIR / f"{data['id']}.json").write_text(json.dumps(data))


def delete_conversation(cid):
    p = CONVERSATIONS_DIR / f"{cid}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def _generate_title(user_msg: str) -> str:
    """Generate a conversation title from the first message. No LLM call."""
    clean = user_msg.strip()
    for prefix in ("hey ", "hi ", "hello ", "can you ", "please ", "could you ",
                    "what is ", "what's ", "tell me "):
        if clean.lower().startswith(prefix):
            clean = clean[len(prefix):]
            break
    if clean:
        clean = clean[0].upper() + clean[1:]
    if len(clean) > 50:
        clean = clean[:47].rsplit(" ", 1)[0] + "..."
    return clean or "Chat"


# ── Cluster health ─────────────────────────────────────────────────────────


def check_health():
    results = []
    for name, url, role in NUC_HEALTH:
        entry = {"name": name, "role": role, "online": False}
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                d = json.loads(r.read())
                if d.get("status") == "ok":
                    entry["online"] = True
                    entry["model"] = d.get("model", "")
                    entry["slots_idle"] = d.get("slots_idle", 0)
                    entry["slots_processing"] = d.get("slots_processing", 0)
        except Exception:
            pass
        results.append(entry)
    return results


# ── State / logs ───────────────────────────────────────────────────────────


def read_state():
    try:
        data = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        data["call_budget"] = DAILY_CALL_BUDGET
        data["token_budget"] = DAILY_TOKEN_BUDGET
        return data
    except Exception:
        return {"error": "unreadable"}


def read_flight_log(n=15):
    try:
        if not FLIGHT_LOG.exists():
            return []
        lines = FLIGHT_LOG.read_text().strip().split('\n')
        entries = []
        for line in lines[-n:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        entries.reverse()
        return entries
    except Exception:
        return []


def read_memory():
    try:
        return MEMORY_FILE.read_text() if MEMORY_FILE.exists() else "(empty)"
    except Exception:
        return "(error)"


# ── Background Summarization ──────────────────────────────────────────────


def _trigger_background_summary(cid: str):
    """Trigger background conversation summarization on NUC3.
    Non-blocking: runs in a daemon thread after response is sent."""
    def _do_summary():
        try:
            convo = get_conversation(cid)
            if not convo:
                return
            messages = convo.get("messages", [])
            summary_through = convo.get("summary_through", 0)

            # Only summarize if 4+ unsummarized messages (2 exchanges)
            unsummarized = len(messages) - summary_through
            if unsummarized < 4:
                return

            # Summarize all but the last 2 messages (keep 1 exchange raw)
            to_summarize = messages[summary_through:-2] if len(messages) > summary_through + 2 else []
            if not to_summarize:
                return

            prev_summary = convo.get("summary", "")

            print(f"[summary] generating for {cid} ({len(to_summarize)} msgs)...", flush=True)
            new_summary = summarize_conversation(prev_summary, to_summarize)

            if new_summary:
                # Re-read to handle concurrent writes
                convo = get_conversation(cid)
                if convo:
                    convo["summary"] = new_summary
                    convo["summary_through"] = len(convo["messages"]) - 2
                    save_conversation(convo)
                    print(f"[summary] saved for {cid}: {new_summary[:80]}...")
        except Exception as e:
            print(f"[summary] error for {cid}: {e}")

    thread = threading.Thread(target=_do_summary, daemon=True)
    thread.start()


# ── HTTP Handler ───────────────────────────────────────────────────────────


class Handler(http.server.BaseHTTPRequestHandler):

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        elif self.path == '/api/health':
            self._json(check_health())
        elif self.path == '/api/state':
            self._json(read_state())
        elif self.path == '/api/conversations':
            self._json(list_conversations())
        elif self.path.startswith('/api/conversations/'):
            cid = self.path.split('/')[-1]
            d = get_conversation(cid)
            self._json(d if d else {"error": "not found"}, 200 if d else 404)
        elif self.path == '/api/flight-recorder':
            self._json(read_flight_log())
        elif self.path == '/api/memory':
            self._json({"content": read_memory()})
        elif self.path == '/api/agent-status':
            self._json(_agent_status)
        else:
            self.send_response(404)
            self.end_headers()

    def _sse_event(self, event_type, data):
        """Send a single SSE event."""
        payload = json.dumps({"type": event_type, **data})
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_POST(self):
        if self.path == '/chat':
            body = self._body()
            msg = body.get('message', '').strip()
            cid = body.get('conversation_id', '')
            if not msg:
                self._json({"error": "empty"}, 400)
                return
            # Smart context assembly: summary + recent raw messages
            history = []
            convo = None
            if cid:
                convo = get_conversation(cid)
                if convo and convo.get("messages"):
                    try:
                        summary = convo.get("summary", "")
                        summary_through = convo.get("summary_through", 0)
                        history = assemble_context(
                            msg, summary, convo["messages"], summary_through
                        )
                    except Exception:
                        # Graceful degradation: naive truncation
                        recent = convo["messages"][-6:]
                        for m in recent:
                            history.append({"role": m["role"], "content": m["content"][:500]})

            # Set up SSE streaming response
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            self._sse_event("status", {"text": "thinking..."})

            def on_token(token):
                """Called per-token during streaming synthesis."""
                try:
                    self._sse_event("token", {"text": token})
                except Exception:
                    pass  # client disconnected

            with _agent_lock:
                _agent_status["running"] = True
                _agent_status["query"] = msg[:80]
                try:
                    print(f"[web] query: {msg[:80]} (history: {len(history)} turns)")
                    response = run_agent_aot(msg, history=history, on_token=on_token)
                    print(f"[web] response: {response[:80]}")
                finally:
                    _agent_status["running"] = False
                    _agent_status["query"] = ""

            # Send final response (includes any critic corrections)
            self._sse_event("done", {"response": response})

            # Save to conversation
            if cid:
                convo = get_conversation(cid)
                if convo:
                    ts = datetime.now().isoformat()
                    convo["messages"].append({"role": "user", "content": msg, "timestamp": ts})
                    convo["messages"].append({"role": "assistant", "content": response, "timestamp": ts})
                    if convo["title"] == "New chat":
                        convo["title"] = _generate_title(msg)
                    save_conversation(convo)
                    # Trigger background summarization (non-blocking)
                    _trigger_background_summary(cid)

        elif self.path == '/api/conversations':
            self._json(create_conversation())

        elif self.path.startswith('/api/conversations/') and self.path.endswith('/delete'):
            cid = self.path.split('/')[-2]
            self._json({"ok": delete_conversation(cid)})

        elif self.path == '/api/state/reset':
            try:
                if STATE_FILE.exists():
                    s = json.loads(STATE_FILE.read_text())
                    s.update({"daily_calls": 0, "daily_tokens": 0,
                              "recent_failures": 0, "consecutive_failures": 0})
                    STATE_FILE.write_text(json.dumps(s))
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ── HTML ───────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmolClaw</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  :root {
    --bg:#0a0a0a; --panel:#111; --card:#1a1a1a; --border:#2a2a2a;
    --accent:#ff6b35; --accent2:#ff8c5a;
    --green:#4ade80; --red:#f87171; --yellow:#facc15;
    --text:#e0e0e0; --muted:#666;
    --user-bg:#1a3a5c; --user-text:#8cc8ff;
  }
  body {
    font-family:'SF Mono','Fira Code','Cascadia Code','Consolas',monospace;
    background:var(--bg); color:var(--text);
    height:100vh; overflow:hidden;
    display:grid;
    grid-template-rows:48px 1fr;
    grid-template-columns:220px 1fr 280px;
    grid-template-areas:"header header header" "sidebar chat cluster";
  }

  /* Header */
  #header {
    grid-area:header; background:var(--panel);
    border-bottom:1px solid var(--border);
    display:flex; align-items:center; padding:0 16px; gap:12px;
  }
  #header h1 { font-size:15px; color:var(--accent); white-space:nowrap; }
  .version { color:var(--muted); font-size:11px; font-weight:normal; }
  .header-dots { display:flex; gap:6px; margin-left:4px; }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--muted); }
  .dot.green { background:var(--green); }
  .dot.red { background:var(--red); }
  .spacer { flex:1; }
  .header-status { font-size:11px; color:var(--muted); }
  .menu-btn {
    background:none; border:none; color:var(--text);
    font-size:18px; cursor:pointer; padding:4px 8px; display:none;
  }

  /* Left sidebar */
  #sidebar {
    grid-area:sidebar; background:var(--panel);
    border-right:1px solid var(--border);
    display:flex; flex-direction:column; overflow:hidden;
  }
  .new-chat-btn {
    margin:10px; padding:10px; background:var(--accent); color:#000;
    border:none; border-radius:6px; font-family:inherit;
    font-size:13px; font-weight:bold; cursor:pointer;
  }
  .new-chat-btn:hover { background:var(--accent2); }
  #convo-list { flex:1; overflow-y:auto; }
  .convo-item {
    padding:10px 12px; border-bottom:1px solid var(--bg);
    cursor:pointer; font-size:12px; color:var(--muted);
    display:flex; align-items:center;
  }
  .convo-item:hover { background:var(--card); }
  .convo-item.active { background:var(--user-bg); color:var(--user-text); }
  .convo-title { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .convo-delete {
    background:none; border:none; color:var(--muted);
    cursor:pointer; font-size:14px; padding:0 4px; opacity:0;
  }
  .convo-item:hover .convo-delete { opacity:1; }
  .convo-count { font-size:10px; color:var(--muted); margin-left:6px; }

  /* Chat */
  #chat { grid-area:chat; display:flex; flex-direction:column; overflow:hidden; }
  #messages {
    flex:1; overflow-y:auto; padding:16px;
    display:flex; flex-direction:column; gap:12px;
  }
  .msg {
    max-width:80%; padding:10px 14px; border-radius:10px;
    line-height:1.5; font-size:13px; white-space:pre-wrap; word-wrap:break-word;
  }
  .msg.user {
    align-self:flex-end; background:var(--user-bg); color:var(--user-text);
    border-bottom-right-radius:3px;
  }
  .msg.assistant {
    align-self:flex-start; background:var(--card);
    border:1px solid var(--border); border-bottom-left-radius:3px;
  }
  .msg.assistant a { color:#58a6ff; text-decoration:underline; word-break:break-all; }
  .msg.assistant a:hover { color:#79c0ff; }
  .msg.thinking {
    align-self:flex-start; background:var(--card);
    border:1px solid var(--border); color:var(--muted); font-style:italic;
  }
  .msg.system { align-self:center; color:var(--muted); font-size:11px; background:none; }
  #input-area {
    background:var(--panel); border-top:1px solid var(--border);
    padding:12px 16px; display:flex; gap:10px;
  }
  #input {
    flex:1; background:var(--card); border:1px solid #333;
    border-radius:6px; color:var(--text); padding:10px 14px;
    font-family:inherit; font-size:13px; outline:none; resize:none;
  }
  #input:focus { border-color:var(--accent); }
  #send {
    background:var(--accent); color:#000; border:none; border-radius:6px;
    padding:10px 20px; font-family:inherit; font-size:13px;
    font-weight:bold; cursor:pointer;
  }
  #send:hover { background:var(--accent2); }
  #send:disabled { background:#333; color:var(--muted); cursor:not-allowed; }

  /* Right sidebar */
  #cluster {
    grid-area:cluster; background:var(--panel);
    border-left:1px solid var(--border);
    overflow-y:auto; padding:12px;
    display:flex; flex-direction:column; gap:16px;
  }
  .ph { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
  .ph h3 { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; }
  .rbtn {
    background:none; border:none; color:var(--muted);
    cursor:pointer; font-size:14px; padding:2px 6px;
  }
  .rbtn:hover { color:var(--text); }

  /* Health cards */
  .hcard {
    display:flex; align-items:center; gap:10px;
    padding:8px 10px; background:var(--card); border-radius:6px;
    border-left:3px solid var(--muted); margin-bottom:4px;
  }
  .hcard.online { border-left-color:var(--green); }
  .hcard.offline { border-left-color:var(--red); }
  .hdot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
  .hdot.green { background:var(--green); }
  .hdot.red { background:var(--red); }
  .hname { font-size:12px; font-weight:bold; }
  .hdetail { font-size:10px; color:var(--muted); }

  /* Gauges */
  .gi { margin-bottom:8px; }
  .gl { font-size:11px; color:var(--muted); margin-bottom:4px; }
  .gbar { background:var(--card); border-radius:4px; height:6px; overflow:hidden; }
  .gfill { height:100%; border-radius:4px; background:var(--accent); transition:width .3s; }
  .gfill.warn { background:var(--yellow); }
  .gfill.danger { background:var(--red); }
  .gmeta { font-size:10px; color:var(--muted); margin-top:4px; }
  .abtn {
    background:var(--card); border:1px solid var(--border); color:var(--muted);
    border-radius:4px; padding:6px 10px; font-family:inherit;
    font-size:11px; cursor:pointer; margin-top:4px; width:100%;
  }
  .abtn:hover { color:var(--text); border-color:#444; }

  /* Flight log */
  .le {
    display:flex; align-items:center; gap:6px;
    padding:4px 0; font-size:11px; border-bottom:1px solid var(--bg);
  }
  .lt { color:var(--accent); min-width:55px; font-size:10px; }
  .la { flex:1; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:10px; }
  .lok { color:var(--green); font-size:10px; }
  .lfail { color:var(--red); font-size:10px; }
  .ltime { color:var(--muted); font-size:9px; }

  /* Memory */
  .membox {
    background:var(--card); border-radius:6px; padding:10px;
    font-size:11px; max-height:200px; overflow-y:auto;
    white-space:pre-wrap; color:var(--muted);
  }

  /* Overlay */
  #overlay {
    display:none; position:fixed; top:0;left:0;right:0;bottom:0;
    background:rgba(0,0,0,.5); z-index:99;
  }
  #overlay.vis { display:block; }

  /* Responsive */
  @media(max-width:900px) {
    body { grid-template-columns:1fr; grid-template-areas:"header" "chat"; }
    .menu-btn { display:block; }
    #sidebar {
      position:fixed; top:48px; left:0; bottom:0; width:260px;
      z-index:100; transform:translateX(-100%); transition:transform .2s;
    }
    #sidebar.open { transform:translateX(0); }
    #cluster {
      position:fixed; top:48px; right:0; bottom:0; width:280px;
      z-index:100; transform:translateX(100%); transition:transform .2s;
    }
    #cluster.open { transform:translateX(0); }
    .msg { max-width:95%; }
    .convo-delete { opacity:1; }
  }

  ::-webkit-scrollbar { width:6px; }
  ::-webkit-scrollbar-track { background:transparent; }
  ::-webkit-scrollbar-thumb { background:#333; border-radius:3px; }
</style>
</head>
<body>

<div id="header">
  <button class="menu-btn" onclick="toggleSidebar()">&#9776;</button>
  <h1>SmolClaw <span class="version">v0.9.0</span></h1>
  <div class="header-dots">
    <span class="dot" id="dot-actor" title="NUC1/Actor"></span>
    <span class="dot" id="dot-critic" title="NUC2/Critic"></span>
    <span class="dot" id="dot-memory" title="NUC3/Memory"></span>
  </div>
  <div class="spacer"></div>
  <span class="header-status" id="agent-status"></span>
  <button class="menu-btn" onclick="toggleCluster()">&#8862;</button>
</div>

<aside id="sidebar">
  <button class="new-chat-btn" onclick="newChat()">+ New Chat</button>
  <div id="convo-list"></div>
</aside>

<main id="chat">
  <div id="messages">
    <div class="msg system">Start a new chat or select a conversation.</div>
  </div>
  <div id="input-area">
    <textarea id="input" rows="1" placeholder="Talk to SmolClaw..." autofocus></textarea>
    <button id="send" onclick="sendMsg()">Send</button>
  </div>
</main>

<aside id="cluster">
  <div>
    <div class="ph"><h3>Cluster Health</h3><button class="rbtn" onclick="refreshHealth()" title="Refresh">&#8635;</button></div>
    <div id="health-cards">Loading...</div>
  </div>
  <div>
    <div class="ph"><h3>Budgets</h3></div>
    <div id="budget-gauges">Loading...</div>
  </div>
  <div>
    <div class="ph"><h3>Recent Activity</h3><button class="rbtn" onclick="refreshFlightLog()" title="Refresh">&#8635;</button></div>
    <div id="flight-log"></div>
  </div>
  <div>
    <div class="ph"><h3>Memory</h3><button class="rbtn" onclick="refreshMemory()" title="Refresh">&#8635;</button></div>
    <div id="memory-view"></div>
  </div>
</aside>

<div id="overlay" onclick="closeDrawers()"></div>

<script>
let currentConvoId = null;

// === Init ===
async function init() {
  refreshHealth(); refreshState(); refreshFlightLog(); refreshMemory();
  loadConversations();
  setInterval(() => { refreshHealth(); refreshState(); }, 30000);

  const input = document.getElementById('input');
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  });
}

// === Health ===
async function refreshHealth() {
  try {
    const nucs = await (await fetch('/api/health')).json();
    document.getElementById('health-cards').innerHTML = nucs.map(n => `
      <div class="hcard ${n.online?'online':'offline'}">
        <div class="hdot ${n.online?'green':'red'}"></div>
        <div>
          <div class="hname">${n.name}</div>
          <div class="hdetail">${n.online ? (n.slots_idle+n.slots_processing)+' slot(s)' : 'OFFLINE'}</div>
        </div>
      </div>`).join('');
    ['actor','critic','memory'].forEach((r,i) => {
      const d = document.getElementById('dot-'+r);
      if (d) d.className = 'dot '+(nucs[i].online?'green':'red');
    });
  } catch(e) { document.getElementById('health-cards').innerHTML = 'Error'; }
}

// === Budgets ===
async function refreshState() {
  try {
    const s = await (await fetch('/api/state')).json();
    if (s.error) return;
    const cp = Math.min(100,((s.daily_calls||0)/s.call_budget)*100);
    const tp = Math.min(100,((s.daily_tokens||0)/s.token_budget)*100);
    const cc = cp>80?'danger':cp>50?'warn':'';
    const tc = tp>80?'danger':tp>50?'warn':'';
    document.getElementById('budget-gauges').innerHTML = `
      <div class="gi">
        <div class="gl">Calls: ${s.daily_calls||0} / ${s.call_budget}</div>
        <div class="gbar"><div class="gfill ${cc}" style="width:${cp}%"></div></div>
      </div>
      <div class="gi">
        <div class="gl">Tokens: ${((s.daily_tokens||0)/1000).toFixed(1)}K / ${(s.token_budget/1000).toFixed(0)}K</div>
        <div class="gbar"><div class="gfill ${tc}" style="width:${tp}%"></div></div>
      </div>
      <div class="gmeta">Lifetime: ${s.total_calls||0} calls</div>
      <button class="abtn" onclick="resetBudgets()">Reset Daily Budgets</button>`;
  } catch(e) {}
}

async function resetBudgets() {
  if (!confirm('Reset daily call and token counters?')) return;
  await fetch('/api/state/reset', {method:'POST'});
  refreshState();
}

// === Conversations ===
async function loadConversations() {
  try {
    const convos = await (await fetch('/api/conversations')).json();
    const el = document.getElementById('convo-list');
    if (!convos.length) {
      el.innerHTML = '<div style="padding:12px;font-size:11px;color:#666">No conversations yet</div>';
      return;
    }
    el.innerHTML = convos.map(c => `
      <div class="convo-item ${c.id===currentConvoId?'active':''}" onclick="loadConvo('${c.id}')">
        <span class="convo-title">${esc(c.title)}</span>
        <span class="convo-count">${c.count}</span>
        <button class="convo-delete" onclick="event.stopPropagation();deleteConvo('${c.id}')" title="Delete">&#10005;</button>
      </div>`).join('');
  } catch(e) {}
}

async function newChat() {
  try {
    const convo = await (await fetch('/api/conversations', {method:'POST'})).json();
    currentConvoId = convo.id;
    document.getElementById('messages').innerHTML =
      '<div class="msg assistant">Hey, I am SmolClaw. I run on three NUCs and think with a 3B brain. Ask me anything.</div>';
    loadConversations();
    closeDrawers();
    document.getElementById('input').focus();
  } catch(e) {}
}

async function loadConvo(id) {
  try {
    const convo = await (await fetch('/api/conversations/'+id)).json();
    if (convo.error) return;
    currentConvoId = convo.id;
    const msgs = document.getElementById('messages');
    if (!convo.messages.length) {
      msgs.innerHTML = '<div class="msg assistant">Hey, I am SmolClaw. I run on three NUCs and think with a 3B brain. Ask me anything.</div>';
    } else {
      msgs.innerHTML = convo.messages.map(m => `<div class="msg ${m.role}">${m.role === 'assistant' ? linkify(m.content) : esc(m.content)}</div>`).join('');
    }
    msgs.scrollTop = msgs.scrollHeight;
    loadConversations();
    closeDrawers();
  } catch(e) {}
}

async function deleteConvo(id) {
  if (!confirm('Delete this conversation?')) return;
  await fetch('/api/conversations/'+id+'/delete', {method:'POST'});
  if (currentConvoId === id) {
    currentConvoId = null;
    document.getElementById('messages').innerHTML = '<div class="msg system">Start a new chat or select a conversation.</div>';
  }
  loadConversations();
}

// === Chat ===
async function sendMsg() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text) return;

  if (!currentConvoId) {
    const convo = await (await fetch('/api/conversations', {method:'POST'})).json();
    currentConvoId = convo.id;
    document.getElementById('messages').innerHTML = '';
    loadConversations();
  }

  const msgs = document.getElementById('messages');
  const sendBtn = document.getElementById('send');
  const statusEl = document.getElementById('agent-status');

  const userDiv = document.createElement('div');
  userDiv.className = 'msg user';
  userDiv.textContent = text;
  msgs.appendChild(userDiv);

  const thinkDiv = document.createElement('div');
  thinkDiv.className = 'msg thinking';
  thinkDiv.textContent = 'thinking...';
  msgs.appendChild(thinkDiv);

  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;
  statusEl.textContent = 'thinking...';
  msgs.scrollTop = msgs.scrollHeight;

  let dots = 0;
  const ti = setInterval(() => {
    dots = (dots+1)%4;
    thinkDiv.textContent = 'thinking'+'.'.repeat(dots);
    statusEl.textContent = 'thinking'+'.'.repeat(dots);
  }, 400);

  try {
    const res = await fetch('/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text, conversation_id:currentConvoId})
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let streamedText = '';
    let streaming = false;
    let sseBuffer = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      sseBuffer += decoder.decode(value, {stream: true});

      let boundary;
      while ((boundary = sseBuffer.indexOf('\n\n')) !== -1) {
        const raw = sseBuffer.slice(0, boundary).trim();
        sseBuffer = sseBuffer.slice(boundary + 2);
        if (!raw.startsWith('data: ')) continue;
        let evt;
        try { evt = JSON.parse(raw.slice(6)); } catch(e) { continue; }

        if (evt.type === 'status') {
          if (!streaming) {
            thinkDiv.textContent = evt.text;
            statusEl.textContent = evt.text;
          }
        } else if (evt.type === 'token') {
          if (!streaming) {
            streaming = true;
            clearInterval(ti);
            thinkDiv.className = 'msg assistant';
            thinkDiv.style.fontStyle = 'normal';
            thinkDiv.innerHTML = '';
            statusEl.textContent = 'streaming...';
          }
          streamedText += evt.text;
          thinkDiv.innerHTML = linkify(streamedText);
          msgs.scrollTop = msgs.scrollHeight;
        } else if (evt.type === 'done') {
          clearInterval(ti);
          thinkDiv.className = 'msg assistant';
          thinkDiv.style.fontStyle = 'normal';
          thinkDiv.innerHTML = linkify(evt.response);
          statusEl.textContent = '';
          loadConversations();
          refreshState();
          refreshFlightLog();
        }
      }
    }
    // If no 'done' event was received (fallback), finalize
    if (statusEl.textContent === 'streaming...') {
      statusEl.textContent = '';
      loadConversations();
      refreshState();
      refreshFlightLog();
    }
  } catch(err) {
    clearInterval(ti);
    thinkDiv.className = 'msg assistant';
    thinkDiv.textContent = 'Error: '+err.message;
    thinkDiv.style.color = 'var(--red)';
    statusEl.textContent = '';
  }

  sendBtn.disabled = false;
  msgs.scrollTop = msgs.scrollHeight;
  input.focus();
}

// === Flight Log ===
async function refreshFlightLog() {
  try {
    const entries = await (await fetch('/api/flight-recorder')).json();
    const el = document.getElementById('flight-log');
    if (!entries.length) { el.innerHTML = '<div class="hdetail">No activity yet</div>'; return; }
    el.innerHTML = entries.slice(0,12).map(e => {
      const t = e.ts ? e.ts.split('T')[1]?.substring(0,5)||'' : '';
      const a = e.args ? (typeof e.args==='string'?e.args:JSON.stringify(e.args)).substring(0,25) : '';
      return `<div class="le">
        <span class="lt">${esc(e.tool||'?')}</span>
        <span class="la" title="${esc(a)}">${esc(a)}</span>
        <span class="${e.ok!==false?'lok':'lfail'}">${e.ok!==false?'OK':'FAIL'}</span>
        <span class="ltime">${t}</span>
      </div>`;
    }).join('');
  } catch(e) { document.getElementById('flight-log').innerHTML = 'Error'; }
}

// === Memory ===
async function refreshMemory() {
  try {
    const data = await (await fetch('/api/memory')).json();
    document.getElementById('memory-view').innerHTML = `<div class="membox">${esc(data.content)}</div>`;
  } catch(e) { document.getElementById('memory-view').innerHTML = 'Error'; }
}

// === Mobile drawers ===
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('cluster').classList.remove('open');
  document.getElementById('overlay').classList.toggle('vis',
    document.getElementById('sidebar').classList.contains('open'));
}
function toggleCluster() {
  document.getElementById('cluster').classList.toggle('open');
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('overlay').classList.toggle('vis',
    document.getElementById('cluster').classList.contains('open'));
}
function closeDrawers() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('cluster').classList.remove('open');
  document.getElementById('overlay').classList.remove('vis');
}

// === Util ===
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function linkify(s) {
  // Escape first for XSS safety, then convert URLs to clickable links
  var safe = esc(s);
  return safe.replace(/(https?:\/\/[^\s<>"&]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

window.addEventListener('load', init);
</script>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print(f"""
    ╔═══════════════════════════════════════════╗
    ║  SmolClaw Web UI v0.9.0                   ║
    ║  http://0.0.0.0:{PORT}                      ║
    ║  Ctrl+C to stop                           ║
    ╚═══════════════════════════════════════════╝
    Agent: {_agent_label}
    """)
    server = ThreadedServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == '__main__':
    main()

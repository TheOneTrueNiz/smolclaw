#!/usr/bin/env python3
"""
SmolClaw Web UI — Lightweight chat interface for the SmolClaw cluster.
Zero dependencies beyond Python stdlib. Runs on the Hackbook (nizbot0).
Talks to agent.py which talks to the 3-NUC cluster.
"""

import http.server
import json
import threading
import sys
import os

# Add smolclaw dir to path so we can import agent
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent import run_agent_aot

PORT = 8080

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmolClaw</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: #0a0a0a; color: #e0e0e0; height: 100vh;
    display: flex; flex-direction: column;
  }
  #header {
    background: #111; border-bottom: 1px solid #2a2a2a; padding: 12px 20px;
    display: flex; align-items: center; gap: 12px;
  }
  #header h1 { font-size: 16px; color: #ff6b35; }
  #header .status { font-size: 11px; color: #666; }
  #header .status .online { color: #4ade80; }
  #messages {
    flex: 1; overflow-y: auto; padding: 20px;
    display: flex; flex-direction: column; gap: 16px;
  }
  .msg {
    max-width: 80%; padding: 12px 16px; border-radius: 12px;
    line-height: 1.5; font-size: 14px; white-space: pre-wrap;
    word-wrap: break-word;
  }
  .msg.user {
    align-self: flex-end; background: #1a3a5c; color: #8cc8ff;
    border-bottom-right-radius: 4px;
  }
  .msg.assistant {
    align-self: flex-start; background: #1a1a1a; color: #e0e0e0;
    border: 1px solid #2a2a2a; border-bottom-left-radius: 4px;
  }
  .msg.thinking {
    align-self: flex-start; background: #1a1a1a; color: #666;
    border: 1px solid #2a2a2a; font-style: italic;
  }
  #input-area {
    background: #111; border-top: 1px solid #2a2a2a; padding: 16px 20px;
    display: flex; gap: 12px;
  }
  #input {
    flex: 1; background: #1a1a1a; border: 1px solid #333; border-radius: 8px;
    color: #e0e0e0; padding: 12px 16px; font-family: inherit; font-size: 14px;
    outline: none; resize: none;
  }
  #input:focus { border-color: #ff6b35; }
  #send {
    background: #ff6b35; color: #000; border: none; border-radius: 8px;
    padding: 12px 24px; font-family: inherit; font-size: 14px;
    font-weight: bold; cursor: pointer;
  }
  #send:hover { background: #ff8c5a; }
  #send:disabled { background: #333; color: #666; cursor: not-allowed; }
  .cluster-info { font-size: 11px; color: #444; text-align: center; padding: 8px; }
</style>
</head>
<body>
<div id="header">
  <h1>SmolClaw v0.8.0</h1>
  <span class="status">
    3-NUC Cognitive Cluster &middot;
    <span class="online">ONLINE</span>
  </span>
</div>
<div id="messages">
  <div class="msg assistant">Hey, I'm SmolClaw. I run on three NUCs and think with a 3B brain. Ask me anything.</div>
</div>
<div id="input-area">
  <textarea id="input" rows="1" placeholder="Talk to SmolClaw..." autofocus></textarea>
  <button id="send" onclick="sendMsg()">Send</button>
</div>
<div class="cluster-info">
  nizbot1 (Actor) &middot; nizbot2 (Critic) &middot; nizbot3 (Memory) &middot; $75 cluster
</div>
<script>
const messages = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');

input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});

// Auto-resize textarea
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 120) + 'px';
});

async function sendMsg() {
  const text = input.value.trim();
  if (!text) return;

  // Show user message
  const userDiv = document.createElement('div');
  userDiv.className = 'msg user';
  userDiv.textContent = text;
  messages.appendChild(userDiv);

  // Show thinking indicator
  const thinkDiv = document.createElement('div');
  thinkDiv.className = 'msg thinking';
  thinkDiv.textContent = 'thinking...';
  messages.appendChild(thinkDiv);

  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;
  messages.scrollTop = messages.scrollHeight;

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
    const data = await res.json();

    // Replace thinking with response
    thinkDiv.className = 'msg assistant';
    thinkDiv.textContent = data.response;
    thinkDiv.style.fontStyle = 'normal';
  } catch (err) {
    thinkDiv.className = 'msg assistant';
    thinkDiv.textContent = 'Error: ' + err.message;
    thinkDiv.style.color = '#ff4444';
  }

  sendBtn.disabled = false;
  messages.scrollTop = messages.scrollHeight;
  input.focus();
}
</script>
</body>
</html>"""


class SmolClawHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode())

    def do_POST(self):
        if self.path == '/chat':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            message = body.get('message', '')

            # Run SmolClaw agent (this calls all 3 NUCs)
            print(f"\n[web] query: {message[:80]}")
            response = run_agent_aot(message)
            print(f"[web] response: {response[:80]}")

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'response': response}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress default HTTP logging


def main():
    print(f"""
    ╔═══════════════════════════════════════════╗
    ║  SmolClaw Web UI                          ║
    ║  http://localhost:{PORT}                    ║
    ║  Ctrl+C to stop                           ║
    ╚═══════════════════════════════════════════╝
    """)
    server = http.server.HTTPServer(('0.0.0.0', PORT), SmolClawHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == '__main__':
    main()

"""
dsh_bridge — talk to a live dsh web instance directly from a ComfyUI node.

Channel (reverse-engineered from the dsh web UI traffic):

    POST http://127.0.0.1:<port>/api/session/prompt
    {"type":"client-request","rpcId":<uuid>,"method":"session/prompt",
     "payload":{"args":{"request":{"requestId":<uuid>,"sessionId":"session-...",
        "mode":"queue","content":[{"type":"text","text":"..."}],
        "clientTimeZone":"Asia/Shanghai"}}}}

Auth: visit the launch URL in ~/.dsh/dsh-web-<port>.state.json once with a
cookie jar — the server sets a session cookie, afterwards plain POSTs work.

Reply tracking (no websocket needed): after sending, poll the session log
(session.v3.jsonl.zstd) until the turn that contains OUR user message
(matched by source.rpcId == requestId) reaches turn/end. This is race-free
even when the message was queued behind another running task.
"""

from __future__ import annotations

import glob
import http.cookiejar
import json
import os
import time
import urllib.request
import uuid

from .dsh_reader import default_sessions_root, read_log_text

STATE_GLOB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-web-*.state.json")


def load_state(state_path: str = "") -> dict:
    """Newest dsh web state file -> {port, pid, url, ...}."""
    if state_path and os.path.isfile(state_path):
        path = state_path
    else:
        files = glob.glob(STATE_GLOB)
        if not files:
            raise RuntimeError(
                "dsh: no state file matching %s — is 'dsh web' running?" % STATE_GLOB
            )
        files.sort(key=os.path.getmtime, reverse=True)
        path = files[0]
    with open(path, "r", encoding="utf-8-sig") as fh:  # dsh writes a BOM
        st = json.load(fh)
    st["_state_path"] = path
    return st


class DshWebClient:
    """Minimal JSON-RPC client for the dsh web server."""

    def __init__(self, state_path: str = "", base_override: str = ""):
        st = load_state(state_path)
        self.base = base_override or ("http://127.0.0.1:%s" % st.get("port", 3080))
        self.state = st
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        # handshake: the launch URL carries the one-time token and sets a cookie
        launch = st.get("url") or self.base
        with self._opener.open(launch, timeout=20) as resp:
            resp.read()

    def rpc(self, method: str, args: dict, timeout: float = 30.0):
        url = "%s/api/%s" % (self.base, method)
        body = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": {"args": args},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        result = data.get("result") or {}
        if not result.get("ok"):
            raise RuntimeError(
                "dsh rpc %s failed: %s" % (method, json.dumps(data, ensure_ascii=False)[:400])
            )
        return result.get("value")

    def list_sessions(self):
        value = self.rpc("session/list", {"_request": {}})
        return (value or {}).get("items") or []

    def create_session(self, cwd: str = "", agent_preset: str = "") -> dict:
        """Create a fresh (blank) session. Returns {"sessionId": ...}."""
        req = {}
        if cwd:
            req["cwd"] = cwd
        if agent_preset:
            req["agentPreset"] = agent_preset
        return self.rpc("session/create", {"request": req}) or {}

    def pick_session(self, session_id: str = "") -> dict:
        """Explicit id wins; otherwise newest non-blank session."""
        items = self.list_sessions()
        if session_id:
            for it in items:
                if it.get("sessionId") == session_id:
                    return it
            # unknown id — still honor it (log may exist on disk)
            return {"sessionId": session_id, "pinned_unknown": True}
        usable = [it for it in items if not it.get("blank")]
        if not usable:
            usable = items
        if not usable:
            raise RuntimeError("dsh: no sessions at all — open one in the web UI first")
        usable.sort(key=lambda it: it.get("updatedAt") or 0, reverse=True)
        return usable[0]

    def send_prompt(self, session_id: str, text: str, mode: str = "queue") -> str:
        """Queue a user message. Returns the requestId used as our anchor."""
        request_id = str(uuid.uuid4())
        self.rpc(
            "session/prompt",
            {
                "request": {
                    "requestId": request_id,
                    "sessionId": session_id,
                    "mode": mode,
                    "content": [{"type": "text", "text": text}],
                    "clientTimeZone": "Asia/Shanghai",
                }
            },
        )
        return request_id


# ---------------------------------------------------------------------------
# session log: locate + wait for OUR turn to finish
# ---------------------------------------------------------------------------

def find_session_file(session_id: str, sessions_root: str = "") -> str:
    root = sessions_root or default_sessions_root()
    hits = glob.glob(os.path.join(root, "*", session_id, "session.v3.jsonl.zstd"))
    if hits:
        return hits[0]
    hits = glob.glob(os.path.join(root, "*", session_id, "*.jsonl*"))
    return hits[0] if hits else ""


def _records(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _turn_text(text: str, turn, include_reasoning: bool = False) -> str:
    parts = []
    for rec in _records(text):
        if rec.get("type") != "assistant/message":
            continue
        data = rec.get("data") or {}
        if data.get("turn") != turn:
            continue
        for block in (data.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            body = block.get("text") or ""
            if kind == "text" or (kind == "reasoning" and include_reasoning):
                if body:
                    parts.append(body)
    return "\n".join(parts)


def wait_reply(path: str, request_id: str, timeout: float = 120.0,
               poll: float = 1.5, include_reasoning: bool = False):
    """Block until the turn containing our message finishes. Returns (reply, info)."""
    deadline = time.time() + max(5.0, timeout)
    last_err = ""
    while time.time() < deadline:
        try:
            text = read_log_text(path)
        except Exception as exc:  # file being rewritten mid-flush etc.
            last_err = str(exc)
            time.sleep(poll)
            continue

        user_seq = None
        for rec in _records(text):
            if rec.get("type") != "user/message":
                continue
            source = (rec.get("data") or {}).get("source") or {}
            if source.get("rpcId") == request_id:
                s = rec.get("seq")
                if isinstance(s, int) and (user_seq is None or s > user_seq):
                    user_seq = s

        if user_seq is not None:
            for rec in _records(text):
                if rec.get("type") != "turn/end":
                    continue
                seq = rec.get("seq")
                if not isinstance(seq, int) or seq <= user_seq:
                    continue
                data = rec.get("data") or {}
                turn = data.get("turn")
                reason = data.get("reason")
                kind = reason.get("kind") if isinstance(reason, dict) else reason
                reply = _turn_text(text, turn, include_reasoning)
                info = {"turn": turn, "reason": kind, "end_seq": seq,
                        "user_seq": user_seq}
                if kind == "completed":
                    return reply, info
                raise RuntimeError(
                    "dsh: turn %s ended with reason %r (not completed); "
                    "partial text: %s" % (turn, kind, reply[:200])
                )
        time.sleep(poll)

    raise TimeoutError(
        "dsh: no completed reply within %ss%s" % (
            timeout, (" | last read error: " + last_err) if last_err else "")
    )

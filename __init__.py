"""
ComfyUI-DSH-Receiver

在 ComfyUI 里直接和 dsh（DeepSeek Harness）对话：节点面板上打字 → 发送 →
等 dsh 回复 → 抓出 {【…】} 包裹的内容 → 喂给提示词节点生图。

节点
----
DSH Talk            : 聊天面板（发消息 + 等回复 + 抓取），主力节点
DSH Reply Extractor : 只读最新回复并抓取（不发消息）
DSH Latest Reply    : 只输出最新回复原文
"""

from __future__ import annotations

import asyncio
import json

from .dsh_reader import (
    default_sessions_root,
    extract_matches,
    find_latest_session,
    latest_reply,
    load_latest,
    signature,
    to_int,
)
from .dsh_bridge import (
    DshWebClient,
    find_session_file,
    wait_reply,
)

NODE_CATEGORY = "dsh"
WEB_DIRECTORY = "./web"


# ---------------------------------------------------------------------------
# shared logic (used by the node AND the HTTP API the panel talks to)
# ---------------------------------------------------------------------------

def _session_title(item: dict) -> str:
    try:
        return ((item.get("projections") or {}).get("values") or {}).get("title") or ""
    except Exception:
        return ""


def _wait_session_file(sid: str, timeout: float = 10.0) -> str:
    """Fresh sessions flush their log slightly after creation — poll briefly."""
    import time
    deadline = time.time() + timeout
    path = find_session_file(sid)
    while not path and time.time() < deadline:
        time.sleep(0.5)
        path = find_session_file(sid)
    return path


def do_talk(message: str, session_id: str = "", timeout: float = 120.0,
            include_reasoning: bool = False) -> dict:
    """Send one message and block until dsh finishes that turn."""
    client = DshWebClient()
    target = client.pick_session(session_id or "")
    sid = target.get("sessionId") or ""
    log_path = _wait_session_file(sid)
    if not log_path:
        raise RuntimeError("dsh talk: 找不到会话日志 %s" % sid)
    request_id = client.send_prompt(sid, message)
    reply, winfo = wait_reply(log_path, request_id, timeout=float(timeout),
                              include_reasoning=include_reasoning)
    return {
        "reply": reply,
        "session_id": sid,
        "title": _session_title(target),
        "turn": winfo.get("turn"),
        "request_id": request_id,
    }


def do_read_latest(session_id: str = "", include_reasoning: bool = False) -> dict:
    """Read the newest reply of a session without sending anything."""
    client = DshWebClient()
    target = client.pick_session(session_id or "")
    sid = target.get("sessionId") or ""
    log_path = find_session_file(sid)
    if not log_path:
        raise RuntimeError("dsh read: 找不到会话日志 %s" % sid)
    reply = latest_reply(log_path, include_reasoning)
    return {"reply": reply, "session_id": sid, "title": _session_title(target)}


def _session_cwd(log_path: str) -> str:
    """cwd recorded in the session log header (first line)."""
    try:
        from .dsh_reader import read_log_text
        first = read_log_text(log_path).splitlines()[0]
        return str(json.loads(first).get("cwd") or "")
    except Exception:
        return ""


def do_new_session(base_session_id: str = "") -> dict:
    """Create a fresh blank dsh session (clean context for prompt talk).

    Inherits cwd from the base session (explicit or latest non-blank) so the
    new session lands in the same project instead of the dsh default cwd.
    """
    client = DshWebClient()
    cwd = ""
    try:
        base = client.pick_session(base_session_id or "")
        log_path = find_session_file(base.get("sessionId") or "")
        if log_path:
            cwd = _session_cwd(log_path)
    except Exception:
        pass  # cwd inheritance is best-effort
    value = client.create_session(cwd=cwd)
    sid = value.get("sessionId") or ""
    if not sid:
        raise RuntimeError("dsh new session: session/create 没返回 sessionId: %r" % value)
    return {"session_id": sid, "cwd": cwd}


# common wrapper pairs dsh tends to use; tried in order when the configured
# delimiters match nothing (dsh follows the *instruction in the message*,
# e.g. "用【】包起来", which may differ from the node defaults)
_FALLBACK_PAIRS = [("【", "】"), ("{", "}"), ("[", "]"), ("「", "」"),
                   ("（", "）"), ("(", ")")]


def extract_payload(reply: str, left: str, right: str, strip: str, match_index: int) -> dict:
    values = extract_matches(reply, left, right, strip)
    used = "%s…%s" % (left, right)
    if not values:
        # delimiter mismatch fallback: try the usual wrappers, keep the first hit
        for l2, r2 in _FALLBACK_PAIRS:
            if (l2, r2) == (left, right):
                continue
            alt = extract_matches(reply, l2, r2, strip)
            if alt:
                values, used = alt, "%s…%s (auto-fallback)" % (l2, r2)
                break
    count = len(values)
    idx = -1
    if count:
        idx = match_index
        if idx < 0:
            idx = count + idx
        idx = max(0, min(idx, count - 1))
    value = values[idx] if count else ""
    return {
        "value": value,
        "number": to_int(value, 0),
        "all_values": ", ".join(values),
        "count": count,
        "match_index": idx,
        "used_delims": used,
    }


# ---------------------------------------------------------------------------
# HTTP API for the in-node chat panel  ->  /dsh/api/*
# ---------------------------------------------------------------------------

try:
    from server import PromptServer
    from aiohttp import web as _web

    _routes = PromptServer.instance.routes

    def _json_error(exc: Exception):
        return _web.json_response(
            {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})

    @_routes.post("/dsh/api/talk")
    async def dsh_api_talk(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        message = (data.get("message") or "").strip()
        if not message:
            return _web.json_response({"ok": False, "error": "message 为空"})
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: do_talk(
                    message,
                    data.get("session_id") or "",
                    float(data.get("timeout") or 120.0),
                    bool(data.get("include_reasoning") or False),
                ),
            )
            ext = extract_payload(
                result["reply"],
                data.get("left") or "{",
                data.get("right") or "}",
                data.get("strip") or "",
                int(data.get("match_index") or 0),
            )
            return _web.json_response({"ok": True, **result, **ext})
        except Exception as exc:
            return _json_error(exc)

    @_routes.post("/dsh/api/read")
    async def dsh_api_read(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: do_read_latest(
                    data.get("session_id") or "",
                    bool(data.get("include_reasoning") or False),
                ),
            )
            ext = extract_payload(
                result["reply"],
                data.get("left") or "{",
                data.get("right") or "}",
                data.get("strip") or "",
                int(data.get("match_index") or 0),
            )
            return _web.json_response({"ok": True, **result, **ext})
        except Exception as exc:
            return _json_error(exc)

    @_routes.post("/dsh/api/new_session")
    async def dsh_api_new_session(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: do_new_session(data.get("session_id") or ""))
            return _web.json_response({"ok": True, **result})
        except Exception as exc:
            return _json_error(exc)

    @_routes.post("/dsh/api/sessions")
    async def dsh_api_sessions(request):
        try:
            items = await asyncio.get_event_loop().run_in_executor(
                None, lambda: DshWebClient().list_sessions())
            items.sort(key=lambda it: it.get("updatedAt") or 0, reverse=True)
            out = [{
                "id": it.get("sessionId"),
                "title": _session_title(it) or "(无标题)",
                "running": bool(it.get("running")),
                "blank": bool(it.get("blank")),
                "updatedAt": it.get("updatedAt") or 0,
            } for it in items[:40]]
            return _web.json_response({"ok": True, "sessions": out})
        except Exception as exc:
            return _json_error(exc)

except Exception:
    # imported outside a running ComfyUI (unit tests) — routes are optional
    pass


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

def _resolve_path(root: str, pinned: str) -> str:
    import os
    if pinned and os.path.isfile(pinned):
        return pinned
    return find_latest_session(root or default_sessions_root())


def _info_line(info: dict, extra: str = "") -> str:
    if not info.get("ok"):
        return "dsh: ERROR %s" % info.get("error", "unknown")
    return "dsh: %s session | %s | reply %s chars%s" % (
        info.get("how", "?"),
        info.get("path", ""),
        info.get("length", 0),
        (" | " + extra) if extra else "",
    )


class DSHTalk:
    """Chat with dsh from the node panel; queue = one round-trip."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "message": ("STRING", {"default": "", "multiline": True,
                                       "placeholder": "在节点面板上直接对话，这里也会同步"}),
            },
            "optional": {
                "mode": (["auto", "resend", "read_only"], {"default": "auto"}),
                "session_id": ("STRING", {"default": "", "multiline": False,
                                          "placeholder": "空 = 最新非空会话"}),
                "wait_timeout": ("INT", {"default": 120, "min": 10, "max": 3600, "step": 10}),
                "left_delim": ("STRING", {"default": "{", "multiline": False}),
                "right_delim": ("STRING", {"default": "}", "multiline": False}),
                "strip_chars": ("STRING", {"default": "【】", "multiline": False}),
                "match_index": ("INT", {"default": 0, "min": -1, "max": 999, "step": 1}),
                "on_missing": (["empty", "error", "full_text"], {"default": "empty"}),
                # panel-persisted state (hidden in the UI, saved with the workflow)
                "last_message": ("STRING", {"default": "", "multiline": True}),
                "last_value": ("STRING", {"default": "", "multiline": True}),
                "last_reply": ("STRING", {"default": "", "multiline": True}),
                "last_session_id": ("STRING", {"default": "", "multiline": False}),
            },
        }

    RETURN_TYPES = ("STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("value", "number", "reply", "info")
    FUNCTION = "run"
    CATEGORY = NODE_CATEGORY

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return "|".join(str(kwargs.get(k, "")) for k in (
            "message", "mode", "session_id", "left_delim", "right_delim",
            "strip_chars", "match_index", "on_missing",
            "last_message", "last_value"))

    def run(self, message, mode="auto", session_id="", wait_timeout=120,
            left_delim="{", right_delim="}", strip_chars="【】", match_index=0,
            on_missing="empty", last_message="", last_value="", last_reply="",
            last_session_id=""):
        message = (message or "").strip()
        try:
            if mode == "read_only":
                res = do_read_latest(session_id or last_session_id or "")
                reply, sid, src = res["reply"], res["session_id"], "read-only"
            elif mode == "auto" and message and message == (last_message or "").strip() and last_reply:
                # the panel already sent this exact message — reuse its reply
                reply, sid, src = last_reply, (last_session_id or session_id), "panel cache"
            else:
                if not message:
                    raise RuntimeError("message 为空：在面板上输入内容并发送，或把 mode 调成 read_only")
                res = do_talk(message, session_id or last_session_id or "", float(wait_timeout))
                reply, sid, src = res["reply"], res["session_id"], "sent (turn %s)" % res.get("turn")
        except Exception as exc:
            if on_missing == "error":
                raise
            return ("", 0, "", "dsh talk: ERROR %s" % exc)

        ext = extract_payload(reply, left_delim, right_delim, strip_chars, match_index)
        if ext["count"] == 0:
            if on_missing == "error":
                raise RuntimeError("dsh talk: 回复里没有可抓取的包裹内容（试过 %s…%s 及常见括号）: %s" % (
                    left_delim, right_delim, reply[:200]))
            value = reply if on_missing == "full_text" else ""
        else:
            value = ext["value"]
        warn = ""
        if ext["count"] == 0 and on_missing == "empty":
            warn = " | ⚠ NO MATCH -> value EMPTY (提示词会缺主体!)"
        info = "dsh talk: %s | session %s | %d match(es) via %s%s" % (
            src, sid, ext["count"], ext["used_delims"], warn)
        return (value, to_int(value, 0), reply, info)


class DSHReplyExtractor:
    """Read the newest dsh reply and output the content wrapped by two markers."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "left_delim": ("STRING", {"default": "{", "multiline": False}),
                "right_delim": ("STRING", {"default": "}", "multiline": False}),
                "strip_chars": ("STRING", {"default": "【】", "multiline": False}),
                "match_index": ("INT", {"default": 0, "min": -1, "max": 999, "step": 1}),
            },
            "optional": {
                "sessions_root": ("STRING", {"default": "", "multiline": False,
                                             "placeholder": "empty = ~/.dsh/sessions"}),
                "session_file": ("STRING", {"default": "", "multiline": False,
                                            "placeholder": "optional: pin one session log"}),
                "include_reasoning": ("BOOLEAN", {"default": False}),
                "on_missing": (["empty", "error", "full_text"], {"default": "empty"}),
            },
        }

    RETURN_TYPES = ("STRING", "INT", "STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("value", "number", "all_values", "count", "reply", "info")
    FUNCTION = "run"
    CATEGORY = NODE_CATEGORY

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        root = kwargs.get("sessions_root") or default_sessions_root()
        path = _resolve_path(root, kwargs.get("session_file") or "")
        if not path:
            return "no-session"
        return signature(path) + "|" + "|".join(
            str(kwargs.get(k, "")) for k in
            ("left_delim", "right_delim", "strip_chars", "match_index",
             "include_reasoning", "on_missing")
        )

    def run(self, left_delim, right_delim, strip_chars, match_index,
            sessions_root="", session_file="", include_reasoning=False,
            on_missing="empty"):
        reply, info = load_latest(sessions_root, session_file, include_reasoning)
        if not info.get("ok"):
            msg = _info_line(info)
            if on_missing == "error":
                raise RuntimeError(msg)
            empty = "" if on_missing != "full_text" else reply
            return (empty, 0, "", 0, reply, msg)

        values = extract_matches(reply, left_delim, right_delim, strip_chars)
        count = len(values)
        if count == 0:
            msg = _info_line(info, "no %s...%s match" % (left_delim, right_delim))
            if on_missing == "error":
                raise RuntimeError(msg)
            fallback = "" if on_missing != "full_text" else reply
            return (fallback, 0, "", 0, reply, msg)

        idx = match_index
        if idx < 0:
            idx = count + idx
        idx = max(0, min(idx, count - 1))
        value = values[idx]
        return (
            value,
            to_int(value, 0),
            ", ".join(values),
            count,
            reply,
            _info_line(info, "%d match(es), using #%d" % (count, idx)),
        )


class DSHLatestReply:
    """Newest dsh assistant reply, verbatim."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "optional": {
                "sessions_root": ("STRING", {"default": "", "multiline": False,
                                             "placeholder": "empty = ~/.dsh/sessions"}),
                "session_file": ("STRING", {"default": "", "multiline": False,
                                            "placeholder": "optional: pin one session log"}),
                "include_reasoning": ("BOOLEAN", {"default": False}),
            }
        }

    RETURN_TYPES = ("STRING", "INT", "STRING")
    RETURN_NAMES = ("reply", "length", "info")
    FUNCTION = "run"
    CATEGORY = NODE_CATEGORY

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        root = kwargs.get("sessions_root") or default_sessions_root()
        path = _resolve_path(root, kwargs.get("session_file") or "")
        if not path:
            return "no-session"
        return signature(path) + "|r%d" % (1 if kwargs.get("include_reasoning") else 0)

    def run(self, sessions_root="", session_file="", include_reasoning=False):
        reply, info = load_latest(sessions_root, session_file, include_reasoning)
        return (reply, len(reply), _info_line(info))


NODE_CLASS_MAPPINGS = {
    "DSHTalk": DSHTalk,
    "DSHReplyExtractor": DSHReplyExtractor,
    "DSHLatestReply": DSHLatestReply,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DSHTalk": "DSH Talk (chat panel)",
    "DSHReplyExtractor": "DSH Reply Extractor (grab wrapped text)",
    "DSHLatestReply": "DSH Latest Reply",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

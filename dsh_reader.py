"""
dsh_reader — read the latest reply from a DeepSeek Harness (dsh) session log.

dsh stores every conversation under:

    ~/.dsh/sessions/<workspace-id>/<session-id>/session.v3.jsonl.zstd

The log is a JSONL file compressed as a *sequence of independent zstd frames*
(dsh appends one frame per flush), so a naive single-shot decompress only ever
returns the first frame. Both readers below handle the multi-frame case.

Record shape we care about:

    {"type":"assistant/message","seq":N,"time":T,
     "data":{"turn":3,"step":48,
             "message":{"role":"assistant",
                        "content":[{"type":"reasoning","text":"..."},
                                   {"type":"text","text":"..."}]}}}
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile

try:
    import zstandard
except Exception:  # pragma: no cover - optional dependency
    zstandard = None


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
LOG_GLOBS = ("*.jsonl.zstd", "*.jsonl")

# Written to a temp file only when the zstandard wheel is unavailable.
_NODE_FALLBACK_JS = r"""
const fs = require('fs'), zlib = require('zlib');
const buf = fs.readFileSync(process.argv[2]);
const magic = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const offs = [];
let i = buf.indexOf(magic);
while (i !== -1) { offs.push(i); i = buf.indexOf(magic, i + 1); }
if (offs.length === 0) { process.stdout.write(buf.toString('utf8')); process.exit(0); }
let out = '';
for (let k = 0; k < offs.length; k++) {
  const s = offs[k], e = (k + 1 < offs.length) ? offs[k + 1] : buf.length;
  try { out += zlib.zstdDecompressSync(buf.subarray(s, e)).toString('utf8'); } catch (err) {}
}
process.stdout.write(out);
"""


def default_sessions_root() -> str:
    """Where dsh keeps its session logs."""
    return os.environ.get("DSH_SESSIONS_DIR") or os.path.join(
        os.path.expanduser("~"), ".dsh", "sessions"
    )


def find_latest_session(root: str) -> str:
    """Newest session log (by mtime) under `root`; '' when nothing is found."""
    from pathlib import Path

    base = Path(root)
    if not base.is_dir():
        return ""
    best, best_m = "", -1.0
    for pattern in LOG_GLOBS:
        for p in base.rglob(pattern):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > best_m:
                best, best_m = str(p), m
    return best


def _find_node_bin() -> str:
    for cand in (
        os.environ.get("DSH_NODE_BIN"),
        shutil.which("node"),
        shutil.which("node.exe"),
        r"C:\Users\y1664\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
    ):
        if cand and os.path.isfile(cand):
            return cand
    return ""


def read_log_text(path: str) -> str:
    """Decompress a dsh session log (multi-frame zstd) into UTF-8 text."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw.startswith(ZSTD_MAGIC):
        return raw.decode("utf-8", "replace")

    if zstandard is not None:
        dctx = zstandard.ZstdDecompressor()
        with dctx.stream_reader(io.BytesIO(raw)) as reader:
            return reader.read().decode("utf-8", "replace")

    node = _find_node_bin()
    if not node:
        raise RuntimeError(
            "dsh: neither the python 'zstandard' module nor a node binary is "
            "available. Install with: <comfy python> -m pip install zstandard"
        )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
        tf.write(_NODE_FALLBACK_JS)
        script = tf.name
    try:
        out = subprocess.run(
            [node, script, path], capture_output=True, timeout=120
        )
        if out.returncode != 0:
            raise RuntimeError(
                "dsh: node fallback failed: " + out.stderr.decode("utf-8", "replace")[:300]
            )
        return out.stdout.decode("utf-8", "replace")
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


def iter_assistant_texts(text: str, include_reasoning: bool = False):
    """Yield (seq, text) for every assistant/message record, in log order."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant/message":
            continue
        message = (rec.get("data") or {}).get("message") or {}
        chunks = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            body = block.get("text") or ""
            if kind == "text" or (kind == "reasoning" and include_reasoning):
                if body:
                    chunks.append(body)
        if chunks:
            yield rec.get("seq", 0), "\n".join(chunks)


def latest_reply(path: str, include_reasoning: bool = False) -> str:
    """Full text of the most recent assistant reply in the session log."""
    text = read_log_text(path)
    last = ""
    for _seq, body in iter_assistant_texts(text, include_reasoning):
        last = body
    return last


def extract_matches(reply: str, left: str, right: str, strip_chars: str = ""):
    """All `left ... right` wrapped fragments inside `reply`.

    Example: reply "{【2】}" with left="{", right="}", strip_chars="【】" -> ["2"].
    """
    if not left or not right:
        return []
    pattern = re.compile(re.escape(left) + r"(.*?)" + re.escape(right), re.DOTALL)
    values = []
    for m in pattern.finditer(reply or ""):
        v = m.group(1)
        if strip_chars:
            v = v.strip(strip_chars)
        v = v.strip()
        values.append(v)
    return values


def to_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        pass
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# tiny mtime-keyed cache so repeated node executions do not re-inflate the log
# ---------------------------------------------------------------------------
_CACHE = {}


def signature(path: str) -> str:
    try:
        st = os.stat(path)
        return "%s|%d|%d" % (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return "%s|missing" % path


def load_latest(root: str = "", pinned: str = "", include_reasoning: bool = False):
    """Resolve + parse the newest session. Returns (reply, info_dict)."""
    root = root or default_sessions_root()
    if pinned and os.path.isfile(pinned):
        path = pinned
        how = "pinned"
    else:
        path = find_latest_session(root)
        how = "latest"
    if not path or not os.path.isfile(path):
        return "", {"ok": False, "error": "no session log found under %s" % root,
                    "path": "", "how": how}

    sig = signature(path) + "|r%d" % (1 if include_reasoning else 0)
    hit = _CACHE.get(sig)
    if hit is not None:
        reply, info = hit
        return reply, dict(info, cached=True)

    try:
        reply = latest_reply(path, include_reasoning)
    except Exception as exc:
        return "", {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                    "path": path, "how": how}

    info = {"ok": True, "path": path, "how": how, "cached": False,
            "sig": sig, "length": len(reply)}
    _CACHE[sig] = (reply, info)
    if len(_CACHE) > 32:
        _CACHE.clear()
    return reply, dict(info)

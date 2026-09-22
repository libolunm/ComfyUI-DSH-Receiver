import { app } from "../../scripts/app.js";

/* DSH Talk — in-node chat panel for the DSHTalk node.
 *
 * The panel talks to the ComfyUI-hosted API (/dsh/api/*), which forwards the
 * message to the running dsh web instance and waits for its reply. State that
 * must survive save/load lives in hidden widgets (last_reply / last_value /
 * last_message / last_session_id).
 */

function loadStyles() {
  const id = "dsh-talk-styles";
  if (document.getElementById(id)) return;
  const link = document.createElement("link");
  link.id = id;
  link.rel = "stylesheet";
  link.href = new URL("./dsh_talk.css", import.meta.url).href;
  document.head.appendChild(link);
}

async function api(path, payload) {
  const r = await fetch("/dsh/api/" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  return r.json();
}

function w(node, name) {
  return node.widgets?.find((x) => x.name === name);
}
function hide(widget) {
  if (!widget) return;
  widget.hidden = true;
  widget.computeSize = () => [0, -4];
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
  ));
}

app.registerExtension({
  name: "dsh.talk.panel",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "DSHTalk") return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);
      buildPanel(this);
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure?.apply(this, arguments);
      restorePanel(this);
      return r;
    };
  },
});

function buildPanel(node) {
  loadStyles();

  const wMessage = w(node, "message");
  const wMode = w(node, "mode");
  const wSession = w(node, "session_id");
  const wTimeout = w(node, "wait_timeout");
  const wLeft = w(node, "left_delim");
  const wRight = w(node, "right_delim");
  const wStrip = w(node, "strip_chars");
  const wIdx = w(node, "match_index");
  const wLastMsg = w(node, "last_message");
  const wLastValue = w(node, "last_value");
  const wLastReply = w(node, "last_reply");
  const wLastSession = w(node, "last_session_id");

  // the panel replaces these widget UIs (values still serialize)
  hide(wMessage);
  hide(wSession);
  hide(wLastMsg);
  hide(wLastValue);
  hide(wLastReply);
  hide(wLastSession);

  const root = document.createElement("div");
  root.className = "dsh-talk";
  root.innerHTML = `
    <div class="dsh-talk-head">
      <select class="dsh-session" title="目标会话">
        <option value="">自动（最新会话）</option>
      </select>
      <button class="dsh-new" title="新建 dsh 对话（上下文干净了，提示词更准）">＋</button>
      <span class="dsh-dot" title="状态"></span>
    </div>
    <div class="dsh-reply" data-empty="true">还没有对话。在下方输入，点「发送」。</div>
    <div class="dsh-value-row">
      <span class="dsh-value-label">value</span>
      <code class="dsh-value">—</code>
    </div>
    <textarea class="dsh-input" rows="3" placeholder="对 dsh 说话… 例：给我一个雪中温泉主题的 danbooru 提示词，用 {【…】} 包裹"></textarea>
    <div class="dsh-actions">
      <button class="dsh-send">发 送</button>
      <button class="dsh-read" title="不发送，只读该会话最新回复">↻ 只读</button>
      <span class="dsh-status"></span>
    </div>`;

  const sel = root.querySelector(".dsh-session");
  const dot = root.querySelector(".dsh-dot");
  const replyBox = root.querySelector(".dsh-reply");
  const valueBox = root.querySelector(".dsh-value");
  const input = root.querySelector(".dsh-input");
  const sendBtn = root.querySelector(".dsh-send");
  const readBtn = root.querySelector(".dsh-read");
  const newBtn = root.querySelector(".dsh-new");
  const status = root.querySelector(".dsh-status");

  node._dshPanel = { sel, dot, replyBox, valueBox, input, status };

  // --- helpers -------------------------------------------------------------
  const setStatus = (text, kind) => {
    status.textContent = text || "";
    status.dataset.kind = kind || "";
    dot.dataset.kind = kind || "";
  };
  const setBusy = (busy, text) => {
    sendBtn.disabled = busy;
    readBtn.disabled = busy;
    newBtn.disabled = busy;
    if (busy) setStatus(text || "等待 dsh 回复…", "busy");
  };
  const extractArgs = () => ({
    left: wLeft?.value ?? "{",
    right: wRight?.value ?? "}",
    strip: wStrip?.value ?? "",
    match_index: wIdx?.value ?? 0,
  });
  const persist = (msg, res) => {
    if (wLastMsg) wLastMsg.value = msg;
    if (wLastReply) wLastReply.value = res.reply || "";
    if (wLastValue) wLastValue.value = res.value || "";
    if (wLastSession) wLastSession.value = res.session_id || "";
    if (wMessage) wMessage.value = msg;
  };
  const render = (res) => {
    replyBox.dataset.empty = res.reply ? "false" : "true";
    replyBox.textContent = res.reply || "（空回复）";
    valueBox.textContent = res.value || "—";
    valueBox.dataset.hit = res.count > 0 ? "true" : "false";
  };

  // --- events ---------------------------------------------------------------
  input.addEventListener("input", () => {
    if (wMessage) wMessage.value = input.value;
  });
  input.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      sendBtn.click();
    }
    e.stopPropagation(); // don't trigger canvas shortcuts while typing
  });
  input.addEventListener("pointerdown", (e) => e.stopPropagation());

  sel.addEventListener("change", () => {
    if (wSession) wSession.value = sel.value;
  });

  sendBtn.addEventListener("click", async () => {
    const msg = input.value.trim();
    if (!msg) { setStatus("先写点内容", "warn"); return; }
    setBusy(true);
    try {
      const res = await api("talk", {
        message: msg,
        session_id: wSession?.value || "",
        timeout: wTimeout?.value || 120,
        ...extractArgs(),
      });
      if (!res.ok) throw new Error(res.error || "未知错误");
      render(res);
      persist(msg, res);
      if (wMode) wMode.value = "auto";
      setStatus(
        `turn ${res.turn ?? "?"} · ${res.count} 个匹配 · ${res.title || res.session_id}`,
        "ok"
      );
      node.setDirtyCanvas(true, true);
    } catch (err) {
      setStatus("失败：" + err.message, "err");
    } finally {
      setBusy(false);
    }
  });

  readBtn.addEventListener("click", async () => {
    setBusy(true, "读取最新回复…");
    try {
      const res = await api("read", {
        session_id: wSession?.value || "",
        ...extractArgs(),
      });
      if (!res.ok) throw new Error(res.error || "未知错误");
      render(res);
      if (wLastReply) wLastReply.value = res.reply || "";
      if (wLastSession) wLastSession.value = res.session_id || "";
      if (wMode) wMode.value = "read_only"; // queue should not re-send
      setStatus(`只读 · ${res.count} 个匹配 · ${res.title || res.session_id}`, "ok");
      node.setDirtyCanvas(true, true);
    } catch (err) {
      setStatus("失败：" + err.message, "err");
    } finally {
      setBusy(false);
    }
  });

  // session list (async, best-effort)
  const loadSessions = async (selectId) => {
    const res = await api("sessions");
    if (!res.ok) throw new Error(res.error);
    sel.innerHTML = '<option value="">自动（最新会话）</option>';
    for (const s of res.sessions) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = (s.running ? "▶ " : "") + (s.blank ? "＋ " : "") + s.title;
      sel.appendChild(opt);
    }
    sel.value = selectId || wSession?.value || "";
  };

  newBtn.addEventListener("click", async () => {
    setBusy(true, "创建新对话…");
    try {
      const res = await api("new_session", { session_id: wSession?.value || "" });
      if (!res.ok) throw new Error(res.error || "未知错误");
      await loadSessions(res.session_id);
      if (wSession) wSession.value = res.session_id;
      // fresh conversation = fresh panel state
      replyBox.dataset.empty = "true";
      replyBox.textContent = "新对话已建好。直接输入，点「发送」。";
      valueBox.textContent = "—";
      valueBox.dataset.hit = "false";
      for (const wx of [wLastMsg, wLastValue, wLastReply]) { if (wx) wx.value = ""; }
      if (wLastSession) wLastSession.value = res.session_id;
      setStatus("新对话 " + res.session_id.slice(0, 16) + "…", "ok");
      node.setDirtyCanvas(true, true);
    } catch (err) {
      setStatus("创建失败：" + err.message, "err");
    } finally {
      setBusy(false);
    }
  });

  (async () => {
    try {
      await loadSessions();
      setStatus("", "");
    } catch (err) {
      setStatus("dsh 未连接：" + err.message, "err");
    }
  })();

  node.addDOMWidget("dsh_panel", "dsh-panel", root, {
    serialize: false,
    hideOnZoom: false,
  });

  // give the panel room
  const size = node.computeSize();
  node.setSize([Math.max(size[0], 480), Math.max(size[1], 540)]);
}

function restorePanel(node) {
  const p = node._dshPanel;
  if (!p) return;
  const lastReply = w(node, "last_reply")?.value || "";
  const lastValue = w(node, "last_value")?.value || "";
  const lastMsg = w(node, "last_message")?.value || "";
  const sid = w(node, "session_id")?.value || "";
  if (lastMsg && !p.input.value) p.input.value = lastMsg;
  if (lastReply) {
    p.replyBox.dataset.empty = "false";
    p.replyBox.textContent = lastReply;
    p.valueBox.textContent = lastValue || "—";
    p.valueBox.dataset.hit = lastValue ? "true" : "false";
  }
  if (sid) p.sel.value = sid;
}

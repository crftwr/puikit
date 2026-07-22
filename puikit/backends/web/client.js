/* PuiKit web backend — browser client.
 *
 * A dumb pixel replayer plus an input reporter. The Python backend does all the
 * base-unit -> pixel math and font resolution, so every frame is a flat list of
 * ops in CSS pixels with ready-made CSS font/color strings; this file only
 * paints them onto a <canvas> and streams normalized input back. Nothing here
 * knows what a widget, a base unit, or a layout is.
 */
"use strict";

(function () {
  const canvas = document.getElementById("screen");
  const ctx = canvas.getContext("2d");
  let dpr = window.devicePixelRatio || 1;
  const images = new Map(); // asset id -> HTMLImageElement
  let clipDepth = 0;
  // A hidden <input> owns IME composition while a text field is focused; while
  // it does, the window-level key handler stands down (see keydown below).
  let ime = null;
  let imeActive = false;
  let shuttingDown = false;

  // --- websocket ---------------------------------------------------------

  const wsURL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  let ws = null;

  function connect() {
    ws = new WebSocket(wsURL);
    ws.onopen = async () => {
      await preloadFonts();
      sendResize();
    };
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      dispatch(msg);
    };
    ws.onclose = () => {
      // The Python process went away (app quit or died). Try to close the tab,
      // else show an "exited" notice instead of a frozen last frame.
      ws = null;
      shutdown();
    };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  async function preloadFonts() {
    if (!document.fonts) return;
    const specs = [
      '400 16px "PuiMono"', '700 16px "PuiMono"',
      '400 16px "PuiSans"', '700 16px "PuiSans"',
    ];
    try {
      await Promise.all(specs.map((s) => document.fonts.load(s)));
    } catch (e) {
      /* fall back to whatever the browser has */
    }
  }

  // --- message dispatch --------------------------------------------------

  function dispatch(msg) {
    switch (msg.type) {
      case "frame":
        render(msg);
        break;
      case "asset": {
        const img = new Image();
        img.onload = () => images.set(msg.id, img);
        img.src = msg.url;
        break;
      }
      case "cursor":
        canvas.style.cursor = msg.shape || "default";
        break;
      case "open_url":
        window.open(msg.url, "_blank", "noopener");
        break;
      case "ime":
        handleIme(msg);
        break;
      case "shutdown":
        shutdown();
        break;
    }
  }

  // The app has exited. Try to close the tab — browsers only allow this for a
  // tab a script opened, so a webbrowser-launched tab usually can't self-close —
  // and if it's still here a moment later, replace the frozen last frame with a
  // clear "exited" notice.
  function shutdown() {
    if (shuttingDown) return;
    shuttingDown = true;
    try {
      window.close();
    } catch (e) {
      /* ignore */
    }
    setTimeout(showExitNotice, 200);
  }

  function showExitNotice() {
    if (document.getElementById("exit-notice")) return;
    const d = document.createElement("div");
    d.id = "exit-notice";
    d.textContent = "Application closed — you can close this tab.";
    Object.assign(d.style, {
      position: "fixed", inset: "0", display: "flex",
      alignItems: "center", justifyContent: "center",
      background: "#181818", color: "#9aa0a6",
      font: '15px -apple-system, system-ui, sans-serif', zIndex: "10",
    });
    document.body.appendChild(d);
  }

  // --- rendering ---------------------------------------------------------

  function resetClips() {
    while (clipDepth > 0) {
      ctx.restore();
      clipDepth--;
    }
  }

  function render(msg) {
    const ops = msg.ops || [];
    // The CSS size this frame was laid out for (from the server). Size the
    // backing store to match it and clear+paint in one synchronous call, so the
    // canvas is never composited blank. Between frames the backing store keeps
    // the last bitmap, which the browser CSS-scales to the live window size —
    // that is what avoids a black flash while a resize's reflowed frame is in
    // flight (setting canvas.width/height at resize time would clear it).
    const fw = msg.w || window.innerWidth;
    const fh = msg.h || window.innerHeight;
    const bw = Math.max(1, Math.round(fw * dpr));
    const bh = Math.max(1, Math.round(fh * dpr));
    if (canvas.width !== bw || canvas.height !== bh) {
      canvas.width = bw;
      canvas.height = bh;
    }
    resetClips();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, fw, fh);
    for (let i = 0; i < ops.length; i++) {
      paint(ops[i]);
    }
    resetClips();
  }

  function roundRectPath(x, y, w, h, r) {
    if (r == null) r = Math.min(w, h) / 2; // pill / circle
    r = Math.max(0, Math.min(r, w / 2, h / 2));
    ctx.beginPath();
    if (ctx.roundRect) {
      ctx.roundRect(x, y, w, h, r);
    } else {
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y, x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r);
      ctx.arcTo(x, y, x + w, y, r);
      ctx.closePath();
    }
  }

  function paint(op) {
    const k = op[0];
    switch (k) {
      case "fill": {
        const [, x, y, w, h, color] = op;
        ctx.fillStyle = color;
        ctx.fillRect(x, y, w, h);
        break;
      }
      case "box": {
        const [, x, y, w, h, stroke, fill, lw] = op;
        if (fill) {
          ctx.fillStyle = fill;
          ctx.fillRect(x, y, w, h);
        }
        if (stroke) {
          ctx.strokeStyle = stroke;
          ctx.lineWidth = lw;
          // Inset by half a line so the 1px stroke stays inside the rect.
          ctx.strokeRect(x + lw / 2, y + lw / 2, w - lw, h - lw);
        }
        break;
      }
      case "rrect": {
        const [, x, y, w, h, radius, stroke, fill, lw] = op;
        roundRectPath(x, y, w, h, radius);
        if (fill) {
          ctx.fillStyle = fill;
          ctx.fill();
        }
        if (stroke) {
          ctx.strokeStyle = stroke;
          ctx.lineWidth = lw;
          ctx.stroke();
        }
        break;
      }
      case "check": {
        const [, x, y, w, h, color] = op;
        ctx.strokeStyle = color;
        ctx.lineWidth = Math.max(1.2, w * 0.12);
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.beginPath();
        ctx.moveTo(x + w * 0.22, y + h * 0.52);
        ctx.lineTo(x + w * 0.42, y + h * 0.72);
        ctx.lineTo(x + w * 0.78, y + h * 0.28);
        ctx.stroke();
        break;
      }
      case "chevron": {
        const [, x, y, w, h, expanded, color] = op;
        ctx.strokeStyle = color;
        ctx.lineWidth = Math.max(1.2, w * 0.12);
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.beginPath();
        if (expanded) {
          ctx.moveTo(x + w * 0.25, y + h * 0.4);
          ctx.lineTo(x + w * 0.5, y + h * 0.65);
          ctx.lineTo(x + w * 0.75, y + h * 0.4);
        } else {
          ctx.moveTo(x + w * 0.4, y + h * 0.25);
          ctx.lineTo(x + w * 0.65, y + h * 0.5);
          ctx.lineTo(x + w * 0.4, y + h * 0.75);
        }
        ctx.stroke();
        break;
      }
      case "text": {
        const [, x, baseline, text, font, color, underline, strike] = op;
        ctx.font = font;
        ctx.fontKerning = "none";
        ctx.textBaseline = "alphabetic";
        ctx.textAlign = "left";
        ctx.fillStyle = color;
        ctx.fillText(text, x, baseline);
        if (underline || strike) {
          const w = ctx.measureText(text).width;
          ctx.strokeStyle = color;
          ctx.lineWidth = 1;
          ctx.beginPath();
          const ly = underline ? baseline + 1.5 : baseline - 4;
          ctx.moveTo(x, ly);
          ctx.lineTo(x + w, ly);
          ctx.stroke();
        }
        break;
      }
      case "dim": {
        const [, x, y, w, h] = op;
        ctx.fillStyle = "rgba(0,0,0,0.5)";
        ctx.fillRect(x, y, w, h);
        break;
      }
      case "shadow": {
        const [, x, y, w, h, radius, color] = op;
        ctx.save();
        ctx.shadowColor = "rgba(0,0,0,0.35)";
        // The blur radius is a fixed softness, independent of the corner radius.
        ctx.shadowBlur = 16;
        ctx.shadowOffsetY = 4;
        ctx.fillStyle = color || "rgba(0,0,0,1)";
        // draw_shadow's contract: radius null/0 is a SQUARE silhouette (matching
        // a square draw_box panel); only a positive radius rounds the corners to
        // match a rounded panel. (Unlike round_rect, where null means a pill —
        // which is why this must not route through roundRectPath's null default.)
        if (radius && radius > 0) {
          roundRectPath(x, y, w, h, radius);
          ctx.fill();
        } else {
          ctx.fillRect(x, y, w, h);
        }
        ctx.restore();
        break;
      }
      case "sbar": {
        const [, x, y, w, h, pos, ratio, thumb, track, orientation] = op;
        ctx.fillStyle = track;
        ctx.fillRect(x, y, w, h);
        ctx.fillStyle = thumb;
        if (orientation === "horizontal") {
          const len = Math.max(6, w * ratio);
          const off = (w - len) * pos;
          roundRectPath(x + off, y + h * 0.25, len, h * 0.5, h * 0.25);
        } else {
          const len = Math.max(6, h * ratio);
          const off = (h - len) * pos;
          roundRectPath(x + w * 0.25, y + off, w * 0.5, len, w * 0.25);
        }
        ctx.fill();
        break;
      }
      case "img": {
        const [, id, sx, sy, sw, sh, dx, dy, dw, dh, alpha] = op;
        const img = images.get(id);
        if (img) {
          ctx.save();
          ctx.globalAlpha = alpha;
          try {
            ctx.drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh);
          } catch (e) {
            /* image not decoded yet */
          }
          ctx.restore();
        }
        break;
      }
      case "clip": {
        const [, x, y, w, h] = op;
        ctx.save();
        clipDepth++;
        ctx.beginPath();
        ctx.rect(x, y, w, h);
        ctx.clip();
        break;
      }
      case "unclip": {
        if (clipDepth > 0) {
          ctx.restore();
          clipDepth--;
        }
        break;
      }
    }
  }

  // --- sizing ------------------------------------------------------------

  function sendResize() {
    dpr = window.devicePixelRatio || 1;
    const w = window.innerWidth;
    const h = window.innerHeight;
    // Only update the CSS (display) size — NOT the backing store. Resizing the
    // backing store here would clear the canvas to black until the reflowed
    // frame arrives; instead the browser scales the last bitmap to the new CSS
    // size, and render() resizes the backing store when it paints the new frame.
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    send({ type: "resize", w: w, h: h, dpr: dpr });
  }

  window.addEventListener("resize", sendResize);

  // --- input -------------------------------------------------------------

  function mods(e) {
    return { shift: e.shiftKey, ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey };
  }

  // Combos the browser should keep (reload / close / new tab / devtools), so
  // the page stays controllable; everything else goes to the app.
  const BROWSER_KEYS = new Set(["r", "w", "t", "n", "l"]);
  function browserOwns(e) {
    if (e.key === "F5" || e.key === "F11" || e.key === "F12") return true;
    if ((e.metaKey || e.ctrlKey) && BROWSER_KEYS.has(e.key.toLowerCase())) return true;
    return false;
  }

  window.addEventListener("keydown", (e) => {
    // While a text field is focused the hidden IME input owns the keyboard;
    // its own handler forwards command keys, so stand down here.
    if (imeActive) return;
    if (browserOwns(e)) return;
    // Bare modifier presses carry no PuiKit key; let them pass.
    if (["Shift", "Control", "Alt", "Meta", "CapsLock"].includes(e.key)) return;
    e.preventDefault();
    send({ type: "key", key: e.key, mods: mods(e) });
  });

  function pos(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  const BUTTONS = { 0: "left", 1: "middle", 2: "right" };

  canvas.addEventListener("mousedown", (e) => {
    // Don't steal focus from the IME input mid-composition; the app drives
    // focus back to the canvas via an "ime end" message when a field blurs.
    if (!imeActive) canvas.focus();
    const p = pos(e);
    send({ type: "mouse", kind: "down", x: p.x, y: p.y, button: BUTTONS[e.button] || "left", mods: mods(e) });
  });
  window.addEventListener("mouseup", (e) => {
    const p = pos(e);
    send({ type: "mouse", kind: "up", x: p.x, y: p.y, button: BUTTONS[e.button] || "left", mods: mods(e) });
  });

  // Coalesce mouse moves to one per animation frame — a drag or hover fires far
  // more often than a frame, and the app only needs the latest position.
  let pendingMove = null;
  let moveScheduled = false;
  function flushMove() {
    moveScheduled = false;
    if (pendingMove) {
      send(pendingMove);
      pendingMove = null;
    }
  }
  window.addEventListener("mousemove", (e) => {
    const p = pos(e);
    pendingMove = { type: "mouse", kind: "move", x: p.x, y: p.y, mods: mods(e) };
    if (!moveScheduled) {
      moveScheduled = true;
      requestAnimationFrame(flushMove);
    }
  });

  // Coalesce wheel events like moves: a trackpad fires far more often than a
  // frame, and each scroll re-renders the page. Accumulate the deltas and send
  // one summed scroll per animation frame so a heavy page (a wrapping text
  // block) can't fall behind an input flood.
  let pendingScroll = null;
  let scrollScheduled = false;
  function flushScroll() {
    scrollScheduled = false;
    if (pendingScroll) {
      send(pendingScroll);
      pendingScroll = null;
    }
  }
  canvas.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const p = pos(e);
      if (pendingScroll) {
        pendingScroll.dx += e.deltaX;
        pendingScroll.dy += e.deltaY;
        pendingScroll.x = p.x;
        pendingScroll.y = p.y;
        pendingScroll.mods = mods(e);
      } else {
        pendingScroll = { type: "mouse", kind: "scroll", x: p.x, y: p.y, dx: e.deltaX, dy: e.deltaY, mods: mods(e) };
      }
      if (!scrollScheduled) {
        scrollScheduled = true;
        requestAnimationFrame(flushScroll);
      }
    },
    { passive: false }
  );

  canvas.addEventListener("contextmenu", (e) => e.preventDefault());

  // --- IME ---------------------------------------------------------------
  //
  // A hidden, caret-positioned <input> engages the OS IME (composition +
  // candidate window) while a text widget is focused. The widget draws the
  // preedit itself (fed by `ime_preedit`), so the input's own text is kept
  // transparent; committed text comes back via `ime_commit`.

  function sendPreedit(text) {
    send({ type: "ime_preedit", text: text || "", caret: (text || "").length });
  }
  function sendCommit(text) {
    if (text) send({ type: "ime_commit", text: text });
  }

  function handleIme(msg) {
    if (!ime) return;
    if (msg.action === "begin") {
      imeActive = true;
      ime.value = "";
      ime.focus();
    } else if (msg.action === "end") {
      imeActive = false;
      ime.value = "";
      ime.blur();
      canvas.focus();
    } else if (msg.action === "caret") {
      ime.style.left = Math.round(msg.x) + "px";
      ime.style.top = Math.round(msg.y) + "px";
      ime.style.height = Math.round(msg.h) + "px";
    }
  }

  (function setupIme() {
    ime = document.createElement("input");
    ime.type = "text";
    ime.autocapitalize = "off";
    ime.autocomplete = "off";
    ime.spellcheck = false;
    ime.setAttribute("aria-hidden", "true");
    Object.assign(ime.style, {
      position: "fixed", left: "0px", top: "0px", width: "1px", height: "16px",
      padding: "0", margin: "0", border: "none", outline: "none",
      background: "transparent", color: "transparent", caretColor: "transparent",
      zIndex: "0",
    });
    document.body.appendChild(ime);

    let composing = false;

    ime.addEventListener("compositionstart", () => {
      composing = true;
    });
    ime.addEventListener("compositionupdate", (e) => {
      // The in-progress composition (marked text); the widget draws it. Do NOT
      // touch ime.value here — the IME owns the input's value during
      // composition, and clearing it aborts the composition after one character.
      sendPreedit(e.data || "");
    });
    ime.addEventListener("compositionend", (e) => {
      composing = false;
      sendPreedit("");
      if (e.data) sendCommit(e.data);
      // Safe to reset now that composition is over, so the next one starts empty.
      ime.value = "";
    });
    ime.addEventListener("input", (e) => {
      // While composing, the IME owns the value — never disturb it here.
      if (e.isComposing || composing) return;
      // Direct (non-IME) typing commits here; a composition commit reports
      // inputType "insertCompositionText" and is handled by compositionend
      // above, so only a plain "insertText" is a direct keystroke to forward.
      if (e.inputType === "insertText" && e.data) sendCommit(e.data);
      ime.value = "";
    });

    const IME_CMD = new Set([
      "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", "Tab",
      "Escape", "Backspace", "Delete", "Home", "End", "PageUp", "PageDown",
    ]);
    ime.addEventListener("keydown", (e) => {
      if (e.isComposing) return; // the IME owns keys during composition
      if (browserOwns(e)) return;
      // Command keys and modifier chords (copy/paste/select-all) go to the app;
      // plain printable keys fall through to fire input / composition events.
      if (IME_CMD.has(e.key) || ((e.ctrlKey || e.metaKey || e.altKey) && e.key.length === 1)) {
        e.preventDefault();
        send({ type: "key", key: e.key, mods: mods(e) });
      }
    });
  })();

  // --- go ----------------------------------------------------------------

  canvas.focus();
  connect();
})();

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
  // Set on every UI render so the post-effect pass re-uploads the UI canvas as
  // a texture only when it actually changed (its own clock still runs each frame).
  let fxUiDirty = true;

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
      case "background":
        if (msg.kind === "shader") bg.setShader(msg);
        else bg.clear();
        break;
      case "posteffect":
        if (msg.on) fx.setEffect(msg);
        else fx.clear();
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
    bg.clear();
    fx.clear();
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
    fxUiDirty = true; // the UI changed; the post-effect pass should re-sample it
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

  // --- WebGL shader background -------------------------------------------
  //
  // A GPU fragment shader painted on its own canvas *behind* the (transparent)
  // UI canvas. Python sends the GLSL program + uniforms; this runs the clock and
  // renders. Where the UI dissolves its surface fills (set_surface_opacity) or
  // drops them (reveal_mode), this scene shows through.

  const bgCanvas = document.createElement("canvas");
  // z-index 0 (not -1): a negative z-index would paint behind the body's own
  // in-flow content and be hidden. The UI canvas is z-index 1, above this.
  Object.assign(bgCanvas.style, {
    position: "fixed", left: "0", top: "0", zIndex: "0", display: "none",
  });
  document.body.appendChild(bgCanvas);

  const bg = (function () {
    let gl = null, prog = null, buf = null, loc = {};
    let current = null, startTime = 0, rafId = 0;
    // GLSL ES 3.00 (WebGL2): scenes use uint bit-hashes and indexed arrays.
    const VERT = "#version 300 es\nin vec2 p;\nvoid main(){ gl_Position = vec4(p,0.0,1.0); }";

    function ensureGl() {
      if (gl) return gl;
      // preserveDrawingBuffer so the post-effect pass can sample this canvas as
      // a texture (its content would otherwise be cleared after compositing).
      gl = bgCanvas.getContext("webgl2", {
        premultipliedAlpha: false, antialias: false, preserveDrawingBuffer: true,
      });
      if (gl) {
        buf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        // One fullscreen triangle (covers the viewport, no index buffer).
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      }
      return gl;
    }

    function compile(type, src) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        console.warn("PuiKit background shader failed to compile:", gl.getShaderInfoLog(s));
        gl.deleteShader(s);
        return null;
      }
      return s;
    }

    function build(fragSrc) {
      const vs = compile(gl.VERTEX_SHADER, VERT);
      const fs = compile(gl.FRAGMENT_SHADER, fragSrc);
      if (!vs || !fs) return null;
      const p = gl.createProgram();
      gl.attachShader(p, vs);
      gl.attachShader(p, fs);
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
        console.warn("PuiKit background link failed:", gl.getProgramInfoLog(p));
        return null;
      }
      return p;
    }

    function setShader(msg) {
      if (!ensureGl()) {
        console.warn("PuiKit: WebGL2 unavailable — no shader background.");
        return; // leave the plain page background
      }
      const np = build(msg.source);
      if (!np) return; // a broken shader degrades to no background, never throws
      if (prog) gl.deleteProgram(prog);
      prog = np;
      loc = {
        p: gl.getAttribLocation(prog, "p"),
        res: gl.getUniformLocation(prog, "resolution"),
        time: gl.getUniformLocation(prog, "time"),
        opacity: gl.getUniformLocation(prog, "opacity"),
        ink: gl.getUniformLocation(prog, "ink"),
        backdrop: gl.getUniformLocation(prog, "backdrop"),
      };
      current = {
        ink: new Float32Array(msg.ink),
        backdrop: new Float32Array(msg.backdrop),
        opacity: msg.opacity,
        speed: msg.speed,
        scale: Math.max(0.1, Math.min(1, msg.resolution_scale || 1)),
        reducedMotion: !!msg.reduced_motion,
      };
      startTime = performance.now();
      bgCanvas.style.display = "block";
      if (!rafId) rafId = requestAnimationFrame(frame);
    }

    function clear() {
      current = null;
      bgCanvas.style.display = "none";
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    }

    function frame(now) {
      rafId = 0;
      if (!current || !gl || !prog) return;
      const w = Math.max(1, Math.round(window.innerWidth * dpr * current.scale));
      const h = Math.max(1, Math.round(window.innerHeight * dpr * current.scale));
      if (bgCanvas.width !== w || bgCanvas.height !== h) {
        bgCanvas.width = w;
        bgCanvas.height = h;
      }
      bgCanvas.style.width = window.innerWidth + "px";
      bgCanvas.style.height = window.innerHeight + "px";
      gl.viewport(0, 0, w, h);
      gl.useProgram(prog);
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.enableVertexAttribArray(loc.p);
      gl.vertexAttribPointer(loc.p, 2, gl.FLOAT, false, 0, 0);
      const t = current.reducedMotion ? 0.0 : ((now - startTime) / 1000) * current.speed;
      if (loc.res) gl.uniform2f(loc.res, w, h);
      if (loc.time) gl.uniform1f(loc.time, t);
      if (loc.opacity) gl.uniform1f(loc.opacity, current.opacity);
      if (loc.ink) gl.uniform4fv(loc.ink, current.ink);
      if (loc.backdrop) gl.uniform4fv(loc.backdrop, current.backdrop);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      rafId = requestAnimationFrame(frame);
    }

    return { setShader, clear };
  })();

  // --- WebGL post effect (CRT / phosphor) --------------------------------
  //
  // A full-frame effect composited over everything: it samples the shader
  // background canvas and the UI canvas as textures, blends them, and applies
  // the CRT look (bloom, scanlines, vignette, tint, glow, roll, flicker) to its
  // own canvas on top. That canvas is pointer-events:none, so input still
  // reaches the UI canvas beneath it.

  const fxCanvas = document.createElement("canvas");
  Object.assign(fxCanvas.style, {
    position: "fixed", left: "0", top: "0", zIndex: "2",
    display: "none", pointerEvents: "none",
  });
  document.body.appendChild(fxCanvas);

  const FX_VERT =
    "#version 300 es\nin vec2 p; out vec2 vUV;" +
    "void main(){ vUV = p*0.5+0.5; gl_Position = vec4(p,0.0,1.0); }";
  const FX_FRAG = `#version 300 es
precision highp float;
uniform sampler2D uUI;
uniform sampler2D uBg;
uniform vec2 uRes;
uniform float uTime;
uniform vec3 uTint;
uniform float uHasTint, uHasBg;
uniform float uBloom, uScanline, uVignette, uGlow, uFlicker, uRoll;
in vec2 vUV;
out vec4 outColor;
const vec3 LUMA = vec3(0.299, 0.587, 0.114);
vec3 frameAt(vec2 uv) {
  vec4 ui = texture(uUI, uv);
  vec3 bg = uHasBg > 0.5 ? texture(uBg, uv).rgb : vec3(0.094);
  return mix(bg, ui.rgb, ui.a);
}
void main() {
  vec2 uv = vUV;
  vec3 col = frameAt(uv);
  if (uBloom > 0.0) {
    vec3 b = vec3(0.0);
    vec2 px = 1.0 / uRes;
    for (int i = 0; i < 8; i++) {
      float a = float(i) / 8.0 * 6.2831853;
      vec2 o = vec2(cos(a), sin(a));
      b += max(frameAt(uv + o * px * 3.0) - 0.55, 0.0);
      b += max(frameAt(uv + o * px * 6.0) - 0.55, 0.0);
    }
    col += uBloom * b * 0.35;
  }
  col *= 1.0 + uGlow * 0.35;
  if (uHasTint > 0.5) { float l = dot(col, LUMA); col = uTint * l * 1.15; }
  float scan = 0.5 + 0.5 * cos(uv.y * uRes.y * 3.14159 * 0.5);
  col *= 1.0 - uScanline * scan * 0.65;
  float d = distance(uv, vec2(0.5));
  col *= 1.0 - uVignette * smoothstep(0.35, 0.9, d);
  if (uRoll > 0.0) {
    float bandY = fract(uTime * 0.09);
    float band = smoothstep(0.06, 0.0, abs(uv.y - bandY));
    float noise = 0.5 + 0.5 * sin(uv.x * uRes.x * 0.7 + uTime * 60.0);
    col += uRoll * band * 0.6 * noise;
  }
  col *= 1.0 - uFlicker * 0.08 * (0.5 + 0.5 * sin(uTime * 47.0));
  outColor = vec4(col, 1.0);
}`;

  const fx = (function () {
    let gl = null, prog = null, buf = null, loc = {}, uiTex = null, bgTex = null;
    let current = null, startTime = 0, rafId = 0;

    function makeTex() {
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
        new Uint8Array([24, 24, 24, 255]));
      return t;
    }
    function compile(type, src) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        console.warn("PuiKit post-effect shader failed:", gl.getShaderInfoLog(s));
        return null;
      }
      return s;
    }
    function ensureGl() {
      if (gl) return gl;
      gl = fxCanvas.getContext("webgl2", { premultipliedAlpha: false, antialias: false });
      if (!gl) return null;
      buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      const vs = compile(gl.VERTEX_SHADER, FX_VERT);
      const fs = compile(gl.FRAGMENT_SHADER, FX_FRAG);
      const p = gl.createProgram();
      gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
        console.warn("PuiKit post-effect link failed:", gl.getProgramInfoLog(p));
        gl = null; return null;
      }
      prog = p;
      for (const u of ["uUI", "uBg", "uRes", "uTime", "uTint", "uHasTint", "uHasBg",
        "uBloom", "uScanline", "uVignette", "uGlow", "uFlicker", "uRoll"]) {
        loc[u] = gl.getUniformLocation(prog, u);
      }
      loc.p = gl.getAttribLocation(prog, "p");
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true); // canvas top row -> texture top
      uiTex = makeTex();
      bgTex = makeTex();
      return gl;
    }

    function setEffect(msg) {
      if (!ensureGl()) {
        console.warn("PuiKit: WebGL2 unavailable — no post effect.");
        return;
      }
      current = {
        tint: msg.tint ? [msg.tint[0] / 255, msg.tint[1] / 255, msg.tint[2] / 255] : null,
        bloom: msg.bloom || 0, scanline: msg.scanline || 0, vignette: msg.vignette || 0,
        glow: msg.glow || 0, flicker: msg.flicker || 0, roll: msg.roll || 0,
      };
      startTime = performance.now();
      fxUiDirty = true;
      fxCanvas.style.display = "block";
      if (!rafId) rafId = requestAnimationFrame(frame);
    }
    function clear() {
      current = null;
      fxCanvas.style.display = "none";
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    }

    function frame(now) {
      rafId = 0;
      if (!current || !gl) return;
      const w = Math.max(1, Math.round(window.innerWidth * dpr));
      const h = Math.max(1, Math.round(window.innerHeight * dpr));
      if (fxCanvas.width !== w || fxCanvas.height !== h) { fxCanvas.width = w; fxCanvas.height = h; }
      fxCanvas.style.width = window.innerWidth + "px";
      fxCanvas.style.height = window.innerHeight + "px";

      const hasBg = bgCanvas.style.display !== "none";
      // Re-upload the UI only when it changed; the background animates, so refresh
      // it every frame while it is active.
      if (fxUiDirty) {
        gl.bindTexture(gl.TEXTURE_2D, uiTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, canvas);
        fxUiDirty = false;
      }
      if (hasBg) {
        gl.bindTexture(gl.TEXTURE_2D, bgTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, bgCanvas);
      }

      gl.viewport(0, 0, w, h);
      gl.useProgram(prog);
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.enableVertexAttribArray(loc.p);
      gl.vertexAttribPointer(loc.p, 2, gl.FLOAT, false, 0, 0);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, uiTex); gl.uniform1i(loc.uUI, 0);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, bgTex); gl.uniform1i(loc.uBg, 1);
      const t = current;
      gl.uniform2f(loc.uRes, w, h);
      gl.uniform1f(loc.uTime, (now - startTime) / 1000);
      gl.uniform3fv(loc.uTint, t.tint || [0, 0, 0]);
      gl.uniform1f(loc.uHasTint, t.tint ? 1 : 0);
      gl.uniform1f(loc.uHasBg, hasBg ? 1 : 0);
      gl.uniform1f(loc.uBloom, t.bloom);
      gl.uniform1f(loc.uScanline, t.scanline);
      gl.uniform1f(loc.uVignette, t.vignette);
      gl.uniform1f(loc.uGlow, t.glow);
      gl.uniform1f(loc.uFlicker, t.flicker);
      gl.uniform1f(loc.uRoll, t.roll);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      rafId = requestAnimationFrame(frame);
    }

    return { setEffect, clear };
  })();

  // --- go ----------------------------------------------------------------

  canvas.focus();
  connect();
})();

// Boot marker for debugging
try { console.info('[orb] main.js loaded'); } catch(_) {}

// === Three.js Scene Setup ===
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(
  75, window.innerWidth / window.innerHeight, 0.1, 1000
);
camera.position.set(0, 1, 7);
camera.lookAt(0, 0, 0);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setClearColor(0x111111, 1);
document.body.appendChild(renderer.domElement);

// Lighting
scene.add(new THREE.AmbientLight(0xffffff, 0.8));
const dirLight = new THREE.DirectionalLight(0xffffff, 1);
dirLight.position.set(5, 10, 7).normalize();
scene.add(dirLight);

// === Palette (match UI) ===
const UI_BLUE = 0x0064ff;   // like frame borders
const UI_CYAN = 0x00ffff;   // like "+" button & text

// === Shader Orb (blue->cyan pulse) ===
const orbUniforms = {
  time: { value: 0.0 },
  pulse: { value: 0.0 },
  baseColor:  { value: new THREE.Color(UI_BLUE) },
  accentColor:{ value: new THREE.Color(UI_CYAN) }
};

const orb = new THREE.Mesh(
  new THREE.SphereGeometry(0.5, 64, 64),
  new THREE.ShaderMaterial({
    uniforms: orbUniforms,
    vertexShader: `
      varying vec3 vNormal;
      void main() {
        vNormal = normalize(normalMatrix * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      uniform float time;
      uniform float pulse;
      uniform vec3 baseColor;
      uniform vec3 accentColor;
      varying vec3 vNormal;

      void main() {
        // subtle moving light across the normal
        float moving = 0.5 + 0.5 * abs(dot(vNormal, normalize(vec3(sin(time*0.7), cos(time*0.9), 1.0))));
        // pulse pushes color towards cyan; otherwise stays closer to deep blue
        float mixAmt = mix(0.25, 1.0, pulse);
        vec3 color = mix(baseColor, accentColor, mixAmt);
        gl_FragColor = vec4(color * moving, 1.0);
      }
    `,
    side: THREE.DoubleSide
  })
);
orb.position.set(0, -1, 0);
scene.add(orb);

// === Glow Shell (cyan/blue additive glow) ===
const shell = new THREE.Mesh(
  new THREE.SphereGeometry(0.55, 64, 64),
  new THREE.ShaderMaterial({
    uniforms: {
      time: { value: 0.0 },
      baseColor:   { value: new THREE.Color(UI_BLUE) },
      accentColor: { value: new THREE.Color(UI_CYAN) }
    },
    transparent: true,
    side: THREE.BackSide,
    blending: THREE.AdditiveBlending,
    vertexShader: `
      varying vec3 vNormal;
      void main() {
        vNormal = normalize(normalMatrix * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      uniform float time;
      uniform vec3 baseColor;
      uniform vec3 accentColor;
      varying vec3 vNormal;
      void main() {
        // shimmering rim-ish glow
        float rim = pow(1.0 - max(dot(vNormal, vec3(0.0,0.0,1.0)), 0.0), 1.2);
        float flicker = 0.25 + 0.25 * sin(time * 2.0 + dot(vNormal, vec3(0.0, 1.0, 0.0)) * 10.0);
        float a = clamp(rim + flicker, 0.0, 0.5);
        vec3 c = mix(baseColor, accentColor, 0.6);
        gl_FragColor = vec4(c, a);
      }
    `
  })
);
shell.position.set(0, -1, 0);
scene.add(shell);

// === Floating Glow Sprites (cyan radial) ===
function createGlowTexture() {
  const s = 128;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const g = c.getContext('2d');
  const r = s / 2;
  const grd = g.createRadialGradient(r, r, 0, r, r, r);
  grd.addColorStop(0.00, 'rgba(255,255,255,0.9)');
  grd.addColorStop(0.40, 'rgba(0,255,255,0.6)');   // cyan mid
  grd.addColorStop(1.00, 'rgba(0,180,255,0.0)');   // blue-cyan fade out
  g.fillStyle = grd;
  g.fillRect(0, 0, s, s);
  const tex = new THREE.Texture(c);
  tex.needsUpdate = true;
  return tex;
}
const glowTexture = createGlowTexture();

for (let i = 0; i < 3; i++) {
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: glowTexture,
    color: UI_CYAN,
    transparent: true,
    opacity: 0.25,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  }));
  sprite.scale.set(2.2 + i * 0.3, 2.2 + i * 0.3, 1);
  sprite.position.set(0, -1, 0);
  scene.add(sprite);
}

// === Particle Shell with Animation (light blue) ===
const particleGeo = new THREE.BufferGeometry();
const count = 250;
const positions = [];
const sphericalData = [];

for (let i = 0; i < count; i++) {
  const theta = Math.random() * 2 * Math.PI;
  const phi = Math.acos(2 * Math.random() - 1);
  const baseR = 0.7 + Math.random() * 0.1;
  sphericalData.push({ theta, phi, baseR });

  const x = baseR * Math.sin(phi) * Math.cos(theta);
  const y = baseR * Math.sin(phi) * Math.sin(theta) - 1;
  const z = baseR * Math.cos(phi);
  positions.push(x, y, z);
}

particleGeo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
particleGeo.userData.spherical = sphericalData;

const particleMat = new THREE.PointsMaterial({
  size: 0.04,
  color: 0x66ccff, // light blue particles
  transparent: true,
  opacity: 0.7,
  depthWrite: false,
  blending: THREE.AdditiveBlending
});

const particleSystem = new THREE.Points(particleGeo, particleMat);
scene.add(particleSystem);

// === Socket Handling ===
const socket = io();
window.socket = socket;
let speaking = false;
const speechBuffers = new Map();
// Default off: Mitch should speak via server-streamed voice only unless explicitly enabled.
const browserTtsEnabled = window.MITCH_BROWSER_TTS === true;
let browserTtsReady = false;
let browserTtsVoice = null;
const browserTtsQueue = [];
let browserTtsActive = false;
let serverAudioActive = false;
let serverAudioContext = null;
let serverAudioCursor = 0;
let serverAudioDecayTimer = null;
let currentAudioMode = "none"; // "none" | "browser" | "server"
let audioOrbitAngle = 0;

function updateAssistantSpeakingState() {
  const browserQueueActive = browserTtsEnabled && browserTtsQueue && browserTtsQueue.length;
  const audioSpeaking = !!(serverAudioActive || browserTtsActive || browserQueueActive);
  window.__mitchAudioSpeaking = audioSpeaking;
  window.__mitchAssistantSpeaking = !!(speaking || audioSpeaking);
}

function selectBrowserVoice() {
  if (!("speechSynthesis" in window)) return null;
  const voices = window.speechSynthesis.getVoices() || [];
  if (!voices.length) return null;
  const preferred = voices.find(v => /en-GB/i.test(v.lang))
    || voices.find(v => /en-/i.test(v.lang))
    || voices[0];
  return preferred || null;
}

function sanitizeForSpeech(text) {
  if (!text) return "";
  let t = String(text);
  t = t.replace(/```[\s\S]*?```/g, " ");
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, "$1");
  t = t.replace(/https?:\/\/\S+/g, "");
  t = t.replace(/\bwww\.\S+/g, "");
  t = t.replace(/[`*_#>-]/g, " ");
  t = t.replace(/[|~<>]+/g, " ");
  t = t.replace(/[\\/]{2,}/g, " ");
  t = t.replace(/\s*\/\s*/g, " ");
  t = t.replace(/\s*[-–—]\s*/g, ", ");
  t = t.replace(/([!?.,;:])\1+/g, "$1");
  t = t.replace(/\s+/g, " ").trim();
  return t;
}

function initBrowserTts() {
  if (!browserTtsEnabled) return;
  if (!("speechSynthesis" in window)) {
    console.warn("[orb] Browser speechSynthesis unavailable.");
    return;
  }
  browserTtsVoice = selectBrowserVoice();
  browserTtsReady = true;
}

function setAudioMode(mode) {
  currentAudioMode = mode || "none";
  if (!window.__audioIndicator) return;
  const { browserSat, serverSat, orbitRing } = window.__audioIndicator;
  if (browserSat) browserSat.visible = currentAudioMode === "browser";
  if (serverSat) serverSat.visible = currentAudioMode === "server";
  if (orbitRing) {
    orbitRing.visible = currentAudioMode !== "none";
    if (currentAudioMode === "server") {
      orbitRing.material.color.setHex(0x00ffb3); // server = neon green
    } else if (currentAudioMode === "browser") {
      orbitRing.material.color.setHex(0x4da3ff); // browser = blue
    }
  }
  updateAssistantSpeakingState();
}

function ensureServerAudioContext() {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  if (!serverAudioContext) serverAudioContext = new AC();
  if (serverAudioContext.state === "suspended") {
    serverAudioContext.resume().catch(() => {});
  }
  return serverAudioContext;
}

function b64ToArrayBuffer(b64) {
  const binary = atob(b64);
  const len = binary.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function queueServerAudioChunk(audioB64) {
  const ctx = ensureServerAudioContext();
  if (!ctx || !audioB64) return;
  const arr = b64ToArrayBuffer(audioB64);
  ctx.decodeAudioData(arr.slice(0), (buffer) => {
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    if (serverAudioCursor < now) serverAudioCursor = now;
    src.start(serverAudioCursor);
    serverAudioCursor += buffer.duration;

    // Keep server-audio indicator in sync with actual queued audio tail.
    if (serverAudioDecayTimer) clearTimeout(serverAudioDecayTimer);
    const remainingMs = Math.max(0, (serverAudioCursor - ctx.currentTime) * 1000) + 250;
    serverAudioDecayTimer = setTimeout(() => {
      serverAudioActive = false;
      if (!browserTtsActive && !browserTtsQueue.length) setAudioMode("none");
      updateAssistantSpeakingState();
    }, remainingMs);
  }, (err) => {
    console.warn("[orb] decodeAudioData failed:", err);
  });
}

function speakNextBrowserTts() {
  if (!browserTtsEnabled || !browserTtsReady || browserTtsActive || serverAudioActive) return;
  const next = browserTtsQueue.shift();
  if (!next) return;
  const payload = sanitizeForSpeech(next);
  if (!payload) {
    speakNextBrowserTts();
    return;
  }
  const utter = new SpeechSynthesisUtterance(payload);
  if (browserTtsVoice) utter.voice = browserTtsVoice;
  utter.rate = 1.0;
  utter.pitch = 1.0;
  utter.volume = 1.0;
  browserTtsActive = true;
  updateAssistantSpeakingState();
  utter.onend = () => {
    browserTtsActive = false;
    if (!browserTtsQueue.length && !serverAudioActive) setAudioMode("none");
    updateAssistantSpeakingState();
    speakNextBrowserTts();
  };
  utter.onerror = () => {
    browserTtsActive = false;
    if (!browserTtsQueue.length) setAudioMode("none");
    updateAssistantSpeakingState();
    speakNextBrowserTts();
  };
  setAudioMode("browser");
  window.speechSynthesis.speak(utter);
}

if (browserTtsEnabled && "speechSynthesis" in window) {
  window.speechSynthesis.onvoiceschanged = () => {
    browserTtsVoice = selectBrowserVoice();
    browserTtsReady = true;
  };
}
initBrowserTts();

["click", "keydown", "touchstart"].forEach((ev) => {
  window.addEventListener(ev, () => {
    ensureServerAudioContext();
    if ("speechSynthesis" in window && window.speechSynthesis.paused) {
      try { window.speechSynthesis.resume(); } catch (_) {}
    }
  }, { passive: true });
});

socket.on("browser_audio", ({ audio_b64 } = {}) => {
  if (!audio_b64) return;
  serverAudioActive = true;
  setAudioMode("server");
  updateAssistantSpeakingState();
  queueServerAudioChunk(audio_b64);
});

socket.on('speak_chunk', ({ chunk, token } = {}) => {
  speaking = true;
  updateAssistantSpeakingState();
  if (!token || !chunk) return;
  const prev = speechBuffers.get(token) || "";
  speechBuffers.set(token, prev + chunk);
});
socket.on('speak_end', ({ token, text } = {}) => {
  speaking = false;
  updateAssistantSpeakingState();
  const buffered = token ? (speechBuffers.get(token) || "") : "";
  if (token) speechBuffers.delete(token);
  const finalText = (text && String(text).trim()) ? String(text) : buffered;
  if (!finalText) return;
  if (serverAudioActive) {
    if (serverAudioDecayTimer) clearTimeout(serverAudioDecayTimer);
    serverAudioDecayTimer = setTimeout(() => {
      serverAudioActive = false;
      if (!browserTtsActive && !browserTtsQueue.length) setAudioMode("none");
      updateAssistantSpeakingState();
    }, 250);
    return;
  }
  if (!browserTtsEnabled) return;
  browserTtsQueue.push(finalText);
  updateAssistantSpeakingState();
  speakNextBrowserTts();
});

/* === Audio Source Orbital Indicator ===
   square   -> browser TTS
   triangle -> server-side streamed voice
*/
const audioOrbitPivot = new THREE.Object3D();
audioOrbitPivot.position.set(0, -1, 0);
scene.add(audioOrbitPivot);

const orbitRing = new THREE.Mesh(
  new THREE.TorusGeometry(1.35, 0.012, 12, 120),
  new THREE.MeshBasicMaterial({
    color: 0x4da3ff,
    transparent: true,
    opacity: 0.75,
    depthTest: false,
    depthWrite: false
  })
);
orbitRing.rotation.x = Math.PI * 0.45;
orbitRing.visible = false;
orbitRing.renderOrder = 1100;
audioOrbitPivot.add(orbitRing);

const browserSat = new THREE.Mesh(
  new THREE.BoxGeometry(0.18, 0.18, 0.18),
  new THREE.MeshBasicMaterial({
    color: 0x4da3ff,
    transparent: true,
    opacity: 1.0,
    depthTest: false,
    depthWrite: false
  })
);
browserSat.position.set(1.35, 0.52, 0);
browserSat.visible = false;
browserSat.renderOrder = 1101;
audioOrbitPivot.add(browserSat);

const serverSat = new THREE.Mesh(
  new THREE.ConeGeometry(0.14, 0.24, 3),
  new THREE.MeshBasicMaterial({
    color: 0x00ffb3,
    transparent: true,
    opacity: 1.0,
    depthTest: false,
    depthWrite: false
  })
);
serverSat.position.set(1.35, 0.52, 0);
serverSat.rotation.z = Math.PI / 2;
serverSat.visible = false;
serverSat.renderOrder = 1101;
audioOrbitPivot.add(serverSat);

window.__audioIndicator = { browserSat, serverSat, orbitRing, pivot: audioOrbitPivot };

// Connection diagnostics
try {
  socket.on('connect', () => console.info('[orb] socket connected:', socket.id));
  socket.on('disconnect', (r) => console.warn('[orb] socket disconnected:', r));
  socket.io.on('error', (e) => console.error('[orb] io error:', e));
  socket.on('connect_error', (e) => console.error('[orb] connect_error:', e && e.message || e));
  socket.onAny((ev, ...args) => {
    if (ev !== 'video_frame') { // avoid spamming
      console.debug('[orb] event:', ev, args && args[0]);
    }
  });
} catch (_) {}

const logConsole = document.getElementById("log-console");
const logLines = [];
socket.on("INNEMONO_LINE", (data) => {
  if (!data.line) return;
  logLines.push(data.line);
  if (logLines.length > 10) logLines.shift();
  logConsole.innerHTML = logLines.map(line => `<div>${line}</div>`).join("");
  logConsole.scrollTop = logConsole.scrollHeight;
});

// === Workspace URL opener (fallback binding) ===
function openWorkspaceUrl(url) {
  const frame = document.getElementById("workspace-frame");
  const fb = document.getElementById("workspace-fallback");
  const btn = document.getElementById("workspace-open");
  const msg = document.getElementById("workspace-msg");
  if (!frame || !url || !/^https?:\/\//i.test(url)) return;

  if (fb) fb.style.display = "none";
  frame.src = url;

  // Keep an explicit external-open path available even if embedding fails.
  if (btn) {
    btn.onclick = () => window.open(url, "_blank", "noopener");
  }

  let handled = false;
  const timer = setTimeout(() => {
    if (handled) return;
    handled = true;
    if (fb) {
      if (msg) msg.textContent = `Site refused to be embedded: ${url}`;
      fb.style.display = "block";
    }
  }, 3000);

  frame.addEventListener(
    "load",
    () => {
      if (handled) return;
      handled = true;
      clearTimeout(timer);
      if (fb) fb.style.display = "none";
    },
    { once: true }
  );
}

if (!window.__mitchOpenUrlBound) {
  socket.on("OPEN_URL", (data) => {
    const url = data && data.url;
    if (!url || typeof url !== "string") return;
    openWorkspaceUrl(url);
    if (logConsole) {
      const d = document.createElement("div");
      d.textContent = `[OPEN_URL/main.js] ${url}`;
      logConsole.appendChild(d);
      while (logConsole.children.length > 25) logConsole.removeChild(logConsole.firstChild);
      logConsole.scrollTop = logConsole.scrollHeight;
    }
  });
  window.__mitchOpenUrlBound = true;
}

// === Token Drift Diagnostics ===
const driftCanvas = document.getElementById("token-drift-canvas");
const driftCtx = driftCanvas ? driftCanvas.getContext("2d") : null;
const driftSeries = [];
const DRIFT_MAX_POINTS = 48;

function pushDriftPoint(point) {
  driftSeries.push(point);
  if (driftSeries.length > DRIFT_MAX_POINTS) driftSeries.shift();
  drawDriftChart();
}

function drawDriftChart() {
  if (!driftCtx || !driftCanvas) return;
  const r = driftCanvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const W = Math.max(1, Math.floor(r.width));
  const H = Math.max(1, Math.floor(r.height));
  if (driftCanvas.width !== W * dpr || driftCanvas.height !== H * dpr) {
    driftCanvas.width = W * dpr;
    driftCanvas.height = H * dpr;
  }
  driftCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  driftCtx.clearRect(0, 0, W, H);

  driftCtx.fillStyle = "rgba(0,0,0,0.35)";
  driftCtx.fillRect(0, 0, W, H);

  driftCtx.fillStyle = "#88e8ff";
  driftCtx.font = "600 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
  driftCtx.fillText("Token Drift (prompt chars + response chars)", 8, 14);

  if (!driftSeries.length) return;

  const values = driftSeries.map((p) => (p.prompt_total_chars || 0) + (p.response_chars || 0));
  const maxV = Math.max(...values, 1);
  const minV = Math.min(...values, 0);
  const span = Math.max(1, maxV - minV);
  const left = 8, right = W - 8, top = 20, bottom = H - 20;

  driftCtx.strokeStyle = "rgba(80,180,255,0.2)";
  driftCtx.lineWidth = 1;
  for (let i = 0; i < 3; i++) {
    const y = top + ((bottom - top) * i) / 2;
    driftCtx.beginPath();
    driftCtx.moveTo(left, y);
    driftCtx.lineTo(right, y);
    driftCtx.stroke();
  }

  driftCtx.strokeStyle = "#00d7ff";
  driftCtx.lineWidth = 2;
  driftCtx.beginPath();
  values.forEach((v, i) => {
    const x = left + (i * (right - left)) / Math.max(values.length - 1, 1);
    const y = bottom - ((v - minV) / span) * (bottom - top);
    if (i === 0) driftCtx.moveTo(x, y);
    else driftCtx.lineTo(x, y);
  });
  driftCtx.stroke();

  const last = driftSeries[driftSeries.length - 1];
  const lastTotal = (last.prompt_total_chars || 0) + (last.response_chars || 0);
  const prev = driftSeries.length > 1 ? driftSeries[driftSeries.length - 2] : null;
  const prevTotal = prev ? (prev.prompt_total_chars || 0) + (prev.response_chars || 0) : lastTotal;
  const delta = lastTotal - prevTotal;

  driftCtx.fillStyle = "#9fe7ff";
  driftCtx.fillText(`now: ${lastTotal} chars`, 8, H - 6);
  driftCtx.fillStyle = delta >= 0 ? "#66ff99" : "#ff8e8e";
  driftCtx.fillText(`drift: ${delta >= 0 ? "+" : ""}${delta}`, W - 130, H - 6);
}

socket.on("TOKEN_DIAGNOSTICS", (data) => {
  if (!data || typeof data !== "object") return;
  pushDriftPoint({
    token: data.token,
    engine: data.engine,
    prompt_total_chars: Number(data.prompt_total_chars || 0),
    response_chars: Number(data.response_chars || 0),
    injection_chars: Number(data.injection_chars || 0),
    timestamp: Date.now()
  });
});

window.addEventListener("resize", drawDriftChart);

// === Map Pin Handler (force-visible cones + pulsing halo) ===
const activePins = [];
socket.on("EMIT_MAP_PIN", ({ lat, lon, label, description }) => {
  if (typeof lat !== 'number' || typeof lon !== 'number') return;
  console.log("📍 Map Pin:", label, lat, lon);
  try {
    if (logConsole) {
      const msg = `📍 ${label || 'Pin'} @ ${lat.toFixed(3)}, ${lon.toFixed(3)}${description? ' — '+description: ''}`;
      const div = document.createElement('div');
      div.textContent = msg;
      logConsole.appendChild(div);
      if (logConsole.children.length > 25) logConsole.removeChild(logConsole.firstChild);
      logConsole.scrollTop = logConsole.scrollHeight;
    }
  } catch (_) {}

  // Push outside the glow/particles so it’s clearly visible
  const radius = 0.70; // was 0.58
  const phi   = (90 - lat) * (Math.PI / 180);
  const theta = (lon + 180) * (Math.PI / 180);

  const x = radius * Math.sin(phi) * Math.cos(theta);
  const y = radius * Math.cos(phi) - 1; // orb center is y = -1
  const z = radius * Math.sin(phi) * Math.sin(theta);

  // Cone: render on top of everything (no depth test), slightly bigger, double-sided
  const pin = new THREE.Mesh(
    new THREE.ConeGeometry(0.10, 0.36, 16),
    new THREE.MeshBasicMaterial({
      color: 0xff0040,
      depthTest: false,
      depthWrite: false,
      transparent: true,
      opacity: 1.0,
      side: THREE.DoubleSide
    })
  );
  pin.position.set(x, y, z);
  pin.lookAt(0, -1, 0);
  pin.renderOrder = 1000;
  scene.add(pin);

  // Glow dot: also on top (pulses in animate)
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(0.09, 16, 16),
    new THREE.MeshBasicMaterial({
      color: 0xff6666,
      depthTest: false,
      depthWrite: false,
      transparent: true,
      opacity: 0.95
    })
  );
  dot.position.set(x, y, z);
  dot.renderOrder = 1000;
  scene.add(dot);

  activePins.push({ pin, dot, created: performance.now() });
});

// Simple debug helper to verify 3D pins without backend
window.debugOrbPin = function(lat, lon, label, description){
  socket.emit && console.debug('[debugOrbPin] adding local pin');
  socket.listeners && console.debug('[debugOrbPin]');
  socket.emit?.("__noop");
  // Reuse the same logic by emitting a synthetic event to our handler
  const evt = new Event('synthetic');
  // Call handler directly
  try { window.dispatchEvent(evt); } catch (_) {}
  // Duplicate minimal logic inline
  if (typeof lat !== 'number' || typeof lon !== 'number') return false;
  const radius = 0.70;
  const phi   = (90 - lat) * (Math.PI / 180);
  const theta = (lon + 180) * (Math.PI / 180);
  const x = radius * Math.sin(phi) * Math.cos(theta);
  const y = radius * Math.cos(phi) - 1;
  const z = radius * Math.sin(phi) * Math.sin(theta);
  const pin = new THREE.Mesh(
    new THREE.ConeGeometry(0.10, 0.36, 16),
    new THREE.MeshBasicMaterial({ color: 0xff0040, depthTest: false, depthWrite: false, transparent: true, opacity: 1.0, side: THREE.DoubleSide })
  );
  pin.position.set(x, y, z); pin.lookAt(0, -1, 0); pin.renderOrder = 1000; scene.add(pin);
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(0.09, 16, 16),
    new THREE.MeshBasicMaterial({ color: 0xff6666, depthTest: false, depthWrite: false, transparent: true, opacity: 0.95 })
  );
  dot.position.set(x, y, z); dot.renderOrder = 1000; scene.add(dot);
  activePins.push({ pin, dot, created: performance.now() });
  return true;
};


// === Animate ===
const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  const t = clock.getElapsedTime();

  orbUniforms.time.value = t;
  // speaking drives the blue->cyan mix
  orbUniforms.pulse.value = speaking ? 1.0 : 0.0;
  shell.material.uniforms.time.value = t;

  const posAttr = particleGeo.attributes.position;
  const spherical = particleGeo.userData.spherical;

  for (let i = 0; i < count; i++) {
    const { theta, phi, baseR } = spherical[i];
    const r = baseR + 0.02 * Math.sin(t * 2.0 + i);
    const i3 = i * 3;
    posAttr.array[i3]     = r * Math.sin(phi) * Math.cos(theta);
    posAttr.array[i3 + 1] = r * Math.sin(phi) * Math.sin(theta) - 1;
    posAttr.array[i3 + 2] = r * Math.cos(phi);
  }
  posAttr.needsUpdate = true;

  // Pulse pin dots to draw attention
  if (activePins.length) {
    const pulse = 0.85 + 0.25 * (0.5 + 0.5 * Math.sin(t * 3.2));
    for (let i = 0; i < activePins.length; i++) {
      const ap = activePins[i];
      if (!ap || !ap.dot) continue;
      ap.dot.scale.set(pulse, pulse, pulse);
    }
  }

  // Audio source indicator satellite orbit
  if (currentAudioMode !== "none") {
    audioOrbitAngle += 0.015;
    audioOrbitPivot.rotation.y = audioOrbitAngle;
    const pulse = 1 + 0.18 * Math.sin(t * 5.0);
    browserSat.scale.set(pulse, pulse, pulse);
    serverSat.scale.set(pulse, pulse, pulse);
    browserSat.rotation.x += 0.03;
    browserSat.rotation.y += 0.03;
    serverSat.rotation.y += 0.05;
  } else {
    browserSat.scale.set(1, 1, 1);
    serverSat.scale.set(1, 1, 1);
  }

  renderer.render(scene, camera);
}
animate();

// === Resize ===
window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

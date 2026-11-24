// SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
//
// SPDX-License-Identifier: MIT

// Mesh Chat panel: read and send Meshtastic channel and direct messages through a gateway.
// Plain web component without external dependencies, styled with Home Assistant theme variables.

// Home Assistant loads this file as a module with the cache buster of ha_frontend/version.py (?v=...). The styles
// are separate files next to it, loaded once and shared by the panel and the markers on the map.
const MODULE_URL = new URL(import.meta.url);
const PANEL_BUILD = MODULE_URL.searchParams.get("v") || "dev"; // shown in the gateway details
const loadStyleSheet = async (file) => {
  const url = new URL(file, MODULE_URL);
  url.search = MODULE_URL.search;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${file}: ${response.status}`);
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(await response.text());
  return sheet;
};
const PANEL_STYLES = loadStyleSheet("chat-panel.css");
const MARKER_STYLES = loadStyleSheet("chat-map-marker.css");
const STORAGE_KEY = "meshtastic-chat";
const HISTORY_LIMIT = 200;
const TARGET_REFRESH_MS = 120 * 1000;
const TOOLTIP_DELAY_MS = 350;

const STATUS_ICONS = {
  sending: { icon: "mdi:clock-outline", label: "Sending" },
  sent: { icon: "mdi:check", label: "Sent – heard by the mesh" },
  delivered: { icon: "mdi:check-all", label: "Delivered – acknowledged by the recipient" },
  no_ack: { icon: "mdi:help-circle-outline", label: "Sent – no acknowledgement received" },
  failed: { icon: "mdi:alert-circle-outline", label: "Failed" },
};

const TELEMETRY_TYPES = [
  ["device_metrics", "Device metrics"],
  ["environment_metrics", "Environment"],
  ["power_metrics", "Power"],
  ["air_quality_metrics", "Air quality"],
];

const NODE_REQUESTS = {
  telemetry: { icon: "mdi:chart-box-outline", label: "Request telemetry", busy: "Requesting telemetry…" },
  position: { icon: "mdi:crosshairs-gps", label: "Request position", busy: "Requesting position…" },
  traceroute: { icon: "mdi:map-marker-path", label: "Traceroute", busy: "Tracing the route…" },
};

const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

const KEY_CHOICES = [
  ["keep", "Keep the current key"],
  ["random", "New random AES-256 key"],
  ["random128", "New random AES-128 key"],
  ["default", "Default key (public, anyone can read)"],
  ["none", "No encryption"],
  ["custom", "Enter a key (base64)"],
];
// the emoji of the picker; a message is limited in bytes and these take 3 to 7 of them each
const EMOJI_GROUPS = [
  ["Smileys", "😀 😃 😄 😁 😆 😅 😂 🤣 🙂 😉 😊 😇 😍 😘 😋 😎 🤔 😐 😴 😢 😭 😡 😱 🥶 🥵"],
  ["Gestures", "👍 👎 👌 ✌️ 🤞 🤝 👏 🙌 🙏 💪 👋 ☝️ 👀"],
  ["Hearts and symbols", "❤️ 🧡 💛 💚 💙 💜 🖤 💔 💯 ✨ 🔥 ⭐ 🎉"],
  ["Outdoors and travel", "⛰️ 🏔️ 🥾 🏕️ 🌲 🌳 🌄 🌅 🧭 🔦 🚶 🚴 🚗 🚁 ✈️ 🏠 📍 🗺️"],
  ["Weather", "☀️ ⛅ ☁️ 🌧️ ⛈️ ❄️ 🌨️ 💨 🌈 🌡️"],
  ["Radio and things", "📡 📻 📶 🔋 📱 💬 📢 ⚠️ 🚨 🆘 ✅ ❌ ❓ ❗ ⏰ ☕ 🍺 🍕 🍔 🔔"],
].map(([title, emoji]) => [title, emoji.split(" ")]);
// the strip above the composer shows the emoji used last, filled up with these until enough were used
const EMOJI_RECENT_COUNT = 8;
const EMOJI_DEFAULTS = ["👍", "❤️", "😂", "🙏", "👋", "✅", "🥾", "🔥"];
const REPLY_SNIPPET_CHARS = 120;
const PRECISION_BITS = [0, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 32];
// nodes without a device tracker are shown to ha-map as states under this prefix (they never reach Home Assistant)
// a position shared with less precision is drawn as a circle of its accuracy around the marker; beyond this radius
// the circle says little and would zoom the map out to the continent
const MAP_MAX_ACCURACY_M = 50000;
const MAP_ENTITY_PREFIX = "device_tracker.meshtastic_chat_node_";
// "1.3 dB · −103 dBm · 1 hop" for a received message; empty when the gateway reported nothing
const signalText = (m) => {
  const parts = [];
  if (isSet(m.snr)) parts.push(`${formatNumber(m.snr, 1)} dB`);
  if (isSet(m.rssi)) parts.push(`${formatNumber(m.rssi, 0)} dBm`);
  if (isSet(m.hops)) parts.push(m.hops === 0 ? "direct" : `${m.hops} hop${m.hops === 1 ? "" : "s"}`);
  return parts.join(" · ");
};
const signalTitle = (m) => {
  const parts = [];
  if (isSet(m.snr)) parts.push(`signal to noise ${formatNumber(m.snr, 1)} dB`);
  if (isSet(m.rssi)) parts.push(`signal strength ${formatNumber(m.rssi, 0)} dBm`);
  if (isSet(m.hops)) parts.push(m.hops === 0 ? "heard directly" : `relayed over ${m.hops} hop${m.hops === 1 ? "" : "s"}`);
  return parts.join(", ");
};

const precisionToMeters = (bits) => (bits && bits >= 2 ? Math.round(5976446 / 2 ** (bits - 2)) : null);
const precisionLabel = (bits) => {
  if (!bits) return "Off – no positions on this channel";
  if (bits >= 32) return "Full precision";
  return `About ${formatDistance(precisionToMeters(bits))}`;
};

// Minimal QR code encoder (byte mode, error correction level M, versions 1-20) used to show channel URLs.
// Returns a square array of booleans (true = dark module). Follows ISO 18004 / Nayuki's reference structure.

const QR_EC_CODEWORDS_PER_BLOCK_M = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26];
const QR_EC_BLOCKS_M = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16];
const QR_MAX_VERSION = 20;

const qrTotalCodewords = (version) => {
  let result = (16 * version + 128) * version + 64;
  if (version >= 2) {
    const align = Math.floor(version / 7) + 2;
    result -= (25 * align - 10) * align - 55;
    if (version >= 7) result -= 36;
  }
  return Math.floor(result / 8);
};

const qrDataCodewords = (version) =>
  qrTotalCodewords(version) - QR_EC_CODEWORDS_PER_BLOCK_M[version] * QR_EC_BLOCKS_M[version];

const qrAlignmentPositions = (version) => {
  if (version === 1) return [];
  const count = Math.floor(version / 7) + 2;
  const size = version * 4 + 17;
  const step = version === 32 ? 26 : Math.ceil((version * 4 + 4) / (count * 2 - 2)) * 2;
  const result = [6];
  for (let pos = size - 7; result.length < count; pos -= step) result.splice(1, 0, pos);
  return result;
};

const gfMultiply = (x, y) => {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z & 0xff;
};

const reedSolomonRemainder = (data, degree) => {
  const divisor = new Array(degree).fill(0);
  divisor[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < degree; j++) {
      divisor[j] = gfMultiply(divisor[j], root);
      if (j + 1 < degree) divisor[j] ^= divisor[j + 1];
    }
    root = gfMultiply(root, 0x02);
  }
  const result = new Array(degree).fill(0);
  for (const b of data) {
    const factor = b ^ result.shift();
    result.push(0);
    divisor.forEach((coef, i) => {
      result[i] ^= gfMultiply(coef, factor);
    });
  }
  return result;
};

const qrEncode = (text, { forceMask = -1 } = {}) => {
  const bytes = Array.from(new TextEncoder().encode(text));
  let version = 1;
  while (version <= QR_MAX_VERSION) {
    const countBits = version < 10 ? 8 : 16;
    if (4 + countBits + bytes.length * 8 <= qrDataCodewords(version) * 8) break;
    version++;
  }
  if (version > QR_MAX_VERSION) return null;

  // data bit stream: mode, count, bytes, terminator, padding
  const bits = [];
  const push = (value, length) => {
    for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1);
  };
  push(4, 4);
  push(bytes.length, version < 10 ? 8 : 16);
  for (const b of bytes) push(b, 8);
  const capacity = qrDataCodewords(version) * 8;
  push(0, Math.min(4, capacity - bits.length));
  while (bits.length % 8) bits.push(0);
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) push(pad, 8);
  const data = [];
  for (let i = 0; i < bits.length; i += 8) data.push(parseInt(bits.slice(i, i + 8).join(""), 2));

  // split into blocks, add error correction, interleave
  const blocks = QR_EC_BLOCKS_M[version];
  const ecLength = QR_EC_CODEWORDS_PER_BLOCK_M[version];
  const total = qrTotalCodewords(version);
  const shortBlocks = blocks - (total % blocks);
  const shortLength = Math.floor(total / blocks) - ecLength;
  const dataBlocks = [];
  const ecBlocks = [];
  for (let i = 0, offset = 0; i < blocks; i++) {
    const length = shortLength + (i < shortBlocks ? 0 : 1);
    const block = data.slice(offset, offset + length);
    offset += length;
    dataBlocks.push(block);
    ecBlocks.push(reedSolomonRemainder(block, ecLength));
  }
  const codewords = [];
  for (let i = 0; i <= shortLength; i++) {
    dataBlocks.forEach((block) => {
      if (i < block.length) codewords.push(block[i]);
    });
  }
  for (let i = 0; i < ecLength; i++) ecBlocks.forEach((block) => codewords.push(block[i]));

  // modules
  const size = version * 4 + 17;
  const modules = Array.from({ length: size }, () => new Array(size).fill(false));
  const reserved = Array.from({ length: size }, () => new Array(size).fill(false));
  const set = (x, y, dark) => {
    modules[y][x] = dark;
    reserved[y][x] = true;
  };
  const finder = (x, y) => {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const xx = x + dx;
        const yy = y + dy;
        if (xx < 0 || yy < 0 || xx >= size || yy >= size) continue;
        const dist = Math.max(Math.abs(dx), Math.abs(dy));
        set(xx, yy, dist !== 2 && dist !== 4);
      }
    }
  };
  finder(3, 3);
  finder(size - 4, 3);
  finder(3, size - 4);
  for (let i = 0; i < size; i++) {
    if (!reserved[6][i]) set(i, 6, i % 2 === 0);
    if (!reserved[i][6]) set(6, i, i % 2 === 0);
  }
  const positions = qrAlignmentPositions(version);
  positions.forEach((cx, i) => {
    positions.forEach((cy, j) => {
      const corner = (i === 0 && j === 0) || (i === 0 && j === positions.length - 1) || (i === positions.length - 1 && j === 0);
      if (corner) return;
      for (let dy = -2; dy <= 2; dy++) {
        for (let dx = -2; dx <= 2; dx++) set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }
    });
  });
  const drawFormat = (mask) => {
    let rem = (0 << 3) | mask; // level M = 00
    const value = rem;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const format = ((value << 10) | rem) ^ 0x5412;
    const bit = (i) => ((format >>> i) & 1) === 1;
    for (let i = 0; i <= 5; i++) set(8, i, bit(i));
    set(8, 7, bit(6));
    set(8, 8, bit(7));
    set(7, 8, bit(8));
    for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
    set(8, size - 8, true);
  };
  drawFormat(0);
  if (version >= 7) {
    let rem = version;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const info = (version << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const dark = ((info >>> i) & 1) === 1;
      const a = size - 11 + (i % 3);
      const b = Math.floor(i / 3);
      set(a, b, dark);
      set(b, a, dark);
    }
  }

  // place codewords in the zigzag order
  let bitIndex = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert++) {
      for (let j = 0; j < 2; j++) {
        const x = right - j;
        const upward = ((right + 1) & 2) === 0;
        const y = upward ? size - 1 - vert : vert;
        if (reserved[y][x] || bitIndex >= codewords.length * 8) continue;
        modules[y][x] = ((codewords[bitIndex >>> 3] >>> (7 - (bitIndex & 7))) & 1) === 1;
        bitIndex++;
      }
    }
  }

  // masking: apply each mask, score it, keep the best
  const maskBit = (mask, x, y) => {
    switch (mask) {
      case 0: return (x + y) % 2 === 0;
      case 1: return y % 2 === 0;
      case 2: return x % 3 === 0;
      case 3: return (x + y) % 3 === 0;
      case 4: return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
      case 5: return ((x * y) % 2) + ((x * y) % 3) === 0;
      case 6: return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0;
      default: return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0;
    }
  };
  const applyMask = (mask) => {
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) if (!reserved[y][x] && maskBit(mask, x, y)) modules[y][x] = !modules[y][x];
    }
  };
  const penalty = () => {
    let result = 0;
    const runPenalty = (history) => {
      const n = history[1];
      if (n === 0 || history[2] !== n || history[3] !== n * 3 || history[4] !== n || history[5] !== n) return 0;
      return (history[0] >= n * 4 ? 1 : 0) + (history[6] >= n * 4 ? 1 : 0);
    };
    const line = (get) => {
      let runColor = false;
      let runLength = 0;
      const history = [0, 0, 0, 0, 0, 0, 0];
      const addRun = (len) => {
        if (!runColor) {
          history.pop();
          history.unshift(len);
          result += runPenalty(history) * 40;
        } else {
          history.pop();
          history.unshift(len);
        }
      };
      for (let i = 0; i < size; i++) {
        const c = get(i);
        if (c === runColor) {
          runLength++;
          if (runLength === 5) result += 3;
          else if (runLength > 5) result++;
        } else {
          addRun(runLength);
          runColor = c;
          runLength = 1;
        }
      }
      addRun(runLength);
      if (runColor) {
        history.pop();
        history.unshift(0);
        result += runPenalty(history) * 40;
      }
    };
    // the reference pads each line with light modules before checking finder-like patterns; emulate by a leading 0
    for (let y = 0; y < size; y++) line((x) => modules[y][x]);
    for (let x = 0; x < size; x++) line((y) => modules[y][x]);
    for (let y = 0; y < size - 1; y++) {
      for (let x = 0; x < size - 1; x++) {
        const c = modules[y][x];
        if (c === modules[y][x + 1] && c === modules[y + 1][x] && c === modules[y + 1][x + 1]) result += 3;
      }
    }
    let dark = 0;
    for (const row of modules) for (const m of row) if (m) dark++;
    const k = Math.ceil(Math.abs(dark * 20 - size * size * 10) / (size * size)) - 1;
    result += k * 10;
    return result;
  };
  let best = forceMask;
  if (best < 0) {
    let bestScore = Infinity;
    for (let mask = 0; mask < 8; mask++) {
      applyMask(mask);
      drawFormat(mask);
      const score = penalty();
      if (score < bestScore) {
        bestScore = score;
        best = mask;
      }
      applyMask(mask);
    }
  }
  applyMask(best);
  drawFormat(best);
  return modules;
};

const qrSvg = (text, { module = 4, margin = 4 } = {}) => {
  const modules = qrEncode(text);
  if (!modules) return "";
  const size = modules.length;
  const total = (size + margin * 2) * module;
  let path = "";
  modules.forEach((row, y) => {
    row.forEach((dark, x) => {
      if (dark) path += `M${(x + margin) * module} ${(y + margin) * module}h${module}v${module}h-${module}z`;
    });
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${total} ${total}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/><path d="${path}" fill="#000"/></svg>`;
};

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Home Assistant rejects a websocket call with { code, message }; fall back to what the error does carry rather
// than printing "[object Object]"
const errorText = (error) => {
  if (error && typeof error.message === "string" && error.message) return error.message;
  if (error && typeof error === "object") return error.code ? `Error: ${error.code}` : JSON.stringify(error);
  return String(error);
};

const utf8Length = (text) => new TextEncoder().encode(text).length;

const isSet = (value) => value !== undefined && value !== null && value !== "";

// "LONG_FAST" -> "Long fast", "CLIENT_MUTE" -> "Client mute"
const prettyEnum = (value) => {
  const text = String(value ?? "").replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
};

// "HELTEC_V3" -> "Heltec V3", "T_ECHO" -> "T Echo", "RAK4631" stays
const prettyModel = (value) =>
  /^\d+$/.test(String(value ?? ""))
    ? `Unknown model (${value})`
    : String(value ?? "")
    .split("_")
    .filter(Boolean)
    .map((word) => (/^[A-Z]+$/.test(word) ? word[0] + word.slice(1).toLowerCase() : word))
    .join(" ");


// "batteryLevel" -> "Battery level"
const prettyKey = (key) => {
  const words = String(key).replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
};

const formatNumber = (value, digits = 2) =>
  typeof value === "number" ? String(Math.round(value * 10 ** digits) / 10 ** digits) : String(value);

const formatDistance = (meters) => (meters < 1000 ? `${Math.round(meters)} m` : `${(meters / 1000).toFixed(meters < 10000 ? 1 : 0)} km`);

const compassPoint = (degrees) => COMPASS[Math.round((((degrees % 360) + 360) % 360) / 22.5) % 16];

const batteryText = (level) => (level > 100 ? "plugged in" : `${level} %`);

const formatDuration = (seconds) => {
  if (!isSet(seconds)) return "";
  const s = Math.max(0, Math.floor(seconds));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
};

// labels and units for the telemetry values a node can answer with
const METRICS = {
  batteryLevel: ["Battery", batteryText],
  voltage: ["Voltage", (v) => `${formatNumber(v, 2)} V`],
  channelUtilization: ["Channel utilisation", (v) => `${formatNumber(v, 1)} %`],
  airUtilTx: ["Air utilisation (TX)", (v) => `${formatNumber(v, 1)} %`],
  uptimeSeconds: ["Uptime", (v) => formatDuration(v)],
  temperature: ["Temperature", (v) => `${formatNumber(v, 1)} °C`],
  relativeHumidity: ["Humidity", (v) => `${formatNumber(v, 0)} %`],
  barometricPressure: ["Pressure", (v) => `${formatNumber(v, 1)} hPa`],
  gasResistance: ["Gas resistance", (v) => `${formatNumber(v, 1)} MΩ`],
  iaq: ["Air quality index", (v) => formatNumber(v, 0)],
  lux: ["Light", (v) => `${formatNumber(v, 0)} lx`],
  windSpeed: ["Wind speed", (v) => `${formatNumber(v, 1)} m/s`],
  windDirection: ["Wind direction", (v) => `${formatNumber(v, 0)}°`],
  current: ["Current", (v) => `${formatNumber(v, 0)} mA`],
  distance: ["Distance", (v) => `${formatNumber(v, 0)} mm`],
  pm10Standard: ["PM1.0", (v) => `${formatNumber(v, 0)} µg/m³`],
  pm25Standard: ["PM2.5", (v) => `${formatNumber(v, 0)} µg/m³`],
  pm100Standard: ["PM10", (v) => `${formatNumber(v, 0)} µg/m³`],
  co2: ["CO₂", (v) => `${formatNumber(v, 0)} ppm`],
};
const formatMetric = (key, value) => {
  const known = METRICS[key];
  if (known) return [known[0], known[1](value)];
  const match = /^(ch\d)(Voltage|Current)$/.exec(key);
  if (match) return [`${match[1].toUpperCase()} ${match[2].toLowerCase()}`, `${formatNumber(value, 2)} ${match[2] === "Voltage" ? "V" : "mA"}`];
  return [prettyKey(key), formatNumber(value)];
};

const loadState = () => {
  try {
    return JSON.parse(window.localStorage.getItem(STORAGE_KEY)) || {};
  } catch (e) {
    return {};
  }
};

const saveState = (state) => {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) {
    // storage unavailable, the panel works without it
  }
};

// avatars and map markers get one of 12 colours (hue-0 ... hue-11 in the style sheets), the same one for the same node
const HUE_COUNT = 12;
const avatarHue = (seed) => {
  let hash = 0;
  for (const c of String(seed)) hash = (hash * 31 + c.charCodeAt(0)) | 0;
  return Math.abs(hash) % HUE_COUNT;
};

// a node on the map: a round badge with the short name, drawn by Home Assistant's map element inside its own
// shadow root, so it brings its own style sheet
class MeshtasticChatMarker extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = "<slot></slot>";
    MARKER_STYLES.then((sheet) => {
      root.adoptedStyleSheets = [sheet];
    }).catch((err) => window.console.warn(`[Mesh Chat] marker styles: ${errorText(err)}`));
  }
}
customElements.define("meshtastic-chat-marker", MeshtasticChatMarker);

class MeshtasticChatPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._gateways = [];
    this._entryId = null;
    this._conversation = null;
    this._messages = new Map(); // `${entryId}|${conversation}` -> message[]
    this._filter = "";
    this._showThread = false;
    this._persisted = loadState();
    this._persisted.lastRead = this._persisted.lastRead || {};
    this._refreshTimer = null;
    this._unsubscribe = null;
    this._initialized = false;
    this._loadError = null;
    this._onReady = null;
    this._details = null; // { kind: "node" | "channel", id, data, error, loading, busy, results }
    this._telemetryType = TELEMETRY_TYPES[0][0];
    this._mapCard = null;
    this._mapStates = null;
    this._mapView = null; // { data, focus, filter, element } while the full map is shown
    this._tooltipTimer = null;
    this._reply = null; // { entryId, conversation, id, name, text } while a message is being answered
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._menuButton) this._menuButton.hass = hass;
    if (this._mapCard && "hass" in this._mapCard) this._mapCard.hass = this._mapHass(hass);
    if (this._mapView && this._mapView.element && "hass" in this._mapView.element) this._mapView.element.hass = hass;
    if (first) this._init();
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    if (this._menuButton) this._menuButton.narrow = narrow;
    this._applyLayout();
  }

  set panel(panel) {
    this._panel = panel;
  }

  set route(route) {
    this._route = route;
  }

  connectedCallback() {
    if (this._hass && this._initialized && !this._unsubscribe) {
      this._subscribe();
      this._loadGateways();
    }
    this._addReadyListener();
    this._startRefresh();
  }

  disconnectedCallback() {
    this._stopRefresh();
    this._removeReadyListener();
    this._hideTooltip();
    if (this._unsubscribe) {
      this._unsubscribe.then((unsub) => unsub()).catch(() => {});
      this._unsubscribe = null;
    }
  }

  // reload after the websocket reconnected; registered only while the element is in the document
  _addReadyListener() {
    if (this._onReady || !this._hass || !this._initialized) return;
    this._onReady = () => {
      this._loadGateways();
      this._reloadOpenConversation();
    };
    this._hass.connection.addEventListener("ready", this._onReady);
  }

  _removeReadyListener() {
    if (!this._onReady) return;
    const connection = this._hass && this._hass.connection;
    if (connection && typeof connection.removeEventListener === "function") {
      connection.removeEventListener("ready", this._onReady);
    }
    this._onReady = null;
  }

  // ---------- data ----------

  async _init() {
    const styles = await PANEL_STYLES.catch((err) => {
      window.console.warn(`[Mesh Chat] styles: ${errorText(err)}`);
      return null;
    });
    this._renderSkeleton(styles);
    this._initialized = true;
    await this._loadGateways();
    this._subscribe();
    this._startRefresh();
    if (this.isConnected) this._addReadyListener();
  }

  _startRefresh() {
    if (this._refreshTimer || !this._initialized) return;
    this._refreshTimer = window.setInterval(() => this._loadGateways(), TARGET_REFRESH_MS);
  }

  _stopRefresh() {
    if (this._refreshTimer) window.clearInterval(this._refreshTimer);
    this._refreshTimer = null;
  }

  async _loadGateways() {
    try {
      this._gateways = await this._hass.callWS({ type: "meshtastic/chat/gateways" });
      this._loadError = null;
    } catch (err) {
      this._loadError = errorText(err);
      this._gateways = [];
    }

    if (!this._gateways.some((g) => g.entry_id === this._entryId)) {
      const remembered = this._persisted.entryId;
      const gateway = this._gateways.find((g) => g.entry_id === remembered) || this._gateways[0];
      this._entryId = gateway ? gateway.entry_id : null;
      this._conversation = null;
      this._closeDetails();
    }

    if (this._entryId && !this._conversation) {
      const gateway = this._gateway();
      const remembered = (this._persisted.conversation || {})[this._entryId];
      const known = remembered && this._conversationInfo(gateway, remembered);
      this._selectConversation(known ? remembered : this._defaultConversation(gateway), { showThread: false });
    }
    this._render();
    if (this._mapView) this._refreshFullMap();
  }

  _subscribe() {
    if (this._unsubscribe) return;
    this._unsubscribe = this._hass.connection.subscribeMessage((update) => this._onUpdate(update), {
      type: "meshtastic/chat/subscribe",
    });
    this._unsubscribe.catch(() => {
      this._unsubscribe = null;
    });
  }

  _onUpdate(update) {
    const gateway = this._gateways.find((g) => g.entry_id === update.entry_id);
    const key = `${update.entry_id}|${update.conversation}`;

    if (update.kind === "cleared") {
      // the history is known to be empty now; an empty list keeps the thread rendered instead of "Loading…"
      this._messages.set(key, []);
      if (gateway) delete gateway.conversations[update.conversation];
      this._render();
      return;
    }

    const message = update.message;
    const cached = this._messages.get(key);
    if (cached) {
      const index = cached.findIndex((m) => m.id === message.id);
      if (index >= 0) cached[index] = message;
      else cached.push(message);
    }

    if (gateway && update.kind === "message") {
      const summary = gateway.conversations[update.conversation] || { count: 0 };
      gateway.conversations[update.conversation] = {
        ts: message.ts,
        text: message.text,
        direction: message.direction,
        from_short: message.from_short,
        count: summary.count + 1,
      };
      if (!this._conversationInfo(gateway, update.conversation)) {
        // message from a node the gateway did not know when the list was loaded
        this._loadGateways();
      }
    }

    const isOpen = update.entry_id === this._entryId && update.conversation === this._conversation;
    if (isOpen && this._isThreadVisible()) this._markRead();
    this._renderList();
    if (isOpen) this._renderMessages({ stickToBottom: true });
  }

  async _loadHistory(entryId, conversation, before) {
    const key = `${entryId}|${conversation}`;
    const request = { type: "meshtastic/chat/history", entry_id: entryId, conversation, limit: HISTORY_LIMIT };
    if (before) request.before = before;
    try {
      const messages = await this._hass.callWS(request);
      const existing = before ? this._messages.get(key) || [] : [];
      this._messages.set(key, [...messages, ...existing]);
    } catch (err) {
      this._messages.set(key, this._messages.get(key) || []);
      this._showError(errorText(err));
    }
  }

  async _reloadOpenConversation() {
    if (!this._entryId || !this._conversation) return;
    await this._loadHistory(this._entryId, this._conversation);
    this._renderMessages({ stickToBottom: true });
  }

  // ---------- helpers ----------

  _gateway() {
    return this._gateways.find((g) => g.entry_id === this._entryId);
  }

  _defaultConversation(gateway) {
    if (!gateway) return null;
    const primary = gateway.channels.find((c) => c.primary) || gateway.channels[0];
    return primary ? primary.conversation : null;
  }

  _conversationInfo(gateway, conversation) {
    if (!gateway || !conversation) return null;
    const channel = gateway.channels.find((c) => c.conversation === conversation);
    if (channel) return { kind: "channel", ...channel };
    const node = gateway.nodes.find((n) => n.conversation === conversation);
    if (node) return { kind: "node", ...node };
    if (conversation.startsWith("node:")) {
      const num = Number(conversation.slice(5));
      const userId = `!${num.toString(16).padStart(8, "0")}`;
      return { kind: "node", num, user_id: userId, long_name: userId, short_name: userId.slice(-4), conversation, stub: true };
    }
    return null;
  }

  _isUnread(gateway, conversation) {
    const summary = gateway.conversations[conversation];
    if (!summary || summary.direction !== "in") return false;
    const lastRead = (this._persisted.lastRead[gateway.entry_id] || {})[conversation] || 0;
    return summary.ts > lastRead;
  }

  _markRead() {
    const gateway = this._gateway();
    if (!gateway || !this._conversation) return;
    const summary = gateway.conversations[this._conversation];
    const lastRead = (this._persisted.lastRead[gateway.entry_id] = this._persisted.lastRead[gateway.entry_id] || {});
    lastRead[this._conversation] = summary ? summary.ts : Date.now() / 1000;
    saveState(this._persisted);
  }

  _isThreadVisible() {
    if (this._mapView) return false;
    return !this._narrow || (this._showThread && !this._details);
  }

  _language() {
    return (this._hass && this._hass.locale && this._hass.locale.language) || navigator.language;
  }

  _formatTime(ts) {
    return new Date(ts * 1000).toLocaleTimeString(this._language(), { hour: "2-digit", minute: "2-digit" });
  }

  _formatDateTime(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString(this._language(), {
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  _formatDay(ts) {
    const date = new Date(ts * 1000);
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (date.toDateString() === today.toDateString()) return "Today";
    if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
    return date.toLocaleDateString(this._language(), { weekday: "long", day: "numeric", month: "long" });
  }

  _formatDateTimeAgo(ts) {
    return `${this._formatDateTime(ts)} (${this._formatAgo(ts)} ago)`;
  }

  _formatAgo(ts) {
    if (!ts) return "";
    const seconds = Math.max(0, Date.now() / 1000 - ts);
    if (seconds < 60) return "now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  }

  _avatar(seed, label, extraClass = "") {
    const hue = avatarHue(seed);
    return `<span class="avatar ${extraClass} hue-${hue}" aria-hidden="true">${escapeHtml(
      String(label || "?").slice(0, 4)
    )}</span>`;
  }

  _channelAvatar(channel) {
    return `<span class="avatar channel ${channel.public ? "public" : ""}" aria-hidden="true"><ha-icon icon="${
      channel.public ? "mdi:earth" : "mdi:lock"
    }"></ha-icon></span>`;
  }

  _star(node, { canFavorite, large = false } = {}) {
    if (!canFavorite && !node.favorite) return "";
    const label = node.favorite ? "Remove from favourites" : "Add to favourites";
    const interactive = canFavorite
      ? `data-action="favorite" data-node="${node.num}" role="button" tabindex="0" title="${escapeHtml(label)}"`
      : 'title="Favourite on the gateway"';
    return `<span class="star ${node.favorite ? "on" : ""} ${large ? "large" : ""}" ${interactive} aria-label="${escapeHtml(
      node.favorite ? "Favourite" : label
    )}">${node.favorite ? "★" : "☆"}</span>`;
  }

  // navigate inside Home Assistant without a page reload
  _navigate(path) {
    window.history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  _moreInfo(entityId) {
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  // ---------- actions ----------

  async _selectConversation(conversation, { showThread = true } = {}) {
    const changed = conversation !== this._conversation;
    if (changed) this._cancelReply();
    this._conversation = conversation;
    this._showThread = showThread;
    if (this._entryId && conversation) {
      this._persisted.entryId = this._entryId;
      this._persisted.conversation = this._persisted.conversation || {};
      this._persisted.conversation[this._entryId] = conversation;
      saveState(this._persisted);
    }
    if (changed && this._details) {
      // keep an open details pane in step with the conversation on wide screens, show the thread on phones
      if (this._narrow) this._closeDetails({ render: false });
      else this._openDetailsFor(conversation);
    }
    this._applyLayout();
    this._renderList();
    this._renderThread();
    if (!this._entryId || !conversation) return;

    const key = `${this._entryId}|${conversation}`;
    if (!this._messages.has(key)) {
      await this._loadHistory(this._entryId, conversation);
    }
    if (this._conversation === conversation) {
      if (this._isThreadVisible()) this._markRead();
      this._renderList();
      this._renderMessages({ stickToBottom: true });
      if (showThread && !this._narrow) this._textarea.focus();
    }
  }

  async _send() {
    const gateway = this._gateway();
    const conversation = this._conversation;
    const draft = this._textarea.value;
    const text = draft.trim();
    if (!gateway || !conversation || !text || this._sending) return;
    if (this._composedLength(text) > gateway.max_bytes) return;

    const reply = this._reply && this._reply.entryId === gateway.entry_id && this._reply.conversation === conversation ? this._reply : null;
    this._sending = true;
    this._updateComposer();
    try {
      const message = await this._hass.callWS({
        type: "meshtastic/chat/send",
        entry_id: gateway.entry_id,
        conversation,
        text,
        ...(reply ? { reply_to: reply.id } : {}),
      });
      // the user may have moved on while the request was pending: the result belongs to the conversation it
      // was sent in. Normally the subscription delivers the message first; this covers a missing subscription.
      const cached = this._messages.get(`${gateway.entry_id}|${conversation}`);
      const stillOpen = this._entryId === gateway.entry_id && this._conversation === conversation;
      if (cached && !cached.some((m) => m.id === message.id)) {
        cached.push(message);
        if (stillOpen) this._renderMessages({ stickToBottom: true });
      }
      if (stillOpen && this._textarea.value === draft) {
        this._textarea.value = "";
        this._autoGrow();
      }
      if (this._reply === reply) this._cancelReply();
    } catch (err) {
      this._showError(errorText(err));
    } finally {
      this._sending = false;
      this._updateComposer();
      this._textarea.focus();
    }
  }

  // ---------- replies and emoji ----------

  _startReply(id) {
    const messages = this._messages.get(`${this._entryId}|${this._conversation}`) || [];
    const message = messages.find((m) => m.id === id);
    if (!message || !message.packet_id) return;
    this._reply = {
      entryId: this._entryId,
      conversation: this._conversation,
      id: message.id,
      name: message.direction === "out" ? "You" : message.from_name,
      text: message.text.slice(0, REPLY_SNIPPET_CHARS),
    };
    this._renderReplyBar();
    this._textarea.focus();
  }

  _cancelReply() {
    if (!this._reply) return;
    this._reply = null;
    this._renderReplyBar();
  }

  _renderReplyBar() {
    if (!this._replyEl) return;
    this._replyEl.hidden = !this._reply;
    if (!this._reply) return;
    this._replyEl.querySelector(".reply-bar-name").textContent = `Replying to ${this._reply.name}`;
    this._replyEl.querySelector(".reply-bar-snippet").textContent = this._reply.text;
  }

  // scroll to a quoted message and highlight it; nothing happens when it is not loaded (older than the page)
  _flashMessage(id) {
    const row = [...this._messagesEl.querySelectorAll("[data-message]")].find((el) => el.dataset.message === id);
    if (!row) return;
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    row.classList.add("flash");
    window.setTimeout(() => row.classList.remove("flash"), 1500);
  }

  _emojiMarkup(emoji) {
    return `<button type="button" class="emoji" data-emoji="${emoji}" aria-label="${emoji}">${emoji}</button>`;
  }

  // the emoji used last (most recent first), padded with the defaults
  _recentEmoji() {
    const used = Array.isArray(this._persisted.recentEmoji) ? this._persisted.recentEmoji : [];
    return [...new Set([...used, ...EMOJI_DEFAULTS])].slice(0, EMOJI_RECENT_COUNT);
  }

  _rememberEmoji(emoji) {
    this._persisted.recentEmoji = [emoji, ...this._recentEmoji().filter((e) => e !== emoji)].slice(0, EMOJI_RECENT_COUNT);
    saveState(this._persisted);
  }

  _toggleEmojiPicker(open = this._emojiEl.hidden) {
    if (open) this._emojiRecentEl.innerHTML = this._recentEmoji().map((e) => this._emojiMarkup(e)).join("");
    else this._toggleAllEmoji(false);
    this._emojiEl.hidden = !open;
    this._emojiButton.setAttribute("aria-expanded", String(open));
  }

  _toggleAllEmoji(open = this._emojiAllEl.hidden) {
    this._emojiAllEl.hidden = !open;
    const expand = this._emojiEl.querySelector(".emoji-expand");
    expand.setAttribute("aria-expanded", String(open));
    expand.classList.toggle("open", open);
  }

  _insertEmoji(emoji) {
    const textarea = this._textarea;
    if (textarea.disabled) return;
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? start;
    textarea.setRangeText(emoji, start, end, "end");
    this._rememberEmoji(emoji);
    textarea.focus();
    this._autoGrow();
    this._updateComposer();
  }

  async _clearConversation() {
    const gateway = this._gateway();
    const info = this._conversationInfo(gateway, this._conversation);
    if (!gateway || !info) return;
    const name = info.kind === "channel" ? info.name : info.long_name;
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Delete the stored history of "${name}" in Home Assistant? Messages on devices are not affected.`)) {
      return;
    }
    try {
      await this._hass.callWS({ type: "meshtastic/chat/clear", entry_id: gateway.entry_id, conversation: this._conversation });
    } catch (err) {
      this._showError(errorText(err));
    }
  }

  async _loadOlder() {
    const key = `${this._entryId}|${this._conversation}`;
    const messages = this._messages.get(key) || [];
    if (!messages.length) return;
    const scroller = this._messagesEl;
    const previousHeight = scroller.scrollHeight;
    await this._loadHistory(this._entryId, this._conversation, messages[0].ts);
    this._renderMessages();
    scroller.scrollTop = scroller.scrollHeight - previousHeight;
  }

  async _toggleFavorite(num) {
    const gateway = this._gateway();
    if (!gateway || !gateway.can_favorite) return;
    const node = gateway.nodes.find((n) => n.num === num);
    const details = this._details && this._details.kind === "node" && this._details.id === num ? this._details : null;
    const current = node ? node.favorite : details && details.data ? details.data.favorite : false;
    const favorite = !current;

    // optimistic: the gateway acknowledges within a few seconds, show the new state right away
    if (node) node.favorite = favorite;
    if (details && details.data) details.data.favorite = favorite;
    this._renderList();
    this._renderDetails();
    try {
      await this._hass.callWS({ type: "meshtastic/chat/favorite", entry_id: gateway.entry_id, node_id: num, favorite });
    } catch (err) {
      if (node) node.favorite = current;
      if (details && details.data) details.data.favorite = current;
      this._renderList();
      this._renderDetails();
      this._showError(`Could not ${favorite ? "add" : "remove"} the favourite: ${errorText(err)}`);
    }
  }

  _openDetailsFor(conversation) {
    const info = this._conversationInfo(this._gateway(), conversation);
    if (!info) return;
    this._openDetails(info.kind, info.kind === "channel" ? info.index : info.num);
  }

  _openNewChannel() {
    const gateway = this._gateway();
    if (!gateway || !gateway.can_manage_channels || !(gateway.free_channel_indexes || []).length) return;
    this._hideTooltip();
    this._details = {
      kind: "new-channel",
      id: gateway.free_channel_indexes[0],
      entryId: gateway.entry_id,
      data: {},
      error: null,
      loading: false,
      busy: null,
      results: {},
    };
    this._applyLayout();
    this._renderDetails();
    const name = this._detailsEl.querySelector("input[name=name]");
    if (name) name.focus();
  }

  // ---------- full map ----------

  _mapPoints(nodes) {
    return nodes.map((n) => ({
      num: n.num,
      short_name: n.short_name,
      long_name: n.long_name,
      latitude: n.position.latitude,
      longitude: n.position.longitude,
      accuracy: n.position.accuracy,
    }));
  }

  async _openFullMap(focus = null) {
    const gateway = this._gateway();
    if (!gateway) return;
    this._hideTooltip();
    if (this._narrow) this._closeDetails({ render: false });
    const view = { entryId: gateway.entry_id, data: null, error: null, focus, element: null, markers: new Map() };
    this._mapView = view;
    this._applyLayout();
    this._renderFullMap();
    try {
      view.data = await this._hass.callWS({ type: "meshtastic/chat/map", entry_id: gateway.entry_id });
    } catch (err) {
      view.error = errorText(err);
    }
    if (this._mapView === view) this._renderFullMap();
  }

  _closeFullMap({ render = true } = {}) {
    if (!this._mapView) return;
    this._mapView = null;
    this._fullMapEl.hidden = true;
    this._fullMapEl.innerHTML = "";
    if (render) {
      this._applyLayout();
      this._renderThread();
    }
  }

  async _refreshFullMap() {
    const view = this._mapView;
    const gateway = this._gateway();
    if (!view || !gateway) return;
    try {
      const data = await this._hass.callWS({ type: "meshtastic/chat/map", entry_id: gateway.entry_id });
      if (this._mapView !== view) return;
      view.data = data;
      if (view.element && "editableLocations" in view.element) view.element.editableLocations = this._fullMapLocations(view);
    } catch (e) {
      // keep what is shown
    }
  }

  _fullMapLocations(view) {
    return this._mapLocations(this._mapPoints(view.data ? view.data.nodes : []), view.markers, view.focus);
  }

  // locations for Home Assistant's map element; markers are kept per node so they survive refreshes
  _mapLocations(points, markers = new Map(), focus = null) {
    const locations = [];
    for (const p of points) {
      let element = markers.get(p.num);
      if (!element) {
        element = this._mapMarkerElement(p);
        markers.set(p.num, element);
      }
      element.classList.toggle("focused", p.num === focus);
      locations.push({
        id: `node-${p.num}`,
        location: [p.latitude, p.longitude],
        title: p.long_name,
        element,
        elementSize: [36, 36],
        activatable: true,
        locationEditable: false,
        radiusEditable: false,
        fit: focus === null,
      });
      // The accuracy circle is a location of its own that does not take part in fitting the map: the map fits the
      // circles of the locations it fits, and one coarse circle (the gateway's, say) would zoom the whole map out
      // until the circles of the other nodes are hidden behind their markers.
      const radius = this._mapRadius(p);
      if (radius) {
        const key = `accuracy-${p.num}`;
        let center = markers.get(key);
        if (!center) {
          center = document.createElement("span"); // the circle needs a centre element; there is nothing to show
          markers.set(key, center);
        }
        locations.push({
          id: key,
          location: [p.latitude, p.longitude],
          radius,
          element: center,
          elementSize: [0, 0],
          activatable: false,
          locationEditable: false,
          radiusEditable: false,
          fit: false,
        });
      }
    }
    return locations;
  }

  // meters around the marker where the node's real position can be; none for exact or very coarse positions
  _mapRadius(point) {
    return point.accuracy > 0 && point.accuracy <= MAP_MAX_ACCURACY_M ? point.accuracy : null;
  }

  // a click on a node marker (the map fires it from inside its own shadow root)
  _onMapLocationClicked(ev) {
    const id = ev.detail && ev.detail.id;
    if (typeof id !== "string" || !id.startsWith("node-")) return;
    ev.stopPropagation();
    this._openDetails("node", Number(id.slice(5)));
  }

  _renderFullMap() {
    const el = this._fullMapEl;
    const view = this._mapView;
    if (!el) return;
    if (!view) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    el.innerHTML = `
      <div class="fullmap-canvas">${view.data ? "" : `<p class="empty center">${escapeHtml(view.error || "Loading…")}</p>`}</div>
      <button class="icon-button map-close" data-action="close-map" aria-label="Back to the chat" title="Back to the chat">
        <ha-icon icon="mdi:close"></ha-icon>
      </button>`;
    if (!view.data) return;
    this._mountFullMap(el.querySelector(".fullmap-canvas"), view);
  }

  async _mountFullMap(canvas, view) {
    const loaded = await this._loadMapElement(canvas);
    if (this._mapView !== view || !canvas.isConnected) return;
    if (!loaded) {
      canvas.innerHTML = `<p class="empty center">The map element could not be loaded in this frontend.</p>`;
      return;
    }
    const map = document.createElement("ha-map");
    if (!("editableLocations" in map)) {
      canvas.innerHTML = `<p class="empty center">This Home Assistant version is too old for the map view.</p>`;
      this._mapNote("the map element has no editableLocations in this frontend");
      return;
    }
    map.entities = [];
    map.editableLocations = this._fullMapLocations(view);
    map.autoFit = view.focus === null;
    map.themeMode = "auto";
    if ("hass" in map) map.hass = this._hass;
    canvas.replaceChildren(map);
    view.element = map;
    if (view.focus !== null) this._focusMapNode(view.focus);
  }

  _focusMapNode(num) {
    const view = this._mapView;
    if (!view || !view.data) return;
    const node = view.data.nodes.find((n) => n.num === num);
    if (!node) return;
    view.focus = num;
    if (view.element && "editableLocations" in view.element) {
      view.element.editableLocations = this._fullMapLocations(view);
      if (typeof view.element.setView === "function") view.element.setView([node.position.latitude, node.position.longitude], 15);
    }
  }

  // ---------- channel management ----------

  // raw field values of a channel form, to survive a re-render of the pane
  // The fields of a channel form are looked up with querySelector: Home Assistant's frontend patches the browser's
  // form field collection and its methods throw "Method not implemented" there.
  _formField(form, name) {
    return form.querySelector(`[name="${name}"]`);
  }

  _channelFormDraft(form) {
    const draft = { index: form.dataset.index, fields: {} };
    form.querySelectorAll("input[name], select[name]").forEach((element) => {
      draft.fields[element.name] = element.type === "checkbox" ? element.checked : element.value;
    });
    return draft;
  }

  _restoreChannelFormDraft(form, draft) {
    for (const [name, value] of Object.entries(draft.fields)) {
      const element = this._formField(form, name);
      if (!element) continue;
      if (element.type === "checkbox") element.checked = value;
      else element.value = value;
    }
    const custom = this._formField(form, "custom_key");
    const key = this._formField(form, "key");
    if (custom && key) custom.hidden = key.value !== "custom";
  }

  _channelFormValues(form) {
    const values = {
      index: Number(form.dataset.index),
      name: this._formField(form, "name").value.trim(),
      key: this._formField(form, "key") ? this._formField(form, "key").value : "keep",
      uplink_enabled: this._formField(form, "uplink_enabled").checked,
      downlink_enabled: this._formField(form, "downlink_enabled").checked,
      position_precision: Number(this._formField(form, "position_precision").value),
    };
    if (values.key === "custom") values.key = this._formField(form, "custom_key").value.trim();
    if (this._formField(form, "index")) values.index = Number(this._formField(form, "index").value);
    return values;
  }

  _keyDescription(key) {
    const known = KEY_CHOICES.find(([value]) => value === key);
    return known ? known[1].charAt(0).toLowerCase() + known[1].slice(1) : "the entered key";
  }

  async _saveChannel(form) {
    const details = this._details;
    const gateway = this._gateway();
    if (!details || !gateway || details.busy) return;
    const values = this._channelFormValues(form);
    if (utf8Length(values.name) > gateway.channel_name_max_bytes) {
      this._showError(`Channel names are limited to ${gateway.channel_name_max_bytes} bytes`);
      return;
    }
    const creating = details.kind === "new-channel";
    const current = creating ? null : details.data;
    const message = { type: "meshtastic/chat/channel/set", entry_id: gateway.entry_id, index: values.index };
    const changes = [];
    if (creating) {
      message.role = "SECONDARY";
      message.name = values.name;
      message.key = values.key;
      message.uplink_enabled = values.uplink_enabled;
      message.downlink_enabled = values.downlink_enabled;
      message.position_precision = values.position_precision;
    } else {
      if (values.name !== (current.settings_name || "")) {
        message.name = values.name;
        changes.push(`name: "${current.settings_name || ""}" → "${values.name}"`);
      }
      if (values.key !== "keep") {
        message.key = values.key;
        changes.push(`key: ${this._keyDescription(values.key)} – devices with the old key will no longer read this channel`);
      }
      if (values.uplink_enabled !== !!current.uplink_enabled) {
        message.uplink_enabled = values.uplink_enabled;
        changes.push(`uplink to MQTT: ${values.uplink_enabled ? "on" : "off"}`);
      }
      if (values.downlink_enabled !== !!current.downlink_enabled) {
        message.downlink_enabled = values.downlink_enabled;
        changes.push(`downlink from MQTT: ${values.downlink_enabled ? "on" : "off"}`);
      }
      if (values.position_precision !== (current.position_precision_bits || 0)) {
        message.position_precision = values.position_precision;
        changes.push(`position sharing: ${precisionLabel(values.position_precision).toLowerCase()}`);
      }
      if (!changes.length) {
        this._showError("Nothing changed");
        return;
      }
    }
    const gatewayName = gateway.gateway.long_name || gateway.title;
    const prompt = creating
      ? `Create channel ${values.index} "${values.name}" on ${gatewayName} with ${this._keyDescription(values.key)}?\n\nThe gateway applies it immediately.`
      : `Save these changes to channel ${values.index} "${current.name}" on ${gatewayName}?\n\n${changes
          .map((c) => `• ${c}`)
          .join("\n")}\n\nThe gateway applies them immediately.`;
    // eslint-disable-next-line no-alert
    if (!window.confirm(prompt)) return;
    await this._applyChannel(message, details, creating ? "Creating…" : "Saving…");
  }

  async _disableChannel() {
    const details = this._details;
    const gateway = this._gateway();
    if (!details || details.kind !== "channel" || !gateway || details.busy) return;
    const name = details.data ? details.data.name : `Channel ${details.id}`;
    // eslint-disable-next-line no-alert
    if (
      !window.confirm(
        `Disable channel ${details.id} "${name}" on ${gateway.gateway.long_name || gateway.title}?\n\nThe gateway stops sending and receiving on it. The stored history stays and the channel can be created again later.`
      )
    ) {
      return;
    }
    await this._applyChannel(
      { type: "meshtastic/chat/channel/set", entry_id: gateway.entry_id, index: details.id, role: "DISABLED" },
      details,
      "Disabling…"
    );
  }

  async _applyChannel(message, details, busyLabel) {
    details.busy = busyLabel;
    this._renderDetails();
    let result;
    try {
      result = await this._hass.callWS(message);
    } catch (err) {
      details.busy = null;
      if (this._details === details) this._renderDetails();
      this._showError(errorText(err));
      return;
    }
    details.busy = null;
    await this._loadGateways();
    if (this._details !== details) return;
    if (result.role === "DISABLED") {
      this._closeDetails({ render: false });
      if (this._conversation === result.conversation) this._selectConversation(this._defaultConversation(this._gateway()), { showThread: false });
      this._render();
      return;
    }
    // show the stored channel; a new channel also becomes the open conversation
    this._details = { ...details, kind: "channel", id: result.index, data: result, error: null, loading: false };
    if (this._conversation !== result.conversation) {
      this._conversation = result.conversation;
      this._showThread = !this._narrow;
      this._persisted.conversation = this._persisted.conversation || {};
      this._persisted.conversation[this._entryId] = result.conversation;
      saveState(this._persisted);
      if (!this._messages.has(`${this._entryId}|${result.conversation}`)) this._loadHistory(this._entryId, result.conversation).then(() => this._renderMessages({ stickToBottom: true }));
    }
    this._applyLayout();
    this._render();
  }

  async _copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      this._showError("Link copied");
    } catch (e) {
      this._showError("Could not copy – select the link and copy it by hand");
    }
  }

  async _openDetails(kind, id) {
    const gateway = this._gateway();
    if (!gateway) return;
    this._hideTooltip();
    const previous = this._details;
    const details = { kind, id, entryId: gateway.entry_id, data: null, error: null, loading: true, busy: null, results: {} };
    if (previous && previous.kind === kind && previous.id === id && previous.entryId === gateway.entry_id) {
      // reopening the same node keeps the results of earlier requests
      details.results = previous.results;
    }
    this._details = details;
    this._applyLayout();
    this._renderDetails();
    this._renderList();
    try {
      if (kind === "channel") {
        details.data = await this._hass.callWS({ type: "meshtastic/chat/channel", entry_id: gateway.entry_id, index: id });
      } else {
        details.data = await this._hass.callWS({ type: "meshtastic/chat/node", entry_id: gateway.entry_id, node_id: id });
      }
    } catch (err) {
      details.error = errorText(err);
    }
    details.loading = false;
    if (this._details === details) this._renderDetails();
  }

  _closeDetails({ render = true } = {}) {
    if (!this._details) return;
    this._details = null;
    this._unmountMap();
    if (render) {
      this._applyLayout();
      this._renderDetails();
      this._renderList();
    }
  }

  async _nodeRequest(request) {
    const details = this._details;
    const gateway = this._gateway();
    if (!details || details.kind !== "node" || !gateway || details.busy) return;
    details.busy = request;
    this._renderDetails();
    const message = {
      type: "meshtastic/chat/node/request",
      entry_id: gateway.entry_id,
      node_id: details.id,
      request,
    };
    if (request === "telemetry") message.telemetry_type = this._telemetryType;
    try {
      const response = await this._hass.callWS(message);
      details.results[request] = { ok: true, ts: Date.now() / 1000, result: response.result, type: message.telemetry_type };
      if (response.node) details.data = response.node;
    } catch (err) {
      window.console.warn("[Mesh Chat] request failed", request, err);
      details.results[request] = { ok: false, ts: Date.now() / 1000, error: errorText(err), type: message.telemetry_type };
    }
    details.busy = null;
    if (this._details === details) this._renderDetails();
  }

  _composedLength(text) {
    const gateway = this._gateway();
    const prefix = gateway && gateway.prefix ? `${gateway.prefix}: ` : "";
    return utf8Length(prefix + text);
  }

  _showError(message) {
    const target = this._details && this._detailsErrorEl ? this._detailsErrorEl : this._errorEl;
    target.textContent = message;
    target.hidden = false;
    window.clearTimeout(this._errorTimer);
    this._errorTimer = window.setTimeout(() => {
      target.hidden = true;
    }, 8000);
  }

  _autoGrow() {
    const textarea = this._textarea;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  }

  // ---------- tooltip ----------

  _scheduleTooltip(item) {
    if (!window.matchMedia || !window.matchMedia("(hover: hover)").matches) return;
    this._hideTooltip();
    this._tooltipTimer = window.setTimeout(() => this._showTooltip(item), TOOLTIP_DELAY_MS);
  }

  _showTooltip(item) {
    const gateway = this._gateway();
    const node = gateway && gateway.nodes.find((n) => n.num === Number(item.dataset.node));
    if (!node || !this._tooltipEl || !item.isConnected) return;

    const rows = [];
    if (isSet(node.hw_model)) rows.push(["Hardware", prettyModel(node.hw_model)]);
    if (isSet(node.role)) rows.push(["Role", prettyEnum(node.role)]);
    if (node.last_heard) rows.push(["Heard", `${this._formatAgo(node.last_heard)} ago${node.via_mqtt ? " via MQTT" : ""}`]);
    if (isSet(node.hops_away)) rows.push(["Hops", String(node.hops_away)]);
    if (isSet(node.snr)) rows.push(["SNR", `${formatNumber(node.snr, 1)} dB`]);
    if (isSet(node.battery_level)) rows.push(["Battery", batteryText(node.battery_level)]);
    const flags = [];
    if (node.favorite) flags.push("favourite");
    if (node.has_public_key) flags.push("public key");
    if (node.has_position) flags.push("position");
    if (flags.length) rows.push(["Flags", flags.join(", ")]);

    this._tooltipEl.innerHTML = `
      <div class="name">${escapeHtml(node.long_name)}</div>
      <div class="sub">${escapeHtml(node.short_name)} · ${escapeHtml(node.user_id)}</div>
      <dl>${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")}</dl>
      <div class="hint">Click to chat, open the header for details</div>
    `;
    const tooltip = this._tooltipEl;
    tooltip.hidden = false;
    const rect = item.getBoundingClientRect();
    const width = tooltip.offsetWidth;
    const height = tooltip.offsetHeight;
    let left = rect.right + 8;
    let top = rect.top;
    if (left + width > window.innerWidth - 8) {
      left = Math.max(8, rect.left + 16);
      top = rect.bottom + 4;
    }
    if (top + height > window.innerHeight - 8) top = Math.max(8, window.innerHeight - height - 8);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  _hideTooltip() {
    window.clearTimeout(this._tooltipTimer);
    this._tooltipTimer = null;
    if (this._tooltipEl) this._tooltipEl.hidden = true;
  }

  // ---------- map ----------

  _unmountMap() {
    this._mapCard = null;
    this._mapStates = null;
  }

  // Home Assistant's map element (ha-map) is loaded on demand by its map card. Creating a map card loads it;
  // afterwards the element is used directly and draws a marker per node. Recent frontends take the markers through
  // the element's "editableLocations" property (it also fires a click event per location); older ones, which still
  // take a "hass" property, get a copy of hass with a synthetic state per node instead.
  _mapHass(hass) {
    if (!this._mapStates || !hass) return hass;
    return { ...hass, states: { ...hass.states, ...this._mapStates } };
  }

  _mapMarkerElement(point) {
    const element = document.createElement("meshtastic-chat-marker");
    element.title = point.long_name;
    element.textContent = String(point.short_name || "?").slice(0, 4);
    element.classList.add(`hue-${avatarHue(point.num)}`);
    return element;
  }

  // Make the frontend load ha-map: a dashboard's card helpers do it when a dashboard was opened before, otherwise
  // a hui-card (available on every page of recent frontends) creating a map card does. Resolves to true when the
  // element is defined.
  async _loadMapElement(container) {
    if (customElements.get("ha-map")) return true;
    const config = { type: "map", geo_location_sources: ["meshtastic"] }; // a valid map card that shows nothing
    let probe = null;
    try {
      // the probe card is attached to the document: the frontend loads lazy cards (and with them ha-map) only
      // for connected elements
      if (customElements.get("hui-card")) {
        probe = document.createElement("hui-card");
        probe.hass = this._hass;
        probe.config = config;
        if (typeof probe.load === "function") probe.load();
      } else if (typeof window.loadCardHelpers === "function") {
        const helpers = await window.loadCardHelpers();
        probe = helpers.createCardElement(config);
        probe.hass = this._hass;
      } else {
        this._mapNote("no way to load the map element in this frontend");
        return false;
      }
      probe.classList.add("map-probe");
      container.appendChild(probe);
      await Promise.race([customElements.whenDefined("ha-map"), new Promise((resolve) => window.setTimeout(resolve, 10000))]);
    } catch (e) {
      this._mapNote(`loading the map element failed: ${errorText(e)}`);
    } finally {
      if (probe) probe.remove();
    }
    if (!customElements.get("ha-map")) {
      this._mapNote("the map element did not load within 10 s");
      return false;
    }
    return true;
  }

  // Why the map is missing, shown under the details map and logged so problems can be reported precisely
  _mapNote(text, root = this._detailsEl) {
    window.console.warn(`[Mesh Chat] map: ${text}`);
    const status = root && root.querySelector(".map-status");
    if (status) {
      status.textContent = `Map (panel ${PANEL_BUILD}): ${text}`;
      status.hidden = false;
    }
  }

  // states and entities for the older "hass" interface of the map element: one synthetic tracker per node
  _syntheticMapStates(points) {
    const states = {};
    const entities = [];
    const now = new Date().toISOString();
    for (const p of points) {
      const entityId = `${MAP_ENTITY_PREFIX}${p.num}`;
      states[entityId] = {
        entity_id: entityId,
        state: String(p.short_name || "?").slice(0, 4),
        last_changed: now,
        last_updated: now,
        context: { id: entityId, parent_id: null, user_id: null },
        attributes: {
          friendly_name: p.long_name,
          latitude: p.latitude,
          longitude: p.longitude,
          source_type: "gps",
          gps_accuracy: this._mapRadius(p) || 0,
          icon: "mdi:radio-tower",
        },
      };
      entities.push({
        entity_id: entityId,
        name: p.long_name,
        label_mode: "state",
        color: `hsl(${avatarHue(p.num) * (360 / HUE_COUNT)}, 45%, 45%)`,
      });
    }
    return { states, entities };
  }

  async _mountMap(container, points, details) {
    this._mapCard = null;
    this._mapStates = null;
    if (!points.length) return;
    try {
      if (!(await this._loadMapElement(container))) return;
      const map = document.createElement("ha-map");
      if ("editableLocations" in map) {
        map.entities = [];
        map.editableLocations = this._mapLocations(points);
        if ("hass" in map) map.hass = this._hass;
      } else if ("hass" in map) {
        const { states, entities } = this._syntheticMapStates(points);
        this._mapStates = states;
        map.entities = entities;
        map.hass = this._mapHass(this._hass);
      } else {
        this._mapNote("the map element has neither editableLocations nor hass; nodes cannot be drawn");
        return;
      }
      map.autoFit = true;
      map.themeMode = "auto";
      if (this._details !== details || !container.isConnected) {
        this._mapStates = null;
        return;
      }
      container.replaceChildren(map);
      this._mapCard = map;
    } catch (e) {
      this._mapNote(`failed: ${errorText(e)}`);
    }
  }

  // ---------- rendering ----------

  _renderSkeleton(styles) {
    if (styles) this.shadowRoot.adoptedStyleSheets = [styles];
    this.shadowRoot.innerHTML = `
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Mesh Chat</div>
        <select class="gateway-select" aria-label="Gateway" hidden></select>
        <button class="icon-button toolbar-button" data-action="map" aria-label="Map of the mesh" title="Map of the mesh">
          <ha-icon icon="mdi:map-marker-multiple-outline"></ha-icon>
        </button>
      </div>
      <div class="layout">
        <aside class="sidebar">
          <div class="search">
            <ha-icon icon="mdi:magnify"></ha-icon>
            <input type="search" placeholder="Search channels and nodes" aria-label="Search channels and nodes" />
          </div>
          <nav class="conversations" aria-label="Conversations"></nav>
        </aside>
        <main class="thread">
          <header class="thread-header"></header>
          <div class="messages" role="log" aria-live="polite"></div>
          <div class="error" role="alert" hidden></div>
          <div class="reply-bar" hidden>
            <ha-icon icon="mdi:reply"></ha-icon>
            <div class="reply-bar-text"><span class="reply-bar-name"></span><span class="reply-bar-snippet"></span></div>
            <button type="button" class="icon-button" data-action="cancel-reply" aria-label="Cancel reply" title="Cancel reply">
              <ha-icon icon="mdi:close"></ha-icon>
            </button>
          </div>
          <div class="emoji-picker" role="group" aria-label="Emoji" hidden>
            <div class="emoji-all" hidden>${EMOJI_GROUPS.map(
              ([title, emoji]) =>
                `<div class="emoji-group" role="group" aria-label="${escapeHtml(title)}"><h3>${escapeHtml(title)}</h3>${emoji
                  .map((e) => this._emojiMarkup(e))
                  .join("")}</div>`
            ).join("")}</div>
            <div class="emoji-bar">
              <div class="emoji-recent"></div>
              <button type="button" class="emoji-expand" data-action="emoji-expand" aria-expanded="false" aria-label="Show all emoji" title="All emoji">
                <ha-icon icon="mdi:chevron-up"></ha-icon>
              </button>
            </div>
          </div>
          <form class="composer">
            <div class="composer-field">
              <button type="button" class="emoji-toggle" data-action="emoji" aria-label="Emoji" aria-expanded="false" title="Emoji">
                <ha-icon icon="mdi:emoticon-happy-outline"></ha-icon>
              </button>
              <textarea rows="1" aria-label="Message"></textarea>
              <span class="counter"></span>
            </div>
            <button type="submit" class="send" aria-label="Send">
              <ha-icon icon="mdi:send"></ha-icon>
            </button>
          </form>
        </main>
        <section class="fullmap" aria-label="Map of the mesh" hidden></section>
        <aside class="details" aria-label="Details" hidden></aside>
      </div>
      <div class="tooltip" role="tooltip" hidden></div>
    `;

    const root = this.shadowRoot;
    this._menuButton = root.querySelector("ha-menu-button");
    this._menuButton.hass = this._hass;
    this._menuButton.narrow = this._narrow;
    this._gatewaySelect = root.querySelector(".gateway-select");
    this._layoutEl = root.querySelector(".layout");
    this._listEl = root.querySelector(".conversations");
    this._headerEl = root.querySelector(".thread-header");
    this._messagesEl = root.querySelector(".messages");
    this._errorEl = root.querySelector(".error");
    this._detailsEl = root.querySelector(".details");
    this._fullMapEl = root.querySelector(".fullmap");
    this._detailsErrorEl = null;
    this._tooltipEl = root.querySelector(".tooltip");
    this._replyEl = root.querySelector(".reply-bar");
    this._emojiEl = root.querySelector(".emoji-picker");
    this._emojiAllEl = root.querySelector(".emoji-all");
    this._emojiRecentEl = root.querySelector(".emoji-recent");
    this._emojiButton = root.querySelector(".emoji-toggle");
    this._form = root.querySelector(".composer");
    this._textarea = root.querySelector("textarea");
    this._counter = root.querySelector(".counter");
    this._sendButton = root.querySelector(".send");
    const search = root.querySelector("input[type=search]");

    search.addEventListener("input", () => {
      this._filter = search.value.trim().toLowerCase();
      this._renderList();
    });

    root.querySelector(".toolbar-button").addEventListener("click", () => {
      if (this._mapView) this._closeFullMap();
      else this._openFullMap();
    });

    this._fullMapEl.addEventListener("click", (ev) => {
      const action = ev.target.closest("[data-action]");
      if (!action) return;
      const { dataset } = action;
      if (dataset.action === "close-map") this._closeFullMap();
    });
    this._fullMapEl.addEventListener("editable-location-clicked", (ev) => this._onMapLocationClicked(ev));

    this._gatewaySelect.addEventListener("change", () => {
      this._entryId = this._gatewaySelect.value;
      this._conversation = null;
      this._closeDetails({ render: false });
      this._closeFullMap({ render: false });
      const gateway = this._gateway();
      const remembered = (this._persisted.conversation || {})[this._entryId];
      this._selectConversation(
        remembered && this._conversationInfo(gateway, remembered) ? remembered : this._defaultConversation(gateway),
        { showThread: false }
      );
    });

    const activate = (ev) => {
      if (ev.target.closest("[data-action=new-channel]")) {
        ev.preventDefault();
        this._openNewChannel();
        return;
      }
      if (ev.target.closest("[data-action=gateway-details]")) {
        ev.preventDefault();
        const gateway = this._gateway();
        if (gateway && gateway.gateway.num !== null && gateway.gateway.num !== undefined) this._openDetails("node", gateway.gateway.num);
        return;
      }
      const star = ev.target.closest("[data-action=favorite]");
      if (star) {
        ev.preventDefault();
        ev.stopPropagation();
        this._toggleFavorite(Number(star.dataset.node));
        return;
      }
      const item = ev.target.closest("[data-conversation]");
      if (item) {
        ev.preventDefault();
        this._selectConversation(item.dataset.conversation);
      }
    };
    this._listEl.addEventListener("click", activate);
    this._listEl.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") activate(ev);
    });
    this._listEl.addEventListener("mouseover", (ev) => {
      const item = ev.target.closest("[data-node]");
      if (item && !item.contains(ev.relatedTarget)) this._scheduleTooltip(item);
    });
    this._listEl.addEventListener("mouseout", (ev) => {
      const item = ev.target.closest("[data-node]");
      if (item && !item.contains(ev.relatedTarget)) this._hideTooltip();
    });
    this._listEl.addEventListener("scroll", () => this._hideTooltip(), { passive: true });
    this._listEl.addEventListener("mousedown", () => this._hideTooltip());

    this._headerEl.addEventListener("click", (ev) => {
      const action = ev.target.closest("[data-action]");
      if (!action) return;
      if (action.dataset.action === "back") {
        this._showThread = false;
        this._applyLayout();
      } else if (action.dataset.action === "clear") {
        this._clearConversation();
      } else if (action.dataset.action === "details") {
        if (this._details) this._closeDetails();
        else this._openDetailsFor(this._conversation);
      }
    });

    this._detailsEl.addEventListener("click", (ev) => {
      const action = ev.target.closest("[data-action]");
      if (!action) return;
      const { dataset } = action;
      if (dataset.action === "close-details") this._closeDetails();
      else if (dataset.action === "favorite") this._toggleFavorite(Number(dataset.node));
      else if (dataset.action === "request") this._nodeRequest(dataset.request);
      else if (dataset.action === "navigate") this._navigate(dataset.href);
      else if (dataset.action === "more-info") this._moreInfo(dataset.entity);
      else if (dataset.action === "open-conversation") this._selectConversation(dataset.conversation);
      else if (dataset.action === "open-node") this._openDetails("node", Number(dataset.node));
      else if (dataset.action === "open-map") this._openFullMap(Number(dataset.node));
      else if (dataset.action === "copy") this._copyText(dataset.text);
      else if (dataset.action === "disable-channel") this._disableChannel();
    });
    this._detailsEl.addEventListener("editable-location-clicked", (ev) => this._onMapLocationClicked(ev));
    this._detailsEl.addEventListener("hass-more-info", (ev) => {
      const entityId = ev.detail && ev.detail.entityId;
      if (!entityId || !entityId.startsWith(MAP_ENTITY_PREFIX)) return;
      ev.stopPropagation();
      ev.preventDefault();
      this._openDetails("node", Number(entityId.slice(MAP_ENTITY_PREFIX.length)));
    });
    this._detailsEl.addEventListener("submit", (ev) => {
      const form = ev.target.closest("form.channel-form");
      if (!form) return;
      ev.preventDefault();
      this._saveChannel(form);
    });
    this._detailsEl.addEventListener("keydown", (ev) => {
      if ((ev.key === "Enter" || ev.key === " ") && ev.target.closest("[data-action=favorite]")) {
        ev.preventDefault();
        this._toggleFavorite(Number(ev.target.closest("[data-action=favorite]").dataset.node));
      }
    });
    this._detailsEl.addEventListener("change", (ev) => {
      if (ev.target.matches("select.telemetry-type")) this._telemetryType = ev.target.value;
      if (ev.target.matches("select[name=key]")) {
        const custom = this._formField(ev.target.form, "custom_key");
        custom.hidden = ev.target.value !== "custom";
        if (!custom.hidden) custom.focus();
      }
      if (ev.target.matches("select[name=index]") && this._details) this._details.id = Number(ev.target.value);
    });

    this._messagesEl.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-action=older]")) this._loadOlder();
      const author = ev.target.closest("[data-action=dm]");
      if (author && author.dataset.conversation !== this._conversation) this._selectConversation(author.dataset.conversation);
      const reply = ev.target.closest("[data-action=reply]");
      if (reply) this._startReply(reply.dataset.id);
      const quote = ev.target.closest("[data-action=goto]");
      if (quote) this._flashMessage(quote.dataset.id);
    });

    this._messagesEl.addEventListener("keydown", (ev) => {
      const quote = ev.target.closest("[data-action=goto]");
      if (quote && (ev.key === "Enter" || ev.key === " ")) {
        ev.preventDefault();
        this._flashMessage(quote.dataset.id);
      }
    });

    this._replyEl.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-action=cancel-reply]")) this._cancelReply();
    });

    this._emojiButton.addEventListener("click", () => this._toggleEmojiPicker());
    // keep the focus (and the cursor) in the message while an emoji is picked
    this._emojiEl.addEventListener("mousedown", (ev) => ev.preventDefault());
    this._emojiEl.addEventListener("click", (ev) => {
      const emoji = ev.target.closest("[data-emoji]");
      if (emoji) this._insertEmoji(emoji.dataset.emoji);
      if (ev.target.closest("[data-action=emoji-expand]")) this._toggleAllEmoji();
    });

    this._form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      this._send();
    });

    this._textarea.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        this._send();
      } else if (ev.key === "Escape") {
        // first the picker, then the reply
        if (!this._emojiEl.hidden) this._toggleEmojiPicker(false);
        else this._cancelReply();
      }
    });

    this._textarea.addEventListener("input", () => {
      this._autoGrow();
      this._updateComposer();
    });
  }

  _applyLayout() {
    if (!this._layoutEl) return;
    this._layoutEl.classList.toggle("narrow", !!this._narrow);
    this._layoutEl.classList.toggle("show-thread", !!this._showThread);
    this._layoutEl.classList.toggle("show-details", !!this._details);
    this._layoutEl.classList.toggle("show-map", !!this._mapView);
  }

  _render() {
    if (!this._layoutEl) return;
    this._renderGatewaySelect();
    this._renderList();
    this._renderThread();
    this._renderDetails();
  }

  _renderGatewaySelect() {
    const select = this._gatewaySelect;
    select.hidden = this._gateways.length < 2;
    select.innerHTML = this._gateways
      .map(
        (g) =>
          `<option value="${escapeHtml(g.entry_id)}" ${g.entry_id === this._entryId ? "selected" : ""}>${escapeHtml(
            g.gateway.long_name || g.title
          )}</option>`
      )
      .join("");
  }

  _renderList() {
    if (!this._listEl) return;
    this._hideTooltip();
    const gateway = this._gateway();
    if (!gateway) {
      this._listEl.innerHTML = this._loadError
        ? `<p class="empty">${escapeHtml(this._loadError)}</p>`
        : `<p class="empty">No gateway has Mesh Chat enabled. Turn it on in the Meshtastic integration options.</p>`;
      return;
    }

    const filter = this._filter;
    const matches = (...values) => !filter || values.some((v) => String(v || "").toLowerCase().includes(filter));

    const channelItems = gateway.channels
      .filter((c) => matches(c.name, `#${c.name}`))
      .map((c) => this._listItem(gateway, c.conversation, {
        avatar: this._channelAvatar(c),
        title: c.name,
        badge: c.public ? "public" : "",
      }))
      .join("");

    const lastTs = (n) => (gateway.conversations[n.conversation] || {}).ts || 0;
    const nodes = gateway.nodes
      .filter((n) => matches(n.long_name, n.short_name, n.user_id))
      .sort(
        (a, b) =>
          (b.favorite === true) - (a.favorite === true) ||
          lastTs(b) - lastTs(a) ||
          (b.last_heard || 0) - (a.last_heard || 0)
      );

    // conversations with nodes that are no longer in the node database
    const knownNodes = new Set(gateway.nodes.map((n) => n.conversation));
    const orphaned = Object.keys(gateway.conversations)
      .filter((c) => c.startsWith("node:") && !knownNodes.has(c))
      .map((c) => this._conversationInfo(gateway, c))
      .filter((n) => matches(n.long_name, n.user_id));

    const nodeItems = [...nodes, ...orphaned]
      .map((n) => this._listItem(gateway, n.conversation, {
        avatar: this._avatar(n.num, n.short_name),
        title: n.long_name,
        trailing: n.stub ? "" : this._star(n, { canFavorite: gateway.can_favorite }),
        meta: n.last_heard ? `heard ${this._formatAgo(n.last_heard)} ago` : "",
        node: n.stub ? null : n.num,
      }))
      .join("");

    const unavailable = gateway.available
      ? ""
      : `<p class="empty warning">The gateway is not connected. History is still available.</p>`;

    const canCreate = gateway.can_manage_channels && (gateway.free_channel_indexes || []).length > 0;
    const own = gateway.gateway || {};
    const gatewayOpen = this._details && this._details.kind === "node" && this._details.id === own.num;
    const gatewayRow =
      own.num !== null && own.num !== undefined
        ? `<div class="item gateway-row ${gatewayOpen ? "selected" : ""}" role="button" tabindex="0" data-action="gateway-details" title="Details of your gateway">
             ${this._avatar(own.num, own.short_name)}
             <span class="item-text">
               <span class="item-title"><span class="name">${escapeHtml(own.long_name || gateway.title)}</span></span>
               <span class="preview">Your gateway · ${escapeHtml(own.user_id || "")}</span>
             </span>
             <ha-icon icon="mdi:information-outline"></ha-icon>
           </div>`
        : "";
    this._listEl.innerHTML = `
      ${unavailable}
      ${gatewayRow}
      <h2>Channels${
        canCreate
          ? `<button class="h2-action" data-action="new-channel" aria-label="New channel" title="New channel"><ha-icon icon="mdi:plus"></ha-icon></button>`
          : ""
      }</h2>
      ${channelItems || `<p class="empty">No matching channels</p>`}
      <h2>Direct messages</h2>
      ${nodeItems || `<p class="empty">No matching nodes</p>`}
    `;
  }

  _listItem(gateway, conversation, { avatar, title, badge = "", trailing = "", meta = "", node = null }) {
    const summary = gateway.conversations[conversation];
    const unread = this._isUnread(gateway, conversation) && conversation !== this._conversation;
    const preview = summary
      ? `${summary.direction === "out" ? "You: " : ""}${summary.text}`
      : meta;
    const time = summary ? this._formatAgo(summary.ts) : "";
    const count = summary && summary.count ? summary.count : 0;
    const countPill = count
      ? `<span class="count" title="${count} message${count === 1 ? "" : "s"} stored">${count > 999 ? "999+" : count}</span>`
      : "";
    const selected = conversation === this._conversation;
    return `
      <div class="item ${selected ? "selected" : ""} ${unread ? "unread" : ""}" role="button" tabindex="0" data-conversation="${escapeHtml(
        conversation
      )}" ${node !== null ? `data-node="${node}"` : ""} ${selected ? 'aria-current="true"' : ""}>
        ${avatar}
        <span class="item-text">
          <span class="item-title">
            <span class="name">${escapeHtml(title)}</span>
            ${badge ? `<span class="badge">${escapeHtml(badge)}</span>` : ""}
            ${trailing}
            ${countPill}
            <span class="time">${escapeHtml(time)}</span>
          </span>
          <span class="preview">${escapeHtml(preview)}</span>
        </span>
        ${unread ? '<span class="dot" aria-label="unread"></span>' : ""}
      </div>
    `;
  }

  _renderThread() {
    if (!this._headerEl) return;
    const gateway = this._gateway();
    const info = this._conversationInfo(gateway, this._conversation);

    if (!gateway || !info) {
      this._headerEl.innerHTML = "";
      this._messagesEl.innerHTML = `<p class="empty center">Select a channel or node to start chatting.</p>`;
      this._updateComposer();
      return;
    }

    let avatar;
    let title;
    let subtitle;
    if (info.kind === "channel") {
      avatar = this._channelAvatar(info);
      title = info.name;
      subtitle = info.public
        ? `Channel ${info.index} · public – anyone on the mesh can read it`
        : `Channel ${info.index} · encrypted`;
    } else {
      avatar = this._avatar(info.num, info.short_name);
      title = info.long_name;
      const parts = [info.user_id];
      if (info.last_heard) parts.push(`heard ${this._formatAgo(info.last_heard)} ago`);
      if (info.hops_away !== undefined && info.hops_away !== null) parts.push(`${info.hops_away} hops`);
      parts.push(info.has_public_key ? "end-to-end encrypted" : "no public key yet");
      subtitle = parts.join(" · ");
    }

    const detailsLabel = this._details ? "Hide details" : "Show details";
    this._headerEl.innerHTML = `
      <button class="icon-button back" data-action="back" aria-label="Back to conversations">
        <ha-icon icon="mdi:arrow-left"></ha-icon>
      </button>
      ${avatar}
      <button class="thread-title" data-action="details" title="${detailsLabel}">
        <span class="name">${escapeHtml(title)}</span>
        <span class="subtitle">${escapeHtml(subtitle)}</span>
      </button>
      <button class="icon-button ${this._details ? "active" : ""}" data-action="details" aria-label="${detailsLabel}" title="${detailsLabel}">
        <ha-icon icon="mdi:information-outline"></ha-icon>
      </button>
      ${
        gateway.can_clear
          ? `<button class="icon-button" data-action="clear" aria-label="Delete stored history" title="Delete stored history">
              <ha-icon icon="mdi:delete-sweep-outline"></ha-icon>
            </button>`
          : ""
      }
    `;

    this._textarea.placeholder =
      info.kind === "channel"
        ? `Message ${info.name}${info.public ? " (public)" : ""}`
        : `Message ${info.long_name}`;
    this._renderMessages({ stickToBottom: true });
    this._updateComposer();
  }

  _renderMessages({ stickToBottom = false } = {}) {
    if (!this._messagesEl) return;
    const gateway = this._gateway();
    if (!gateway || !this._conversation) return;
    const key = `${this._entryId}|${this._conversation}`;
    const messages = this._messages.get(key);
    const scroller = this._messagesEl;
    const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;

    if (!messages) {
      scroller.innerHTML = `<p class="empty center">Loading…</p>`;
      return;
    }
    if (!messages.length) {
      scroller.innerHTML = `<p class="empty center">No messages yet. Messages received or sent from here will appear in this conversation.</p>`;
      return;
    }

    const summary = gateway.conversations[this._conversation];
    const hasOlder = summary && summary.count > messages.length;
    let html = hasOlder ? `<button class="older" data-action="older">Load older messages</button>` : "";
    let lastDay = null;
    let lastSender = null;

    for (const m of messages) {
      const day = this._formatDay(m.ts);
      if (day !== lastDay) {
        html += `<div class="day"><span>${escapeHtml(day)}</span></div>`;
        lastDay = day;
        lastSender = null;
      }
      const outgoing = m.direction === "out";
      const continued = lastSender === `${m.direction}:${m.from}`;
      lastSender = `${m.direction}:${m.from}`;

      let footer = `<span class="time">${escapeHtml(this._formatTime(m.ts))}</span>`;
      if (outgoing) {
        const status = STATUS_ICONS[m.status] || STATUS_ICONS.sending;
        const label = m.error ? `${status.label}: ${m.error}` : status.label;
        footer += `<ha-icon class="status ${escapeHtml(m.status)}" icon="${status.icon}" title="${escapeHtml(
          label
        )}" aria-label="${escapeHtml(label)}"></ha-icon>`;
      }

      const sentBy = outgoing && m.sent_by ? `<span class="via">${escapeHtml(m.sent_by)} via Home Assistant</span>` : "";
      const signal = !outgoing && signalText(m) ? `<span class="via signal" title="${escapeHtml(signalTitle(m))}">${escapeHtml(signalText(m))}</span>` : "";
      const author =
        !outgoing && !continued
          ? `<button class="author" data-action="dm" data-conversation="node:${Number(m.from)}" title="Send a direct message">${escapeHtml(
              m.from_name
            )} <span>${escapeHtml(m.from_user_id || "")}</span></button>`
          : "";

      const replyButton =
        m.packet_id && m.status !== "failed"
          ? `<button class="reply-button" data-action="reply" data-id="${escapeHtml(m.id)}" aria-label="Reply" title="Reply"><ha-icon icon="mdi:reply"></ha-icon></button>`
          : "";

      html += `
        <div class="row ${outgoing ? "out" : "in"} ${continued ? "continued" : ""}" data-message="${escapeHtml(m.id)}">
          ${!outgoing ? (continued ? '<span class="avatar spacer"></span>' : this._avatar(m.from, m.from_short)) : ""}
          ${outgoing ? replyButton : ""}
          <div class="bubble ${m.status === "failed" ? "failed" : ""}">
            ${author}
            ${this._quoteHtml(m, gateway)}
            <div class="text">${escapeHtml(m.text)}</div>
            <div class="footer">${sentBy}${signal}${footer}</div>
          </div>
          ${!outgoing ? replyButton : ""}
        </div>`;
    }

    scroller.innerHTML = html;
    if (stickToBottom || atBottom) scroller.scrollTop = scroller.scrollHeight;
  }

  // the quoted message of a reply: who said what, kept with the reply; clicking it jumps to the original
  _quoteHtml(message, gateway) {
    if (!message.reply_to) return "";
    const quote = message.reply_preview;
    if (!quote) return `<div class="quote"><span class="quote-text">Reply to an earlier message</span></div>`;
    const own = gateway.gateway && quote.from === gateway.gateway.num;
    return `<div class="quote" data-action="goto" data-id="${escapeHtml(quote.id)}" role="button" tabindex="0"><span class="quote-name">${escapeHtml(
      own ? "You" : quote.name
    )}</span><span class="quote-text">${escapeHtml(quote.text)}</span></div>`;
  }

  _updateComposer() {
    if (!this._textarea) return;
    const gateway = this._gateway();
    const enabled = !!(gateway && this._conversation && gateway.available);
    const text = this._textarea.value.trim();
    const length = gateway ? this._composedLength(text) : 0;
    const max = gateway ? gateway.max_bytes : 200;
    const tooLong = length > max;

    this._textarea.disabled = !enabled;
    this._emojiButton.disabled = !enabled;
    this._sendButton.disabled = !enabled || !text || tooLong || !!this._sending;
    this._counter.textContent = text ? `${length}/${max}` : "";
    this._counter.classList.toggle("over", tooLong);
    this._counter.title = gateway && gateway.prefix ? `Includes the prefix "${gateway.prefix}: "` : "";
  }

  // ---------- details pane ----------

  _renderDetails() {
    const el = this._detailsEl;
    if (!el) return;
    const details = this._details;
    const gateway = this._gateway();
    if (!details || !gateway) {
      el.hidden = true;
      el.innerHTML = "";
      this._detailsErrorEl = null;
      this._unmountMap();
      return;
    }

    const conversation = details.kind === "channel" ? `channel:${details.id}` : `node:${details.id}`;
    const ownGateway = details.kind === "node" && gateway.gateway && details.id === gateway.gateway.num;
    const info = (ownGateway ? gateway.gateway : this._conversationInfo(gateway, conversation)) || {};
    const data = details.data;
    const subject = data || info;

    let header;
    if (details.kind === "new-channel") {
      header = `
        <span class="avatar channel" aria-hidden="true"><ha-icon icon="mdi:plus"></ha-icon></span>
        <div class="details-title">
          <div class="name">New channel</div>
          <div class="subtitle">on ${escapeHtml(gateway.gateway.long_name || gateway.title)}</div>
        </div>`;
    } else if (details.kind === "channel") {
      header = `
        ${this._channelAvatar(subject)}
        <div class="details-title">
          <div class="name">${escapeHtml(subject.name || `Channel ${details.id}`)}</div>
          <div class="subtitle">Channel ${details.id} · ${escapeHtml(
            data ? prettyEnum(data.role) : subject.primary ? "Primary" : "Secondary"
          )}</div>
        </div>`;
    } else {
      header = `
        ${this._avatar(details.id, subject.short_name)}
        <div class="details-title">
          <div class="name">${escapeHtml(subject.long_name || info.user_id || "")}</div>
          <div class="subtitle">${escapeHtml(subject.short_name || "")} · ${escapeHtml(subject.user_id || "")}</div>
        </div>
        ${data ? this._star(data, { canFavorite: gateway.can_favorite, large: true }) : ""}`;
    }

    let body;
    if (details.error) {
      body = `<p class="empty">${escapeHtml(details.error)}</p>`;
    } else if (details.kind === "new-channel") {
      body = this._channelForm(null, details);
    } else if (!data) {
      body = `<p class="empty">Loading…</p>`;
    } else if (details.kind === "channel") {
      body = this._channelDetailsBody(data, details);
    } else {
      body = this._nodeDetailsBody(data, details);
    }

    // the periodic gateway refresh re-renders the pane; keep what the user typed into a channel form
    const previousForm = el.querySelector("form.channel-form");
    const draft = previousForm ? this._channelFormDraft(previousForm) : null;

    el.hidden = false;
    el.innerHTML = `
      <header class="details-header">
        <button class="icon-button" data-action="close-details" aria-label="Close details" title="Close details">
          <ha-icon icon="${this._narrow ? "mdi:arrow-left" : "mdi:close"}"></ha-icon>
        </button>
        ${header}
      </header>
      <div class="error" role="alert" hidden></div>
      <div class="details-body">${body}</div>
    `;
    this._detailsErrorEl = el.querySelector(".error");
    const form = el.querySelector("form.channel-form");
    if (form && draft && draft.index === form.dataset.index) this._restoreChannelFormDraft(form, draft);

    const mapContainer = el.querySelector(".map");
    if (mapContainer) {
      this._mountMap(mapContainer, this._nodeMapPoints(details.data), details);
    } else {
      this._unmountMap();
    }
  }

  _section(title, rows) {
    const items = rows.filter((row) => row && isSet(row[1]));
    if (!items.length) return "";
    return `
      <h3>${escapeHtml(title)}</h3>
      <dl class="kv">${items
        .map(([key, value, raw]) => `<dt>${escapeHtml(key)}</dt><dd>${raw ? value : escapeHtml(value)}</dd>`)
        .join("")}</dl>`;
  }

  _nodeDetailsBody(node, details) {
    const yesNo = (value) => (value ? "yes" : "no");
    const identity = this._section("Node", [
      ["User ID", node.user_id],
      ["Hardware", isSet(node.hw_model) ? prettyModel(node.hw_model) : null],
      ["Role", isSet(node.role) ? prettyEnum(node.role) : null],
      ["Licensed operator", node.is_licensed ? "yes" : null],
      ["Gateway", node.is_gateway ? "this is the connected node" : null],
    ]);

    const g = node.gateway_info;
    const gauges = g ? this._gatewayGauges(node, g) : "";
    const a = node.activity;
    const messages = a
      ? this._section("Messages", [
          [
            "From this node",
            a.total ? `${a.total} stored (${a.direct} direct, ${a.channels} on channels)` : "none stored yet",
          ],
          ["Last message", a.last_ts ? this._formatDateTimeAgo(a.last_ts) : null],
        ])
      : "";
    const gatewaySection = g
      ? this._section("Gateway", [
          ["Home Assistant entry", g.title],
          ["Firmware", g.firmware_version],
          ["Hardware", isSet(g.hw_model) ? prettyModel(g.hw_model) : null],
          ["Role", isSet(g.role) ? prettyEnum(g.role) : null],
          ["Region", isSet(g.region) ? prettyEnum(g.region).toUpperCase() : null],
          ["Modem preset", isSet(g.modem_preset) ? prettyEnum(g.modem_preset) : null],
          ["Hop limit", isSet(g.hop_limit) ? String(g.hop_limit) : null],
          ["TX power", isSet(g.tx_power) ? `${g.tx_power} dBm` : null],
          ["Transmit", g.tx_enabled === false ? "disabled" : null],
          ["MQTT", g.mqtt_enabled ? "enabled" : "disabled"],
          ["Channels", String(g.channels)],
          ["Nodes known", String(g.nodes_known)],
          ["Mesh Chat panel", `build ${PANEL_BUILD}`],
        ])
      : "";

    const radio = node.is_gateway ? "" : this._section("Radio", [
      ["Last heard", node.last_heard ? this._formatDateTimeAgo(node.last_heard) : "never"],
      ["Hops away", isSet(node.hops_away) ? String(node.hops_away) : null],
      ["SNR", isSet(node.snr) ? `${formatNumber(node.snr, 1)} dB` : null],
      ["RSSI", isSet(node.rssi) ? `${formatNumber(node.rssi, 0)} dBm` : null],
      ["Via MQTT", node.via_mqtt ? "yes" : null],
      ["Heard on channel", isSet(node.channel) ? String(node.channel) : null],
    ]);

    const metrics = this._section("Device", [
      ["Battery", isSet(node.battery_level) ? batteryText(node.battery_level) : null],
      ["Voltage", isSet(node.voltage) ? `${formatNumber(node.voltage, 2)} V` : null],
      ["Channel utilisation", isSet(node.channel_utilization) ? `${formatNumber(node.channel_utilization, 1)} %` : null],
      ["Air utilisation (TX)", isSet(node.air_util_tx) ? `${formatNumber(node.air_util_tx, 1)} %` : null],
      ["Uptime", isSet(node.uptime_seconds) ? formatDuration(node.uptime_seconds) : null],
    ]);

    const security = this._section("Security", [
      ["Favourite", yesNo(node.favorite)],
      ["Public key", node.has_public_key ? "known – direct messages are end-to-end encrypted" : "not received yet"],
      ["Ignored", node.ignored ? "yes" : null],
    ]);

    let position;
    const p = node.position;
    if (p) {
      const rows = [
        ["Coordinates", `${formatNumber(p.latitude, 5)}, ${formatNumber(p.longitude, 5)}`],
        ["Altitude", isSet(p.altitude) ? `${p.altitude} m` : null],
        [
          "Precision",
          p.accuracy === 0 ? "full" : isSet(p.accuracy) ? `about ${formatDistance(p.accuracy)}` : null,
        ],
        ["Received", p.time ? this._formatDateTimeAgo(p.time) : null],
        [
          "From the gateway",
          isSet(p.distance) && !node.is_gateway ? `${formatDistance(p.distance)} · ${p.bearing}° ${compassPoint(p.bearing)}` : null,
        ],
      ];
      position = `
        ${this._section("Position", rows)}
        <div class="map"></div>
        <p class="hint map-status" hidden></p>
        <div class="actions">
          <button class="chip-button" data-action="open-map" data-node="${node.num}"><ha-icon icon="mdi:map"></ha-icon>Open the map</button>
        </div>`;
    } else {
      position = `${this._section("Position", [["Position", "not received yet"]])}`;
    }

    const homeAssistant = node.device_id
      ? `<h3>Home Assistant</h3>
         <div class="actions">
           <button class="chip-button" data-action="navigate" data-href="/config/devices/device/${escapeHtml(node.device_id)}">
             <ha-icon icon="mdi:devices"></ha-icon>Open device
           </button>
         </div>`
      : `<h3>Home Assistant</h3><p class="hint">No device for this node yet. Nodes selected in the integration options get a device with sensors and a tracker.</p>`;

    const busy = details.busy;
    const telemetryOptions = TELEMETRY_TYPES.map(
      ([value, label]) => `<option value="${value}" ${value === this._telemetryType ? "selected" : ""}>${label}</option>`
    ).join("");
    const requestButton = (key) =>
      `<button class="chip-button primary" data-action="request" data-request="${key}" ${busy ? "disabled" : ""}>
         <ha-icon icon="${NODE_REQUESTS[key].icon}"></ha-icon>${busy === key ? NODE_REQUESTS[key].busy : NODE_REQUESTS[key].label}
       </button>`;
    const actions = node.is_gateway
      ? ""
      : `
      <h3>Ask the node</h3>
      <div class="actions">
        <label class="select-field">
          <select class="telemetry-type" aria-label="Telemetry type" ${busy ? "disabled" : ""}>${telemetryOptions}</select>
        </label>
        ${requestButton("telemetry")}
        ${requestButton("position")}
        ${requestButton("traceroute")}
      </div>
      <p class="hint">Requests go over the mesh and can take up to a minute; the reply updates the values above.</p>
      ${["telemetry", "position", "traceroute"].map((key) => this._requestResult(key, details.results[key], node)).join("")}`;

    return gauges + identity + gatewaySection + radio + messages + metrics + position + security + homeAssistant + actions;
  }

  // the markers of the details map: the node itself and, for other nodes, the gateway to show the distance
  _nodeMapPoints(node) {
    const p = node && node.position;
    if (!p) return [];
    const points = [
      { num: node.num, short_name: node.short_name, long_name: node.long_name, latitude: p.latitude, longitude: p.longitude, accuracy: p.accuracy },
    ];
    if (node.gateway_position && !node.is_gateway) points.push(node.gateway_position);
    return points;
  }

  // ring gauges with the gateway's key numbers; the colour follows the usual thresholds
  _gatewayGauges(node, g) {
    const level = (value, warn, bad, inverted = false) => {
      if (!isSet(value)) return "";
      const v = inverted ? 100 - value : value;
      return v >= bad ? "bad" : v >= warn ? "warn" : "good";
    };
    const ring = (fraction, text, label, cls, title) => {
      const r = 20;
      const c = 2 * Math.PI * r;
      const f = Math.max(0, Math.min(1, fraction ?? 0));
      return `
        <div class="gauge ${cls}" title="${escapeHtml(title || label)}">
          <svg viewBox="0 0 52 52" aria-hidden="true">
            <circle class="track" cx="26" cy="26" r="${r}"></circle>
            <circle class="value" cx="26" cy="26" r="${r}" stroke-dasharray="${(f * c).toFixed(1)} ${c.toFixed(1)}"></circle>
          </svg>
          <span class="gauge-text">${escapeHtml(text)}</span>
          <span class="gauge-label">${escapeHtml(label)}</span>
        </div>`;
    };
    const util = node.channel_utilization;
    const air = node.air_util_tx;
    const battery = node.battery_level;
    const plugged = isSet(battery) && battery > 100;
    return `
      <div class="gauges">
        ${ring(isSet(util) ? util / 100 : 0, isSet(util) ? `${formatNumber(util, 0)}%` : "–", "Channel", level(util, 25, 50), "Channel utilisation")}
        ${ring(isSet(air) ? air / 100 : 0, isSet(air) ? `${formatNumber(air, 1)}%` : "–", "TX air", level(air, 5, 10), "Air utilisation (transmit)")}
        ${ring(
          plugged ? 1 : isSet(battery) ? battery / 100 : 0,
          plugged ? "AC" : isSet(battery) ? `${battery}%` : "–",
          "Battery",
          plugged ? "good" : level(battery, 50, 80, true),
          plugged ? "Plugged in" : "Battery"
        )}
        ${ring(1, String(g.nodes_known), "Nodes", "neutral", "Nodes known to the gateway")}
        ${ring(1, String(g.messages_today), "Messages", "neutral", `Messages today (${g.messages_total} stored)`)}
      </div>`;
  }

  _requestResult(key, result, node) {
    if (!result) return "";
    const what = NODE_REQUESTS[key].label.replace("Request ", "");
    const title = `${what.charAt(0).toUpperCase()}${what.slice(1)} · ${this._formatTime(result.ts)}`;
    if (!result.ok) {
      return `<div class="result failed"><div class="result-title">${escapeHtml(title)}</div>${escapeHtml(result.error)}</div>`;
    }
    const data = result.result || {};
    let body;
    if (key === "traceroute") {
      body = this._routeHtml(data, node);
    } else {
      let values = data;
      if (key === "telemetry") {
        const metricsKey = Object.keys(data).find((k) => k.endsWith("Metrics")) || null;
        values = metricsKey ? data[metricsKey] : data;
      } else {
        values = {
          latitude: data.latitude,
          longitude: data.longitude,
          altitude: data.altitude,
          satsInView: data.satsInView,
          time: data.time ? this._formatDateTime(data.time) : undefined,
        };
      }
      const rows = Object.entries(values)
        .filter(([, v]) => isSet(v) && typeof v !== "object")
        .map(([k, v]) => (key === "telemetry" ? formatMetric(k, v) : [prettyKey(k), formatNumber(v)]));
      body = rows.length
        ? `<dl class="kv">${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")}</dl>`
        : `<span>The node answered without values${key === "telemetry" ? " for this telemetry type" : ""}.</span>`;
    }
    return `<div class="result"><div class="result-title">${escapeHtml(title)}</div>${body}</div>`;
  }

  _routeHtml(route, node) {
    const gateway = this._gateway();
    const names = route.nodes || {};
    const label = (num) => {
      const known = names[String(num)];
      if (known) return known.short_name || known.long_name || known.user_id;
      if (gateway && gateway.gateway.num === num) return gateway.gateway.short_name || "gateway";
      return `!${Number(num).toString(16).padStart(8, "0")}`;
    };
    const title = (num) => {
      const known = names[String(num)];
      return known ? `${known.long_name} (${known.user_id})` : "";
    };
    const chain = (hops, snrs, from, to) => {
      const stops = [from, ...(hops || []), to];
      const parts = [];
      stops.forEach((num, i) => {
        if (i > 0) {
          const snr = snrs && snrs[i - 1] !== undefined && snrs[i - 1] !== -128 ? `${(snrs[i - 1] / 4).toFixed(1)} dB` : "";
          parts.push(`<span class="arrow" title="${escapeHtml(snr)}">→${snr ? `<small>${escapeHtml(snr)}</small>` : ""}</span>`);
        }
        parts.push(`<span class="hop" title="${escapeHtml(title(num))}">${escapeHtml(label(num))}</span>`);
      });
      return `<div class="route">${parts.join("")}</div>`;
    };
    const gatewayNum = gateway ? gateway.gateway.num : null;
    const relays = (route.route || []).length;
    let html = `<div class="route-label">Towards ${escapeHtml(label(node.num))} (${relays === 0 ? "direct" : `${relays} relay${relays === 1 ? "" : "s"}`})</div>`;
    html += chain(route.route, route.snrTowards, gatewayNum, node.num);
    if (route.routeBack && route.routeBack.length) {
      html += `<div class="route-label">Back</div>${chain(route.routeBack, route.snrBack, node.num, gatewayNum)}`;
    }
    return html;
  }

  _channelForm(channel, details) {
    const gateway = this._gateway();
    const creating = !channel;
    const primary = !creating && channel.role === "PRIMARY";
    const busy = details.busy;
    const currentBits = creating ? 32 : channel.position_precision_bits || 0;
    const bits = PRECISION_BITS.includes(currentBits) ? PRECISION_BITS : [...PRECISION_BITS, currentBits].sort((a, b) => a - b);
    const precisionOptions = bits
      .map((b) => `<option value="${b}" ${b === currentBits ? "selected" : ""}>${escapeHtml(precisionLabel(b))}</option>`)
      .join("");
    const keyOptions = KEY_CHOICES.filter(([value]) => (creating ? value !== "keep" : true))
      .map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`)
      .join("");
    const indexField = creating
      ? `<label>Slot
           <select name="index">${(gateway.free_channel_indexes || [])
             .map((i) => `<option value="${i}" ${i === details.id ? "selected" : ""}>Channel ${i}</option>`)
             .join("")}</select>
         </label>`
      : "";
    const keyField = primary
      ? `<p class="hint">The key of the primary channel can only be changed in the Meshtastic app: a wrong key cuts the gateway off the mesh.</p>`
      : `<label>Key
           <select name="key" ${busy ? "disabled" : ""}>${keyOptions}</select>
         </label>
         <input name="custom_key" placeholder="Key in base64 (16 or 32 bytes)" aria-label="Key in base64" hidden ${busy ? "disabled" : ""}>`;
    return `
      <h3>${creating ? "Settings" : "Edit channel"}</h3>
      <form class="channel-form" data-index="${creating ? details.id : channel.index}">
        ${indexField}
        <label>Name
          <input name="name" maxlength="${gateway.channel_name_max_bytes}" value="${escapeHtml(creating ? "" : channel.settings_name || "")}" placeholder="${
            creating ? `Up to ${gateway.channel_name_max_bytes} characters` : "Empty keeps the default name"
          }" ${busy ? "disabled" : ""}>
        </label>
        ${keyField}
        <label class="check"><input type="checkbox" name="uplink_enabled" ${!creating && channel.uplink_enabled ? "checked" : ""} ${busy ? "disabled" : ""}> Uplink to MQTT</label>
        <label class="check"><input type="checkbox" name="downlink_enabled" ${!creating && channel.downlink_enabled ? "checked" : ""} ${busy ? "disabled" : ""}> Downlink from MQTT</label>
        <label>Position sharing
          <select name="position_precision" ${busy ? "disabled" : ""}>${precisionOptions}</select>
        </label>
        <div class="actions">
          <button type="submit" class="chip-button primary" ${busy ? "disabled" : ""}>
            <ha-icon icon="${creating ? "mdi:plus" : "mdi:content-save-outline"}"></ha-icon>${busy || (creating ? "Create channel" : "Save changes")}
          </button>
          ${
            !creating && !primary
              ? `<button type="button" class="chip-button danger" data-action="disable-channel" ${busy ? "disabled" : ""}>
                   <ha-icon icon="mdi:cancel"></ha-icon>Disable channel
                 </button>`
              : ""
          }
        </div>
        <p class="hint">Changes are confirmed before they are sent and the gateway applies them immediately.</p>
      </form>`;
  }

  _channelShare(channel) {
    if (!channel.url) return "";
    return `
      <h3>Share</h3>
      <div class="qr" role="img" aria-label="QR code with the settings of this channel">${qrSvg(channel.url)}</div>
      <p class="hint">Scan with the Meshtastic app to add this channel. The code contains the key, so share it only with people who should read the channel.</p>
      <div class="actions">
        <button class="chip-button" data-action="copy" data-text="${escapeHtml(channel.url)}"><ha-icon icon="mdi:content-copy"></ha-icon>Copy link</button>
        <a class="chip-button" href="${escapeHtml(channel.url)}" target="_blank" rel="noopener"><ha-icon icon="mdi:open-in-new"></ha-icon>Open link</a>
        ${
          channel.url_all
            ? `<button class="chip-button" data-action="copy" data-text="${escapeHtml(channel.url_all)}"><ha-icon icon="mdi:content-copy"></ha-icon>Copy link with all channels</button>`
            : ""
        }
      </div>`;
  }

  _channelDetailsBody(channel, details) {
    const gateway = this._gateway();
    const manage = gateway && gateway.can_manage_channels;
    const onOff = (value) => (value ? "on" : "off");
    let encryption;
    if (channel.public) {
      encryption = channel.key_bytes === 0 ? "none – messages are sent in clear text" : "default key – anyone in range can read it";
    } else if (channel.key_bytes === 16) {
      encryption = "AES-128, private key";
    } else if (channel.key_bytes === 32) {
      encryption = "AES-256, private key";
    } else if (channel.key_bytes === null) {
      encryption = "unknown key";
    } else {
      encryption = `private key (${channel.key_bytes} bytes)`;
    }
    const bits = channel.position_precision_bits;
    let precision;
    if (!bits) precision = "positions are not shared on this channel";
    else if (channel.position_precision === 0) precision = "full precision";
    else precision = `about ${formatDistance(channel.position_precision)}`;

    const settings = this._section("Channel", [
      ["Index", String(channel.index)],
      ["Role", prettyEnum(channel.role)],
      ["Encryption", encryption],
      ["Position sharing", precision],
      ["Muted on the gateway", channel.client_muted ? "yes" : null],
    ]);

    const g = channel.gateway || {};
    const mqtt = this._section("MQTT", [
      ["Uplink to MQTT", onOff(channel.uplink_enabled)],
      ["Downlink from MQTT", onOff(channel.downlink_enabled)],
      ["Gateway MQTT", g.mqtt_enabled ? `enabled${g.mqtt_address ? ` (${g.mqtt_address})` : ""}` : "disabled"],
      ["MQTT encryption", g.mqtt_enabled ? onOff(g.mqtt_encryption_enabled) : null],
    ]);
    const mqttHint =
      (channel.uplink_enabled || channel.downlink_enabled) && !g.mqtt_enabled
        ? `<p class="hint">Uplink and downlink only take effect while MQTT is enabled on the gateway.</p>`
        : "";

    const radio = this._section("Gateway radio", [
      ["Region", isSet(g.region) ? prettyEnum(g.region).toUpperCase() : null],
      ["Modem preset", isSet(g.modem_preset) ? prettyEnum(g.modem_preset) : null],
      ["Hop limit", isSet(g.hop_limit) ? String(g.hop_limit) : null],
      ["Store and forward", g.store_forward_enabled ? "enabled" : null],
    ]);

    const a = channel.activity || {};
    const activity = this._section("Activity in Home Assistant", [
      ["Messages kept", a.messages ? `${a.messages} (${a.received} received, ${a.sent} sent)` : "none yet"],
      ["First message", a.first_ts ? this._formatDateTime(a.first_ts) : null],
      ["Last message", a.last_ts ? this._formatDateTimeAgo(a.last_ts) : null],
    ]);
    const nodes = (a.nodes || []).length
      ? `<h3>Nodes heard on this channel</h3>
         <div class="node-list">${a.nodes
           .map(
             (n) => `
          <button class="node-row" data-action="open-conversation" data-conversation="${escapeHtml(n.conversation)}" title="Open direct messages">
            ${this._avatar(n.num, n.short_name)}
            <span class="item-text">
              <span class="item-title"><span class="name">${escapeHtml(n.long_name)}</span><span class="time">${escapeHtml(
                this._formatAgo(n.last_ts)
              )}</span></span>
              <span class="preview">${escapeHtml(n.user_id || "")} · ${n.count} message${n.count === 1 ? "" : "s"}</span>
            </span>
          </button>`
           )
           .join("")}</div>`
      : "";

    // editing and sharing come right after the settings; the activity list below can be long on a busy mesh
    const management = manage ? this._channelForm(channel, details) + this._channelShare(channel) : "";
    return settings + management + mqtt + mqttHint + radio + activity + nodes;
  }
}

customElements.define("meshtastic-chat-panel", MeshtasticChatPanel);

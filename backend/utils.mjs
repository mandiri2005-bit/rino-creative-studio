/**
 * Shared utility functions — imported by server.js and unit tests.
 */

/** Parse mime type → {bits, rate} */
export function parseAudioMime(mime) {
  let bits = 16, rate = 24000;
  for (const p of mime.split(";")) {
    const t = p.trim();
    if (t.toLowerCase().startsWith("rate=")) rate = parseInt(t.split("=")[1]) || rate;
    else if (t.startsWith("audio/L")) bits = parseInt(t.split("L")[1]) || bits;
  }
  return { bits, rate };
}

/** Build a 44-byte WAV header */
export function makeWavHeader(dataLen, bits = 16, rate = 24000, ch = 1) {
  const align = ch * (bits / 8);
  const byteRate = rate * align;
  const b = Buffer.alloc(44);
  b.write("RIFF", 0, "ascii");  b.writeUInt32LE(36 + dataLen, 4);
  b.write("WAVE", 8, "ascii");  b.write("fmt ", 12, "ascii");
  b.writeUInt32LE(16, 16);      b.writeUInt16LE(1, 20);
  b.writeUInt16LE(ch, 22);      b.writeUInt32LE(rate, 24);
  b.writeUInt32LE(byteRate, 28);b.writeUInt16LE(align, 32);
  b.writeUInt16LE(bits, 34);    b.write("data", 36, "ascii");
  b.writeUInt32LE(dataLen, 40);
  return b;
}

/** Raw PCM → WAV buffer */
export function convertToWav(raw, mime) {
  const { bits, rate } = parseAudioMime(mime);
  return Buffer.concat([makeWavHeader(raw.length, bits, rate), raw]);
}

/** Prepend silence to an existing WAV buffer */
export function prependSilence(wav, sec = 0.5) {
  const rate = wav.readUInt32LE(24);
  const bits = wav.readUInt16LE(34);
  const ch   = wav.readUInt16LE(22);
  const sil  = Buffer.alloc(Math.floor(rate * sec) * (bits / 8) * ch, 0);
  const audio = wav.slice(44);
  const combined = Buffer.concat([sil, audio]);
  return Buffer.concat([makeWavHeader(combined.length, bits, rate, ch), combined]);
}

/** Build JSONL for a batch image job */
export function buildJsonl(settings, jobs) {
  return jobs.map((j, i) => JSON.stringify({
    key: `image-${i + 1}`,
    request: {
      contents: [{ parts: [{ text: j.prompt }], role: "user" }],
      generation_config: {
        responseModalities: ["IMAGE"],
        imageConfig: {
          aspectRatio: settings.aspectRatio || "16:9",
          imageSize:   settings.imageSize   || "1K",
        },
      },
    },
  })).join("\n");
}

/** Random short ID */
export function mkId() {
  return Math.random().toString(36).slice(2, 9);
}

/** Derive badge class + label from a job state string */
export function badgeFor(state) {
  if (!state) return ["pend", "—"];
  const u = state.toUpperCase();
  if (u.includes("SUCCEEDED") || u === "DONE")      return ["ok",   "done"];
  if (u.includes("RUNNING"))                         return ["run",  "running"];
  if (u.includes("PENDING")  || u === "QUEUED")      return ["pend", "queued"];
  if (u.includes("FAIL") || u.includes("ERROR") ||
      u.includes("EXPIRED")  || u.includes("CANCEL")) return ["fail", u.replace("JOB_STATE_","").toLowerCase()];
  return ["pend", u.replace("JOB_STATE_","").toLowerCase() || "?"];
}

/** Cost helper: price = [$/M input, $/M output] */
export function calcCost(price, inputTok, outputTok) {
  return (inputTok * price[0] + outputTok * price[1]) / 1_000_000;
}

/** Rough token estimator (~3.8 chars per token) */
export function estTok(text) {
  return Math.ceil((text || "").length / 3.8);
}

/** Parse one SSE line into a structured event */
export function parseSSELine(line) {
  if (!line.startsWith("data: ")) return null;
  const d = line.slice(6);
  if (d === "[DONE]")           return { type: "done" };
  if (d.startsWith("[ERROR"))   return { type: "error", message: d };
  if (d.startsWith("[USAGE:")) {
    try { return { type: "usage", ...JSON.parse(d.slice(7, -1)) }; }
    catch { return null; }
  }
  if (d.startsWith("[TOOL_CALL]"))   return { type: "tool", event: d.slice(11) };
  if (d.startsWith("[TOOL_RESULT]")) return { type: "tool", event: d.slice(13) };
  return { type: "text", text: d };
}

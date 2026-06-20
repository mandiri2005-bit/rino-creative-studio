// ─────────────────────────────────────────────────────────────────────────────
// video/ffmpeg.mjs — Step 6c: the dynamic FFmpeg stitcher.
//
// "No stitch, no product." This builds the filter graph programmatically for any
// scene count (2 → 43) and fuses per-scene visuals + per-scene narration into one
// MP4. The governing rule from the roadmap: THE VOICEOVER IS THE MASTER CLOCK —
// every visual is cut to its scene's measured narration length, never the reverse.
//
//   image scene → Ken Burns move stretched to the scene duration
//   clip scene  → trimmed (or padded) to the scene duration, letterboxed to frame
//   between scenes → xfade (video) + acrossfade (audio), kept in lockstep
//
// The pure builders (buildFilterComplex / buildStitchArgs) take no I/O and are
// unit-tested; stitch() and ffprobeDuration() shell out to the ffmpeg/ffprobe the
// Dockerfile installs (`apt-get install -y ffmpeg`).
// ─────────────────────────────────────────────────────────────────────────────
import { spawn } from "node:child_process";
import { assertWorkerProcess } from "./runtime.mjs";
import { withEncoderSlot } from "./ffmpeg-cpu.mjs";

export const FFMPEG = process.env.FFMPEG_BIN || "ffmpeg";
export const FFPROBE = process.env.FFPROBE_BIN || "ffprobe";

export const VIDEO_DEFAULTS = Object.freeze({
  fps: Number(process.env.VIDEO_FPS || 30),
  width: Number(process.env.VIDEO_WIDTH || 1920),
  height: Number(process.env.VIDEO_HEIGHT || 1080),
  xfade: Number(process.env.VIDEO_XFADE || 0.5),       // crossfade seconds between scenes
  transition: process.env.VIDEO_TRANSITION || "fade",  // any xfade transition name
  fadeDuration: Number(process.env.VIDEO_FADE || 0.8), // fade-in at start + fade-to-black at end (0 disables)
  // veryfast (was "medium") cuts encode wall-time hard for a small size/quality cost;
  // set VIDEO_PRESET=medium to restore the pre-fix output.
  preset: process.env.VIDEO_PRESET || "veryfast",
  crf: Number(process.env.VIDEO_CRF || 20),
  // CPU caps per encode. `threads` bounds the libx264 CODEC pool; `filterThreads`
  // bounds the SEPARATE filtergraph pool (zoompan/scale/xfade) — without the latter,
  // -threads alone doesn't cap CPU because filtering spins up to nproc threads.
  // 0 → omit the flag = ffmpeg default (all cores) = the env escape hatch.
  threads: Number(process.env.VIDEO_THREADS || 2),
  filterThreads: Number(process.env.VIDEO_FILTER_THREADS || 2),
});

// -filter_complex_threads is a GLOBAL option → goes up front (after -y). -threads is an
// OUTPUT option → goes with the encoder args. Either is omitted when set to 0 so the
// escape-hatch env (VIDEO_THREADS=0 / VIDEO_FILTER_THREADS=0) reproduces old behaviour.
function filterThreadArgs(o) {
  return Number(o.filterThreads) > 0 ? ["-filter_complex_threads", String(o.filterThreads)] : [];
}
function codecThreadArgs(o) {
  return Number(o.threads) > 0 ? ["-threads", String(o.threads)] : [];
}

// Ken Burns moves rotate by scene index so adjacent stills don't move identically.
export const KEN_BURNS_MOVES = ["zoom_in", "pan_left", "pan_right", "zoom_out"];

/**
 * A zoompan expression for an image scene. `frames` = duration × fps. Motion is
 * gentle (≤1.12 zoom) so stills feel alive without lurching.
 */
export function kenBurnsExpr(move, frames, width, height, fps) {
  const f = Math.max(1, Math.round(frames));
  // zoompan zooms around z; pan via x/y. 'on' is the output frame index.
  const z = "min(zoom+0.0009,1.12)";
  const presets = {
    zoom_in:   { z, x: "iw/2-(iw/zoom/2)", y: "ih/2-(ih/zoom/2)" },
    zoom_out:  { z: "if(eq(on,0),1.12,max(zoom-0.0009,1.0))", x: "iw/2-(iw/zoom/2)", y: "ih/2-(ih/zoom/2)" },
    pan_left:  { z: "1.1", x: `(iw-iw/zoom)*(1-on/${f})`, y: "ih/2-(ih/zoom/2)" },
    pan_right: { z: "1.1", x: `(iw-iw/zoom)*(on/${f})`, y: "ih/2-(ih/zoom/2)" },
  };
  const p = presets[move] || presets.zoom_in;
  // s= sets the zoompan output size; d= the frame count; fps stabilises timing.
  return `zoompan=z='${p.z}':x='${p.x}':y='${p.y}':d=${f}:s=${width}x${height}:fps=${fps}`;
}

/**
 * Compute the running xfade offsets for a list of scene durations. Joining the
 * (k+1)-th scene starts its transition at (combined-so-far − xfade). Returns the
 * offsets array (length n−1) and the final combined duration.
 */
export function xfadeOffsets(durations, xfade) {
  const offsets = [];
  let combined = durations[0] || 0;
  for (let k = 1; k < durations.length; k++) {
    offsets.push(Number((combined - xfade).toFixed(3)));
    combined = Number((combined + durations[k] - xfade).toFixed(3));
  }
  return { offsets, total: Number(combined.toFixed(3)) };
}

// ── Captions (Step 6e) — built from the KNOWN script + measured timing, no ASR ──
/**
 * Non-overlapping caption windows on the xfade timeline: scene k runs from its
 * start to the next scene's start (last → its full duration). Returns
 * [{ index, start, end }] in seconds.
 */
export function captionWindows(durations, xfade) {
  const wins = [];
  let start = 0;
  for (let k = 0; k < durations.length; k++) {
    const isLast = k === durations.length - 1;
    const nextStart = Number((start + durations[k] - xfade).toFixed(3));
    const end = isLast ? Number((start + durations[k]).toFixed(3)) : nextStart;
    wins.push({ index: k, start: Number(start.toFixed(3)), end });
    start = nextStart;
  }
  return wins;
}

function _srtTime(sec) {
  const s = Math.max(0, sec);
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = Math.floor(s % 60);
  const ms = Math.round((s - Math.floor(s)) * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(hh)}:${p(mm)}:${p(ss)},${p(ms, 3)}`;
}

/** Build an SRT from per-scene texts + measured durations. The voiceover is the
 * master clock, so caption timing is exact — no ASR needed (the script is known). */
export function buildSrt(texts, durations, xfade = VIDEO_DEFAULTS.xfade) {
  const wins = captionWindows(durations, xfade);
  return wins.map((w, i) => {
    const text = String(texts[i] ?? "").trim().replace(/\r?\n/g, " ");
    return `${i + 1}\n${_srtTime(w.start)} --> ${_srtTime(w.end)}\n${text}\n`;
  }).join("\n");
}

// ── Caption styling (.ass) ───────────────────────────────────────────────────
// Burned captions use a FULL ASS style (not subtitles=srt:force_style) so we get
// proper font / outline / shadow / word-wrap AND avoid the comma-in-force_style trap
// that broke the single-pass filtergraph. Font note: Calibri/Carlito are NOT in the
// worker image (Debian apt has no redistributable Calibri and we don't install
// fonts-crosextra-carlito); the best installed sans is Liberation Sans
// (fonts-liberation, Arial-metric) — DejaVu Sans / Noto Sans are also present. All
// knobs are env-tunable.
export const CAPTION_DEFAULTS = Object.freeze({
  font: process.env.VIDEO_CAPTION_FONT || "Liberation Sans",
  fontSize: Number(process.env.VIDEO_CAPTION_FONTSIZE || 22),
  marginV: Number(process.env.VIDEO_CAPTION_MARGINV || 45),
  outline: Number(process.env.VIDEO_CAPTION_OUTLINE || 2),
  shadow: Number(process.env.VIDEO_CAPTION_SHADOW || 1),
});

// ASS canvas reference height. Fontsize/MarginV are expressed against this; PlayResX
// is matched to the frame aspect so libass scales the canvas to the real frame with
// NO horizontal stretch (a 4:3 default canvas on a 16:9 frame would widen the text).
const CAPTION_REF_H = 360;

// ASS timestamp: H:MM:SS.cc (centiseconds).
function _assTime(sec) {
  let s = Math.max(0, sec);
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  let ss = Math.floor(s % 60);
  let cs = Math.round((s - Math.floor(s)) * 100);
  if (cs === 100) { cs = 0; ss += 1; }   // rounding roll-over
  const p = (n) => String(n).padStart(2, "0");
  return `${hh}:${p(mm)}:${p(ss)}.${p(cs)}`;
}

// Make narration safe for an ASS Dialogue line: collapse newlines (libass auto-wraps)
// and neutralise the override-block braces. Commas are FINE here — the Text field is
// everything after the 9th comma, so a comma in narration is safe (the whole reason
// for moving off inline subtitles=srt:force_style).
function _assText(text) {
  return String(text ?? "").trim().replace(/\r?\n/g, " ").replace(/\{/g, "(").replace(/\}/g, ")");
}

/**
 * Build a full .ass subtitle file. `cues` = [{ start, end, text }] in seconds.
 * White bold text, black outline + soft shadow, bottom-centre (Alignment=2), smart
 * word-wrap (WrapStyle 0). opts: { width, height, captionFont, captionFontSize,
 * captionMarginV, captionOutline, captionShadow }.
 */
export function buildAss(cues, opts = {}) {
  const W = Number(opts.width || VIDEO_DEFAULTS.width);
  const H = Number(opts.height || VIDEO_DEFAULTS.height);
  const playY = CAPTION_REF_H;
  const playX = Math.max(1, Math.round(CAPTION_REF_H * W / H));   // match frame aspect → uniform scale
  const font = opts.captionFont || CAPTION_DEFAULTS.font;
  const fs = Number(opts.captionFontSize || CAPTION_DEFAULTS.fontSize);
  const mv = Number(opts.captionMarginV ?? CAPTION_DEFAULTS.marginV);
  const ol = Number(opts.captionOutline ?? CAPTION_DEFAULTS.outline);
  const sh = Number(opts.captionShadow ?? CAPTION_DEFAULTS.shadow);
  const header =
    "[Script Info]\n" +
    "ScriptType: v4.00+\n" +
    "WrapStyle: 0\n" +                  // smart wrap (lower line wider) — no manual \\N
    "ScaledBorderAndShadow: yes\n" +    // outline/shadow scale with the frame, not pinned px
    `PlayResX: ${playX}\nPlayResY: ${playY}\n\n` +
    "[V4+ Styles]\n" +
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, " +
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, " +
    "Alignment, MarginL, MarginR, MarginV, Encoding\n" +
    // PrimaryColour white &H00FFFFFF; OutlineColour black &H00000000; BackColour soft
    // black shadow &H64000000; Bold=1; BorderStyle=1 (outline+shadow); Alignment=2 (bottom-centre).
    `Style: Default,${font},${fs},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,` +
    `100,100,0,0,1,${ol},${sh},2,40,40,${mv},1\n\n` +
    "[Events]\n" +
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n";
  const events = (cues || [])
    .filter((c) => _assText(c.text))
    .map((c) => `Dialogue: 0,${_assTime(c.start)},${_assTime(c.end)},Default,,0,0,0,,${_assText(c.text)}`)
    .join("\n");
  return header + events + "\n";
}

/** Build the captions .ass for the single-pass path — per-scene windows on the xfade
 * timeline (same timing model as buildSrt). */
export function buildAssFromScenes(texts, durations, xfade = VIDEO_DEFAULTS.xfade, opts = {}) {
  const cues = captionWindows(durations, xfade).map((w, i) => ({ start: w.start, end: w.end, text: texts[i] }));
  return buildAss(cues, opts);
}

/**
 * Build the full filter_complex for N scenes. `scenes` is
 *   [{ kind: 'image'|'clip', duration: <seconds>, move?: <ken-burns> }, ...]
 * Inputs are assumed interleaved as (visual_i, audio_i) pairs — i.e. ffmpeg input
 * index 2k is scene k's visual, 2k+1 is scene k's narration audio. Returns
 * { filter, vlabel, alabel, total }.
 */
export function buildFilterComplex(scenes, opts = {}) {
  const o = { ...VIDEO_DEFAULTS, ...opts };
  const n = scenes.length;
  if (n === 0) throw new Error("buildFilterComplex: no scenes");
  const W = o.width, H = o.height, FPS = o.fps;
  // floor each scene at a small minimum, then never let the crossfade exceed the
  // shortest scene — otherwise an xfade offset goes negative and ffmpeg rejects
  // the filter graph (a sub-crossfade-length scene is pathological but possible).
  const durations = scenes.map((s) => Math.max(0.2, Number(s.duration) || 0.2));
  const XF = n > 1 ? Math.max(0.05, Math.min(o.xfade, Math.min(...durations) * 0.8)) : o.xfade;
  const parts = [];

  // ── per-scene preprocessing → [v0..v{n-1}] and [a0..a{n-1}] ──
  scenes.forEach((s, k) => {
    const vin = `${2 * k}:v`;
    const ain = `${2 * k + 1}:a`;
    const dur = durations[k];
    const frames = Math.max(1, Math.round(dur * FPS));
    const fit = `scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H}`;
    const box = `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2`;

    if (s.kind === "image") {
      const move = s.move || KEN_BURNS_MOVES[k % KEN_BURNS_MOVES.length];
      parts.push(
        `[${vin}]${fit},${kenBurnsExpr(move, frames, W, H, FPS)},` +
        `trim=0:${dur},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v${k}]`
      );
    } else {
      // clip: trim to the scene's narration length; pad (freeze last frame) if the
      // clip is shorter, so the visual always exactly fills the master clock.
      parts.push(
        `[${vin}]${box},fps=${FPS},trim=0:${dur},setpts=PTS-STARTPTS,` +
        `tpad=stop_mode=clone:stop_duration=${dur},trim=0:${dur},setsar=1,format=yuv420p[v${k}]`
      );
    }
    // narration audio: trim/pad to the exact scene duration → the master clock.
    parts.push(
      `[${ain}]atrim=0:${dur},asetpts=PTS-STARTPTS,` +
      `apad=whole_dur=${dur},atrim=0:${dur},aformat=sample_rates=48000:channel_layouts=stereo[a${k}]`
    );
  });

  // ── single scene vs xfade video chain + acrossfade audio chain ──
  let vlabel, alabel, total;
  if (n === 1) {
    vlabel = "v0"; alabel = "a0"; total = durations[0];
  } else {
    const xf = xfadeOffsets(durations, XF);
    total = xf.total;
    let vprev = "v0", aprev = "a0";
    for (let k = 1; k < n; k++) {
      const vout = k === n - 1 ? "vout" : `vx${k}`;
      const aout = k === n - 1 ? "aout" : `ax${k}`;
      parts.push(
        `[${vprev}][v${k}]xfade=transition=${o.transition}:duration=${XF}:offset=${xf.offsets[k - 1]}[${vout}]`
      );
      parts.push(`[${aprev}][a${k}]acrossfade=d=${XF}:c1=tri:c2=tri[${aout}]`);
      vprev = vout; aprev = aout;
    }
    vlabel = "vout"; alabel = "aout";
  }

  // ── optional burned-in captions (Step 6e): a FULL ASS style (font/outline/shadow/
  // wrap) burned via the `ass` filter. `ass` is a basename in the ffmpeg cwd (runFfmpeg
  // is invoked with cwd = the job temp dir). Using ass=file (not subtitles=srt:
  // force_style) means commas in narration are safe AND we get full styling here and
  // in the per-scene path. The style/font live inside the .ass (see buildAss). ──
  if (o.ass) {
    parts.push(`[${vlabel}]ass=${o.ass}[vsub]`);
    vlabel = "vsub";
  }

  // ── fade in from black at the start + fade to black at the end (applied last,
  // after captions, so the titles fade too). Timed off the master-clock total. ──
  const fd = Math.min(Number(o.fadeDuration ?? 0), total / 2.2);
  if (fd > 0.05) {
    const outStart = (total - fd).toFixed(3);
    parts.push(`[${vlabel}]fade=t=in:st=0:d=${fd.toFixed(3)},fade=t=out:st=${outStart}:d=${fd.toFixed(3)}[vfade]`);
    parts.push(`[${alabel}]afade=t=in:st=0:d=${fd.toFixed(3)},afade=t=out:st=${outStart}:d=${fd.toFixed(3)}[afade]`);
    vlabel = "vfade"; alabel = "afade";
  }

  return { filter: parts.join(";"), vlabel, alabel, total };
}

/**
 * Build the ffmpeg argv. `scenes` is
 *   [{ kind, duration, visualPath, audioPath, move? }, ...]
 * Image visuals are looped to their duration; clips are read as-is. Output is a
 * web-friendly H.264/AAC MP4 with +faststart.
 */
export function buildStitchArgs(scenes, outPath, opts = {}) {
  const o = { ...VIDEO_DEFAULTS, ...opts };
  const args = ["-y", ...filterThreadArgs(o)];
  scenes.forEach((s) => {
    if (s.kind === "image") {
      args.push("-loop", "1", "-t", String(s.duration), "-i", s.visualPath);
    } else {
      args.push("-i", s.visualPath);
    }
    args.push("-i", s.audioPath);
  });
  const { filter, vlabel, alabel } = buildFilterComplex(scenes, o);
  args.push(
    // This single-pass xfade graph is only used for SHORT videos now (≤ the stitch()
    // threshold) — small enough that the multi-threaded filter framework is safe and
    // fast. Long videos take the per-scene render path instead.
    "-filter_complex", filter,
    "-map", `[${vlabel}]`, "-map", `[${alabel}]`,
    "-r", String(o.fps),
    "-c:v", "libx264", ...codecThreadArgs(o), "-pix_fmt", "yuv420p", "-preset", o.preset || "medium",
    "-crf", String(o.crf || 20),
    "-c:a", "aac", "-b:a", "192k",
    "-max_muxing_queue_size", "9999",
    "-movflags", "+faststart",
    "-shortest",
    outPath
  );
  return args;
}

// Whether this ffmpeg build has the `subtitles` filter (libass). Cached. Some
// ffmpeg builds (e.g. minimal/homebrew without libass) lack it; the Debian apt
// build in the Docker image has it. When absent, the stitcher skips burn-in
// rather than failing the whole render.
let _hasSubs = null;
export function hasSubtitlesFilter() {
  if (_hasSubs !== null) return Promise.resolve(_hasSubs);
  return new Promise((resolve) => {
    const p = spawn(FFMPEG, ["-hide_banner", "-filters"]);
    let out = "";
    p.stdout.on("data", (d) => (out += d));
    p.on("error", () => { _hasSubs = false; resolve(false); });
    p.on("close", () => { _hasSubs = /\bsubtitles\b/.test(out); resolve(_hasSubs); });
  });
}

/** Probe a media file's duration in seconds (number), or null. */
export function ffprobeDuration(filePath) {
  return new Promise((resolve) => {
    const p = spawn(FFPROBE, [
      "-v", "error", "-show_entries", "format=duration",
      "-of", "default=noprint_wrappers=1:nokey=1", filePath,
    ]);
    let out = "";
    p.stdout.on("data", (d) => (out += d));
    p.on("error", () => resolve(null));
    p.on("close", () => {
      const v = parseFloat(out.trim());
      resolve(Number.isFinite(v) ? v : null);
    });
  });
}

/** Run ffmpeg with the given argv; resolves on exit 0, rejects with stderr tail.
 * Guarded: an ffmpeg encode is CPU-heavy and must run in the worker process, not
 * an API handler (the Step 3 trap). See runtime.assertWorkerProcess.
 *
 * Pass { encode: true } for libx264 ENCODE invocations — they're routed through the
 * box-level encoder semaphore (ffmpeg-cpu.withEncoderSlot) so concurrent encoders
 * can't oversubscribe the CPU. The concat-copy (-c copy) and ffprobe are NOT encodes
 * and run unbounded. */
export function runFfmpeg(args, { onProgress, cwd, encode = false } = {}) {
  assertWorkerProcess("ffmpeg encode");
  const exec = () => new Promise((resolve, reject) => {
    const p = spawn(FFMPEG, args, cwd ? { cwd } : undefined);
    let err = "";
    p.stderr.on("data", (d) => {
      err += d;
      if (err.length > 8000) err = err.slice(-8000);
      if (onProgress) {
        const m = /time=(\d+):(\d+):(\d+\.\d+)/.exec(d.toString());
        if (m) onProgress(+m[1] * 3600 + +m[2] * 60 + +m[3]);
      }
    });
    p.on("error", reject);
    p.on("close", (code) =>
      code === 0 ? resolve() : reject(new Error(`ffmpeg exited ${code}:\n${err.slice(-1500)}`))
    );
  });
  return encode ? withEncoderSlot(exec) : exec();
}

// One-cue styled .ass for a single scene clip (the caption shows for the whole clip).
function sceneAss(text, dur, opts = {}) {
  return buildAss([{ start: 0, end: Math.max(0.2, dur - 0.05), text }], opts);
}

/**
 * ffmpeg argv to render ONE scene to a self-contained, uniformly-encoded clip
 * (image → Ken Burns, clip → trim/freeze-pad; audio trimmed/padded to the master
 * clock). first/last scenes carry the opening fade-in / closing fade-to-black.
 * These clips are byte-concatenable (-c copy), which is how long videos avoid the
 * single giant filter graph that OOMs the worker.
 */
export function buildSceneClipArgs(scene, idx, outPath, opts = {}) {
  const o = { ...VIDEO_DEFAULTS, ...opts };
  const W = o.width, H = o.height, FPS = o.fps;
  const dur = Math.max(0.2, Number(scene.duration) || 0.2);
  const frames = Math.max(1, Math.round(dur * FPS));
  const args = ["-y", ...filterThreadArgs(o)];
  if (scene.kind === "image") args.push("-loop", "1", "-t", String(dur), "-i", scene.visualPath);
  else args.push("-i", scene.visualPath);
  args.push("-i", scene.audioPath);

  const fit = `scale=${W}:${H}:force_original_aspect_ratio=increase,crop=${W}:${H}`;
  const box = `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2`;
  let vf;
  if (scene.kind === "image") {
    const move = scene.move || KEN_BURNS_MOVES[idx % KEN_BURNS_MOVES.length];
    vf = `[0:v]${fit},${kenBurnsExpr(move, frames, W, H, FPS)},trim=0:${dur},setpts=PTS-STARTPTS,setsar=1,format=yuv420p`;
  } else {
    vf = `[0:v]${box},fps=${FPS},trim=0:${dur},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=${dur},trim=0:${dur},setsar=1,format=yuv420p`;
  }
  if (opts.ass) vf += `,ass=${opts.ass}`;
  const fd = Math.min(Number(o.fadeDuration ?? 0), dur / 2.2);
  if (opts.isFirst && fd > 0.05) vf += `,fade=t=in:st=0:d=${fd.toFixed(3)}`;
  if (opts.isLast && fd > 0.05) vf += `,fade=t=out:st=${(dur - fd).toFixed(3)}:d=${fd.toFixed(3)}`;
  vf += "[v]";

  let af = `[1:a]atrim=0:${dur},asetpts=PTS-STARTPTS,apad=whole_dur=${dur},atrim=0:${dur},aformat=sample_rates=48000:channel_layouts=stereo`;
  if (opts.isFirst && fd > 0.05) af += `,afade=t=in:st=0:d=${fd.toFixed(3)}`;
  if (opts.isLast && fd > 0.05) af += `,afade=t=out:st=${(dur - fd).toFixed(3)}:d=${fd.toFixed(3)}`;
  af += "[a]";

  args.push(
    "-filter_complex", `${vf};${af}`, "-map", "[v]", "-map", "[a]",
    "-r", String(FPS), "-c:v", "libx264", ...codecThreadArgs(o), "-pix_fmt", "yuv420p", "-preset", o.preset || "medium",
    "-crf", String(o.crf || 20), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    "-video_track_timescale", "90000", "-movflags", "+faststart", outPath
  );
  return args;
}

/**
 * ffmpeg argv for the concat-demuxer byte-join (-c copy → NO re-encode, so it stays
 * cheap and is intentionally NOT thread-capped or semaphore-gated). Kept as a pure
 * exported builder so the "the copy step carries no -threads" guarantee is unit-testable.
 */
export function buildConcatArgs(listName, outPath) {
  return ["-y", "-f", "concat", "-safe", "0", "-i", listName, "-c", "copy", "-movflags", "+faststart", outPath];
}

/**
 * Re-encode the PRE-RENDERED per-scene clips into one MP4 with a short crossfade at each
 * seam (xfade video + acrossfade audio) — smoother than the byte-concat hard cut, and the
 * crossfade lands on each clip's trailing-silence pad so it also softens the audio gap.
 * Clips are already W×H/yuv420p (no zoompan/scale here), so this graph is much lighter
 * than the original single-pass; still, it decodes ALL clips at once, so the caller gates
 * it by scene count and falls back to byte-concat for very long videos (OOM-safe).
 * `durations` = each clip's length (seconds), same order as `clipPaths`.
 */
export function buildXfadeConcatArgs(clipPaths, durations, outPath, opts = {}) {
  const o = { ...VIDEO_DEFAULTS, ...opts };
  const n = clipPaths.length;
  const durs = durations.map((d) => Math.max(0.2, Number(d) || 0.2));
  const XF = n > 1 ? Math.max(0.05, Math.min(Number(o.seamXf ?? 0.4), Math.min(...durs) * 0.8)) : 0;
  const args = ["-y", ...filterThreadArgs(o)];
  for (const c of clipPaths) args.push("-i", c);
  const parts = [];
  let vlab = "0:v", alab = "0:a";
  if (n > 1) {
    const { offsets } = xfadeOffsets(durs, XF);
    for (let k = 1; k < n; k++) {
      const vout = k === n - 1 ? "vout" : `vx${k}`;
      const aout = k === n - 1 ? "aout" : `ax${k}`;
      parts.push(`[${vlab}][${k}:v]xfade=transition=${o.transition}:duration=${XF}:offset=${offsets[k - 1]}[${vout}]`);
      parts.push(`[${alab}][${k}:a]acrossfade=d=${XF}:c1=tri:c2=tri[${aout}]`);
      vlab = vout; alab = aout;
    }
  }
  if (parts.length) args.push("-filter_complex", parts.join(";"), "-map", `[${vlab}]`, "-map", `[${alab}]`);
  else args.push("-map", "0:v", "-map", "0:a");
  args.push(
    "-r", String(o.fps), "-c:v", "libx264", ...codecThreadArgs(o), "-pix_fmt", "yuv420p",
    "-preset", o.preset || "veryfast", "-crf", String(o.crf || 20),
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
    "-movflags", "+faststart", outPath
  );
  return args;
}

/**
 * Pure planner: split `count` clips into chunks of at most `chunkSize` so each xfade
 * graph decodes only ~chunkSize clips (bounded memory). Returns [startIndex, len] pairs;
 * a trailing chunk of 1 is a lone clip (carried forward, crossfaded at the next level).
 * Exported for unit testing.
 */
export function planXfadeChunks(count, chunkSize) {
  const size = Math.max(2, Number(chunkSize) || 2);
  const chunks = [];
  for (let i = 0; i < count; i += size) chunks.push([i, Math.min(size, count - i)]);
  return chunks;
}

/**
 * Crossfade pre-rendered per-scene clips into ONE mp4 with BOUNDED memory. When there are
 * more clips than VIDEO_XFADE_CHUNK, render xfade-concat in chunks (each a small graph),
 * ffprobe the real segment durations, then crossfade the segments — recursively. Peak
 * simultaneous decoders ≈ chunk size, so seams crossfade at ANY scene count without the
 * single-graph OOM that used to force a byte-concat hard cut on long videos.
 * `durations` = each clip's length (s), same order as `clipPaths`.
 */
export async function stitchXfadeChunked(clipPaths, durations, outPath, opts = {}, level = 0) {
  const { join } = await import("node:path");
  const cwd = opts.cwd || ".";
  const chunkSize = Math.max(2, Number(opts.xfadeChunk ?? process.env.VIDEO_XFADE_CHUNK ?? 12));
  const n = clipPaths.length;
  // Small enough for one graph (the proven-safe size) — render it directly.
  if (n <= chunkSize) {
    await runFfmpeg(buildXfadeConcatArgs(clipPaths, durations, outPath, opts), { cwd, encode: true });
    return;
  }
  // Otherwise: chunk → render each chunk to a segment → recurse on the segments.
  const segPaths = [], segDurs = [];
  const chunks = planXfadeChunks(n, chunkSize);
  for (let g = 0; g < chunks.length; g++) {
    const [start, len] = chunks[g];
    const cp = clipPaths.slice(start, start + len);
    const cd = durations.slice(start, start + len);
    if (len === 1) {
      segPaths.push(cp[0]); segDurs.push(cd[0]);   // lone tail — crossfades at next level
      continue;
    }
    const seg = join(cwd, `xseg_L${level}_${g}.mp4`);
    await runFfmpeg(buildXfadeConcatArgs(cp, cd, seg, opts), { cwd, encode: true });
    segPaths.push(seg);
    // measure the real segment length (xfades shrink it) so the next level's offsets are exact
    segDurs.push((await ffprobeDuration(seg)) || cd.reduce((a, b) => a + b, 0));
  }
  return stitchXfadeChunked(segPaths, segDurs, outPath, opts, level + 1);
}

/**
 * Render each scene to its own clip, then join them. Each per-scene render is a small,
 * bounded filter graph, so this scales to arbitrarily long videos (100+ scenes) where the
 * single-pass xfade graph OOMs. Seams crossfade via stitchXfadeChunked (bounded-memory) at
 * ANY scene count; VIDEO_SEAM_XFADE=0 falls back to a byte-concat hard cut.
 */
export async function stitchPerScene(scenes, outPath, opts = {}) {
  const { writeFile } = await import("node:fs/promises");
  const { join } = await import("node:path");
  const cwd = opts.cwd || ".";
  const wantCaps = opts.captions && (await hasSubtitlesFilter());
  const clipPaths = new Array(scenes.length);

  async function renderScene(i) {
    const clip = join(cwd, `scene_${i}.mp4`);
    let ass = null;
    if (wantCaps && (scenes[i].text || "").trim()) {
      ass = `scene_${i}.ass`;
      await writeFile(join(cwd, ass), sceneAss(scenes[i].text, scenes[i].duration, {
        width: opts.width, height: opts.height, captionFont: opts.captionFont,
      }), "utf8");
    }
    const args = buildSceneClipArgs(scenes[i], i, clip, {
      ...opts, ass, isFirst: i === 0, isLast: i === scenes.length - 1,
    });
    await runFfmpeg(args, { cwd, encode: true });
    clipPaths[i] = clip;
  }

  // Render the scene clips CONCURRENTLY (each is a tiny graph) — N workers pull from
  // a shared index, so a long video isn't rendered one-slow-clip-at-a-time.
  const conc = Math.max(1, Number(opts.renderConcurrency ?? process.env.VIDEO_RENDER_CONCURRENCY ?? 4));
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(conc, scenes.length) }, async () => {
    while (next < scenes.length) await renderScene(next++);
  }));

  // Seam crossfade (re-encode) for smoother transitions — now at ANY scene count via
  // bounded-memory chunking (stitchXfadeChunked): clips beyond VIDEO_XFADE_CHUNK are
  // crossfaded in chunks so the graph never decodes the whole video at once (no OOM).
  // The old VIDEO_XFADE_CONCAT_MAX hard cliff is retired. VIDEO_SEAM_XFADE=0 disables
  // crossfade entirely → pure byte-concat hard cut.
  const seamXf = Number(opts.seamXf ?? process.env.VIDEO_SEAM_XFADE ?? 0.4);
  if (seamXf > 0.05 && scenes.length > 1) {
    const durs = scenes.map((s) => Math.max(0.2, Number(s.duration) || 0.2));
    await stitchXfadeChunked(clipPaths, durs, outPath, { ...opts, seamXf, cwd });
  } else {
    // byte-concat hard-cut (single scene / crossfade disabled) — paths relative to cwd; quote-escape.
    const list = join(cwd, "concat.txt");
    await writeFile(list, clipPaths.map((c) => `file '${c.split("/").pop().replace(/'/g, "'\\''")}'`).join("\n"), "utf8");
    await runFfmpeg(buildConcatArgs("concat.txt", outPath), { cwd });
  }
  const duration = await ffprobeDuration(outPath);
  return { path: outPath, duration, sceneCount: scenes.length };
}

/** Stitch scenes into outPath and return the measured result. Long videos go
 * through the per-scene render+concat path (scales); short ones keep the
 * single-pass xfade (smooth crossfades). */
export async function stitch(scenes, outPath, opts = {}) {
  if (!scenes?.length) throw new Error("stitch: no scenes");
  const singlePassMax = Number(opts.singlePassMax ?? process.env.VIDEO_SINGLEPASS_MAX ?? 4);
  if (scenes.length > singlePassMax) {
    return stitchPerScene(scenes, outPath, opts);
  }
  const args = buildStitchArgs(scenes, outPath, opts);
  await runFfmpeg(args, { ...opts, encode: true });
  const duration = await ffprobeDuration(outPath);
  return { path: outPath, duration, sceneCount: scenes.length };
}

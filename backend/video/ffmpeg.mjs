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

export const FFMPEG = process.env.FFMPEG_BIN || "ffmpeg";
export const FFPROBE = process.env.FFPROBE_BIN || "ffprobe";

export const VIDEO_DEFAULTS = Object.freeze({
  fps: Number(process.env.VIDEO_FPS || 30),
  width: Number(process.env.VIDEO_WIDTH || 1920),
  height: Number(process.env.VIDEO_HEIGHT || 1080),
  xfade: Number(process.env.VIDEO_XFADE || 0.5),       // crossfade seconds between scenes
  transition: process.env.VIDEO_TRANSITION || "fade",  // any xfade transition name
  fadeDuration: Number(process.env.VIDEO_FADE || 0.8), // fade-in at start + fade-to-black at end (0 disables)
  preset: process.env.VIDEO_PRESET || "medium",
  crf: Number(process.env.VIDEO_CRF || 20),
});

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

  // ── optional burned-in captions (Step 6e). `srt` is a basename in the ffmpeg
  // cwd, so no path-escaping; runFfmpeg is invoked with cwd = the job temp dir.
  // No force_style here: commas in a style string break filter_complex parsing —
  // libass renders the SRT with safe defaults (white, bottom-centre). ──
  if (o.srt) {
    parts.push(`[${vlabel}]subtitles=${o.srt}[vsub]`);
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
  const args = ["-y"];
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
    "-filter_complex", filter,
    "-map", `[${vlabel}]`, "-map", `[${alabel}]`,
    "-r", String(o.fps),
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", o.preset || "medium",
    "-crf", String(o.crf || 20),
    "-c:a", "aac", "-b:a", "192k",
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
 * an API handler (the Step 3 trap). See runtime.assertWorkerProcess. */
export function runFfmpeg(args, { onProgress, cwd } = {}) {
  assertWorkerProcess("ffmpeg encode");
  return new Promise((resolve, reject) => {
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
}

/** Stitch scenes into outPath and return the measured result. */
export async function stitch(scenes, outPath, opts = {}) {
  if (!scenes?.length) throw new Error("stitch: no scenes");
  const args = buildStitchArgs(scenes, outPath, opts);
  await runFfmpeg(args, opts);
  const duration = await ffprobeDuration(outPath);
  return { path: outPath, duration, sceneCount: scenes.length };
}

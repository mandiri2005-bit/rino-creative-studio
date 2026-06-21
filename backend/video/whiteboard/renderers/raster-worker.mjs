// raster-worker.mjs — worker_threads rasterizer for the svg_ffmpeg backend (perf, opt-in via pool).
// Mirrors svgFfmpeg.mjs rasterizeSvg EXACTLY (resvg → sharp → rsvg-convert) so output is byte-identical
// regardless of thread. The main thread builds the SVG (deterministic) and ships {svg,width,outPng};
// this only does the CPU-bound rasterization in parallel. Any failure is reported back so the main
// thread can fall back to sequential rendering — this worker never changes pixels, only who computes them.
import { parentPort } from "node:worker_threads";
import { writeFileSync } from "node:fs";
import { spawn } from "node:child_process";

async function rasterize(svg, width, outPng) {
  try {
    const { Resvg } = await import("@resvg/resvg-js");
    const opts = { fitTo: { mode: "width", value: width }, font: { loadSystemFonts: true, defaultFontFamily: "DejaVu Sans" } };
    writeFileSync(outPng, new Resvg(svg, opts).render().asPng());
    return;
  } catch (e) { if (!/Cannot find|ERR_MODULE/.test(String(e.message))) throw e; }
  try {
    const sharp = (await import("sharp")).default;
    await sharp(Buffer.from(svg)).png().toFile(outPng); return;
  } catch (e) { if (!/Cannot find|ERR_MODULE/.test(String(e.message))) throw e; }
  await new Promise((res, rej) => {
    const tmp = outPng + ".svg"; writeFileSync(tmp, svg);
    const p = spawn("rsvg-convert", ["-w", String(width), "-o", outPng, tmp]);
    p.on("error", rej); p.on("close", (c) => (c === 0 ? res() : rej(new Error("no rasterizer"))));
  });
}

parentPort.on("message", async (m) => {
  try { await rasterize(m.svg, m.width, m.outPng); parentPort.postMessage({ ok: true, i: m.i }); }
  catch (e) { parentPort.postMessage({ ok: false, i: m.i, err: String(e && e.message || e) }); }
});

// Parse an arbitrary SVG string -> { viewBox, strokes: [{ d, stroke?, width? }] } for the
// self-draw engine. Converts path/circle/rect/line/polyline/polygon/ellipse to a `d`,
// colour from stroke || fill. The engine strokes (fill:none), so even FILLED vector art
// becomes an outline that draws itself — the whiteboard look.
//
// AI illustrators (Recraft) output FILLED flat shapes, often with a full-canvas
// background path and lots of WHITE fills — invisible on a light board. Options handle that:
//   split          : split each path's merged subpaths (on M/m) into separate strokes.
//   dropBg         : drop a full-canvas background <rect> OR rectangle-path.
//   dropLight      : drop near-white strokes (channels >= lightThreshold) — they're
//                    invisible fills/highlights on a light board.
//   lightThreshold : default 245 (drop only near-pure-white).
//   ink            : force EVERY remaining stroke to this colour (monochrome marker).
// LIMITATION: ignores <g>/transform (assumes flat SVG; Recraft is flat by design).

const num = (a, k) => parseFloat(a[k] ?? "0");

function pointsToD(points, close) {
  const v = points.trim().split(/[\s,]+/).map(Number);
  let d = "";
  for (let i = 0; i + 1 < v.length; i += 2) d += `${i === 0 ? "M" : "L"}${v[i]} ${v[i + 1]} `;
  return d.trim() + (close ? " Z" : "");
}

export function nodeToD(tag, a) {
  switch (tag) {
    case "path":
      return a.d || null;
    case "line":
      return `M${num(a, "x1")} ${num(a, "y1")} L${num(a, "x2")} ${num(a, "y2")}`;
    case "polyline":
      return a.points ? pointsToD(a.points, false) : null;
    case "polygon":
      return a.points ? pointsToD(a.points, true) : null;
    case "circle": {
      const cx = num(a, "cx"), cy = num(a, "cy"), r = num(a, "r");
      return `M${cx - r} ${cy} a ${r} ${r} 0 1 0 ${2 * r} 0 a ${r} ${r} 0 1 0 ${-2 * r} 0`;
    }
    case "ellipse": {
      const cx = num(a, "cx"), cy = num(a, "cy"), rx = num(a, "rx"), ry = num(a, "ry");
      return `M${cx - rx} ${cy} a ${rx} ${ry} 0 1 0 ${2 * rx} 0 a ${rx} ${ry} 0 1 0 ${-2 * rx} 0`;
    }
    case "rect": {
      const x = num(a, "x"), y = num(a, "y"), w = num(a, "width"), h = num(a, "height");
      let rx = a.rx != null ? num(a, "rx") : 0;
      let ry = a.ry != null ? num(a, "ry") : rx;
      if (!rx) rx = ry;
      if (!rx && !ry) return `M${x} ${y} h${w} v${h} h${-w} Z`;
      return (
        `M${x + rx} ${y} h${w - 2 * rx} a${rx} ${ry} 0 0 1 ${rx} ${ry} ` +
        `v${h - 2 * ry} a${rx} ${ry} 0 0 1 ${-rx} ${ry} h${-(w - 2 * rx)} ` +
        `a${rx} ${ry} 0 0 1 ${-rx} ${-ry} v${-(h - 2 * ry)} a${rx} ${ry} 0 0 1 ${rx} ${-ry} Z`
      );
    }
    default:
      return null;
  }
}

function parseAttrs(s) {
  const o = {};
  const re = /([\w:-]+)\s*=\s*"([^"]*)"/g;
  let m;
  while ((m = re.exec(s))) o[m[1]] = m[2];
  return o;
}

export function parseColor(c) {
  if (!c) return null;
  const m = c.match(/rgb\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
  if (m) return [+m[1], +m[2], +m[3]];
  const h = c.replace("#", "").trim();
  if (/^[0-9a-f]{6}$/i.test(h)) return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  if (/^[0-9a-f]{3}$/i.test(h)) return [0, 1, 2].map((i) => parseInt(h[i] + h[i], 16));
  return null;
}

// near-white / near-bg => invisible on a light board
export function isLightColor(c, threshold = 245) {
  const rgb = parseColor(c);
  return rgb ? rgb.every((v) => v >= threshold) : false;
}

// a simple rectangle path covering ~the whole canvas (Recraft's background plate)
export function isFullCanvasPath(d, vw, vh) {
  if (!d || !vw || !vh) return false;
  const cmds = (d.match(/[a-zA-Z]/g) || []).length;
  if (cmds > 7) return false; // detailed shape, not a bg plate
  const nums = (d.match(/-?\d*\.?\d+/g) || []).map(Number);
  const xs = [], ys = [];
  for (let i = 0; i + 1 < nums.length; i += 2) { xs.push(nums[i]); ys.push(nums[i + 1]); }
  if (xs.length < 2) return false;
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  return minX <= vw * 0.02 && minY <= vh * 0.02 && maxX >= vw * 0.98 && maxY >= vh * 0.98;
}

export function parseSvg(svg, { split = false, dropBg = false, dropLight = false, lightThreshold = 245, ink = null } = {}) {
  const vb = svg.match(/viewBox\s*=\s*"([^"]+)"/i);
  let viewBox = vb ? vb[1].trim() : null;
  if (!viewBox) {
    const w = svg.match(/\bwidth\s*=\s*"([\d.]+)/i)?.[1];
    const h = svg.match(/\bheight\s*=\s*"([\d.]+)/i)?.[1];
    viewBox = w && h ? `0 0 ${w} ${h}` : "0 0 100 100";
  }
  const [, , vw, vh] = viewBox.split(/\s+/).map(Number);

  let strokes = [];
  const re = /<(path|circle|rect|line|polyline|polygon|ellipse)\b([^>]*?)\/?>/gis;
  let m;
  while ((m = re.exec(svg))) {
    const tag = m[1].toLowerCase();
    const a = parseAttrs(m[2]);
    const d = nodeToD(tag, a);
    if (!d) continue;
    if (dropBg && isFullCanvasPath(d, vw, vh)) continue;
    const color =
      a.stroke && a.stroke !== "none" ? a.stroke : a.fill && a.fill !== "none" ? a.fill : undefined;
    if (dropLight && isLightColor(color, lightThreshold)) continue;
    const width = a["stroke-width"] ? parseFloat(a["stroke-width"]) : undefined;
    strokes.push({ d, color, width });
  }

  if (split) {
    strokes = strokes.flatMap((s) =>
      s.d.split(/(?=[Mm])/).map((p) => p.trim()).filter(Boolean).map((d) => ({ ...s, d }))
    );
  }

  return {
    viewBox,
    strokes: strokes.map((s) => ({
      d: s.d,
      ...(ink ? { stroke: ink } : s.color ? { stroke: s.color } : {}),
      ...(s.width ? { width: s.width } : {}),
    })),
  };
}

// DIAGRAM parse: ordered items (boxes/arrows as strokes + <text> labels) in document
// order, so the hand can DRAW the shapes and WRITE the labels in sequence (no fades).
export function parseSvgDiagram(svg) {
  const vb = svg.match(/viewBox\s*=\s*"([^"]+)"/i);
  const viewBox = vb ? vb[1].trim() : "0 0 100 100";
  const items = [];
  const re = /<(path|circle|rect|line|polyline|polygon|ellipse|text)\b([^>]*?)(?:\/>|>([\s\S]*?)<\/\1>)/gi;
  let m;
  while ((m = re.exec(svg))) {
    const tag = m[1].toLowerCase();
    const a = parseAttrs(m[2]);
    if (tag === "text") {
      const text = (m[3] || "").replace(/\s+/g, " ").trim();
      if (!text) continue;
      items.push({
        kind: "text",
        x: num(a, "x"),
        y: num(a, "y"),
        text,
        fill: a.fill && a.fill !== "none" ? a.fill : "#1A1A1A",
        fontSize: a["font-size"] ? parseFloat(a["font-size"]) : 40,
        anchor: a["text-anchor"] || "start",
      });
    } else {
      const d = nodeToD(tag, a);
      if (!d) continue;
      const stroke =
        a.stroke && a.stroke !== "none" ? a.stroke : a.fill && a.fill !== "none" ? a.fill : "#1A1A1A";
      const width = a["stroke-width"] ? parseFloat(a["stroke-width"]) : 4;
      items.push({ kind: "stroke", d, stroke, width });
    }
  }
  return { viewBox, items };
}

// Like parseSvg but PRESERVES fills — returns shapes for the fill-REVEAL mode, where the
// full COLOURED art is shown and wiped in. This is the right technique for filled AI
// vector art (Recraft etc.) that stroke-self-draw can't reconstruct. Drops the bg plate.
export function parseSvgShapes(svg, { dropBg = true } = {}) {
  const vb = svg.match(/viewBox\s*=\s*"([^"]+)"/i);
  let viewBox = vb ? vb[1].trim() : null;
  if (!viewBox) {
    const w = svg.match(/\bwidth\s*=\s*"([\d.]+)/i)?.[1];
    const h = svg.match(/\bheight\s*=\s*"([\d.]+)/i)?.[1];
    viewBox = w && h ? `0 0 ${w} ${h}` : "0 0 100 100";
  }
  const [, , vw, vh] = viewBox.split(/\s+/).map(Number);
  const shapes = [];
  const re = /<(path|circle|rect|line|polyline|polygon|ellipse)\b([^>]*?)\/?>/gis;
  let m;
  while ((m = re.exec(svg))) {
    const tag = m[1].toLowerCase();
    const a = parseAttrs(m[2]);
    const d = nodeToD(tag, a);
    if (!d) continue;
    if (dropBg && isFullCanvasPath(d, vw, vh)) continue;
    const fill = a.fill && a.fill !== "none" ? a.fill : undefined;
    const stroke = a.stroke && a.stroke !== "none" ? a.stroke : undefined;
    if (!fill && !stroke) continue;
    const width = a["stroke-width"] ? parseFloat(a["stroke-width"]) : undefined;
    shapes.push({ d, ...(fill ? { fill } : {}), ...(stroke ? { stroke } : {}), ...(width ? { width } : {}) });
  }
  return { viewBox, shapes };
}

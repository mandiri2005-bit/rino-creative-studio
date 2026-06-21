// Slot-based layout (guide §H). The Visual Director emits SEMANTIC slots only
// (e.g. "left_center") — NEVER pixel coordinates. This engine turns a slot into a
// concrete box {x,y,w,h} on a 1920x1080 canvas (x,y = CENTER of the box).
//
// SLOT_MAP_16_9 covers EVERY allowedSlot used by the 5 templates in templates.mjs,
// so any valid plan lays out without an "unknown slot" error. Pure data + one fn.

export const SLOT_MAP_16_9 = {
  // detail genre: ONE full-frame hero illustration that draws on (covers the whole 16:9 canvas)
  full_canvas: { x: 960, y: 540, w: 1920, h: 1080 },
  // single_concept
  center: { x: 960, y: 540, w: 360, h: 320 },
  top_center: { x: 960, y: 210, w: 700, h: 150 },
  bottom_center: { x: 960, y: 880, w: 700, h: 150 },
  left_note: { x: 470, y: 540, w: 320, h: 260 },
  right_note: { x: 1450, y: 540, w: 320, h: 260 },

  // problem_solution — heights kept SHORT so each icon's below-label clears the next stacked icon
  // (icons/lineart draw the label BELOW the box; the right column stacks 3 → was overlapping). (Rino)
  left_center: { x: 560, y: 470, w: 300, h: 200 },
  left_bottom: { x: 560, y: 820, w: 300, h: 170 },
  center_arrow: { x: 960, y: 520, w: 200, h: 110 },
  right_center: { x: 1360, y: 540, w: 280, h: 150 },
  right_top: { x: 1360, y: 285, w: 280, h: 150 },
  right_bottom: { x: 1360, y: 800, w: 280, h: 150 },

  // process_flow (4 steps + 3 connectors between them)
  step_1: { x: 340, y: 560, w: 260, h: 240 },
  step_2: { x: 740, y: 560, w: 260, h: 240 },
  step_3: { x: 1180, y: 560, w: 260, h: 240 },
  step_4: { x: 1580, y: 560, w: 260, h: 240 },
  connector_1: { x: 540, y: 560, w: 160, h: 90 },
  connector_2: { x: 960, y: 560, w: 160, h: 90 },
  connector_3: { x: 1380, y: 560, w: 160, h: 90 },

  // comparison (two columns + titles)
  left_title: { x: 560, y: 230, w: 520, h: 130 },
  right_title: { x: 1360, y: 230, w: 520, h: 130 },
  left_1: { x: 560, y: 470, w: 480, h: 150 },
  left_2: { x: 560, y: 650, w: 480, h: 150 },
  left_3: { x: 560, y: 830, w: 480, h: 150 },
  right_1: { x: 1360, y: 470, w: 480, h: 150 },
  right_2: { x: 1360, y: 650, w: 480, h: 150 },
  right_3: { x: 1360, y: 830, w: 480, h: 150 },

  // timeline (title + 5 milestones on a horizontal line)
  title: { x: 960, y: 150, w: 760, h: 120 },
  milestone_1: { x: 360, y: 580, w: 250, h: 240 },
  milestone_2: { x: 660, y: 580, w: 250, h: 240 },
  milestone_3: { x: 960, y: 580, w: 250, h: 240 },
  milestone_4: { x: 1260, y: 580, w: 250, h: 240 },
  milestone_5: { x: 1560, y: 580, w: 250, h: 240 },
};

export function slotBox(slot) {
  return SLOT_MAP_16_9[slot] || null;
}

// Attach a concrete `box` to every element from its semantic slot. TOLERANT: an unknown/missing
// slot falls back to an auto grid instead of THROWING (one off-slot must never blank the scene).
// ASPECT-AWARE (additive): the SLOT_MAP is LANDSCAPE 16:9 (x up to 1560) — those coords run OFF a
// PORTRAIT canvas (9:16, height>width). For portrait we lay flow elements out as an even VERTICAL
// stack sized to the canvas instead. The 16:9 branch below is byte-identical to before; the portrait
// branch NEVER runs for landscape, so existing 16:9 output is unchanged. (cycle/funnel/branch diagram
// layouts are canvas-relative in resolvePlan → they already adapt to any aspect.)
export function layoutWhiteboardPlan(plan, canvas) {
  const els = plan.elements || [];
  const n = els.length;
  const W = Math.round(canvas?.width) || 1920;
  const H = Math.round(canvas?.height) || 1080;
  if (H > W) {
    // PORTRAIT (e.g. 9:16): vertical stack, never off-canvas / never overlapping. full_canvas (the
    // detail-genre hero) fills the whole portrait canvas.
    return { ...plan, elements: els.map((element, i) => ({
      ...element,
      box: element.slot === "full_canvas"
        ? { x: Math.round(W / 2), y: Math.round(H / 2), w: W, h: H }
        : portraitStackBox(i, n, W, H),
    })) };
  }
  // ===== LANDSCAPE 16:9 — unchanged behaviour =====
  let boxed = els.map((element, i) => ({ ...element, box: SLOT_MAP_16_9[element.slot] || fallbackBox(i, n, W, H) }));
  // Overprint guard (Rino: "label ditimpa"): the VD sometimes assigns the SAME slot to 2+ elements
  // (e.g. two "center" → both get SLOT_MAP.center) or a fallback lands on a slotted box, so icons AND
  // labels stack on the identical spot. If any two boxes clearly overlap, re-grid the WHOLE scene into
  // an even auto-grid so every element gets a distinct cell. Runs at resolve time → fixes cached plans.
  if (hasBoxCollision(boxed)) {
    boxed = els.map((element, i) => ({ ...element, box: fallbackBox(i, n, W, H) }));
  }
  return { ...plan, elements: boxed };
}

// true if any two element boxes clearly intersect (centres closer than ~60% of the combined half-extent
// on BOTH axes). Identical boxes (duplicate slot) trivially collide; lightly-touching slots don't.
function hasBoxCollision(els) {
  for (let i = 0; i < els.length; i++) {
    for (let j = i + 1; j < els.length; j++) {
      const a = els[i].box, b = els[j].box;
      if (!a || !b) continue;
      if (Math.abs(a.x - b.x) < (a.w + b.w) / 2 * 0.6 && Math.abs(a.y - b.y) < (a.h + b.h) / 2 * 0.6) return true;
    }
  }
  return false;
}

// even centred grid for elements whose slot isn't in the map (canvas-aware; 16:9 defaults = old values)
function fallbackBox(i, n, W = 1920, H = 1080) {
  const perRow = Math.min(Math.max(1, n), 4);
  const rows = Math.max(1, Math.ceil(n / perRow));
  const col = i % perRow, row = Math.floor(i / perRow);
  const cellW = W / (perRow + 1), cellH = H / (rows + 1);
  const sz = Math.round(Math.min(cellW, cellH) * 0.62);
  return { x: Math.round(cellW * (col + 1)), y: Math.round(cellH * (row + 1)), w: sz, h: sz };
}

// PORTRAIT vertical stack: n boxes centred horizontally, each in its row-cell with room BELOW for the
// (wrapped) label → no overlap by construction, nothing off-canvas. boxW ~74% width, boxH ~half the cell.
function portraitStackBox(i, n, W, H) {
  const top = Math.round(H * 0.11), bottom = Math.round(H * 0.05);
  const cell = (H - top - bottom) / Math.max(1, n);
  const boxH = Math.round(Math.min(cell * 0.5, 360));
  const boxW = Math.round(W * 0.74);
  const cy = Math.round(top + cell * i + cell * 0.36);
  return { x: Math.round(W / 2), y: cy, w: boxW, h: boxH };
}

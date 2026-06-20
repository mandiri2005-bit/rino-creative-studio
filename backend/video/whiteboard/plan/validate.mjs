// Deterministic plan validator (guide §P). LENIENT by design: the renderer is fully tolerant
// (resolvePlan runs strict:false, rescalePlanTiming rewrites beat/scene duration, layoutWhiteboardPlan
// auto-grids unknown slots, missing/bad beats default, unknown template falls back to auto-layout).
// So a plan only FAILS if it is structurally unrenderable — i.e. it has no element to draw. Every
// other issue (slot/label/beats/template/duration/scene_id) is a NON-FATAL warning: rejecting a plan
// for a minor flaw used to blank the WHOLE scene to plain text, which is far worse than rendering a
// slightly-off plan. Returns { ok, errors, warnings } — never throws. Pure → unit-testable.

import { WHITEBOARD_TEMPLATES } from "./templates.mjs";
import { SLOT_MAP_16_9 } from "./slots.mjs";
import { ALLOWED_ACTIONS, ALLOWED_ELEMENT_TYPES, ELEMENT_TARGET_ACTIONS, MAX_LABEL_WORDS } from "./schema.mjs";

export function validateWhiteboardPlan(plan, templateRegistry = WHITEBOARD_TEMPLATES) {
  const errors = [];    // FATAL → caller degrades the scene to handwriting/text
  const warnings = [];  // NON-FATAL → render tolerates; logged for observability only
  if (!plan || typeof plan !== "object") return { ok: false, errors: ["plan is not an object"], warnings };

  // The ONLY hard requirement: at least one element to draw.
  const elements = Array.isArray(plan.elements) ? plan.elements : [];
  if (!elements.length) errors.push("no elements to draw");

  // --- everything below is advisory (render handles it) ---
  if (!plan.scene_id) warnings.push("missing scene_id");
  if (!(typeof plan.duration === "number" && plan.duration > 0)) warnings.push("duration not positive (scene duration used)");

  const template = templateRegistry[plan.template];
  if (!template) warnings.push(`unknown template: ${plan.template} (auto-layout)`);
  if (!Array.isArray(plan.beats)) warnings.push("beats not an array (defaulted)");

  const elementIds = new Set(elements.map((e) => e.id));
  if (template && elements.length > template.maxElements) {
    warnings.push(`many elements (${elements.length} > ${template.maxElements} for ${plan.template})`);
  }

  for (const el of elements) {
    if (!el.id) warnings.push("element missing id");
    if (el.type && !ALLOWED_ELEMENT_TYPES.includes(el.type)) warnings.push(`element ${el.id}: unusual type ${el.type}`);
    // slot mismatch → layoutWhiteboardPlan auto-grids it (no throw); must stay non-fatal.
    if (el.slot && !SLOT_MAP_16_9[el.slot]) warnings.push(`element ${el.id}: slot ${el.slot} not in slot map (auto-placed)`);
    else if (el.slot && template && !template.allowedSlots.includes(el.slot)) warnings.push(`element ${el.id}: slot ${el.slot} not in ${plan.template} (auto-placed)`);
    if (el.label && String(el.label).trim().split(/\s+/).length > MAX_LABEL_WORDS) {
      warnings.push(`element ${el.id}: long label (>${MAX_LABEL_WORDS} words)`);
    }
  }

  // beat timing is rescaled to the scene duration before render, so only flag oddities — never reject.
  for (const b of (Array.isArray(plan.beats) ? plan.beats : [])) {
    if (!ALLOWED_ACTIONS.includes(b.action)) warnings.push(`beat: unusual action ${b.action}`);
    if (ELEMENT_TARGET_ACTIONS.has(b.action) && !elementIds.has(b.target)) {
      warnings.push(`beat ${b.action}: target ${b.target} not an element (ignored)`);
    }
  }

  for (const c of plan.camera || []) {
    if (c.target && c.target !== "full_canvas" && !elementIds.has(c.target)) {
      warnings.push(`camera: target ${c.target} not found (ignored)`);
    }
  }

  return { ok: errors.length === 0, errors, warnings };
}

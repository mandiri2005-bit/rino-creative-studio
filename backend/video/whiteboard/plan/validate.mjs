// Deterministic plan validator (guide §P). Catches bad LLM output BEFORE rendering:
// unknown template/slot/action, overcrowding, long labels, beats outside duration,
// dangling targets. Returns { ok, errors } — never throws. Pure → unit-testable.

import { WHITEBOARD_TEMPLATES } from "./templates.mjs";
import { SLOT_MAP_16_9 } from "./slots.mjs";
import { ALLOWED_ACTIONS, ALLOWED_ELEMENT_TYPES, ELEMENT_TARGET_ACTIONS, MAX_LABEL_WORDS } from "./schema.mjs";

export function validateWhiteboardPlan(plan, templateRegistry = WHITEBOARD_TEMPLATES) {
  const errors = [];
  const warnings = [];
  if (!plan || typeof plan !== "object") return { ok: false, errors: ["plan is not an object"], warnings };

  if (!plan.scene_id) errors.push("Missing scene_id");
  if (!(typeof plan.duration === "number" && plan.duration > 0)) errors.push("duration must be a positive number");

  const template = templateRegistry[plan.template];
  if (!template) errors.push(`Unknown template: ${plan.template}`);

  const elements = Array.isArray(plan.elements) ? plan.elements : (errors.push("elements must be an array"), []);
  const beats = Array.isArray(plan.beats) ? plan.beats : (errors.push("beats must be an array"), []);

  const elementIds = new Set(elements.map((e) => e.id));
  if (template && elements.length > template.maxElements) {
    errors.push(`Too many elements (${elements.length}). Max ${template.maxElements} for ${plan.template}`);
  }

  for (const el of elements) {
    if (!el.id) errors.push("Element missing id");
    if (el.type && !ALLOWED_ELEMENT_TYPES.includes(el.type)) errors.push(`Element ${el.id}: invalid type ${el.type}`);
    // slot mismatch is NON-FATAL now — layoutWhiteboardPlan falls back to an auto grid for any
    // unknown/missing/wrong-template slot, so it must NOT reject the plan (that blanked scenes).
    if (el.slot && !SLOT_MAP_16_9[el.slot]) warnings.push(`Element ${el.id}: slot ${el.slot} not in slot map (auto-placed)`);
    else if (el.slot && template && !template.allowedSlots.includes(el.slot)) warnings.push(`Element ${el.id}: slot ${el.slot} not in ${plan.template} (auto-placed)`);
    if (el.label && String(el.label).trim().split(/\s+/).length > MAX_LABEL_WORDS) {
      errors.push(`Element ${el.id}: label too long (>${MAX_LABEL_WORDS} words): "${el.label}"`);
    }
    if (el.label && String(el.label).trim().split(/\s+/).length > MAX_LABEL_WORDS) {
      errors.push(`Element ${el.id}: label too long (>${MAX_LABEL_WORDS} words): "${el.label}"`);
    }
  }

  for (const b of beats) {
    if (!ALLOWED_ACTIONS.includes(b.action)) errors.push(`Beat: invalid action ${b.action}`);
    if (!(typeof b.start === "number") || b.start < 0) errors.push(`Beat ${b.action}: start must be >= 0`);
    if (!(typeof b.end === "number") || b.end <= b.start) errors.push(`Beat ${b.action}: end must be > start`);
    if (typeof b.end === "number" && b.end > plan.duration + 0.05) errors.push(`Beat ${b.action}: end ${b.end} exceeds duration ${plan.duration}`);
    if (ELEMENT_TARGET_ACTIONS.has(b.action) && !elementIds.has(b.target)) {
      errors.push(`Beat ${b.action}: target not in elements: ${b.target}`);
    }
  }

  for (const c of plan.camera || []) {
    if (!(typeof c.start === "number") || !(typeof c.end === "number") || c.end <= c.start) {
      errors.push(`Camera: bad window ${c.start}..${c.end}`);
    }
    if (c.target && c.target !== "full_canvas" && !elementIds.has(c.target)) {
      errors.push(`Camera: target not found: ${c.target}`);
    }
  }

  return { ok: errors.length === 0, errors, warnings };
}

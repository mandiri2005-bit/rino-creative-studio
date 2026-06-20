// Whiteboard Visual Director agent (guide §E). Converts a narration scene into a
// whiteboard_visual_plan via an LLM, then VALIDATES and REPAIRS (re-asks with the concrete
// errors) up to maxRepairs times. The LLM call is INJECTED (`callLLM`) so this is unit-
// testable with a mock and decoupled from any specific provider. Duration is FORCED to the
// VO-measured value — never the LLM's guess (the gotcha that decides sync, guide §D/§F).

import { TEMPLATE_NAMES } from "./templates.mjs";
import { ALLOWED_ACTIONS } from "./schema.mjs";
import { validateWhiteboardPlan } from "./validate.mjs";

export const VISUAL_DIRECTOR_SYSTEM = `You are a senior whiteboard explainer visual director.

Your job:
Convert a narration scene into a structured whiteboard visual plan.

Rules:
- Return strict JSON only.
- Use one allowed template.
- Use simple visual metaphors.
- Do not create long on-screen text.
- Labels must be 1 to 5 words.
- Use progressive reveal.
- Every beat must have start, end, action, and target when applicable.
- Do not output pixel coordinates.
- Use semantic slots only.
- Prefer reusable SVG icons.
- Match the scene duration exactly.
- Avoid overcrowding: max 6 main elements per scene.`;

export function buildUserPrompt(scene) {
  return `Create a whiteboard visual plan for this scene.

Scene ID: ${scene.scene_id}
Narration: ${scene.narration_text}
Duration seconds: ${scene.duration_actual}
Audience: ${scene.audience || "general"}
Topic: ${scene.topic || "general"}
Tone: ${scene.tone || "clear, friendly"}

Allowed templates:
${TEMPLATE_NAMES.map((t) => `- ${t}`).join("\n")}

Allowed actions:
${ALLOWED_ACTIONS.map((a) => `- ${a}`).join("\n")}

The "duration" field MUST equal ${scene.duration_actual}. Slots must be the template's slots.
Return strict JSON only.`;
}

// Pull the first JSON object out of an LLM reply (tolerates ```json fences / prose).
export function extractJson(text) {
  if (!text) return null;
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const body = fenced ? fenced[1] : text;
  const start = body.indexOf("{");
  const end = body.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try { return JSON.parse(body.slice(start, end + 1)); } catch { return null; }
}

// Force VO-synced timing: duration = measured VO; clamp any beat that overruns.
function enforceDuration(plan, durationActual) {
  if (!plan || typeof durationActual !== "number") return plan;
  plan.duration = durationActual;
  if (Array.isArray(plan.beats)) {
    for (const b of plan.beats) {
      if (typeof b.end === "number") b.end = Math.min(b.end, durationActual);
      if (typeof b.start === "number") b.start = Math.min(b.start, Math.max(0, durationActual - 0.1));
    }
  }
  return plan;
}

export async function generateWhiteboardVisualPlan(scene, { callLLM, maxRepairs = 2 } = {}) {
  if (typeof callLLM !== "function") throw new Error("generateWhiteboardVisualPlan needs a callLLM(system, user) function");
  const messages = [{ role: "system", content: VISUAL_DIRECTOR_SYSTEM }, { role: "user", content: buildUserPrompt(scene) }];

  let lastErrors = [];
  for (let attempt = 0; attempt <= maxRepairs; attempt++) {
    const reply = await callLLM(messages[0].content, messages.slice(1).map((m) => m.content).join("\n\n"));
    let plan = extractJson(reply);
    if (plan) {
      plan = enforceDuration(plan, scene.duration_actual);
      const v = validateWhiteboardPlan(plan);
      if (v.ok) return { plan, attempts: attempt + 1 };
      lastErrors = v.errors;
    } else {
      lastErrors = ["Output was not valid JSON"];
    }
    // ask the model to fix exactly these problems
    messages.push({
      role: "user",
      content: `That plan was invalid. Fix these problems and return strict JSON only:\n- ${lastErrors.join("\n- ")}`,
    });
  }
  return { plan: null, attempts: maxRepairs + 1, errors: lastErrors };
}

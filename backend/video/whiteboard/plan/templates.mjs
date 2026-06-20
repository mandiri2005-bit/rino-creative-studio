// Whiteboard visual-plan TEMPLATES (guide §G). The LLM (Visual Director) does NOT draw
// freely — it picks ONE template and fills its semantic slots. Each template bounds the
// element count and the slots that are legal, which is what keeps output tidy + scalable.
// Pure data + tiny helpers → fully unit-testable, no Remotion.

export const WHITEBOARD_TEMPLATES = {
  single_concept: {
    maxElements: 4,
    allowedSlots: ["center", "top_center", "bottom_center", "left_note", "right_note"],
  },
  problem_solution: {
    maxElements: 6,
    allowedSlots: ["left_center", "left_bottom", "center_arrow", "right_center", "right_top", "right_bottom"],
  },
  process_flow: {
    maxElements: 7,
    allowedSlots: ["step_1", "step_2", "step_3", "step_4", "connector_1", "connector_2", "connector_3"],
  },
  comparison: {
    maxElements: 8,
    allowedSlots: ["left_title", "right_title", "left_1", "left_2", "left_3", "right_1", "right_2", "right_3"],
  },
  timeline: {
    maxElements: 7,
    allowedSlots: ["title", "milestone_1", "milestone_2", "milestone_3", "milestone_4", "milestone_5"],
  },
};

export const TEMPLATE_NAMES = Object.keys(WHITEBOARD_TEMPLATES);

export function isTemplate(name) {
  return Object.prototype.hasOwnProperty.call(WHITEBOARD_TEMPLATES, name);
}

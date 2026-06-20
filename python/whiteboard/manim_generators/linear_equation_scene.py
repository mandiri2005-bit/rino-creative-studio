# Manim scene generator (Guide-2 §J) — SCAFFOLD / EXPERIMENTAL, NOT wired into the pipeline.
#
# Controlled TEMPLATE: the LLM only fills `data` (title/formula/points), never writes free Manim
# code (roadmap §J caution). Specialized math/science backend — NOT for general whiteboard scenes.
# Needs manim + LaTeX/Cairo installed (heavy); the worker image does NOT ship these yet, so the
# dispatcher keeps Remotion as default until this is proven.
#
# Run: manim -ql linear_equation_scene.py GeneratedLinearEquationScene --format mp4 data.json
import json
import sys

try:
    from manim import (
        Scene, Text, MathTex, Axes, Dot, VGroup, Create, Write, FadeIn, LaggedStart,
        BLUE, UP, DOWN, RIGHT,
    )
except Exception:  # manim not installed in this environment — scaffold only
    Scene = object


class GeneratedLinearEquationScene(Scene):  # type: ignore[misc]
    def construct(self):
        data_path = sys.argv[-1] if sys.argv[-1].endswith(".json") else None
        if data_path:
            with open(data_path, "r") as f:
                data = json.load(f)
        else:
            data = {"title": "Linear Growth", "formula": "y = mx + b",
                    "points": [[0, 1], [1, 3], [2, 5], [3, 7]]}

        title = Text(data.get("title", "Linear Growth"), font_size=44).to_edge(UP)
        formula = MathTex(data.get("formula", "y = mx + b"), font_size=64)
        formula.next_to(title, DOWN, buff=0.5)
        axes = Axes(x_range=[0, 4, 1], y_range=[0, 8, 1], x_length=7, y_length=4, tips=True).to_edge(DOWN)
        points = data.get("points", [[0, 1], [1, 3], [2, 5], [3, 7]])
        dots = VGroup(*[Dot(axes.c2p(x, y), color=BLUE) for x, y in points])
        line = axes.plot(lambda x: 2 * x + 1, x_range=[0, 3], color=BLUE)

        self.play(Write(title), run_time=1.0)
        self.play(Write(formula), run_time=1.5)
        self.play(Create(axes), run_time=1.2)
        self.play(LaggedStart(*[Create(d) for d in dots], lag_ratio=0.25), run_time=2.0)
        self.play(Create(line), run_time=1.5)
        slope = Text("Slope = growth rate", font_size=30, color=BLUE).next_to(line, RIGHT)
        self.play(FadeIn(slope), run_time=1.0)
        self.wait(1.0)

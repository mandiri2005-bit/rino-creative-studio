// Each icon is a stroke-only vector path (fill="none") drawn with the self-draw
// (stroke-dashoffset) technique. All share a 0 0 100 100 viewBox so stroke widths
// and the following-hand math stay uniform. Sub-paths (extra "M..." segments) draw
// in order, which reads like the pen lifting and continuing — exactly the look we want.

export interface IconDef {
  d: string;
  viewBox: string;
  size: number; // default on-screen px box (overridable per layout)
}

const VB = "0 0 100 100";
const def = (d: string, size = 170): IconDef => ({ d, viewBox: VB, size });

export const ICONS: Record<string, IconDef> = {
  bulb: def(
    "M50 12 C32 12 20 26 20 42 C20 54 28 60 33 68 L33 76 L67 76 L67 68 C72 60 80 54 80 42 C80 26 68 12 50 12 Z M38 84 L62 84 M42 92 L58 92"
  ),
  arrow: def("M14 50 L82 50 M60 32 L84 50 L60 68"),
  check: def("M18 52 L42 76 L84 26", 150),
  star: def("M50 12 L61 40 L91 42 L67 61 L75 90 L50 73 L25 90 L33 61 L9 42 L39 40 Z"),
  play: def("M34 26 L76 50 L34 74 Z"),
  heart: def(
    "M50 80 C18 58 20 30 40 30 C49 30 50 40 50 44 C50 40 51 30 60 30 C80 30 82 58 50 80 Z"
  ),
  target: def(
    "M50 50 m-34 0 a34 34 0 1 0 68 0 a34 34 0 1 0 -68 0 M50 50 m-18 0 a18 18 0 1 0 36 0 a18 18 0 1 0 -36 0 M50 50 m-4 0 a4 4 0 1 0 8 0 a4 4 0 1 0 -8 0"
  ),
  rocket: def(
    "M50 12 C63 24 67 42 60 62 L40 62 C33 42 37 24 50 12 Z M40 62 L30 76 L44 66 M60 62 L70 76 L56 66 M50 36 m-6 0 a6 6 0 1 0 12 0 a6 6 0 1 0 -12 0"
  ),
  pin: def(
    "M50 16 C36 16 26 26 26 40 C26 58 50 84 50 84 C50 84 74 58 74 40 C74 26 64 16 50 16 Z M50 40 m-9 0 a9 9 0 1 0 18 0 a9 9 0 1 0 -18 0"
  ),
  book: def(
    "M50 28 C42 22 26 22 18 26 L18 74 C26 70 42 70 50 76 C58 70 74 70 82 74 L82 26 C74 22 58 22 50 28 Z M50 28 L50 76"
  ),
  film: def(
    "M22 28 L78 28 L78 72 L22 72 Z M22 40 L78 40 M22 60 L78 60 M34 28 L34 72 M66 28 L66 72"
  ),
  mic: def(
    "M50 20 C44 20 40 24 40 30 L40 48 C40 54 44 58 50 58 C56 58 60 54 60 48 L60 30 C60 24 56 20 50 20 Z M32 46 C32 60 40 68 50 68 C60 68 68 60 68 46 M50 68 L50 80 M40 80 L60 80"
  ),
  globe: def(
    "M50 50 m-34 0 a34 34 0 1 0 68 0 a34 34 0 1 0 -68 0 M16 50 L84 50 M50 16 C32 30 32 70 50 84 M50 16 C68 30 68 70 50 84"
  ),
  coin: def(
    "M50 50 m-32 0 a32 32 0 1 0 64 0 a32 32 0 1 0 -64 0 M50 30 L50 70 M58 38 C54 34 46 34 42 38 C38 42 42 48 50 50 C58 52 62 58 58 62 C54 66 46 66 42 62"
  ),
  clock: def(
    "M50 50 m-32 0 a32 32 0 1 0 64 0 a32 32 0 1 0 -64 0 M50 30 L50 50 L66 58"
  ),
  lightning: def("M54 12 L30 54 L48 54 L44 88 L72 44 L52 44 Z"),
  cloud: def(
    "M34 66 C20 66 18 48 32 47 C33 32 54 30 57 44 C70 38 82 50 73 60 C81 62 79 66 70 66 Z"
  ),
  sun: def(
    "M50 50 m-14 0 a14 14 0 1 0 28 0 a14 14 0 1 0 -28 0 M50 16 L50 26 M50 74 L50 84 M16 50 L26 50 M74 50 L84 50 M27 27 L34 34 M66 66 L73 73 M73 27 L66 34 M34 66 L27 73"
  ),
  smile: def(
    "M50 50 m-32 0 a32 32 0 1 0 64 0 a32 32 0 1 0 -64 0 M39 44 m-3 0 a3 3 0 1 0 6 0 a3 3 0 1 0 -6 0 M61 44 m-3 0 a3 3 0 1 0 6 0 a3 3 0 1 0 -6 0 M36 60 C42 70 58 70 64 60"
  ),
  search: def(
    "M44 44 m-22 0 a22 22 0 1 0 44 0 a22 22 0 1 0 -44 0 M60 60 L82 82",
    150
  ),
  flag: def("M30 16 L30 86 M30 20 L72 20 L62 34 L72 48 L30 48"),
  mountain: def("M12 76 L40 32 L55 56 L65 42 L88 76 Z M40 32 L48 44"),
  chart: def("M20 80 L20 28 M20 80 L84 80 M34 80 L34 60 M50 80 L50 44 M66 80 L66 34"),
  chat: def("M22 30 L78 30 L78 64 L46 64 L34 76 L34 64 L22 64 Z"),
  gear: def(
    "M50 50 m-12 0 a12 12 0 1 0 24 0 a12 12 0 1 0 -24 0 M50 22 L50 34 M50 66 L50 78 M22 50 L34 50 M66 50 L78 50 M30 30 L39 39 M61 61 L70 70 M70 30 L61 39 M39 61 L30 70"
  ),
};

export const ICON_NAMES = Object.keys(ICONS);

// A hand-drawn underline swoosh used by `accent`. Same self-draw technique.
export const UNDERLINE_PATH = "M8 24 C140 6 320 6 430 22 C470 28 500 24 512 14";
export const UNDERLINE_VIEWBOX = "0 0 520 40";
export const UNDERLINE_W = 520;
export const UNDERLINE_H = 40;

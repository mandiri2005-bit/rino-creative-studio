import React from "react";

// Optional real-hand image (a PNG/photo of a hand holding a pen, tip at the top-left).
// Set via spec.handImage; Whiteboard calls setHandImage() once per render.
let HAND_IMAGE: string | null = null;
export const setHandImage = (url: string | null) => {
  HAND_IMAGE = url || null;
};

// A hand + forearm gripping a marker, entering from the lower-right with the pen tip at
// the top-left (the draw point). Replaces the bare-pencil sprite. Swap in a real photo
// hand via spec.handImage when you want photorealism.
export const Hand: React.FC<{
  x: number; // px, where the pen tip should sit (within the relative wrapper)
  y: number;
  size: number;
  nib: string; // ink colour at the very tip (matches the stroke being drawn)
  body?: string; // unused (kept for call-site compatibility)
}> = ({ x, y, size, nib }) => {
  const common: React.CSSProperties = {
    position: "absolute",
    left: x,
    top: y,
    width: size,
    height: size,
    transform: "translate(-3%, -3%)", // pen tip sits ~3% in from the top-left
    pointerEvents: "none",
  };

  if (HAND_IMAGE) {
    return <img src={HAND_IMAGE} style={{ ...common, objectFit: "contain" }} alt="" />;
  }

  return (
    <div style={common}>
      <svg width={size} height={size} viewBox="0 0 256 256">
        {/* forearm */}
        <path
          d="M132 150 C165 180 195 205 225 228 C238 238 256 242 256 256 L150 256 C128 244 116 212 119 180 C121 165 125 156 132 150 Z"
          fill="#E6B089"
        />
        {/* sleeve cuff */}
        <path d="M256 256 L150 256 C176 240 192 214 201 193 L256 220 Z" fill="#3F6FB2" />
        {/* fist / palm */}
        <path
          d="M104 110 C108 92 132 86 148 96 C166 86 188 98 190 120 C206 128 209 154 193 166 C189 190 159 203 134 192 C112 199 92 180 94 158 C86 146 90 122 104 110 Z"
          fill="#E6B089"
        />
        {/* thumb */}
        <path
          d="M100 118 C86 112 75 126 85 140 C93 151 110 149 116 136 C114 126 108 120 100 118 Z"
          fill="#E6B089"
        />
        {/* knuckle creases */}
        <path d="M120 116 C134 108 152 112 164 124" stroke="#C98F66" strokeWidth={2.5} fill="none" strokeLinecap="round" />
        <path d="M116 134 C132 127 150 130 162 140" stroke="#C98F66" strokeWidth={2.5} fill="none" strokeLinecap="round" />
        {/* pen barrel */}
        <path d="M21 11 L156 146 L146 156 L11 21 Z" fill="#303030" />
        <path d="M31 19 L150 138" stroke="#5a5a5a" strokeWidth={2} strokeLinecap="round" />
        {/* nib (ink colour) */}
        <path d="M21 11 L7 7 L11 21 Z" fill={nib} />
      </svg>
    </div>
  );
};

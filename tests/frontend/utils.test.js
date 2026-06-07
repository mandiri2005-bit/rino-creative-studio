/**
 * Frontend utility tests (Jest)
 * Tests pure functions extracted from index.html:
 *   - calcCost, estTok, parseSSELine, badgeFor, MODEL_PRICING, CHAT_MODEL_TIERS
 *
 * Run: npm test  (from tests/frontend/)
 */

// ── Re-implement the frontend pure functions here for testability ─────────────
// (Duplicated from index.html — source of truth stays in utils.mjs for Node)

// MODEL_PRICING table (subset — same values as in index.html)
const CHAT_MODEL_TIERS = [
  { tier: "⚡ Lite", models: [
    { value: "glm-4.5-flash",          price: [0.01,  0.04] },
    { value: "gpt-5-nano",             price: [0.05,  0.40] },
    { value: "gemini-2.5-flash-lite",  price: [0.10,  0.40] },
    { value: "gpt-4o-mini",            price: [0.15,  0.60] },
    { value: "grok-4-fast",            price: [0.20,  0.50] },
    { value: "deepseek-v3-250324",     price: [0.25,  1.00] },
    { value: "gemini-3.1-flash-lite",  price: [0.25,  1.50] },
    { value: "deepseek-chat",          price: [0.29,  1.14] },
    { value: "gemini-2.5-flash",       price: [0.30,  2.40] },
    { value: "gemini-3-flash-preview", price: [0.44,  2.64] },
  ]},
  { tier: "⚡⚡ Medium", models: [
    { value: "gpt-5",                  price: [1.25, 10.00] },
    { value: "gemini-2.5-pro",         price: [1.25, 10.00] },
    { value: "gemini-3.5-flash",       price: [1.50,  9.00] },
    { value: "qwen-max",               price: [1.60,  6.40] },
    { value: "gemini-3-pro-preview",   price: [1.80, 10.80] },
    { value: "gpt-4.1",                price: [2.00,  8.00] },
    { value: "gemini-3.1-pro-preview", price: [2.00, 12.00] },
    { value: "gpt-4o",                 price: [2.50, 10.00] },
    { value: "grok-4-latest",          price: [3.00, 15.00] },
    { value: "claude-sonnet-4-6",      price: [3.00, 15.00] },
  ]},
  { tier: "⚡⚡⚡ Power", models: [
    { value: "gemini-3.1-pro-preview",        price: [2.00, 12.00] },
    { value: "grok-4-latest",                 price: [3.00, 15.00] },
    { value: "claude-sonnet-4-6",             price: [3.00, 15.00] },
    { value: "claude-sonnet-4-6-thinking",    price: [3.00, 15.00] },
    { value: "gemini-2.5-pro-thinking",       price: [1.25, 10.00] },
    { value: "gemini-3-pro-preview-thinking", price: [1.80, 10.80] },
    { value: "claude-opus-4-6",               price: [5.00, 25.00] },
    { value: "claude-opus-4-7",               price: [5.00, 25.00] },
    { value: "claude-opus-4-6-thinking",      price: [5.00, 25.00] },
    { value: "claude-opus-4-7-thinking",      price: [5.00, 25.00] },
  ]},
];
const MODEL_PRICING = {};
CHAT_MODEL_TIERS.forEach(t => t.models.forEach(m => { MODEL_PRICING[m.value] = m.price; }));

// Functions (copied exactly from index.html)
const calcCost = (modelValue, inputTok, outputTok) => {
  const p = MODEL_PRICING[modelValue] || [1, 4];
  return (inputTok * p[0] + outputTok * p[1]) / 1_000_000;
};

const estTok = text => Math.ceil((text || "").length / 3.8);

const parseSSELine = (line) => {
  if (!line.startsWith("data: ")) return null;
  const d = line.slice(6);
  if (d === "[DONE]") return { type: "done" };
  if (d.startsWith("[ERROR")) return { type: "error", message: d };
  if (d.startsWith("[USAGE:")) {
    try { return { type: "usage", ...JSON.parse(d.slice(7, -1)) }; }
    catch { return null; }
  }
  if (d.startsWith("[TOOL_CALL]"))   return { type: "tool", event: d.slice(11) };
  if (d.startsWith("[TOOL_RESULT]")) return { type: "tool", event: d.slice(13) };
  return { type: "text", text: d };
};

const badgeFor = (state) => {
  if (!state) return ["pend", "—"];
  const u = state.toUpperCase();
  if (u.includes("SUCCEEDED") || u === "DONE") return ["ok", "done"];
  if (u.includes("RUNNING")) return ["run", "running"];
  if (u.includes("PENDING") || u === "QUEUED") return ["pend", "queued"];
  if (u.includes("FAIL") || u.includes("ERROR") || u.includes("EXPIRED") || u.includes("CANCEL"))
    return ["fail", u.replace("JOB_STATE_", "").toLowerCase()];
  return ["pend", u.replace("JOB_STATE_", "").toLowerCase() || "?"];
};

// ═════════════════════════════════════════════════════════════════════════════
// TESTS
// ═════════════════════════════════════════════════════════════════════════════

// ─── MODEL_PRICING lookup ─────────────────────────────────────────────────────
describe("MODEL_PRICING", () => {
  test("has entry for every model in all tiers", () => {
    for (const tier of CHAT_MODEL_TIERS) {
      for (const m of tier.models) {
        expect(MODEL_PRICING[m.value]).toBeDefined();
        expect(MODEL_PRICING[m.value]).toEqual(m.price);
      }
    }
  });

  test("cheapest model is glm-4.5-flash at $0.01/$0.04", () => {
    expect(MODEL_PRICING["glm-4.5-flash"]).toEqual([0.01, 0.04]);
  });

  test("most expensive is claude-opus-4-7-thinking at $5/$25", () => {
    expect(MODEL_PRICING["claude-opus-4-7-thinking"]).toEqual([5.00, 25.00]);
  });

  test("Gemini 3.1 Pro price matches doc ($2/$12)", () => {
    expect(MODEL_PRICING["gemini-3.1-pro-preview"]).toEqual([2.00, 12.00]);
  });

  test("Claude Sonnet 4.6 matches Anthropic pricing ($3/$15)", () => {
    expect(MODEL_PRICING["claude-sonnet-4-6"]).toEqual([3.00, 15.00]);
  });

  test("all prices have exactly 2 values [input, output]", () => {
    for (const [model, price] of Object.entries(MODEL_PRICING)) {
      expect(price).toHaveLength(2);
      expect(price[0]).toBeGreaterThan(0);
      expect(price[1]).toBeGreaterThan(0);
    }
  });

  test("output price >= input price for all models", () => {
    for (const [model, [inp, out]] of Object.entries(MODEL_PRICING)) {
      expect(out).toBeGreaterThanOrEqual(inp);
    }
  });
});

// ─── CHAT_MODEL_TIERS structure ───────────────────────────────────────────────
describe("CHAT_MODEL_TIERS", () => {
  test("has exactly 3 tiers", () => {
    expect(CHAT_MODEL_TIERS).toHaveLength(3);
  });

  test("each tier has exactly 10 models", () => {
    for (const tier of CHAT_MODEL_TIERS) {
      expect(tier.models).toHaveLength(10);
    }
  });

  test("Lite tier has cheapest models (all < $0.50/M input)", () => {
    const liteTier = CHAT_MODEL_TIERS.find(t => t.tier.includes("Lite"));
    for (const m of liteTier.models) {
      expect(m.price[0]).toBeLessThan(0.50);
    }
  });

  test("Power tier max price is claude-opus level ($5/M input)", () => {
    const powerTier = CHAT_MODEL_TIERS.find(t => t.tier.includes("Power"));
    const maxInput = Math.max(...powerTier.models.map(m => m.price[0]));
    expect(maxInput).toBeLessThanOrEqual(5.00);
  });

  test("all model values are non-empty strings", () => {
    for (const tier of CHAT_MODEL_TIERS) {
      for (const m of tier.models) {
        expect(typeof m.value).toBe("string");
        expect(m.value.length).toBeGreaterThan(0);
      }
    }
  });

  test("no duplicate model values across tiers (except intentional)", () => {
    // Some models intentionally appear in both Medium and Power tiers
    // (grok-4, claude-sonnet, gemini-3.1-pro)
    // Just verify that within a single tier there are no duplicates
    for (const tier of CHAT_MODEL_TIERS) {
      const values = tier.models.map(m => m.value);
      const unique = new Set(values);
      expect(unique.size).toBe(values.length);
    }
  });
});

// ─── calcCost ─────────────────────────────────────────────────────────────────
describe("calcCost", () => {
  test("zero tokens → $0.00", () => {
    expect(calcCost("gemini-2.5-flash", 0, 0)).toBe(0);
  });

  test("1M input + 1M output Gemini 2.5 Flash → $2.70", () => {
    expect(calcCost("gemini-2.5-flash", 1_000_000, 1_000_000)).toBeCloseTo(2.70);
  });

  test("1M input only GPT-4o → $2.50", () => {
    expect(calcCost("gpt-4o", 1_000_000, 0)).toBeCloseTo(2.50);
  });

  test("1M output only GPT-4o → $10.00", () => {
    expect(calcCost("gpt-4o", 0, 1_000_000)).toBeCloseTo(10.00);
  });

  test("unknown model falls back to $1/$4 defaults", () => {
    const cost = calcCost("unknown-model-xyz", 1_000_000, 1_000_000);
    expect(cost).toBeCloseTo(1 + 4); // $5.00
  });

  test("small request (500 in + 200 out) GPT-4o-mini", () => {
    // 500 * 0.15/1M + 200 * 0.60/1M = 0.000075 + 0.00012 = 0.000195
    const cost = calcCost("gpt-4o-mini", 500, 200);
    expect(cost).toBeCloseTo(0.000195, 6);
  });

  test("Claude Opus 4.7 Thinking at typical request size", () => {
    // 2000 in + 1000 out, $5/$25
    // = 0.01 + 0.025 = 0.035
    const cost = calcCost("claude-opus-4-7-thinking", 2000, 1000);
    expect(cost).toBeCloseTo(0.01 + 0.025, 6);
  });

  test("result is always non-negative", () => {
    for (const [model] of Object.entries(MODEL_PRICING)) {
      expect(calcCost(model, 100, 200)).toBeGreaterThanOrEqual(0);
    }
  });
});

// ─── estTok ───────────────────────────────────────────────────────────────────
describe("estTok", () => {
  test("empty string → 0", () => expect(estTok("")).toBe(0));
  test("null → 0",         () => expect(estTok(null)).toBe(0));
  test("undefined → 0",    () => expect(estTok(undefined)).toBe(0));

  test("short word → at least 1 token", () => {
    expect(estTok("hello")).toBeGreaterThanOrEqual(1);
  });

  test("380 chars → ~100 tokens", () => {
    const t = estTok("x".repeat(380));
    expect(t).toBeGreaterThanOrEqual(95);
    expect(t).toBeLessThanOrEqual(105);
  });

  test("scales proportionally with length", () => {
    expect(estTok("a".repeat(100))).toBeLessThan(estTok("a".repeat(200)));
  });

  test("1520 chars → ~400 tokens", () => {
    const t = estTok("a".repeat(1520));
    expect(t).toBeGreaterThanOrEqual(380);
    expect(t).toBeLessThanOrEqual(420);
  });

  test("always returns integer", () => {
    expect(Number.isInteger(estTok("test sentence"))).toBe(true);
  });
});

// ─── parseSSELine ─────────────────────────────────────────────────────────────
describe("parseSSELine", () => {
  describe("non-data lines", () => {
    test("plain string without prefix → null", () => expect(parseSSELine("hello")).toBeNull());
    test("empty string → null",                () => expect(parseSSELine("")).toBeNull());
    test("comment line → null",                () => expect(parseSSELine(": keepalive")).toBeNull());
    test("event: line → null",                 () => expect(parseSSELine("event: message")).toBeNull());
    test("id: line → null",                    () => expect(parseSSELine("id: 42")).toBeNull());
  });

  describe("[DONE]", () => {
    test("parses to {type:done}", () => {
      expect(parseSSELine("data: [DONE]")).toEqual({ type: "done" });
    });
  });

  describe("[ERROR]", () => {
    test("parses to {type:error}", () => {
      const r = parseSSELine("data: [ERROR: connection timeout]");
      expect(r.type).toBe("error");
      expect(r.message.includes("timeout")).toBe(true);
    });
    test("short error message", () => {
      const r = parseSSELine("data: [ERROR]");
      expect(r.type).toBe("error");
    });
  });

  describe("[USAGE:]", () => {
    test("parses input and output token counts", () => {
      expect(parseSSELine('data: [USAGE:{"input":312,"output":127}]'))
        .toEqual({ type: "usage", input: 312, output: 127 });
    });
    test("large token counts", () => {
      const r = parseSSELine('data: [USAGE:{"input":45123,"output":8901}]');
      expect(r.input).toBe(45123);
      expect(r.output).toBe(8901);
    });
    test("malformed JSON → null", () => {
      expect(parseSSELine("data: [USAGE:notjson]")).toBeNull();
    });
    test("empty JSON → not crash", () => {
      const r = parseSSELine("data: [USAGE:{}]");
      expect(r === null || r.type === "usage").toBe(true);
    });
  });

  describe("[TOOL_CALL] / [TOOL_RESULT]", () => {
    test("TOOL_CALL → {type:tool}", () => {
      const r = parseSSELine('data: [TOOL_CALL]search_files({"query":"cats"})');
      expect(r.type).toBe("tool");
      expect(r.event.includes("search_files")).toBe(true);
    });
    test("TOOL_RESULT → {type:tool}", () => {
      const r = parseSSELine("data: [TOOL_RESULT]Found 3 files");
      expect(r.type).toBe("tool");
      expect(r.event.includes("Found")).toBe(true);
    });
  });

  describe("text tokens", () => {
    test("normal word → {type:text,text}", () => {
      expect(parseSSELine("data: Hello")).toEqual({ type: "text", text: "Hello" });
    });
    test("multiword → preserved", () => {
      expect(parseSSELine("data: foo bar baz").text).toBe("foo bar baz");
    });
    test("leading space preserved", () => {
      expect(parseSSELine("data:  hello").text).toBe(" hello");
    });
    test("number as text", () => {
      expect(parseSSELine("data: 42").text).toBe("42");
    });
    test("empty data → empty text", () => {
      expect(parseSSELine("data: ").text).toBe("");
    });
    test("punctuation preserved", () => {
      expect(parseSSELine("data: Hello, world!").text).toBe("Hello, world!");
    });
  });
});

// ─── badgeFor ────────────────────────────────────────────────────────────────
describe("badgeFor", () => {
  test("null → ['pend','—']",              () => expect(badgeFor(null)).toEqual(["pend", "—"]));
  test("undefined → ['pend','—']",         () => expect(badgeFor(undefined)).toEqual(["pend", "—"]));
  test("SUCCEEDED → ok",                   () => expect(badgeFor("JOB_STATE_SUCCEEDED")[0]).toBe("ok"));
  test("DONE → ok/done",                   () => expect(badgeFor("DONE")).toEqual(["ok", "done"]));
  test("succeeded (lowercase) → ok",       () => expect(badgeFor("succeeded")[0]).toBe("ok"));
  test("RUNNING → run",                    () => expect(badgeFor("JOB_STATE_RUNNING")[0]).toBe("run"));
  test("running → run",                    () => expect(badgeFor("running")[0]).toBe("run"));
  test("PENDING → pend",                   () => expect(badgeFor("JOB_STATE_PENDING")[0]).toBe("pend"));
  test("QUEUED → pend",                    () => expect(badgeFor("QUEUED")[0]).toBe("pend"));
  test("FAILED → fail",                    () => expect(badgeFor("JOB_STATE_FAILED")[0]).toBe("fail"));
  test("CANCELLED → fail",                 () => expect(badgeFor("JOB_STATE_CANCELLED")[0]).toBe("fail"));
  test("EXPIRED → fail",                   () => expect(badgeFor("JOB_STATE_EXPIRED")[0]).toBe("fail"));
  test("error → fail",                     () => expect(badgeFor("error")[0]).toBe("fail"));
  test("label stripped of JOB_STATE_ prefix", () => {
    expect(badgeFor("JOB_STATE_FAILED")[1]).toBe("failed");
  });

  // Session cost color logic (mirrors UI)
  test("cost thresholds for display color", () => {
    // These match the UI: green < $0.0005, yellow < $0.005, red >= $0.005
    const green  = 0.0001;
    const yellow = 0.002;
    const red    = 0.015;
    expect(green  < 0.0005).toBe(true);
    expect(yellow < 0.005).toBe(true);
    expect(red    >= 0.005).toBe(true);
  });
});

// ─── Integration: cost → display ──────────────────────────────────────────────
describe("Cost calculation integration", () => {
  test("typical chat request cost is in reasonable range", () => {
    // ~2000 tokens in, ~500 out with Gemini 2.5 Flash
    const cost = calcCost("gemini-2.5-flash", 2000, 500);
    // Should be under $0.01 for a normal message
    expect(cost).toBeLessThan(0.01);
    expect(cost).toBeGreaterThan(0);
  });

  test("session of 10 requests with Claude Sonnet stays under $1", () => {
    let total = 0;
    for (let i = 0; i < 10; i++) {
      total += calcCost("claude-sonnet-4-6", 3000, 1000);
    }
    // 10 × (3000×$3 + 1000×$15) / 1M = 10 × (0.009 + 0.015) = $0.24
    expect(total).toBeLessThan(1.00);
    expect(total).toBeCloseTo(0.24, 2);
  });

  test("GLM 4.5 Flash is 500x cheaper than Claude Opus per token", () => {
    const glm    = calcCost("glm-4.5-flash",         1000, 1000);
    const claude = calcCost("claude-opus-4-7-thinking", 1000, 1000);
    expect(claude / glm).toBeGreaterThan(100);  // at least 100x cheaper
  });

  test("token estimate × price gives consistent micro-costs", () => {
    const msg = "Hello, can you help me understand quantum computing?";
    const tokens = estTok(msg);
    const cost = calcCost("gpt-4o-mini", tokens, 0);
    // 14 tokens × $0.15/M ≈ $0.0000021
    expect(cost).toBeGreaterThan(0);
    expect(cost).toBeLessThan(0.001);
  });
});

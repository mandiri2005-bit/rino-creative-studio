const fs = require("fs");
const { Client } = require("pg");

const env = fs.readFileSync("../.env", "utf8");
const m = env.match(/^DATABASE_URL_DEV=(.*)$/m);
if (!m) { console.error("DATABASE_URL_DEV not found in ../.env"); process.exit(1); }
let raw = m[1].trim().replace(/^["']|["']$/g, "");

const variants = {
  A_asis: raw,
  B_require_nocb: raw.replace(/[?&]channel_binding=require/g, "")
                     .replace(/sslmode=verify-full/g, "sslmode=require"),
};

(async () => {
  for (const [name, url] of Object.entries(variants)) {
    const masked = url.replace(/(:\/\/[^:]+:)[^@]+(@)/, "$1***$2");
    const c = new Client({ connectionString: url, ssl: { rejectUnauthorized: false } });
    try {
      await c.connect();
      const r = await c.query("select current_user");
      console.log("\n" + name + "  OK ->", r.rows[0].current_user);
      await c.end();
    } catch (e) {
      console.log("\n" + name + "  FAIL");
      console.log("   url :", masked);
      console.log("   code:", e.code);
      console.log("   msg :", e.message);
    }
  }
})();

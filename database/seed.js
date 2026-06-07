#!/usr/bin/env node
// ═══════════════════════════════════════════════════════════════════════════════
// seed.js — Insert realistic test data into Neon PostgreSQL
// ═══════════════════════════════════════════════════════════════════════════════
//   Usage:  node database/seed.js
//   Deps:   npm install pg dotenv
//
//   Idempotent: uses deterministic UUIDs + ON CONFLICT (id) DO NOTHING.
//   Running twice produces zero duplicates.
// ═══════════════════════════════════════════════════════════════════════════════

const { Client } = require('pg');
const path = require('path');

require('dotenv').config({ path: path.resolve(__dirname, '..', '.env') });

// ── Pick the right Neon branch ──────────────────────────────────────────────
const DATABASE_URL =
  process.env.NODE_ENV === 'staging'    ? process.env.DATABASE_URL_STAGING :
  process.env.NODE_ENV === 'production' ? process.env.DATABASE_URL :
                                          process.env.DATABASE_URL_DEV;

if (!DATABASE_URL) {
  const envVar =
    process.env.NODE_ENV === 'staging'    ? 'DATABASE_URL_STAGING' :
    process.env.NODE_ENV === 'production' ? 'DATABASE_URL' :
                                            'DATABASE_URL_DEV';
  console.error('✖  ' + envVar + ' is not set (NODE_ENV=' + (process.env.NODE_ENV || 'undefined') + '). Check your .env file.');
  process.exit(1);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Deterministic UUIDs  (fixed so re-runs hit ON CONFLICT → idempotent)
// ═══════════════════════════════════════════════════════════════════════════════

const T1  = '10000000-0000-4000-a000-000000000001'; // Rino Test Org
const T2  = '10000000-0000-4000-a000-000000000002'; // Free Tier Demo

const U1  = '20000000-0000-4000-a000-000000000001'; // rino admin
const U2  = '20000000-0000-4000-a000-000000000002'; // rino member
const U3  = '20000000-0000-4000-a000-000000000003'; // free admin
const U4  = '20000000-0000-4000-a000-000000000004'; // free member

const AK1 = '30000000-0000-4000-a000-000000000001'; // rino LaoZhang key
const AK2 = '30000000-0000-4000-a000-000000000002'; // free LaoZhang key

const CS1 = '40000000-0000-4000-a000-000000000001'; // rino session 1 (gemini)
const CS2 = '40000000-0000-4000-a000-000000000002'; // rino session 2 (deepseek)
const CS3 = '40000000-0000-4000-a000-000000000003'; // rino session 3 (claude)
const CS4 = '40000000-0000-4000-a000-000000000004'; // free session 1 (gemini)

// chat_messages: 50000000-...-0000000000NN
const cm = (n) => '50000000-0000-4000-a000-' + String(n).padStart(12, '0');

const J1  = '60000000-0000-4000-a000-000000000001'; // oneshot_fix  (done)
const J2  = '60000000-0000-4000-a000-000000000002'; // batch_image  (done)
const J3  = '60000000-0000-4000-a000-000000000003'; // tts          (error)

// usage_logs: 70000000-...-0000000000NN
const ul = (n) => '70000000-0000-4000-a000-' + String(n).padStart(12, '0');

const SUB1 = '80000000-0000-4000-a000-000000000001';
const SUB2 = '80000000-0000-4000-a000-000000000002';

// ═══════════════════════════════════════════════════════════════════════════════
// Seed statements
//   All multi-line / special-char content uses PostgreSQL dollar-quoting
//   ($msg$...$msg$) so JS never needs to escape quotes, newlines, or backticks.
// ═══════════════════════════════════════════════════════════════════════════════

const seeds = [
  // ── 1. tenants ────────────────────────────────────────────────────────────
  {
    label: 'tenants',
    sql: "INSERT INTO tenants (id, name, slug, email, plan, is_active, settings) VALUES "
      + "('" + T1 + "', 'Rino Test Org',  'rino-test', 'billing@rino-test.com', 'pro',  true, "
      + "'{\"feature_mcp\": true, \"feature_batch_images\": true, \"max_sessions\": 200}'), "
      + "('" + T2 + "', 'Free Tier Demo', 'free-demo', 'hello@freedemo.id',     'free', true, "
      + "'{\"feature_mcp\": false}') "
      + "ON CONFLICT (id) DO NOTHING;",
  },

  // ── 2. users ──────────────────────────────────────────────────────────────
  {
    label: 'users',
    sql: "INSERT INTO users (id, tenant_id, email, display_name, role, is_active, last_login_at) VALUES "
      + "('" + U1 + "', '" + T1 + "', 'admin@rino-test.com', 'Rino Admin',   'admin',  true, now() - interval '2 hours'), "
      + "('" + U2 + "', '" + T1 + "', 'anisa@rino-test.com', 'Anisa Putri',  'member', true, now() - interval '1 day'), "
      + "('" + U3 + "', '" + T2 + "', 'admin@freedemo.id',   'Demo Admin',   'admin',  true, now() - interval '6 hours'), "
      + "('" + U4 + "', '" + T2 + "', 'budi@freedemo.id',    'Budi Santoso', 'member', true, now() - interval '3 days') "
      + "ON CONFLICT (id) DO NOTHING;",
  },

  // ── 3. api_keys ───────────────────────────────────────────────────────────
  {
    label: 'api_keys',
    sql: "INSERT INTO api_keys (id, tenant_id, provider, label, key_value_enc, is_active, last_used_at) VALUES "
      + "('" + AK1 + "', '" + T1 + "', 'laozhang', 'Production LaoZhang', "
      + "pgp_sym_encrypt('PLACEHOLDER_KEY', 'seed-encryption-key'), true, now() - interval '30 minutes'), "
      + "('" + AK2 + "', '" + T2 + "', 'laozhang', 'Free tier key', "
      + "pgp_sym_encrypt('PLACEHOLDER_KEY', 'seed-encryption-key'), true, now() - interval '2 days') "
      + "ON CONFLICT (id) DO NOTHING;",
  },

  // ── 4. chat_sessions ─────────────────────────────────────────────────────
  {
    label: 'chat_sessions',
    sql: "INSERT INTO chat_sessions "
      + "(id, tenant_id, user_id, title, model, system_prompt, temperature, max_tokens, use_tools, last_message_at) VALUES "
      + "('" + CS1 + "', '" + T1 + "', '" + U1 + "', 'Artikel AI Industri Kreatif', "
      + "'gemini-2.5-flash', 'Kamu adalah penulis konten kreatif berbahasa Indonesia.', 0.9, 8192, false, "
      + "now() - interval '1 hour'), "
      + "('" + CS2 + "', '" + T1 + "', '" + U2 + "', 'Node.js Retry with Backoff', "
      + "'deepseek-chat', 'You are a senior Node.js engineer. Write clean, production-grade code.', 0.7, 4096, false, "
      + "now() - interval '3 hours'), "
      + "('" + CS3 + "', '" + T1 + "', '" + U1 + "', 'Novel Chapter Review', "
      + "'claude-sonnet-4-6', 'You are a skilled fiction editor specialising in Indonesian literary fiction.', 0.8, 8192, false, "
      + "now() - interval '5 hours'), "
      + "('" + CS4 + "', '" + T2 + "', '" + U3 + "', 'Belajar Machine Learning', "
      + "'gemini-2.5-flash', '', 0.9, 4096, false, "
      + "now() - interval '1 day') "
      + "ON CONFLICT (id) DO NOTHING;",
  },

  // ── 5. chat_messages (20 rows: 5 per session) ────────────────────────────
  //    Dollar-quoting ($msg$...$msg$) lets PostgreSQL handle newlines,
  //    single quotes, and backticks without JS escaping conflicts.
  {
    label: 'chat_messages',
    sql: "INSERT INTO chat_messages "
      + "(id, tenant_id, session_id, role, content, finish_reason, tokens_in, tokens_out, sequence_number, created_at) VALUES "

      // ── Session 1: Gemini — AI article outline (Rino admin) ──────────
      + "('" + cm(1) + "', '" + T1 + "', '" + CS1 + "', 'user', "
      + "$msg$Tolong buatkan outline untuk artikel tentang AI dalam industri kreatif di Indonesia.$msg$, "
      + "NULL, NULL, NULL, 1, now() - interval '65 minutes'), "

      + "('" + cm(2) + "', '" + T1 + "', '" + CS1 + "', 'assistant', "
      + "$msg$Berikut outline artikel tentang AI dalam industri kreatif:\n\n1. Pendahuluan: Era Baru Kreativitas Digital\n2. Lanskap AI di Indonesia\n3. Studi Kasus: Musik, Film, dan Desain\n4. Tantangan Etis dan Hak Cipta\n5. Masa Depan: Kolaborasi Manusia-AI\n6. Kesimpulan$msg$, "
      + "'stop', 245, 380, 2, now() - interval '64 minutes'), "

      + "('" + cm(3) + "', '" + T1 + "', '" + CS1 + "', 'user', "
      + "$msg$Bagus! Sekarang kembangkan bagian pertama menjadi 2 paragraf.$msg$, "
      + "NULL, NULL, NULL, 3, now() - interval '62 minutes'), "

      + "('" + cm(4) + "', '" + T1 + "', '" + CS1 + "', 'assistant', "
      + "$msg$Pendahuluan: Era Baru Kreativitas Digital\n\nDalam dekade terakhir, kecerdasan buatan telah mengubah cara manusia berkreasi. Dari pembuatan musik otomatis hingga desain grafis berbasis prompt, AI kini menjadi mitra kreatif yang semakin tak terpisahkan dari proses produksi konten.\n\nDi Indonesia, tren ini berkembang pesat seiring dengan meningkatnya akses terhadap platform AI generatif. Para kreator lokal mulai memanfaatkan teknologi ini untuk mempercepat alur kerja, menghasilkan variasi desain, dan bahkan menulis naskah awal untuk film pendek.$msg$, "
      + "'stop', 412, 620, 4, now() - interval '61 minutes'), "

      + "('" + cm(5) + "', '" + T1 + "', '" + CS1 + "', 'user', "
      + "$msg$Tambahkan statistik terbaru tentang adopsi AI di Asia Tenggara.$msg$, "
      + "NULL, NULL, NULL, 5, now() - interval '60 minutes'), "

      // ── Session 2: DeepSeek — retry mechanism (Anisa) ────────────────
      + "('" + cm(6) + "', '" + T1 + "', '" + CS2 + "', 'user', "
      + "$msg$How do I implement a retry mechanism with exponential backoff in Node.js?$msg$, "
      + "NULL, NULL, NULL, 1, now() - interval '3 hours 30 minutes'), "

      + "('" + cm(7) + "', '" + T1 + "', '" + CS2 + "', 'assistant', "
      + "$msg$Here's a clean implementation:\n\n```js\nasync function withRetry(fn, maxRetries = 3) {\n  for (let i = 0; i <= maxRetries; i++) {\n    try { return await fn(); }\n    catch (err) {\n      if (i === maxRetries) throw err;\n      await new Promise(r => setTimeout(r, 1000 * Math.pow(2, i)));\n    }\n  }\n}\n```\n\nThis doubles the wait on each retry: 1s, 2s, 4s.$msg$, "
      + "'stop', 320, 480, 2, now() - interval '3 hours 28 minutes'), "

      + "('" + cm(8) + "', '" + T1 + "', '" + CS2 + "', 'user', "
      + "$msg$Can you add jitter to prevent the thundering herd problem?$msg$, "
      + "NULL, NULL, NULL, 3, now() - interval '3 hours 25 minutes'), "

      + "('" + cm(9) + "', '" + T1 + "', '" + CS2 + "', 'assistant', "
      + "$msg$Good thinking! Adding jitter distributes retry attempts across time:\n\n```js\nconst jitter = Math.random() * 500;\nconst delay = 1000 * Math.pow(2, attempt) + jitter;\n```\n\nFull-jitter (randomising the entire delay window) tends to outperform equal-jitter in high-concurrency scenarios. Here's the updated version with configurable options.$msg$, "
      + "'stop', 485, 720, 4, now() - interval '3 hours 22 minutes'), "

      + "('" + cm(10) + "', '" + T1 + "', '" + CS2 + "', 'user', "
      + "$msg$Now wrap it as an npm-publishable module with TypeScript types.$msg$, "
      + "NULL, NULL, NULL, 5, now() - interval '3 hours 20 minutes'), "

      // ── Session 3: Claude — novel review (Rino admin) ────────────────
      + "('" + cm(11) + "', '" + T1 + "', '" + CS3 + "', 'user', "
      + "$msg$Review this paragraph from my novel: \"Hujan turun deras malam itu. Rina berlari ke warung terdekat. Dia basah kuyup. Dia memesan kopi.\"$msg$, "
      + "NULL, NULL, NULL, 1, now() - interval '5 hours 15 minutes'), "

      + "('" + cm(12) + "', '" + T1 + "', '" + CS3 + "', 'assistant', "
      + "$msg$The paragraph has clear imagery but reads as a list of short declarative sentences. Consider varying sentence length and weaving in sensory detail:\n\n\"Hujan menerpa malam itu tanpa aba-aba — dentuman pertama menghantam atap seng warung Pak Darmo saat Rina masih setengah berlari di trotoar. Begitu ia menyelinap masuk, rambutnya sudah melekat di kening, dan aroma kopi tubruk dari balik etalase kaca menariknya seperti janji yang tak perlu diucapkan.\"\n\nThis version connects the actions with cause-and-effect and engages smell alongside sight.$msg$, "
      + "'stop', 380, 540, 2, now() - interval '5 hours 12 minutes'), "

      + "('" + cm(13) + "', '" + T1 + "', '" + CS3 + "', 'user', "
      + "$msg$I like the atmosphere. Adjust the tone to be a bit more melancholic — she just lost someone.$msg$, "
      + "NULL, NULL, NULL, 3, now() - interval '5 hours 8 minutes'), "

      + "('" + cm(14) + "', '" + T1 + "', '" + CS3 + "', 'assistant', "
      + "$msg$Here's the revised version with a heavier emotional undertone:\n\n\"Hujan yang menerpa malam itu terasa seperti kelanjutan dari sesuatu yang sudah lama pecah di dalam dirinya. Rina tidak berlari — kakinya hanya bergerak, membawanya ke warung terdekat tanpa niat yang jelas. Air mengalir dari ujung rambutnya ke lantai keramik yang retak, dan ia duduk di kursi plastik yang biasa diduduki mereka berdua. Kopi yang datang terlalu panas untuk diminum, tapi ia menggenggam cangkirnya erat-erat — karena malam ini, kehangatan hanya bisa datang dari benda mati.\"$msg$, "
      + "'stop', 510, 680, 4, now() - interval '5 hours 5 minutes'), "

      + "('" + cm(15) + "', '" + T1 + "', '" + CS3 + "', 'user', "
      + "$msg$Perfect. Now write a two-sentence transition into the next chapter where morning arrives.$msg$, "
      + "NULL, NULL, NULL, 5, now() - interval '5 hours'), "

      // ── Session 4: Gemini — ML basics (Free tier) ────────────────────
      + "('" + cm(16) + "', '" + T2 + "', '" + CS4 + "', 'user', "
      + "$msg$Apa itu machine learning? Jelaskan dengan sederhana.$msg$, "
      + "NULL, NULL, NULL, 1, now() - interval '1 day 2 hours'), "

      + "('" + cm(17) + "', '" + T2 + "', '" + CS4 + "', 'assistant', "
      + "$msg$Machine learning adalah cabang dari kecerdasan buatan di mana komputer belajar dari data tanpa diprogram secara eksplisit. Bayangkan seperti mengajari anak kecil mengenali kucing: kamu tidak menjelaskan semua aturan, tapi menunjukkan banyak foto kucing sampai dia bisa mengenali sendiri.\n\nKomputer melakukan hal serupa — diberi ribuan contoh, lalu menemukan pola untuk membuat prediksi pada data baru.$msg$, "
      + "'stop', 180, 310, 2, now() - interval '1 day 1 hour 58 minutes'), "

      + "('" + cm(18) + "', '" + T2 + "', '" + CS4 + "', 'user', "
      + "$msg$Contoh penggunaannya sehari-hari?$msg$, "
      + "NULL, NULL, NULL, 3, now() - interval '1 day 1 hour 55 minutes'), "

      + "('" + cm(19) + "', '" + T2 + "', '" + CS4 + "', 'assistant', "
      + "$msg$Beberapa contoh ML yang kamu pakai setiap hari:\n\n- Rekomendasi video di YouTube dan TikTok\n- Filter spam di Gmail\n- Autocomplete saat mengetik di WhatsApp\n- Face unlock di smartphone\n- Deteksi penipuan di transaksi bank\n- Google Translate\n\nSemua ini menggunakan model yang dilatih dari jutaan contoh data.$msg$, "
      + "'stop', 220, 285, 4, now() - interval '1 day 1 hour 52 minutes'), "

      + "('" + cm(20) + "', '" + T2 + "', '" + CS4 + "', 'user', "
      + "$msg$Terima kasih! Sangat membantu.$msg$, "
      + "NULL, NULL, NULL, 5, now() - interval '1 day 1 hour 50 minutes') "

      + "ON CONFLICT (id) DO NOTHING;",
  },

  // ── 6. jobs (3 rows — Rino Test Org) ──────────────────────────────────────
  {
    label: 'jobs',
    sql: "INSERT INTO jobs "
      + "(id, tenant_id, user_id, job_type, status, model, input_payload, "
      + "progress_current, progress_total, progress_message, logs, "
      + "result_payload, error_message, external_job_id, output_prefix, "
      + "started_at, completed_at, created_at) VALUES "

      // Completed oneshot_fix
      + "('" + J1 + "', '" + T1 + "', '" + U1 + "', 'oneshot_fix', 'done', "
      + "'gemini-2.5-pro', "
      + "'{\"transcriptBody\": \"Bab 1: Awal Mula...\", \"language\": \"id\"}'::jsonb, "
      + "1, 1, 'Manuskrip berhasil diperbaiki', "
      + "'[\"Membaca manuskrip…\", \"Memperbaiki tata bahasa…\", \"Selesai — 47 koreksi diterapkan\"]'::jsonb, "
      + "'{\"fixed_book\": \"Bab 1: Awal Mula (corrected)...\", \"corrections\": 47}'::jsonb, "
      + "NULL, 'gemini-batch-abc123', 'narasi_fix_20250601', "
      + "now() - interval '2 days', now() - interval '2 days' + interval '45 seconds', "
      + "now() - interval '2 days'), "

      // Completed batch_image
      + "('" + J2 + "', '" + T1 + "', '" + U2 + "', 'batch_image', 'done', "
      + "'imagen-3.0-generate-002', "
      + "'{\"prompts\": [\"Sunset over Jakarta skyline, cinematic\", \"Traditional Javanese dancer, watercolor\", \"Futuristic warung, cyberpunk\"], \"aspectRatio\": \"16:9\"}'::jsonb, "
      + "3, 3, 'Semua gambar selesai digenerate', "
      + "'[\"Memulai batch…\", \"Gambar 1/3 selesai\", \"Gambar 2/3 selesai\", \"Gambar 3/3 selesai\"]'::jsonb, "
      + "'{\"files\": [{\"file\": \"jakarta_sunset.jpg\", \"size\": 524288}, {\"file\": \"javanese_dancer.jpg\", \"size\": 491520}, {\"file\": \"cyber_warung.jpg\", \"size\": 610304}]}'::jsonb, "
      + "NULL, NULL, 'batch_img_20250602', "
      + "now() - interval '1 day', now() - interval '1 day' + interval '90 seconds', "
      + "now() - interval '1 day'), "

      // Failed TTS
      + "('" + J3 + "', '" + T1 + "', '" + U1 + "', 'tts', 'error', "
      + "'tts-1-hd', "
      + "'{\"text\": \"Selamat datang di Rino Creative Studio...\", \"voice\": \"nova\", \"speed\": 1.0}'::jsonb, "
      + "0, 1, 'Gagal: upstream API timeout', "
      + "'[\"Memulai TTS…\", \"Mengirim ke upstream API…\", \"ERROR: Request timeout setelah 30 detik\"]'::jsonb, "
      + "NULL, "
      + "'Upstream API timeout after 30000ms', "
      + "NULL, 'tts_20250603', "
      + "now() - interval '6 hours', NULL, "
      + "now() - interval '6 hours') "

      + "ON CONFLICT (id) DO NOTHING;",
  },

  // ── 7. usage_logs (5 rows) ────────────────────────────────────────────────
  {
    label: 'usage_logs',
    sql: "INSERT INTO usage_logs "
      + "(id, tenant_id, user_id, session_id, job_id, "
      + "endpoint, model_alias, model_upstream, provider, "
      + "tokens_in, tokens_out, cost_usd, "
      + "finish_reason, latency_ms, http_status, created_at) VALUES "

      // Gemini flash — session 1, msg 2
      + "('" + ul(1) + "', '" + T1 + "', '" + U1 + "', '" + CS1 + "', NULL, "
      + "'chat', 'gemini-2.5-flash', 'gemini-2.5-flash-preview-04-17', 'laozhang', "
      + "245, 380, 0.00009375, 'stop', 1820, 200, now() - interval '64 minutes'), "

      // Gemini flash — session 1, msg 4
      + "('" + ul(2) + "', '" + T1 + "', '" + U1 + "', '" + CS1 + "', NULL, "
      + "'chat', 'gemini-2.5-flash', 'gemini-2.5-flash-preview-04-17', 'laozhang', "
      + "412, 620, 0.00015450, 'stop', 2340, 200, now() - interval '61 minutes'), "

      // DeepSeek — session 2, msg 4
      + "('" + ul(3) + "', '" + T1 + "', '" + U2 + "', '" + CS2 + "', NULL, "
      + "'chat', 'deepseek-chat', 'deepseek-chat', 'deepseek', "
      + "485, 720, 0.00036200, 'stop', 3150, 200, now() - interval '3 hours 22 minutes'), "

      // Claude — session 3, msg 4
      + "('" + ul(4) + "', '" + T1 + "', '" + U1 + "', '" + CS3 + "', NULL, "
      + "'chat', 'claude-sonnet-4-6', 'claude-sonnet-4-6-20250514', 'laozhang', "
      + "510, 680, 0.00507000, 'stop', 4200, 200, now() - interval '5 hours 5 minutes'), "

      // Gemini flash — session 4, msg 4 (free tenant)
      + "('" + ul(5) + "', '" + T2 + "', '" + U3 + "', '" + CS4 + "', NULL, "
      + "'chat', 'gemini-2.5-flash', 'gemini-2.5-flash-preview-04-17', 'laozhang', "
      + "220, 285, 0.00007575, 'stop', 1450, 200, now() - interval '1 day 1 hour 52 minutes') "

      + "ON CONFLICT (id) DO NOTHING;",
  },

  // ── 8. subscriptions (2 rows) ─────────────────────────────────────────────
  {
    label: 'subscriptions',
    sql: "INSERT INTO subscriptions "
      + "(id, tenant_id, "
      + "stripe_customer_id, stripe_subscription_id, stripe_price_id, stripe_product_id, "
      + "plan, status, "
      + "current_period_start, current_period_end, "
      + "monthly_token_limit, monthly_job_limit, seats) VALUES "
      + "('" + SUB1 + "', '" + T1 + "', "
      + "'cus_test_rino_001', 'sub_test_rino_001', 'price_test_pro_monthly', 'prod_test_pro', "
      + "'pro', 'active', "
      + "date_trunc('month', now()), date_trunc('month', now()) + interval '1 month', "
      + "50000000, 500, 5), "
      + "('" + SUB2 + "', '" + T2 + "', "
      + "'cus_test_free_001', 'sub_test_free_001', 'price_test_free', 'prod_test_free', "
      + "'free', 'active', "
      + "date_trunc('month', now()), date_trunc('month', now()) + interval '1 month', "
      + "1000000, 20, 1) "
      + "ON CONFLICT (id) DO NOTHING;",
  },
];

// ═══════════════════════════════════════════════════════════════════════════════
// Runner
// ═══════════════════════════════════════════════════════════════════════════════

async function main() {
  const client = new Client({ connectionString: DATABASE_URL });

  try {
    await client.connect();
    const branch =
      process.env.NODE_ENV === 'staging'    ? 'staging' :
      process.env.NODE_ENV === 'production' ? 'main (production)' :
                                              'develop';
    console.log('✔  Connected to Neon branch: ' + branch + '\n');

    const summary = [];

    await client.query('BEGIN');

    for (const { label, sql } of seeds) {
      const result = await client.query(sql);
      const count = result.rowCount || 0;
      summary.push({ table: label, inserted: count });
    }

    await client.query('COMMIT');

    // ── Summary ───────────────────────────────────────────────────────────
    console.log('   Table              Inserted');
    console.log('   ─────────────────  ────────');
    let total = 0;
    for (const { table, inserted } of summary) {
      console.log('   ' + table.padEnd(18) + ' ' + inserted);
      total += inserted;
    }
    console.log('   ' + '─'.repeat(18) + ' ' + '─'.repeat(8));
    console.log('   ' + 'TOTAL'.padEnd(18) + ' ' + total);

    if (total === 0) {
      console.log('\n✔  All seed data already exists. Nothing new inserted.\n');
    } else {
      console.log('\n✔  Seeded ' + total + ' rows successfully.\n');
    }
  } catch (err) {
    try { await client.query('ROLLBACK'); } catch (_) {}
    console.error('✖  Seed failed: ' + err.message);
    if (err.detail) console.error('   Detail: ' + err.detail);
    if (err.hint) console.error('   Hint: ' + err.hint);
    process.exit(1);
  } finally {
    await client.end();
  }
}

main();

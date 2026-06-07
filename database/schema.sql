-- =============================================================================
--  Rino Creative Studio — Multi-Tenant PostgreSQL Schema  (LIVE SNAPSHOT)
--  Generated from the live Neon database via pg_dump --schema-only.
--  PostgreSQL 15+  |  UUID primary keys  |  Row-Level Security (FORCE) enabled
-- =============================================================================
--
--  Tables (13):
--     1.  tenants           – one row per organisation / individual customer
--     2.  users             – one row per user, belongs to a tenant
--     3.  api_keys          – encrypted upstream API keys per tenant
--     4.  chat_sessions     – chat sessions (replaces in-memory sessions dict)
--     5.  chat_messages     – individual messages inside sessions
--     6.  jobs              – unified async jobs (oneshot_fix, batch_image, tts,
--                             imagen, veo, sora, narasi) — replaces *.json + Maps
--     7.  usage_logs        – every LLM API call with tokens + cost per tenant
--     8.  assets            – S3/R2-backed output file references
--     9.  subscriptions     – Stripe subscription state per tenant
--    10.  correction_pairs  – MOAT: original→corrected edit pairs (training data)
--    11.  moat_sessions     – MOAT: narration generation sessions + RAG context
--    12.  narasi_outlines   – MOAT: generated outlines (training data capture)
--    13.  migrations        – applied-migration ledger (used by migrate.js)
--
--  Enums:
--    job_status_enum  : queued · processing · running · cancelling · cancelled · done · error
--    job_type_enum    : oneshot_fix · batch_image · tts · imagen · veo · sora · narasi
--
--  Functions:
--    provision_tenant(...)       – idempotent tenant+user+subscription bootstrap (SECURITY DEFINER)
--    job_tenant(uuid)            – RLS-safe tenant lookup for a job id      (SECURITY DEFINER)
--    set_current_tenant_id(uuid) – sets app.current_tenant_id for the txn (activates RLS)
--    set_updated_at()            – BEFORE UPDATE trigger to bump updated_at
--
--  NOTE: All RLS tables use FORCE ROW LEVEL SECURITY (applies even to table owner).
--        Policies key off current_setting('app.current_tenant_id').
-- =============================================================================

BEGIN;

-- pg_dump emits functions before the tables they reference; this lets the file
-- run top-to-bottom on an empty database without "relation does not exist".
SET check_function_bodies = false;

-- ---------------------------------------------------------------------------
-- 0.  Prerequisites
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid(), pgp_sym_encrypt
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- fast LIKE / ILIKE on text columns


CREATE TYPE public.job_status_enum AS ENUM (
    'queued',
    'processing',
    'running',
    'cancelling',
    'cancelled',
    'done',
    'error'
);


CREATE TYPE public.job_type_enum AS ENUM (
    'oneshot_fix',
    'batch_image',
    'tts',
    'imagen',
    'veo',
    'sora',
    'narasi'
);


CREATE FUNCTION public.job_tenant(p_job_id uuid) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  SELECT tenant_id FROM jobs WHERE id = p_job_id;
$$;


CREATE FUNCTION public.provision_tenant(p_tenant_id uuid, p_name text, p_slug text, p_email text, p_plan text, p_clerk_user text, p_role text DEFAULT 'admin'::text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE v_tenant uuid;
BEGIN
  INSERT INTO tenants (id, name, slug, email, plan)
       VALUES (COALESCE(p_tenant_id, gen_random_uuid()), p_name, p_slug, p_email, p_plan)
  ON CONFLICT (id) DO UPDATE SET updated_at = now()
  RETURNING id INTO v_tenant;

  IF v_tenant IS NULL THEN
    SELECT id INTO v_tenant FROM tenants WHERE email = p_email LIMIT 1;
  END IF;

  INSERT INTO users (tenant_id, email, display_name, external_id, role)
       VALUES (v_tenant, p_email, p_name, p_clerk_user, p_role)
  ON CONFLICT (tenant_id, email) DO UPDATE SET external_id = EXCLUDED.external_id;

  INSERT INTO subscriptions (
         tenant_id, stripe_customer_id, stripe_subscription_id,
         stripe_price_id, stripe_product_id, plan, status,
         current_period_start, current_period_end)
       VALUES (
         v_tenant, 'cus_free_' || p_clerk_user, 'sub_free_' || p_clerk_user,
         'price_free', 'prod_free', p_plan, 'active',
         now(), now() + interval '1 year')
  ON CONFLICT (stripe_subscription_id) DO NOTHING;

  RETURN v_tenant;
END $$;


CREATE FUNCTION public.set_current_tenant_id(p_tenant_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM set_config('app.current_tenant_id', p_tenant_id::TEXT, TRUE);
END;
$$;


COMMENT ON FUNCTION public.set_current_tenant_id(p_tenant_id uuid) IS 'Set app.current_tenant_id for current transaction; activates RLS policies.';


CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


CREATE TABLE public.api_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    provider text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    key_value_enc bytea NOT NULL,
    key_hint text GENERATED ALWAYS AS ('***'::text) STORED,
    is_active boolean DEFAULT true NOT NULL,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT api_keys_provider_check CHECK ((provider = ANY (ARRAY['laozhang'::text, 'laozhang_image'::text, 'deepseek'::text, 'gemini'::text, 'openai'::text, 'other'::text])))
);

ALTER TABLE ONLY public.api_keys FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.api_keys IS 'Encrypted upstream API credentials per tenant. Replaces shared env vars.';


COMMENT ON COLUMN public.api_keys.key_value_enc IS 'pgp_sym_encrypt(raw_key, app_secret) — never store plaintext.';


COMMENT ON COLUMN public.api_keys.key_hint IS 'Last-4-character hint shown in UI; override at INSERT.';


CREATE TABLE public.assets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid,
    job_id uuid,
    bucket text NOT NULL,
    s3_key text NOT NULL,
    original_filename text,
    content_type text NOT NULL,
    size_bytes bigint DEFAULT 0 NOT NULL,
    asset_type text NOT NULL,
    source_job_type public.job_type_enum,
    signed_url text,
    signed_url_expires_at timestamp with time zone,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT assets_asset_type_check CHECK ((asset_type = ANY (ARRAY['video'::text, 'audio'::text, 'image'::text, 'document'::text, 'archive'::text, 'other'::text])))
);

ALTER TABLE ONLY public.assets FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.assets IS 'File references in object storage, replacing all Docker volume paths (Veo, Sora, TTS, Imagen, batch).';


COMMENT ON COLUMN public.assets.s3_key IS 'Full object key within the bucket, e.g. "tenants/{tid}/jobs/{jid}/output_001.wav".';


COMMENT ON COLUMN public.assets.signed_url IS 'Cached pre-signed URL; regenerate when signed_url_expires_at < now().';


COMMENT ON COLUMN public.assets.metadata IS 'Free-form JSONB: video duration, image width/height, TTS voice, sample rate, etc.';


CREATE TABLE public.chat_messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    session_id uuid NOT NULL,
    role text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    tool_calls jsonb,
    tool_results jsonb,
    finish_reason text,
    tokens_in integer,
    tokens_out integer,
    sequence_number integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chat_messages_role_check CHECK ((role = ANY (ARRAY['system'::text, 'user'::text, 'assistant'::text, 'tool'::text])))
);

ALTER TABLE ONLY public.chat_messages FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.chat_messages IS 'Individual message turns inside a chat session. Replaces history arrays in Conversation objects.';


COMMENT ON COLUMN public.chat_messages.tool_calls IS 'JSONB array of tool_use blocks emitted by the model (MCP).';


COMMENT ON COLUMN public.chat_messages.sequence_number IS '1-based monotonic counter within the session for stable ordering.';


CREATE TABLE public.chat_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid,
    title text,
    model text NOT NULL,
    system_prompt text DEFAULT ''::text NOT NULL,
    temperature numeric(4,3) DEFAULT 0.9 NOT NULL,
    max_tokens integer DEFAULT 8192 NOT NULL,
    use_tools boolean DEFAULT false NOT NULL,
    mcp_paths text,
    is_archived boolean DEFAULT false NOT NULL,
    last_message_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chat_sessions_temperature_check CHECK (((temperature >= (0)::numeric) AND (temperature <= (2)::numeric)))
);

ALTER TABLE ONLY public.chat_sessions FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.chat_sessions IS 'Persistent chat sessions. Replaces the in-memory sessions dict in laozhang_api.py.';


COMMENT ON COLUMN public.chat_sessions.mcp_paths IS 'Comma-separated folder paths forwarded to the MCP file-search sidecar.';


COMMENT ON COLUMN public.chat_sessions.last_message_at IS 'Denormalised timestamp of the most recent message for efficient sorting.';


CREATE TABLE public.correction_pairs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    moat_session_id uuid,
    tenant_id uuid NOT NULL,
    user_id uuid,
    original_text text,
    corrected_text text,
    edit_distance integer,
    edit_ratio numeric(6,4),
    quality_tier text,
    style_label text,
    topic text,
    duration_minutes integer,
    language text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.correction_pairs FORCE ROW LEVEL SECURITY;


CREATE TABLE public.jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid,
    job_type public.job_type_enum NOT NULL,
    status public.job_status_enum DEFAULT 'queued'::public.job_status_enum NOT NULL,
    model text,
    input_payload jsonb,
    progress_current integer DEFAULT 0 NOT NULL,
    progress_total integer DEFAULT 0 NOT NULL,
    progress_message text,
    logs jsonb DEFAULT '[]'::jsonb NOT NULL,
    result_payload jsonb,
    error_message text,
    external_job_id text,
    output_prefix text,
    session_id uuid,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.jobs FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.jobs IS 'Unified job table replacing _oneshot_jobs dict, jobs.json, tts-jobs.json, imagen-jobs.json, and the activeJobs Map.';


COMMENT ON COLUMN public.jobs.input_payload IS 'Serialised request parameters: prompts[], voice, speed, transcriptBody, model, aspectRatio, etc.';


COMMENT ON COLUMN public.jobs.logs IS 'Ordered array of human-readable log strings mirroring job.logs[] in server.js.';


COMMENT ON COLUMN public.jobs.result_payload IS 'Serialised output: fixed_book text, array of {file, url} objects, destFile path, etc.';


COMMENT ON COLUMN public.jobs.external_job_id IS 'Provider-assigned identifier: Gemini batch name, Veo task_id, Sora job id.';


CREATE TABLE public.migrations (
    id integer NOT NULL,
    filename text NOT NULL,
    checksum text,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    duration_ms integer
);


COMMENT ON TABLE public.migrations IS 'Records every migration file that has been applied. Used by migrate.js to skip already-run files.';


COMMENT ON COLUMN public.migrations.checksum IS 'MD5 hash of the .sql file contents at the time it was applied, for drift detection.';


CREATE SEQUENCE public.migrations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.migrations_id_seq OWNED BY public.migrations.id;


CREATE TABLE public.moat_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid,
    topic text,
    style text,
    rag_used boolean DEFAULT false,
    sources jsonb,
    passages jsonb,
    prompt_used text,
    generated_narration text,
    model text,
    tokens_in integer DEFAULT 0,
    tokens_out integer DEFAULT 0,
    cost_usd numeric(12,8) DEFAULT 0,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.moat_sessions FORCE ROW LEVEL SECURITY;


CREATE TABLE public.narasi_outlines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid,
    topic text,
    style text,
    language text,
    chap_count integer,
    outline_text text,
    chapters jsonb,
    model text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


CREATE TABLE public.subscriptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    stripe_customer_id text NOT NULL,
    stripe_subscription_id text NOT NULL,
    stripe_price_id text NOT NULL,
    stripe_product_id text NOT NULL,
    plan text NOT NULL,
    status text NOT NULL,
    current_period_start timestamp with time zone NOT NULL,
    current_period_end timestamp with time zone NOT NULL,
    trial_start timestamp with time zone,
    trial_end timestamp with time zone,
    cancel_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    ended_at timestamp with time zone,
    monthly_token_limit bigint,
    monthly_job_limit integer,
    seats smallint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT subscriptions_plan_check CHECK ((plan = ANY (ARRAY['free'::text, 'starter'::text, 'pro'::text, 'enterprise'::text]))),
    CONSTRAINT subscriptions_status_check CHECK ((status = ANY (ARRAY['trialing'::text, 'active'::text, 'past_due'::text, 'unpaid'::text, 'cancelled'::text, 'incomplete'::text, 'incomplete_expired'::text, 'paused'::text])))
);

ALTER TABLE ONLY public.subscriptions FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.subscriptions IS 'Mirrors Stripe subscription state per tenant. One active row at a time; history is retained.';


COMMENT ON COLUMN public.subscriptions.stripe_subscription_id IS 'Stripe sub_... identifier; used for webhook reconciliation.';


COMMENT ON COLUMN public.subscriptions.monthly_token_limit IS 'Denormalised from Stripe metadata. NULL = plan has no token cap.';


CREATE TABLE public.tenants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    email text NOT NULL,
    plan text DEFAULT 'free'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tenants_plan_check CHECK ((plan = ANY (ARRAY['free'::text, 'starter'::text, 'pro'::text, 'enterprise'::text])))
);

ALTER TABLE ONLY public.tenants FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.tenants IS 'One row per organisation or individual customer.';


COMMENT ON COLUMN public.tenants.slug IS 'URL-safe short name, used as subdomain prefix.';


COMMENT ON COLUMN public.tenants.settings IS 'Per-tenant feature flags and UI preferences (free-form JSONB).';


CREATE TABLE public.usage_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    user_id uuid,
    session_id uuid,
    job_id uuid,
    endpoint text NOT NULL,
    model_alias text NOT NULL,
    model_upstream text NOT NULL,
    provider text NOT NULL,
    tokens_in integer DEFAULT 0 NOT NULL,
    tokens_out integer DEFAULT 0 NOT NULL,
    cost_usd numeric(12,8) DEFAULT 0 NOT NULL,
    finish_reason text,
    latency_ms integer,
    http_status smallint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_logs_endpoint_check CHECK ((endpoint = ANY (ARRAY['chat'::text, 'image'::text, 'tts'::text, 'video'::text, 'embedding'::text, 'batch'::text, 'narasi'::text, 'other'::text]))),
    CONSTRAINT usage_logs_provider_check CHECK ((provider = ANY (ARRAY['laozhang'::text, 'deepseek'::text, 'gemini'::text, 'openai'::text, 'other'::text])))
);

ALTER TABLE ONLY public.usage_logs FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.usage_logs IS 'One row per upstream LLM/image/TTS/video API call. Captures tokens and cost for billing dashboards.';


COMMENT ON COLUMN public.usage_logs.model_alias IS 'User-facing model alias (e.g. "gemini-2.5-pro") before MODELS dict resolution.';


COMMENT ON COLUMN public.usage_logs.model_upstream IS 'Resolved upstream model string actually sent to the provider.';


COMMENT ON COLUMN public.usage_logs.cost_usd IS 'Computed by the application layer using per-model rate tables; 8 decimal places for sub-cent precision.';


CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    email text NOT NULL,
    display_name text,
    password_hash text,
    external_id text,
    role text DEFAULT 'member'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT users_role_check CHECK ((role = ANY (ARRAY['owner'::text, 'admin'::text, 'member'::text, 'viewer'::text])))
);

ALTER TABLE ONLY public.users FORCE ROW LEVEL SECURITY;


COMMENT ON TABLE public.users IS 'One row per human user, always scoped to a tenant.';


COMMENT ON COLUMN public.users.password_hash IS 'bcrypt/argon2 hash; NULL for SSO-only accounts.';


COMMENT ON COLUMN public.users.external_id IS 'OAuth sub-claim or SAML nameID for federated identity.';


ALTER TABLE ONLY public.migrations ALTER COLUMN id SET DEFAULT nextval('public.migrations_id_seq'::regclass);


ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_bucket_s3_key_key UNIQUE (bucket, s3_key);


ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.correction_pairs
    ADD CONSTRAINT correction_pairs_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.migrations
    ADD CONSTRAINT migrations_filename_key UNIQUE (filename);


ALTER TABLE ONLY public.migrations
    ADD CONSTRAINT migrations_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.moat_sessions
    ADD CONSTRAINT moat_sessions_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.narasi_outlines
    ADD CONSTRAINT narasi_outlines_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_stripe_subscription_id_key UNIQUE (stripe_subscription_id);


ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_email_key UNIQUE (email);


ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_slug_key UNIQUE (slug);


ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_email_key UNIQUE (tenant_id, email);


CREATE INDEX idx_api_keys_provider ON public.api_keys USING btree (tenant_id, provider, is_active);


CREATE INDEX idx_api_keys_tenant_id ON public.api_keys USING btree (tenant_id);


CREATE INDEX idx_assets_asset_type ON public.assets USING btree (tenant_id, asset_type);


CREATE INDEX idx_assets_created_at ON public.assets USING btree (tenant_id, created_at DESC);


CREATE INDEX idx_assets_job_id ON public.assets USING btree (job_id) WHERE (job_id IS NOT NULL);


CREATE INDEX idx_assets_signed_url_expiry ON public.assets USING btree (signed_url_expires_at) WHERE (signed_url IS NOT NULL);


CREATE INDEX idx_assets_source_job_type ON public.assets USING btree (tenant_id, source_job_type);


CREATE INDEX idx_assets_tenant_id ON public.assets USING btree (tenant_id);


CREATE INDEX idx_assets_user_id ON public.assets USING btree (user_id);


CREATE INDEX idx_chat_messages_created_at ON public.chat_messages USING btree (session_id, created_at);


CREATE INDEX idx_chat_messages_session_id ON public.chat_messages USING btree (session_id, sequence_number);


CREATE INDEX idx_chat_messages_tenant_id ON public.chat_messages USING btree (tenant_id);


CREATE INDEX idx_chat_sessions_is_archived ON public.chat_sessions USING btree (tenant_id, is_archived);


CREATE INDEX idx_chat_sessions_last_message_at ON public.chat_sessions USING btree (tenant_id, last_message_at DESC);


CREATE INDEX idx_chat_sessions_tenant_id ON public.chat_sessions USING btree (tenant_id);


CREATE INDEX idx_chat_sessions_user_id ON public.chat_sessions USING btree (user_id);


CREATE INDEX idx_corr_created ON public.correction_pairs USING btree (created_at);


CREATE INDEX idx_corr_quality ON public.correction_pairs USING btree (quality_tier);


CREATE INDEX idx_corr_style ON public.correction_pairs USING btree (style_label);


CREATE INDEX idx_corr_tenant ON public.correction_pairs USING btree (tenant_id);


CREATE INDEX idx_jobs_created_at ON public.jobs USING btree (tenant_id, created_at DESC);


CREATE INDEX idx_jobs_external_job_id ON public.jobs USING btree (external_job_id) WHERE (external_job_id IS NOT NULL);


CREATE INDEX idx_jobs_job_type ON public.jobs USING btree (tenant_id, job_type);


CREATE INDEX idx_jobs_session_id ON public.jobs USING btree (session_id) WHERE (session_id IS NOT NULL);


CREATE INDEX idx_jobs_status ON public.jobs USING btree (tenant_id, status);


CREATE INDEX idx_jobs_tenant_id ON public.jobs USING btree (tenant_id);


CREATE INDEX idx_jobs_user_id ON public.jobs USING btree (user_id);


CREATE INDEX idx_migrations_filename ON public.migrations USING btree (filename);


CREATE INDEX idx_moat_sessions_created ON public.moat_sessions USING btree (created_at);


CREATE INDEX idx_moat_sessions_tenant ON public.moat_sessions USING btree (tenant_id);


CREATE INDEX idx_narasi_outlines_tenant ON public.narasi_outlines USING btree (tenant_id, created_at DESC);


CREATE INDEX idx_subscriptions_current_period_end ON public.subscriptions USING btree (current_period_end) WHERE (status = ANY (ARRAY['active'::text, 'trialing'::text]));


CREATE INDEX idx_subscriptions_status ON public.subscriptions USING btree (status);


CREATE INDEX idx_subscriptions_stripe_customer_id ON public.subscriptions USING btree (stripe_customer_id);


CREATE INDEX idx_subscriptions_stripe_subscription_id ON public.subscriptions USING btree (stripe_subscription_id);


CREATE INDEX idx_subscriptions_tenant_id ON public.subscriptions USING btree (tenant_id);


CREATE INDEX idx_tenants_email ON public.tenants USING btree (email);


CREATE INDEX idx_tenants_is_active ON public.tenants USING btree (is_active);


CREATE INDEX idx_tenants_slug ON public.tenants USING btree (slug);


CREATE INDEX idx_usage_logs_created_at ON public.usage_logs USING btree (tenant_id, created_at DESC);


CREATE INDEX idx_usage_logs_endpoint ON public.usage_logs USING btree (tenant_id, endpoint, created_at DESC);


CREATE INDEX idx_usage_logs_job_id ON public.usage_logs USING btree (job_id) WHERE (job_id IS NOT NULL);


CREATE INDEX idx_usage_logs_model ON public.usage_logs USING btree (tenant_id, model_upstream);


CREATE INDEX idx_usage_logs_session_id ON public.usage_logs USING btree (session_id) WHERE (session_id IS NOT NULL);


CREATE INDEX idx_usage_logs_tenant_id ON public.usage_logs USING btree (tenant_id);


CREATE INDEX idx_usage_logs_user_id ON public.usage_logs USING btree (user_id);


CREATE INDEX idx_users_email ON public.users USING btree (email);


CREATE INDEX idx_users_external_id ON public.users USING btree (external_id) WHERE (external_id IS NOT NULL);


CREATE INDEX idx_users_tenant_id ON public.users USING btree (tenant_id);


CREATE TRIGGER trg_api_keys_updated_at BEFORE UPDATE ON public.api_keys FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


CREATE TRIGGER trg_assets_updated_at BEFORE UPDATE ON public.assets FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


CREATE TRIGGER trg_chat_messages_updated_at BEFORE UPDATE ON public.chat_messages FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


CREATE TRIGGER trg_chat_sessions_updated_at BEFORE UPDATE ON public.chat_sessions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


CREATE TRIGGER trg_jobs_updated_at BEFORE UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


CREATE TRIGGER trg_subscriptions_updated_at BEFORE UPDATE ON public.subscriptions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


CREATE TRIGGER trg_tenants_updated_at BEFORE UPDATE ON public.tenants FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


CREATE TRIGGER trg_usage_logs_updated_at BEFORE UPDATE ON public.usage_logs FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON public.users FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.correction_pairs
    ADD CONSTRAINT correction_pairs_moat_session_id_fkey FOREIGN KEY (moat_session_id) REFERENCES public.moat_sessions(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.correction_pairs
    ADD CONSTRAINT correction_pairs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.correction_pairs
    ADD CONSTRAINT correction_pairs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.moat_sessions
    ADD CONSTRAINT moat_sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.moat_sessions
    ADD CONSTRAINT moat_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.narasi_outlines
    ADD CONSTRAINT narasi_outlines_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.narasi_outlines
    ADD CONSTRAINT narasi_outlines_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.assets ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.chat_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.correction_pairs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.moat_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.narasi_outlines ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_iso ON public.correction_pairs USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_iso ON public.moat_sessions USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.api_keys USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.assets USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.chat_messages USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.chat_sessions USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.jobs USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.narasi_outlines USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.subscriptions USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.tenants USING ((id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.usage_logs USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


CREATE POLICY tenant_isolation ON public.users USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.usage_logs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
COMMIT;

--
-- PostgreSQL database dump
--

\restrict sJWbnnsg2th7VyQn9o96aS9v4CqCFrF8ydjUAM39aUkHB0qAA267HPtmcKZCEEt

-- Dumped from database version 18.4 (72c6e7c)
-- Dumped by pg_dump version 18.4 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: job_status_enum; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.job_status_enum AS ENUM (
    'queued',
    'processing',
    'running',
    'cancelling',
    'cancelled',
    'done',
    'error'
);


--
-- Name: job_type_enum; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.job_type_enum AS ENUM (
    'oneshot_fix',
    'batch_image',
    'tts',
    'imagen',
    'veo',
    'sora',
    'narasi'
);


--
-- Name: job_tenant(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.job_tenant(p_job_id uuid) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  SELECT tenant_id FROM jobs WHERE id = p_job_id;
$$;


--
-- Name: provision_tenant(uuid, text, text, text, text, text, text); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: set_current_tenant_id(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_current_tenant_id(p_tenant_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM set_config('app.current_tenant_id', p_tenant_id::TEXT, TRUE);
END;
$$;


--
-- Name: FUNCTION set_current_tenant_id(p_tenant_id uuid); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.set_current_tenant_id(p_tenant_id uuid) IS 'Set app.current_tenant_id for current transaction; activates RLS policies.';


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE api_keys; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.api_keys IS 'Encrypted upstream API credentials per tenant. Replaces shared env vars.';


--
-- Name: COLUMN api_keys.key_value_enc; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.api_keys.key_value_enc IS 'pgp_sym_encrypt(raw_key, app_secret) — never store plaintext.';


--
-- Name: COLUMN api_keys.key_hint; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.api_keys.key_hint IS 'Last-4-character hint shown in UI; override at INSERT.';


--
-- Name: assets; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE assets; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.assets IS 'File references in object storage, replacing all Docker volume paths (Veo, Sora, TTS, Imagen, batch).';


--
-- Name: COLUMN assets.s3_key; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.assets.s3_key IS 'Full object key within the bucket, e.g. "tenants/{tid}/jobs/{jid}/output_001.wav".';


--
-- Name: COLUMN assets.signed_url; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.assets.signed_url IS 'Cached pre-signed URL; regenerate when signed_url_expires_at < now().';


--
-- Name: COLUMN assets.metadata; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.assets.metadata IS 'Free-form JSONB: video duration, image width/height, TTS voice, sample rate, etc.';


--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE chat_messages; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.chat_messages IS 'Individual message turns inside a chat session. Replaces history arrays in Conversation objects.';


--
-- Name: COLUMN chat_messages.tool_calls; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.chat_messages.tool_calls IS 'JSONB array of tool_use blocks emitted by the model (MCP).';


--
-- Name: COLUMN chat_messages.sequence_number; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.chat_messages.sequence_number IS '1-based monotonic counter within the session for stable ordering.';


--
-- Name: chat_sessions; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE chat_sessions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.chat_sessions IS 'Persistent chat sessions. Replaces the in-memory sessions dict in laozhang_api.py.';


--
-- Name: COLUMN chat_sessions.mcp_paths; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.chat_sessions.mcp_paths IS 'Comma-separated folder paths forwarded to the MCP file-search sidecar.';


--
-- Name: COLUMN chat_sessions.last_message_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.chat_sessions.last_message_at IS 'Denormalised timestamp of the most recent message for efficient sorting.';


--
-- Name: correction_pairs; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE jobs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.jobs IS 'Unified job table replacing _oneshot_jobs dict, jobs.json, tts-jobs.json, imagen-jobs.json, and the activeJobs Map.';


--
-- Name: COLUMN jobs.input_payload; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.input_payload IS 'Serialised request parameters: prompts[], voice, speed, transcriptBody, model, aspectRatio, etc.';


--
-- Name: COLUMN jobs.logs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.logs IS 'Ordered array of human-readable log strings mirroring job.logs[] in server.js.';


--
-- Name: COLUMN jobs.result_payload; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.result_payload IS 'Serialised output: fixed_book text, array of {file, url} objects, destFile path, etc.';


--
-- Name: COLUMN jobs.external_job_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.jobs.external_job_id IS 'Provider-assigned identifier: Gemini batch name, Veo task_id, Sora job id.';


--
-- Name: migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.migrations (
    id integer NOT NULL,
    filename text NOT NULL,
    checksum text,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    duration_ms integer
);


--
-- Name: TABLE migrations; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.migrations IS 'Records every migration file that has been applied. Used by migrate.js to skip already-run files.';


--
-- Name: COLUMN migrations.checksum; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.migrations.checksum IS 'MD5 hash of the .sql file contents at the time it was applied, for drift detection.';


--
-- Name: migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.migrations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: migrations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.migrations_id_seq OWNED BY public.migrations.id;


--
-- Name: moat_sessions; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: narasi_outlines; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE subscriptions; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.subscriptions IS 'Mirrors Stripe subscription state per tenant. One active row at a time; history is retained.';


--
-- Name: COLUMN subscriptions.stripe_subscription_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.subscriptions.stripe_subscription_id IS 'Stripe sub_... identifier; used for webhook reconciliation.';


--
-- Name: COLUMN subscriptions.monthly_token_limit; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.subscriptions.monthly_token_limit IS 'Denormalised from Stripe metadata. NULL = plan has no token cap.';


--
-- Name: tenants; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE tenants; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.tenants IS 'One row per organisation or individual customer.';


--
-- Name: COLUMN tenants.slug; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tenants.slug IS 'URL-safe short name, used as subdomain prefix.';


--
-- Name: COLUMN tenants.settings; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.tenants.settings IS 'Per-tenant feature flags and UI preferences (free-form JSONB).';


--
-- Name: usage_logs; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE usage_logs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.usage_logs IS 'One row per upstream LLM/image/TTS/video API call. Captures tokens and cost for billing dashboards.';


--
-- Name: COLUMN usage_logs.model_alias; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_logs.model_alias IS 'User-facing model alias (e.g. "gemini-2.5-pro") before MODELS dict resolution.';


--
-- Name: COLUMN usage_logs.model_upstream; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_logs.model_upstream IS 'Resolved upstream model string actually sent to the provider.';


--
-- Name: COLUMN usage_logs.cost_usd; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.usage_logs.cost_usd IS 'Computed by the application layer using per-model rate tables; 8 decimal places for sub-cent precision.';


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: TABLE users; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.users IS 'One row per human user, always scoped to a tenant.';


--
-- Name: COLUMN users.password_hash; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users.password_hash IS 'bcrypt/argon2 hash; NULL for SSO-only accounts.';


--
-- Name: COLUMN users.external_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.users.external_id IS 'OAuth sub-claim or SAML nameID for federated identity.';


--
-- Name: migrations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.migrations ALTER COLUMN id SET DEFAULT nextval('public.migrations_id_seq'::regclass);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: assets assets_bucket_s3_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_bucket_s3_key_key UNIQUE (bucket, s3_key);


--
-- Name: assets assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_pkey PRIMARY KEY (id);


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);


--
-- Name: chat_sessions chat_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_pkey PRIMARY KEY (id);


--
-- Name: correction_pairs correction_pairs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.correction_pairs
    ADD CONSTRAINT correction_pairs_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: migrations migrations_filename_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.migrations
    ADD CONSTRAINT migrations_filename_key UNIQUE (filename);


--
-- Name: migrations migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.migrations
    ADD CONSTRAINT migrations_pkey PRIMARY KEY (id);


--
-- Name: moat_sessions moat_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.moat_sessions
    ADD CONSTRAINT moat_sessions_pkey PRIMARY KEY (id);


--
-- Name: narasi_outlines narasi_outlines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.narasi_outlines
    ADD CONSTRAINT narasi_outlines_pkey PRIMARY KEY (id);


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


--
-- Name: subscriptions subscriptions_stripe_subscription_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_stripe_subscription_id_key UNIQUE (stripe_subscription_id);


--
-- Name: tenants tenants_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_email_key UNIQUE (email);


--
-- Name: tenants tenants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id);


--
-- Name: tenants tenants_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_slug_key UNIQUE (slug);


--
-- Name: usage_logs usage_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_tenant_id_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_email_key UNIQUE (tenant_id, email);


--
-- Name: idx_api_keys_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_api_keys_provider ON public.api_keys USING btree (tenant_id, provider, is_active);


--
-- Name: idx_api_keys_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_api_keys_tenant_id ON public.api_keys USING btree (tenant_id);


--
-- Name: idx_assets_asset_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_asset_type ON public.assets USING btree (tenant_id, asset_type);


--
-- Name: idx_assets_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_created_at ON public.assets USING btree (tenant_id, created_at DESC);


--
-- Name: idx_assets_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_job_id ON public.assets USING btree (job_id) WHERE (job_id IS NOT NULL);


--
-- Name: idx_assets_signed_url_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_signed_url_expiry ON public.assets USING btree (signed_url_expires_at) WHERE (signed_url IS NOT NULL);


--
-- Name: idx_assets_source_job_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_source_job_type ON public.assets USING btree (tenant_id, source_job_type);


--
-- Name: idx_assets_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_tenant_id ON public.assets USING btree (tenant_id);


--
-- Name: idx_assets_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_user_id ON public.assets USING btree (user_id);


--
-- Name: idx_chat_messages_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_messages_created_at ON public.chat_messages USING btree (session_id, created_at);


--
-- Name: idx_chat_messages_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_messages_session_id ON public.chat_messages USING btree (session_id, sequence_number);


--
-- Name: idx_chat_messages_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_messages_tenant_id ON public.chat_messages USING btree (tenant_id);


--
-- Name: idx_chat_sessions_is_archived; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_sessions_is_archived ON public.chat_sessions USING btree (tenant_id, is_archived);


--
-- Name: idx_chat_sessions_last_message_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_sessions_last_message_at ON public.chat_sessions USING btree (tenant_id, last_message_at DESC);


--
-- Name: idx_chat_sessions_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_sessions_tenant_id ON public.chat_sessions USING btree (tenant_id);


--
-- Name: idx_chat_sessions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_sessions_user_id ON public.chat_sessions USING btree (user_id);


--
-- Name: idx_corr_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_corr_created ON public.correction_pairs USING btree (created_at);


--
-- Name: idx_corr_quality; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_corr_quality ON public.correction_pairs USING btree (quality_tier);


--
-- Name: idx_corr_style; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_corr_style ON public.correction_pairs USING btree (style_label);


--
-- Name: idx_corr_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_corr_tenant ON public.correction_pairs USING btree (tenant_id);


--
-- Name: idx_jobs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_created_at ON public.jobs USING btree (tenant_id, created_at DESC);


--
-- Name: idx_jobs_external_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_external_job_id ON public.jobs USING btree (external_job_id) WHERE (external_job_id IS NOT NULL);


--
-- Name: idx_jobs_job_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_job_type ON public.jobs USING btree (tenant_id, job_type);


--
-- Name: idx_jobs_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_session_id ON public.jobs USING btree (session_id) WHERE (session_id IS NOT NULL);


--
-- Name: idx_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_status ON public.jobs USING btree (tenant_id, status);


--
-- Name: idx_jobs_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_tenant_id ON public.jobs USING btree (tenant_id);


--
-- Name: idx_jobs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_user_id ON public.jobs USING btree (user_id);


--
-- Name: idx_migrations_filename; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_migrations_filename ON public.migrations USING btree (filename);


--
-- Name: idx_moat_sessions_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_moat_sessions_created ON public.moat_sessions USING btree (created_at);


--
-- Name: idx_moat_sessions_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_moat_sessions_tenant ON public.moat_sessions USING btree (tenant_id);


--
-- Name: idx_narasi_outlines_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_narasi_outlines_tenant ON public.narasi_outlines USING btree (tenant_id, created_at DESC);


--
-- Name: idx_subscriptions_current_period_end; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_current_period_end ON public.subscriptions USING btree (current_period_end) WHERE (status = ANY (ARRAY['active'::text, 'trialing'::text]));


--
-- Name: idx_subscriptions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_status ON public.subscriptions USING btree (status);


--
-- Name: idx_subscriptions_stripe_customer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_stripe_customer_id ON public.subscriptions USING btree (stripe_customer_id);


--
-- Name: idx_subscriptions_stripe_subscription_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_stripe_subscription_id ON public.subscriptions USING btree (stripe_subscription_id);


--
-- Name: idx_subscriptions_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_tenant_id ON public.subscriptions USING btree (tenant_id);


--
-- Name: idx_tenants_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tenants_email ON public.tenants USING btree (email);


--
-- Name: idx_tenants_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tenants_is_active ON public.tenants USING btree (is_active);


--
-- Name: idx_tenants_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tenants_slug ON public.tenants USING btree (slug);


--
-- Name: idx_usage_logs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_logs_created_at ON public.usage_logs USING btree (tenant_id, created_at DESC);


--
-- Name: idx_usage_logs_endpoint; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_logs_endpoint ON public.usage_logs USING btree (tenant_id, endpoint, created_at DESC);


--
-- Name: idx_usage_logs_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_logs_job_id ON public.usage_logs USING btree (job_id) WHERE (job_id IS NOT NULL);


--
-- Name: idx_usage_logs_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_logs_model ON public.usage_logs USING btree (tenant_id, model_upstream);


--
-- Name: idx_usage_logs_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_logs_session_id ON public.usage_logs USING btree (session_id) WHERE (session_id IS NOT NULL);


--
-- Name: idx_usage_logs_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_logs_tenant_id ON public.usage_logs USING btree (tenant_id);


--
-- Name: idx_usage_logs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_logs_user_id ON public.usage_logs USING btree (user_id);


--
-- Name: idx_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_email ON public.users USING btree (email);


--
-- Name: idx_users_external_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_external_id ON public.users USING btree (external_id) WHERE (external_id IS NOT NULL);


--
-- Name: idx_users_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_tenant_id ON public.users USING btree (tenant_id);


--
-- Name: api_keys trg_api_keys_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_api_keys_updated_at BEFORE UPDATE ON public.api_keys FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: assets trg_assets_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_assets_updated_at BEFORE UPDATE ON public.assets FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: chat_messages trg_chat_messages_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_chat_messages_updated_at BEFORE UPDATE ON public.chat_messages FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: chat_sessions trg_chat_sessions_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_chat_sessions_updated_at BEFORE UPDATE ON public.chat_sessions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: jobs trg_jobs_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jobs_updated_at BEFORE UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: subscriptions trg_subscriptions_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_subscriptions_updated_at BEFORE UPDATE ON public.subscriptions FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: tenants trg_tenants_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tenants_updated_at BEFORE UPDATE ON public.tenants FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: usage_logs trg_usage_logs_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_usage_logs_updated_at BEFORE UPDATE ON public.usage_logs FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: users trg_users_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON public.users FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


--
-- Name: api_keys api_keys_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: assets assets_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: assets assets_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: assets assets_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: chat_messages chat_messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE CASCADE;


--
-- Name: chat_messages chat_messages_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: chat_sessions chat_sessions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: chat_sessions chat_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: correction_pairs correction_pairs_moat_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.correction_pairs
    ADD CONSTRAINT correction_pairs_moat_session_id_fkey FOREIGN KEY (moat_session_id) REFERENCES public.moat_sessions(id) ON DELETE CASCADE;


--
-- Name: correction_pairs correction_pairs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.correction_pairs
    ADD CONSTRAINT correction_pairs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: correction_pairs correction_pairs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.correction_pairs
    ADD CONSTRAINT correction_pairs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: jobs jobs_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE SET NULL;


--
-- Name: jobs jobs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: jobs jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: moat_sessions moat_sessions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.moat_sessions
    ADD CONSTRAINT moat_sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: moat_sessions moat_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.moat_sessions
    ADD CONSTRAINT moat_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: narasi_outlines narasi_outlines_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.narasi_outlines
    ADD CONSTRAINT narasi_outlines_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: narasi_outlines narasi_outlines_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.narasi_outlines
    ADD CONSTRAINT narasi_outlines_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: subscriptions subscriptions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: usage_logs usage_logs_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: usage_logs usage_logs_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.chat_sessions(id) ON DELETE SET NULL;


--
-- Name: usage_logs usage_logs_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: usage_logs usage_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_logs
    ADD CONSTRAINT usage_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: users users_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) ON DELETE CASCADE;


--
-- Name: api_keys; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;

--
-- Name: assets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.assets ENABLE ROW LEVEL SECURITY;

--
-- Name: chat_messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;

--
-- Name: chat_sessions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.chat_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: correction_pairs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.correction_pairs ENABLE ROW LEVEL SECURITY;

--
-- Name: jobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;

--
-- Name: moat_sessions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.moat_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: narasi_outlines; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.narasi_outlines ENABLE ROW LEVEL SECURITY;

--
-- Name: subscriptions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

--
-- Name: correction_pairs tenant_iso; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_iso ON public.correction_pairs USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: moat_sessions tenant_iso; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_iso ON public.moat_sessions USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: api_keys tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.api_keys USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: assets tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.assets USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: chat_messages tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.chat_messages USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: chat_sessions tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.chat_sessions USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: jobs tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.jobs USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: narasi_outlines tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.narasi_outlines USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: subscriptions tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.subscriptions USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: tenants tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.tenants USING ((id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: usage_logs tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.usage_logs USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: users tenant_isolation; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_isolation ON public.users USING ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)) WITH CHECK ((tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid));


--
-- Name: tenants; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;

--
-- Name: usage_logs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.usage_logs ENABLE ROW LEVEL SECURITY;

--
-- Name: users; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;

--
-- PostgreSQL database dump complete
--

\unrestrict sJWbnnsg2th7VyQn9o96aS9v4CqCFrF8ydjUAM39aUkHB0qAA267HPtmcKZCEEt


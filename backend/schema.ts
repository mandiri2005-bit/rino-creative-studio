/**
 * schema.ts — Drizzle ORM table definitions for Rino Creative Studio
 * Covers only the tables Node.js (server.js) touches directly:
 *   tenants, users, jobs, assets, usage_logs
 * Full schema is in schema.sql — these definitions must stay in sync.
 */

import {
  pgTable, pgEnum,
  uuid, text, boolean, integer, bigint, smallint,
  numeric, timestamp, jsonb,
} from "drizzle-orm/pg-core";
import { sql } from "drizzle-orm";

// ── Enums (must match schema.sql exactly) ────────────────────────────────────

export const jobTypeEnum = pgEnum("job_type_enum", [
  "oneshot_fix",
  "batch_image",
  "tts",
  "imagen",
  "veo",
  "sora",
]);

export const jobStatusEnum = pgEnum("job_status_enum", [
  "queued",
  "processing",
  "running",
  "cancelling",
  "cancelled",
  "done",
  "error",
]);

// ── 1. tenants ────────────────────────────────────────────────────────────────

export const tenants = pgTable("tenants", {
  id:        uuid("id").primaryKey().default(sql`gen_random_uuid()`),
  name:      text("name").notNull(),
  slug:      text("slug").notNull().unique(),
  email:     text("email").notNull().unique(),
  plan:      text("plan").notNull().default("free"),
  isActive:  boolean("is_active").notNull().default(true),
  settings:  jsonb("settings").notNull().default(sql`'{}'`),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
});

// ── 2. users ──────────────────────────────────────────────────────────────────

export const users = pgTable("users", {
  id:           uuid("id").primaryKey().default(sql`gen_random_uuid()`),
  tenantId:     uuid("tenant_id").notNull().references(() => tenants.id, { onDelete: "cascade" }),
  email:        text("email").notNull(),
  displayName:  text("display_name"),
  passwordHash: text("password_hash"),
  externalId:   text("external_id"),
  role:         text("role").notNull().default("member"),
  isActive:     boolean("is_active").notNull().default(true),
  lastLoginAt:  timestamp("last_login_at", { withTimezone: true }),
  createdAt:    timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt:    timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
});

// ── 6. jobs ───────────────────────────────────────────────────────────────────

export const jobs = pgTable("jobs", {
  id:              uuid("id").primaryKey().default(sql`gen_random_uuid()`),
  tenantId:        uuid("tenant_id").notNull().references(() => tenants.id, { onDelete: "cascade" }),
  userId:          uuid("user_id").references(() => users.id, { onDelete: "set null" }),
  jobType:         jobTypeEnum("job_type").notNull(),
  status:          jobStatusEnum("status").notNull().default("queued"),

  // Input
  model:           text("model"),
  inputPayload:    jsonb("input_payload"),

  // Progress
  progressCurrent: integer("progress_current").notNull().default(0),
  progressTotal:   integer("progress_total").notNull().default(0),
  progressMessage: text("progress_message"),
  logs:            jsonb("logs").notNull().default(sql`'[]'`),

  // Output
  resultPayload:   jsonb("result_payload"),
  errorMessage:    text("error_message"),

  // External
  externalJobId:   text("external_job_id"),
  outputPrefix:    text("output_prefix"),

  // Timestamps
  startedAt:       timestamp("started_at", { withTimezone: true }),
  completedAt:     timestamp("completed_at", { withTimezone: true }),
  createdAt:       timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt:       timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
});

// ── 7. usage_logs ─────────────────────────────────────────────────────────────

export const usageLogs = pgTable("usage_logs", {
  id:            uuid("id").primaryKey().default(sql`gen_random_uuid()`),
  tenantId:      uuid("tenant_id").notNull().references(() => tenants.id, { onDelete: "cascade" }),
  userId:        uuid("user_id").references(() => users.id, { onDelete: "set null" }),
  jobId:         uuid("job_id").references(() => jobs.id, { onDelete: "set null" }),

  endpoint:      text("endpoint").notNull(),
  modelAlias:    text("model_alias").notNull(),
  modelUpstream: text("model_upstream").notNull(),
  provider:      text("provider").notNull(),

  tokensIn:      integer("tokens_in").notNull().default(0),
  tokensOut:     integer("tokens_out").notNull().default(0),
  costUsd:       numeric("cost_usd", { precision: 12, scale: 8 }).notNull().default("0"),

  finishReason:  text("finish_reason"),
  latencyMs:     integer("latency_ms"),
  httpStatus:    smallint("http_status"),

  createdAt:     timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt:     timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
});

// ── 8. assets ─────────────────────────────────────────────────────────────────

export const assets = pgTable("assets", {
  id:                  uuid("id").primaryKey().default(sql`gen_random_uuid()`),
  tenantId:            uuid("tenant_id").notNull().references(() => tenants.id, { onDelete: "cascade" }),
  userId:              uuid("user_id").references(() => users.id, { onDelete: "set null" }),
  jobId:               uuid("job_id").references(() => jobs.id, { onDelete: "set null" }),

  bucket:              text("bucket").notNull(),
  s3Key:               text("s3_key").notNull(),
  originalFilename:    text("original_filename"),

  contentType:         text("content_type").notNull(),
  sizeBytes:           bigint("size_bytes", { mode: "number" }).notNull().default(0),

  assetType:           text("asset_type").notNull(),
  sourceJobType:       jobTypeEnum("source_job_type"),

  signedUrl:           text("signed_url"),
  signedUrlExpiresAt:  timestamp("signed_url_expires_at", { withTimezone: true }),

  metadata:            jsonb("metadata").notNull().default(sql`'{}'`),
  isDeleted:           boolean("is_deleted").notNull().default(false),

  createdAt:           timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt:           timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
});

// ── Type exports (useful for TypeScript callers) ──────────────────────────────

export type Tenant   = typeof tenants.$inferSelect;
export type User     = typeof users.$inferSelect;
export type Job      = typeof jobs.$inferSelect;
export type UsageLog = typeof usageLogs.$inferSelect;
export type Asset    = typeof assets.$inferSelect;

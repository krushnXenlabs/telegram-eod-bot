-- ─────────────────────────────────────────────────────────────────────────────
-- EOD Bot – Supabase Schema
-- Run this once in the Supabase SQL editor (Dashboard → SQL Editor → New query)
-- ─────────────────────────────────────────────────────────────────────────────

-- Activities: one row per logged activity
create table if not exists activities (
  id              uuid        default gen_random_uuid() primary key,
  date            text        not null,               -- DD-MM-YYYY
  name            text        not null,
  category        text,                               -- 'Personal' or 'Professional'
  time_spent_hrs  float,                              -- parsed from "1 hr", "2.5 hrs" etc.
  outcome         text,
  reflection      text,
  skills          text[],                             -- auto-extracted by Claude
  created_at      timestamptz default now(),
  unique (date, name)                                 -- safe to upsert repeatedly
);

-- Conversations: full message history per session
create table if not exists conversations (
  id            uuid        default gen_random_uuid() primary key,
  date          text        not null,                 -- DD-MM-YYYY
  session_type  text        not null,                 -- 'checkin' | 'backfill' | 'importeod' | 'log'
  messages      jsonb       not null,                 -- [{role, content}, ...]
  created_at    timestamptz default now()
);

-- Indexes for fast analytics queries
create index if not exists idx_activities_date     on activities (date);
create index if not exists idx_activities_category on activities (category);
create index if not exists idx_conversations_date  on conversations (date);

-- ─────────────────────────────────────────────────────────────────────────────
-- Useful analytics queries (save these as Supabase views or run ad-hoc)
-- ─────────────────────────────────────────────────────────────────────────────

-- Total hours per category (all time)
-- select category, round(sum(time_spent_hrs)::numeric, 1) as total_hrs
-- from activities group by category order by total_hrs desc;

-- Top skills by frequency
-- select skill, count(*) as times_practiced
-- from activities, unnest(skills) as skill
-- group by skill order by times_practiced desc;

-- Hours per skill
-- select skill, round(sum(time_spent_hrs)::numeric, 1) as hrs
-- from activities, unnest(skills) as skill
-- group by skill order by hrs desc;

-- Daily hours breakdown
-- select date, category, round(sum(time_spent_hrs)::numeric, 1) as hrs
-- from activities group by date, category order by date desc;

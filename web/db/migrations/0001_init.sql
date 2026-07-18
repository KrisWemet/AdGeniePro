-- AdGeniePro initial schema. All money columns are integer cents.

create table if not exists products (
  id uuid primary key default gen_random_uuid(),
  source text not null default 'clickbank',
  vendor_id text not null,
  title text not null,
  description text,
  category text,
  gravity numeric,
  commission_pct numeric,
  avg_dollars_per_sale numeric,
  affiliate_link text,
  ai_score numeric,
  ai_rationale text,
  status text not null default 'discovered'
    check (status in ('discovered', 'shortlisted', 'active', 'rejected')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source, vendor_id)
);

create table if not exists campaigns (
  id uuid primary key default gen_random_uuid(),
  product_id uuid not null references products (id),
  meta_campaign_id text,
  meta_adset_id text,
  name text not null,
  status text not null default 'draft'
    check (status in ('draft', 'pending_approval', 'active', 'paused', 'killed')),
  daily_budget_cents integer not null default 1000,
  landing_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists ads (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references campaigns (id) on delete cascade,
  meta_ad_id text,
  meta_creative_id text,
  headline text not null,
  primary_text text not null,
  description text,
  angle text,
  status text not null default 'draft',
  created_at timestamptz not null default now()
);

create table if not exists metrics_daily (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references campaigns (id) on delete cascade,
  date date not null,
  spend_cents integer not null default 0,
  impressions integer,
  clicks integer,
  meta_conversions numeric,
  revenue_cents integer not null default 0,
  unique (campaign_id, date)
);

-- Every AI (and human) decision, with rationale. The audit trail is the core
-- trust mechanism of the system.
create table if not exists ai_actions (
  id uuid primary key default gen_random_uuid(),
  actor text not null,
  action text not null,
  target_type text,
  target_id text,
  detail jsonb,
  rationale text,
  created_at timestamptz not null default now()
);

create index if not exists ai_actions_created_at_idx on ai_actions (created_at desc);
create index if not exists metrics_daily_campaign_idx on metrics_daily (campaign_id, date);

-- Singleton guardrail settings row.
create table if not exists app_settings (
  id integer primary key default 1 check (id = 1),
  autonomy_mode text not null default 'approve' check (autonomy_mode in ('approve', 'auto')),
  kill_switch boolean not null default false,
  daily_budget_cap_cents integer not null default 5000,
  total_budget_cap_cents integer not null default 50000,
  max_budget_change_pct integer not null default 20,
  target_roas numeric not null default 1.5,
  min_spend_before_judgement_cents integer not null default 2000,
  updated_at timestamptz not null default now()
);

insert into app_settings (id) values (1) on conflict (id) do nothing;

-- The app talks to the database exclusively through the service-role key, so
-- lock every table down: RLS on, no anon/authenticated policies.
alter table products enable row level security;
alter table campaigns enable row level security;
alter table ads enable row level security;
alter table metrics_daily enable row level security;
alter table ai_actions enable row level security;
alter table app_settings enable row level security;

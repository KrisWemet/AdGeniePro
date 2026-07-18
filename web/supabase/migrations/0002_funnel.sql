-- Funnel layer: leads captured from bridge pages, AI-generated own digital
-- products (post-purchase upsell + non-buyer stepping-stone), and email
-- sequences driving both paths.

create table if not exists leads (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  name text,
  source_campaign_id uuid references campaigns (id),
  clickbank_product_id uuid references products (id),
  status text not null default 'subscriber'
    check (status in ('subscriber', 'buyer', 'tripwire_buyer', 'unsubscribed')),
  purchased_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists own_products (
  id uuid primary key default gen_random_uuid(),
  clickbank_product_id uuid not null references products (id),
  kind text not null check (kind in ('upsell', 'tripwire')),
  title text not null,
  slug text not null unique,
  price_cents integer not null default 0,
  summary text,
  gap_rationale text,
  content_md text,
  checkout_url text,
  status text not null default 'draft' check (status in ('draft', 'ready', 'live')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists email_sequences (
  id uuid primary key default gen_random_uuid(),
  own_product_id uuid not null references own_products (id) on delete cascade,
  audience text not null check (audience in ('buyers', 'non_buyers')),
  name text not null,
  created_at timestamptz not null default now()
);

create table if not exists email_steps (
  id uuid primary key default gen_random_uuid(),
  sequence_id uuid not null references email_sequences (id) on delete cascade,
  step_number integer not null,
  delay_hours integer not null default 24,
  subject text not null,
  body_md text not null,
  unique (sequence_id, step_number)
);

create table if not exists email_sends (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid not null references leads (id) on delete cascade,
  step_id uuid not null references email_steps (id) on delete cascade,
  scheduled_at timestamptz not null,
  sent_at timestamptz,
  status text not null default 'scheduled'
    check (status in ('scheduled', 'sent', 'failed', 'skipped')),
  error text,
  unique (lead_id, step_id)
);

create index if not exists email_sends_due_idx
  on email_sends (status, scheduled_at);
create index if not exists leads_email_idx on leads (email);

alter table leads enable row level security;
alter table own_products enable row level security;
alter table email_sequences enable row level security;
alter table email_steps enable row level security;
alter table email_sends enable row level security;

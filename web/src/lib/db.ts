import postgres from "postgres";

// Plain-Postgres data layer. Works with any provider that hands out a
// connection string (Neon, Railway, RDS, Supabase, ...). Serverless-friendly:
// one connection, no prepared statements (safe behind pgbouncer/Neon pooler).
let client: postgres.Sql | null = null;

export function sql(): postgres.Sql {
  if (!client) {
    const url = process.env.DATABASE_URL;
    if (!url) {
      throw new Error("DATABASE_URL must be set (e.g. a Neon Postgres connection string)");
    }
    client = postgres(url, {
      ssl: "require",
      max: 1,
      prepare: false,
      types: {
        // Return numeric columns as JS numbers instead of strings.
        numeric: {
          to: 1700,
          from: [1700],
          serialize: (x: number) => String(x),
          parse: (x: string) => Number(x),
        },
      },
    });
  }
  return client;
}

export function isUuid(v: string | null | undefined): v is string {
  return !!v && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);
}

export interface AppSettings {
  id: number;
  autonomy_mode: "approve" | "auto";
  kill_switch: boolean;
  daily_budget_cap_cents: number;
  total_budget_cap_cents: number;
  max_budget_change_pct: number;
  target_roas: number;
  min_spend_before_judgement_cents: number;
}

export interface Product {
  id: string;
  source: string;
  vendor_id: string;
  title: string;
  description: string | null;
  category: string | null;
  gravity: number | null;
  commission_pct: number | null;
  avg_dollars_per_sale: number | null;
  affiliate_link: string | null;
  ai_score: number | null;
  ai_rationale: string | null;
  status: "discovered" | "shortlisted" | "active" | "rejected";
}

export interface Campaign {
  id: string;
  product_id: string;
  meta_campaign_id: string | null;
  meta_adset_id: string | null;
  name: string;
  status: "draft" | "pending_approval" | "active" | "paused" | "killed";
  daily_budget_cents: number;
  landing_url: string | null;
}

export interface MetricsDaily {
  campaign_id: string;
  date: string;
  spend_cents: number;
  impressions: number | null;
  clicks: number | null;
  meta_conversions: number | null;
  revenue_cents: number;
}

export async function getSettings(): Promise<AppSettings> {
  const rows = await sql()<AppSettings[]>`select * from app_settings where id = 1`;
  if (rows.length === 0) throw new Error("app_settings row missing — run the migrations");
  return rows[0];
}

export async function logAction(entry: {
  actor: string;
  action: string;
  target_type?: string;
  target_id?: string;
  detail?: unknown;
  rationale?: string;
}): Promise<void> {
  try {
    await sql()`
      insert into ai_actions (actor, action, target_type, target_id, detail, rationale)
      values (${entry.actor}, ${entry.action}, ${entry.target_type ?? null},
              ${entry.target_id ?? null},
              ${entry.detail ? sql().json(entry.detail as never) : null},
              ${entry.rationale ?? null})`;
  } catch (err) {
    console.error("failed to log ai_action:", err);
  }
}

// Shared by every spend-affecting code path.
export async function currentSpendState(): Promise<{
  activeDailyBudgetCents: number;
  totalSpendCents: number;
}> {
  const [row] = await sql()<
    Array<{ active_daily: number; total_spend: number }>
  >`
    select
      coalesce((select sum(daily_budget_cents) from campaigns where status = 'active'), 0)::int as active_daily,
      coalesce((select sum(spend_cents) from metrics_daily), 0)::int as total_spend`;
  return { activeDailyBudgetCents: row.active_daily, totalSpendCents: row.total_spend };
}

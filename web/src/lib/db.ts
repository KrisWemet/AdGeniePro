import { createClient, SupabaseClient } from "@supabase/supabase-js";

// Server-only Supabase client using the service-role key. Never import this
// from a client component.
let client: SupabaseClient | null = null;

export function db(): SupabaseClient {
  if (!client) {
    const url = process.env.SUPABASE_URL;
    const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
    if (!url || !key) {
      throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
    }
    client = createClient(url, key, { auth: { persistSession: false } });
  }
  return client;
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
  const { data, error } = await db()
    .from("app_settings")
    .select("*")
    .eq("id", 1)
    .single();
  if (error) throw new Error(`failed to load settings: ${error.message}`);
  return data as AppSettings;
}

export async function logAction(entry: {
  actor: string;
  action: string;
  target_type?: string;
  target_id?: string;
  detail?: unknown;
  rationale?: string;
}): Promise<void> {
  const { error } = await db().from("ai_actions").insert(entry);
  if (error) console.error("failed to log ai_action:", error.message);
}

// Minimal Meta (Facebook/Instagram) Marketing API client. Every campaign is
// created PAUSED; activation is a separate, guardrail-gated step.

const API_VERSION = process.env.META_API_VERSION || "v23.0";
const BASE = `https://graph.facebook.com/${API_VERSION}`;

function creds() {
  const token = process.env.FB_ACCESS_TOKEN;
  const account = process.env.FB_AD_ACCOUNT_ID; // numeric, no "act_" prefix
  const page = process.env.FB_PAGE_ID;
  if (!token || !account) {
    throw new Error("FB_ACCESS_TOKEN and FB_AD_ACCOUNT_ID must be set");
  }
  return { token, account, page };
}

async function metaFetch<T>(
  path: string,
  init: { method?: string; body?: Record<string, string> } = {}
): Promise<T> {
  const { token } = creds();
  const params = new URLSearchParams({ access_token: token, ...(init.body ?? {}) });
  const url =
    init.method === "POST" ? `${BASE}${path}` : `${BASE}${path}?${params.toString()}`;
  const res = await fetch(url, {
    method: init.method ?? "GET",
    body: init.method === "POST" ? params : undefined,
  });
  const json = (await res.json()) as T & { error?: { message: string; code: number } };
  if (!res.ok || json.error) {
    throw new Error(
      `Meta API ${path} failed: ${json.error?.message ?? `HTTP ${res.status}`}`
    );
  }
  return json;
}

export async function createCampaign(name: string): Promise<string> {
  const { account } = creds();
  const r = await metaFetch<{ id: string }>(`/act_${account}/campaigns`, {
    method: "POST",
    body: {
      name,
      objective: "OUTCOME_SALES",
      status: "PAUSED",
      special_ad_categories: "[]",
    },
  });
  return r.id;
}

export async function createAdSet(opts: {
  name: string;
  campaignId: string;
  dailyBudgetCents: number;
  countries?: string[];
}): Promise<string> {
  const { account } = creds();
  const r = await metaFetch<{ id: string }>(`/act_${account}/adsets`, {
    method: "POST",
    body: {
      name: opts.name,
      campaign_id: opts.campaignId,
      daily_budget: String(opts.dailyBudgetCents),
      billing_event: "IMPRESSIONS",
      optimization_goal: "OFFSITE_CONVERSIONS",
      bid_strategy: "LOWEST_COST_WITHOUT_CAP",
      status: "PAUSED",
      targeting: JSON.stringify({
        geo_locations: { countries: opts.countries ?? ["US", "CA"] },
      }),
    },
  });
  return r.id;
}

export async function createAdWithCreative(opts: {
  name: string;
  adsetId: string;
  landingUrl: string;
  headline: string;
  primaryText: string;
  description: string;
}): Promise<{ adId: string; creativeId: string }> {
  const { account, page } = creds();
  if (!page) throw new Error("FB_PAGE_ID must be set to create ad creatives");
  const creative = await metaFetch<{ id: string }>(`/act_${account}/adcreatives`, {
    method: "POST",
    body: {
      name: `${opts.name} creative`,
      object_story_spec: JSON.stringify({
        page_id: page,
        link_data: {
          link: opts.landingUrl,
          message: opts.primaryText,
          name: opts.headline,
          description: opts.description,
          call_to_action: { type: "LEARN_MORE" },
        },
      }),
    },
  });
  const ad = await metaFetch<{ id: string }>(`/act_${account}/ads`, {
    method: "POST",
    body: {
      name: opts.name,
      adset_id: opts.adsetId,
      creative: JSON.stringify({ creative_id: creative.id }),
      status: "PAUSED",
    },
  });
  return { adId: ad.id, creativeId: creative.id };
}

export async function setStatus(
  objectId: string,
  status: "ACTIVE" | "PAUSED"
): Promise<void> {
  await metaFetch(`/${objectId}`, { method: "POST", body: { status } });
}

export async function setAdSetDailyBudget(
  adsetId: string,
  dailyBudgetCents: number
): Promise<void> {
  await metaFetch(`/${adsetId}`, {
    method: "POST",
    body: { daily_budget: String(dailyBudgetCents) },
  });
}

export interface CampaignInsights {
  spendCents: number;
  impressions: number;
  clicks: number;
  conversions: number;
}

export async function getCampaignInsights(
  metaCampaignId: string,
  date: string // YYYY-MM-DD
): Promise<CampaignInsights> {
  const r = await metaFetch<{
    data: Array<{
      spend?: string;
      impressions?: string;
      clicks?: string;
      actions?: Array<{ action_type: string; value: string }>;
    }>;
  }>(`/${metaCampaignId}/insights`, {
    body: {
      time_range: JSON.stringify({ since: date, until: date }),
      fields: "spend,impressions,clicks,actions",
    },
  });
  const row = r.data?.[0];
  const purchases =
    row?.actions?.find((a) => a.action_type.includes("purchase"))?.value ?? "0";
  return {
    spendCents: Math.round(Number(row?.spend ?? 0) * 100),
    impressions: Number(row?.impressions ?? 0),
    clicks: Number(row?.clicks ?? 0),
    conversions: Number(purchases),
  };
}

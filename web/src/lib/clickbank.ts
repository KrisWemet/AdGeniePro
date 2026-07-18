import { XMLParser } from "fast-xml-parser";
import type { CandidateProduct } from "./anthropic";

const FEED_URL =
  process.env.CLICKBANK_FEED_URL ||
  "https://accounts.clickbank.com/marketplace/feed";

// Fetches and parses the ClickBank marketplace feed. The feed format has
// changed over the years; parsing is defensive and any failure is surfaced to
// the caller so it lands in the activity log instead of failing silently.
export async function fetchMarketplaceCandidates(
  limit = 40
): Promise<CandidateProduct[]> {
  const res = await fetch(FEED_URL, {
    headers: { "user-agent": "AdGeniePro/0.1" },
    // Marketplace data changes slowly; avoid hammering it.
    next: { revalidate: 3600 },
  });
  if (!res.ok) {
    throw new Error(`ClickBank feed returned HTTP ${res.status}`);
  }
  const xml = await res.text();
  const parser = new XMLParser({ ignoreAttributes: false });
  const doc = parser.parse(xml);

  // Feed shape: <Catalog><Category><Site>...</Site></Category></Catalog>
  // with Site fields Id, Title, Description, Gravity, PercentPerSale,
  // AverageEarningsPerSale. Tolerate single-item vs array nodes.
  const categories = toArray(doc?.Catalog?.Category);
  const out: CandidateProduct[] = [];
  for (const cat of categories) {
    const catName = String(cat?.Name ?? cat?.["@_name"] ?? "unknown");
    for (const site of toArray(cat?.Site)) {
      const gravity = Number(site?.Gravity ?? 0);
      const commission = Number(site?.PercentPerSale ?? 0);
      const eps = Number(site?.AverageEarningsPerSale ?? 0);
      const id = String(site?.Id ?? "").trim();
      if (!id) continue;
      // Pre-filter before spending Claude tokens: proven sellers with real
      // commissions only.
      if (gravity < 15 || commission < 40 || eps < 20) continue;
      out.push({
        vendor_id: id,
        title: String(site?.Title ?? id),
        description: String(site?.Description ?? ""),
        category: catName,
        gravity,
        commission_pct: commission,
        avg_dollars_per_sale: eps,
      });
    }
  }
  out.sort((a, b) => b.gravity - a.gravity);
  return out.slice(0, limit);
}

export function hoplink(vendorId: string): string | null {
  const nickname = process.env.CLICKBANK_NICKNAME;
  if (!nickname) return null;
  return `https://${nickname}.${vendorId}.hop.clickbank.net`;
}

// Best-effort daily revenue pull from the ClickBank Analytics API. Returns
// null when credentials aren't configured so the sync job can skip cleanly.
export async function fetchRevenueCentsForDay(date: string): Promise<number | null> {
  const devKey = process.env.CLICKBANK_DEV_KEY;
  const apiKey = process.env.CLICKBANK_API_KEY;
  if (!devKey || !apiKey) return null;
  const url =
    `https://api.clickbank.com/rest/1.3/analytics/summary` +
    `?startDate=${date}&endDate=${date}&dimension=ACCOUNT_NICKNAME&select=SALE_AMOUNT`;
  const res = await fetch(url, {
    headers: {
      Authorization: `${devKey}:${apiKey}`,
      Accept: "application/json",
    },
  });
  if (!res.ok) throw new Error(`ClickBank analytics returned HTTP ${res.status}`);
  const body = (await res.json()) as Record<string, unknown>;
  const amount = extractFirstNumber(body, "SALE_AMOUNT");
  return amount === null ? 0 : Math.round(amount * 100);
}

function toArray<T>(v: T | T[] | undefined | null): T[] {
  if (v == null) return [];
  return Array.isArray(v) ? v : [v];
}

function extractFirstNumber(obj: unknown, key: string): number | null {
  if (obj == null || typeof obj !== "object") return null;
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    if (k === key && (typeof v === "number" || typeof v === "string")) {
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    }
    const nested = extractFirstNumber(v, key);
    if (nested !== null) return nested;
  }
  return null;
}

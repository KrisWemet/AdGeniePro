import { db, getSettings } from "@/lib/db";

export const dynamic = "force-dynamic";

function usd(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

export default async function Overview() {
  const settings = await getSettings();
  const { data: metrics } = await db().from("metrics_daily").select("*");
  const { data: campaigns } = await db().from("campaigns").select("id,status");

  const spend = (metrics ?? []).reduce((a, m) => a + (m.spend_cents ?? 0), 0);
  const revenue = (metrics ?? []).reduce((a, m) => a + (m.revenue_cents ?? 0), 0);
  const profit = revenue - spend;
  const active = (campaigns ?? []).filter((c) => c.status === "active").length;
  const pending = (campaigns ?? []).filter((c) => c.status === "pending_approval").length;

  const cards = [
    { label: "Total spend", value: usd(spend) },
    { label: "Total revenue", value: usd(revenue) },
    {
      label: "Profit",
      value: usd(profit),
      tone: profit >= 0 ? "text-emerald-400" : "text-red-400",
    },
    { label: "ROAS", value: spend > 0 ? (revenue / spend).toFixed(2) : "—" },
    { label: "Active campaigns", value: String(active) },
    { label: "Awaiting approval", value: String(pending) },
  ];

  return (
    <div className="space-y-6">
      {settings.kill_switch && (
        <div className="rounded border border-red-700 bg-red-950 p-3 text-red-300 text-sm">
          Kill switch is engaged — all delivery is stopped and the AI will not act.
        </div>
      )}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        {cards.map((c) => (
          <div key={c.label} className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
            <div className="text-xs text-zinc-500 uppercase tracking-wide">{c.label}</div>
            <div className={`text-2xl font-semibold mt-1 ${c.tone ?? ""}`}>{c.value}</div>
          </div>
        ))}
      </div>
      <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-4 text-sm text-zinc-400">
        Guardrails: daily budget cap {usd(settings.daily_budget_cap_cents)} · lifetime cap{" "}
        {usd(settings.total_budget_cap_cents)} · target ROAS {settings.target_roas} · mode{" "}
        <span className="text-zinc-200">{settings.autonomy_mode}</span>
      </div>
    </div>
  );
}

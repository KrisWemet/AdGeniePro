import { sql, type Campaign } from "@/lib/db";

export const dynamic = "force-dynamic";

function usd(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

interface CampaignRow extends Campaign {
  spend_total: number;
  revenue_total: number;
}

export default async function Campaigns() {
  const campaigns = await sql()<CampaignRow[]>`
    select c.*,
           coalesce(sum(m.spend_cents), 0)::int as spend_total,
           coalesce(sum(m.revenue_cents), 0)::int as revenue_total
    from campaigns c
    left join metrics_daily m on m.campaign_id = c.id
    group by c.id
    order by c.created_at desc`;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Campaigns</h1>
      {campaigns.length === 0 ? (
        <div className="rounded border border-zinc-800 bg-zinc-900 p-6 text-zinc-400 text-sm">
          No campaigns yet. Launch one from a shortlisted product via{" "}
          <code>POST /api/campaigns/launch</code>.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900 text-zinc-400 text-left">
              <tr>
                <th className="p-3">Name</th>
                <th className="p-3">Status</th>
                <th className="p-3">Budget/day</th>
                <th className="p-3">Spend</th>
                <th className="p-3">Revenue</th>
                <th className="p-3">ROAS</th>
                <th className="p-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {campaigns.map((c) => (
                <tr key={c.id} className="border-t border-zinc-800">
                  <td className="p-3 font-medium">{c.name}</td>
                  <td className="p-3">
                    <span
                      className={
                        c.status === "active"
                          ? "text-emerald-400"
                          : c.status === "pending_approval"
                            ? "text-amber-400"
                            : c.status === "killed"
                              ? "text-red-400"
                              : "text-zinc-400"
                      }
                    >
                      {c.status}
                    </span>
                  </td>
                  <td className="p-3">{usd(c.daily_budget_cents)}</td>
                  <td className="p-3">{usd(c.spend_total)}</td>
                  <td className="p-3">{usd(c.revenue_total)}</td>
                  <td className="p-3">
                    {c.spend_total > 0 ? (c.revenue_total / c.spend_total).toFixed(2) : "—"}
                  </td>
                  <td className="p-3">
                    <div className="flex gap-2">
                      {c.status === "pending_approval" && (
                        <form action={`/api/campaigns/${c.id}`} method="post">
                          <input type="hidden" name="op" value="approve" />
                          <button className="rounded bg-emerald-600 px-2 py-1 text-xs font-medium hover:bg-emerald-500">
                            Approve &amp; launch
                          </button>
                        </form>
                      )}
                      {c.status === "active" && (
                        <form action={`/api/campaigns/${c.id}`} method="post">
                          <input type="hidden" name="op" value="pause" />
                          <button className="rounded bg-zinc-700 px-2 py-1 text-xs font-medium hover:bg-zinc-600">
                            Pause
                          </button>
                        </form>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

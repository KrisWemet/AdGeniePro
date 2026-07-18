import { sql, type Product } from "@/lib/db";

export const dynamic = "force-dynamic";

export default async function Products() {
  const products = await sql()<Product[]>`
    select * from products
    order by ai_score desc nulls last
    limit 100`;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Discovered products</h1>
      <p className="text-sm text-zinc-400">
        Found by the daily ClickBank discovery job and scored by Claude for
        sellability via paid Meta traffic. Products scoring ≥ 70 are shortlisted.
      </p>
      {products.length === 0 ? (
        <div className="rounded border border-zinc-800 bg-zinc-900 p-6 text-zinc-400 text-sm">
          No products yet — the discovery cron hasn&apos;t run, or the ClickBank feed
          was unreachable. Check the AI Activity page for errors.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900 text-zinc-400 text-left">
              <tr>
                <th className="p-3">Product</th>
                <th className="p-3">Gravity</th>
                <th className="p-3">Commission</th>
                <th className="p-3">$/sale</th>
                <th className="p-3">AI score</th>
                <th className="p-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {products.map((p) => (
                <tr key={p.id} className="border-t border-zinc-800">
                  <td className="p-3">
                    <div className="font-medium">{p.title}</div>
                    <div className="text-xs text-zinc-500 max-w-md truncate">
                      {p.ai_rationale ?? p.description}
                    </div>
                  </td>
                  <td className="p-3">{p.gravity?.toFixed(0) ?? "—"}</td>
                  <td className="p-3">{p.commission_pct ? `${p.commission_pct}%` : "—"}</td>
                  <td className="p-3">
                    {p.avg_dollars_per_sale ? `$${p.avg_dollars_per_sale.toFixed(0)}` : "—"}
                  </td>
                  <td className="p-3 font-semibold">
                    {p.ai_score !== null ? p.ai_score : "—"}
                  </td>
                  <td className="p-3">
                    <span
                      className={
                        p.status === "shortlisted"
                          ? "text-emerald-400"
                          : p.status === "active"
                            ? "text-sky-400"
                            : "text-zinc-500"
                      }
                    >
                      {p.status}
                    </span>
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

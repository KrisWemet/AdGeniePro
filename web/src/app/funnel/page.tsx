import { db } from "@/lib/db";

export const dynamic = "force-dynamic";

interface OwnProductRow {
  id: string;
  kind: "upsell" | "tripwire";
  title: string;
  slug: string;
  price_cents: number;
  status: string;
  checkout_url: string | null;
  gap_rationale: string | null;
  clickbank_product: { title: string } | null;
}

export default async function Funnel() {
  const { data: products } = await db()
    .from("own_products")
    .select("id, kind, title, slug, price_cents, status, checkout_url, gap_rationale, clickbank_product:products(title)")
    .order("created_at", { ascending: false });
  const { data: leads } = await db().from("leads").select("status");
  const { data: sends } = await db().from("email_sends").select("status");

  const count = (rows: Array<{ status: string }> | null, s: string) =>
    (rows ?? []).filter((r) => r.status === s).length;

  const stats = [
    { label: "Subscribers", value: count(leads, "subscriber") },
    { label: "Buyers", value: count(leads, "buyer") },
    { label: "Tripwire buyers", value: count(leads, "tripwire_buyer") },
    { label: "Emails sent", value: count(sends, "sent") },
    { label: "Emails scheduled", value: count(sends, "scheduled") },
    { label: "Unsubscribed", value: count(leads, "unsubscribed") },
  ];

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Funnel — own products &amp; email</h1>
      <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
        {stats.map((s) => (
          <div key={s.label} className="rounded-lg border border-zinc-800 bg-zinc-900 p-3">
            <div className="text-xs text-zinc-500">{s.label}</div>
            <div className="text-xl font-semibold">{s.value}</div>
          </div>
        ))}
      </div>

      <p className="text-sm text-zinc-400">
        Generate a companion product for any discovered ClickBank product via{" "}
        <code>POST /api/products/own/generate</code> with{" "}
        <code>{`{"product_id": "...", "kind": "upsell" | "tripwire"}`}</code>. Buyers get
        the upsell sequence after a ClickBank sale (INS webhook); non-buyers get the
        stepping-stone sequence after opting in.
      </p>

      <div className="space-y-3">
        {(products ?? []).length === 0 && (
          <div className="rounded border border-zinc-800 bg-zinc-900 p-6 text-zinc-400 text-sm">
            No own products yet.
          </div>
        )}
        {((products ?? []) as unknown as OwnProductRow[]).map((p) => (
          <div key={p.id} className="rounded-lg border border-zinc-800 bg-zinc-900 p-4 space-y-2">
            <div className="flex items-center gap-3">
              <span
                className={`rounded px-1.5 py-0.5 text-xs ${
                  p.kind === "upsell" ? "bg-sky-900 text-sky-300" : "bg-amber-900 text-amber-300"
                }`}
              >
                {p.kind}
              </span>
              <span className="font-medium">{p.title}</span>
              <span className="text-zinc-500 text-sm">
                ${(p.price_cents / 100).toFixed(2)} · {p.status}
              </span>
              <a href={`/p/${p.slug}`} className="ml-auto text-sm text-emerald-400 hover:underline">
                view content →
              </a>
            </div>
            {p.clickbank_product && (
              <div className="text-xs text-zinc-500">
                Companion to: {p.clickbank_product.title}
              </div>
            )}
            {p.gap_rationale && (
              <div className="text-sm text-zinc-400">{p.gap_rationale}</div>
            )}
            <form action={`/api/products/own/${p.id}`} method="post" className="flex gap-2 items-center pt-1">
              <input
                name="checkout_url"
                defaultValue={p.checkout_url ?? ""}
                placeholder="Checkout URL (Stripe Payment Link) — empty = delivered free"
                className="flex-1 rounded border border-zinc-700 bg-zinc-950 p-1.5 text-xs"
              />
              <select
                name="status"
                defaultValue={p.status}
                className="rounded border border-zinc-700 bg-zinc-950 p-1.5 text-xs"
              >
                <option value="ready">ready</option>
                <option value="live">live</option>
                <option value="draft">draft</option>
              </select>
              <button className="rounded bg-emerald-600 px-3 py-1.5 text-xs font-medium hover:bg-emerald-500">
                Save
              </button>
            </form>
          </div>
        ))}
      </div>
    </div>
  );
}

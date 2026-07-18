import { NextRequest, NextResponse } from "next/server";
import { sql, isUuid, logAction } from "@/lib/db";

// Update an own-product from the Funnel page: set it live (starts enrollment)
// or attach a checkout URL (e.g. a Stripe Payment Link).
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  if (!isUuid(id)) {
    return NextResponse.json({ error: "invalid id" }, { status: 400 });
  }
  const form = await req.formData();

  const update: Record<string, unknown> = { updated_at: new Date().toISOString() };
  const status = String(form.get("status") ?? "");
  if (["draft", "ready", "live"].includes(status)) update.status = status;
  const checkout = form.get("checkout_url");
  if (checkout !== null) update.checkout_url = String(checkout) || null;

  try {
    await sql()`update own_products set ${sql()(update)} where id = ${id}`;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: message }, { status: 500 });
  }

  await logAction({
    actor: "human",
    action: "update_own_product",
    target_type: "own_product",
    target_id: id,
    detail: update,
    rationale: "own-product updated from dashboard",
  });

  return NextResponse.redirect(new URL("/funnel", req.url), 303);
}

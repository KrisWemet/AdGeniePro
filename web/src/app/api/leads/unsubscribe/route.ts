import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { unsubscribeSignature } from "@/lib/funnel";

export async function GET(req: NextRequest) {
  const email = (req.nextUrl.searchParams.get("email") ?? "").toLowerCase();
  const sig = req.nextUrl.searchParams.get("sig") ?? "";
  if (!email || sig !== unsubscribeSignature(email)) {
    return new NextResponse("Invalid unsubscribe link", { status: 400 });
  }
  await db().from("leads").update({ status: "unsubscribed" }).eq("email", email);
  await db()
    .from("email_sends")
    .update({ status: "skipped", error: "unsubscribed" })
    .eq("status", "scheduled")
    .in(
      "lead_id",
      (await db().from("leads").select("id").eq("email", email)).data?.map((l) => l.id) ?? []
    );
  return new NextResponse(
    "<html><body style='font-family:sans-serif;padding:40px'><h2>You're unsubscribed.</h2><p>You won't receive any more emails from us.</p></body></html>",
    { headers: { "content-type": "text/html" } }
  );
}

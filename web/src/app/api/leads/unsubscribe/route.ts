import { NextRequest, NextResponse } from "next/server";
import { sql } from "@/lib/db";
import { unsubscribeSignature } from "@/lib/funnel";

export async function GET(req: NextRequest) {
  const email = (req.nextUrl.searchParams.get("email") ?? "").toLowerCase();
  const sig = req.nextUrl.searchParams.get("sig") ?? "";
  if (!email || sig !== unsubscribeSignature(email)) {
    return new NextResponse("Invalid unsubscribe link", { status: 400 });
  }
  await sql()`update leads set status = 'unsubscribed' where email = ${email}`;
  await sql()`
    update email_sends set status = 'skipped', error = 'unsubscribed'
    where status = 'scheduled'
      and lead_id in (select id from leads where email = ${email})`;
  return new NextResponse(
    "<html><body style='font-family:sans-serif;padding:40px'><h2>You're unsubscribed.</h2><p>You won't receive any more emails from us.</p></body></html>",
    { headers: { "content-type": "text/html" } }
  );
}

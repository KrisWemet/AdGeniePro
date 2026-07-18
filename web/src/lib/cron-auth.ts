import { NextRequest, NextResponse } from "next/server";

// Vercel Cron sends `Authorization: Bearer ${CRON_SECRET}` when the env var is
// configured. Reject anything else so these endpoints can't be triggered by
// strangers.
export function requireCronAuth(req: NextRequest): NextResponse | null {
  const secret = process.env.CRON_SECRET;
  if (!secret) {
    return NextResponse.json(
      { error: "CRON_SECRET is not configured" },
      { status: 500 }
    );
  }
  if (req.headers.get("authorization") !== `Bearer ${secret}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  return null;
}

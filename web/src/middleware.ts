import { NextRequest, NextResponse } from "next/server";

// HTTP Basic auth for the dashboard. Cron routes are excluded here and protect
// themselves with CRON_SECRET instead.
export function middleware(req: NextRequest) {
  const user = process.env.DASHBOARD_USER || "admin";
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) {
    return new NextResponse("DASHBOARD_PASSWORD is not configured", { status: 500 });
  }

  const header = req.headers.get("authorization") ?? "";
  if (header.startsWith("Basic ")) {
    const [u, p] = atob(header.slice(6)).split(":");
    if (u === user && p === password) return NextResponse.next();
  }
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="AdGeniePro"' },
  });
}

export const config = {
  matcher: [
    "/((?!api/cron|api/webhooks|api/leads|p/|_next/static|_next/image|favicon.ico).*)",
  ],
};

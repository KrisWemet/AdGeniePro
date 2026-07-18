import { createDecipheriv, createHash } from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { logAction } from "@/lib/db";
import { promoteLeadToBuyer } from "@/lib/funnel";

// ClickBank Instant Notification Service (INS v6): encrypted JSON payload
// {"notification": base64, "iv": base64}, AES-256-CBC, key = first 32 hex
// chars of SHA-1 of your INS secret. Configure the same secret in the
// ClickBank vendor settings and in CLICKBANK_INS_SECRET.
export async function POST(req: NextRequest) {
  const secret = process.env.CLICKBANK_INS_SECRET;
  if (!secret) {
    return NextResponse.json({ error: "CLICKBANK_INS_SECRET not set" }, { status: 500 });
  }
  try {
    const { notification, iv } = (await req.json()) as {
      notification: string;
      iv: string;
    };
    const key = createHash("sha1").update(secret).digest("hex").slice(0, 32);
    const decipher = createDecipheriv(
      "aes-256-cbc",
      Buffer.from(key, "utf8"),
      Buffer.from(iv, "base64")
    );
    const decrypted = Buffer.concat([
      decipher.update(Buffer.from(notification, "base64")),
      decipher.final(),
    ]).toString("utf8");
    // Payload is JSON, sometimes null-padded.
    const event = JSON.parse(decrypted.replace(/\0+$/, "")) as {
      transactionType?: string;
      customer?: { billing?: { email?: string } };
    };

    const type = event.transactionType ?? "";
    const email = event.customer?.billing?.email;

    if ((type === "SALE" || type === "TEST_SALE") && email) {
      await promoteLeadToBuyer(email, "buyer");
      await logAction({
        actor: "funnel",
        action: "clickbank_sale",
        rationale: `INS ${type} received — buyer sequence triggered for ${email}`,
      });
    }

    return NextResponse.json({ ok: true });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logAction({
      actor: "funnel",
      action: "clickbank_webhook_failed",
      rationale: message,
    });
    return NextResponse.json({ error: message }, { status: 400 });
  }
}

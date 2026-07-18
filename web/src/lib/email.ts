import { marked } from "marked";
import { unsubscribeSignature } from "./funnel";

// Sends one email through the Resend HTTP API. Placeholders supported in
// subject/body: {{first_name}}, {{product_link}}, {{clickbank_link}},
// {{unsubscribe_link}}.
export async function sendEmail(opts: {
  to: string;
  toName: string | null;
  subject: string;
  bodyMd: string;
  productLink: string;
  clickbankLink: string;
}): Promise<void> {
  const apiKey = process.env.RESEND_API_KEY;
  const from = process.env.EMAIL_FROM;
  if (!apiKey || !from) {
    throw new Error("RESEND_API_KEY and EMAIL_FROM must be set to send email");
  }
  const base = process.env.APP_BASE_URL ?? "";
  const unsubscribe = `${base}/api/leads/unsubscribe?email=${encodeURIComponent(
    opts.to
  )}&sig=${unsubscribeSignature(opts.to)}`;

  const fill = (s: string) =>
    s
      .replaceAll("{{first_name}}", opts.toName?.split(" ")[0] ?? "there")
      .replaceAll("{{product_link}}", opts.productLink)
      .replaceAll("{{clickbank_link}}", opts.clickbankLink)
      .replaceAll("{{unsubscribe_link}}", unsubscribe);

  const bodyHtml = await marked.parse(fill(opts.bodyMd));
  const html = `${bodyHtml}<hr style="margin-top:32px;border:none;border-top:1px solid #ddd"/><p style="font-size:12px;color:#888">You're receiving this because you opted in. <a href="${unsubscribe}">Unsubscribe</a></p>`;

  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from,
      to: [opts.to],
      subject: fill(opts.subject),
      html,
    }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Resend API HTTP ${res.status}: ${text.slice(0, 300)}`);
  }
}

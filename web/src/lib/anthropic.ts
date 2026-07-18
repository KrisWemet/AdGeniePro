import Anthropic from "@anthropic-ai/sdk";

const MODEL = process.env.ANTHROPIC_MODEL || "claude-opus-4-8";

let client: Anthropic | null = null;
function anthropic(): Anthropic {
  if (!client) client = new Anthropic(); // reads ANTHROPIC_API_KEY
  return client;
}

function firstText(msg: Anthropic.Message): string {
  const block = msg.content.find((b) => b.type === "text");
  if (!block || block.type !== "text") throw new Error("no text block in Claude response");
  return block.text;
}

export interface CandidateProduct {
  vendor_id: string;
  title: string;
  description: string;
  category: string;
  gravity: number;
  commission_pct: number;
  avg_dollars_per_sale: number;
}

export interface ProductScore {
  vendor_id: string;
  score: number; // 0-100 sellability via paid social
  rationale: string;
}

const scoreSchema = {
  type: "object",
  properties: {
    scores: {
      type: "array",
      items: {
        type: "object",
        properties: {
          vendor_id: { type: "string" },
          score: { type: "integer" },
          rationale: { type: "string" },
        },
        required: ["vendor_id", "score", "rationale"],
        additionalProperties: false,
      },
    },
  },
  required: ["scores"],
  additionalProperties: false,
} as const;

export async function scoreProducts(
  candidates: CandidateProduct[]
): Promise<ProductScore[]> {
  const msg = await anthropic().messages.create({
    model: MODEL,
    max_tokens: 8000,
    system:
      "You are a performance-marketing analyst evaluating affiliate products for paid Facebook/Instagram traffic. " +
      "Score each product 0-100 for how likely a small advertiser can run it profitably: favor broad-appeal problems, " +
      "clear value propositions, strong commissions ($30+ per sale), and offers that comply with Meta ad policies. " +
      "Penalize health claims, income claims, and anything likely to be rejected by Meta review. Be blunt in rationales.",
    messages: [
      {
        role: "user",
        content:
          "Score these ClickBank products:\n\n" +
          JSON.stringify(candidates, null, 2),
      },
    ],
    output_config: { format: { type: "json_schema", schema: scoreSchema } },
  });
  const parsed = JSON.parse(firstText(msg)) as { scores: ProductScore[] };
  return parsed.scores;
}

export interface AdVariant {
  angle: string;
  headline: string; // <= 40 chars
  primary_text: string;
  description: string; // <= 30 chars
}

const adCopySchema = {
  type: "object",
  properties: {
    variants: {
      type: "array",
      items: {
        type: "object",
        properties: {
          angle: { type: "string" },
          headline: { type: "string" },
          primary_text: { type: "string" },
          description: { type: "string" },
        },
        required: ["angle", "headline", "primary_text", "description"],
        additionalProperties: false,
      },
    },
  },
  required: ["variants"],
  additionalProperties: false,
} as const;

export async function generateAdCopy(product: {
  title: string;
  description: string | null;
  category: string | null;
}): Promise<AdVariant[]> {
  const msg = await anthropic().messages.create({
    model: MODEL,
    max_tokens: 8000,
    system:
      "You write direct-response Facebook ad copy. Produce 3 variants with distinct angles " +
      "(e.g. problem-agitation, curiosity, social proof). Headlines must be 40 characters or fewer, " +
      "descriptions 30 characters or fewer, primary text 2-4 short sentences. " +
      "Strictly comply with Meta ad policies: no personal attributes ('Are you struggling with your weight?'), " +
      "no income or health-result promises, no before/after framing.",
    messages: [
      {
        role: "user",
        content:
          `Write ad copy for this product:\nTitle: ${product.title}\n` +
          `Category: ${product.category ?? "unknown"}\nDescription: ${product.description ?? "n/a"}`,
      },
    ],
    output_config: { format: { type: "json_schema", schema: adCopySchema } },
  });
  const parsed = JSON.parse(firstText(msg)) as { variants: AdVariant[] };
  return parsed.variants;
}

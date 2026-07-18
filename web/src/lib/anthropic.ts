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

export interface OwnProductDraft {
  title: string;
  slug: string;
  summary: string;
  gap_rationale: string;
  price_dollars: number;
  content_md: string;
}

const ownProductSchema = {
  type: "object",
  properties: {
    title: { type: "string" },
    slug: { type: "string" },
    summary: { type: "string" },
    gap_rationale: { type: "string" },
    price_dollars: { type: "number" },
    content_md: { type: "string" },
  },
  required: ["title", "slug", "summary", "gap_rationale", "price_dollars", "content_md"],
  additionalProperties: false,
} as const;

// Generates a complete companion digital product for a ClickBank offer.
// "upsell": sold post-purchase to buyers, filling a gap the main product
// leaves open. "tripwire": a low-priced stepping stone for non-buyers that
// delivers a quick win and naturally leads back to the main offer.
export async function generateOwnProduct(
  product: { title: string; description: string | null; category: string | null },
  kind: "upsell" | "tripwire"
): Promise<OwnProductDraft> {
  const brief =
    kind === "upsell"
      ? "Design a COMPANION product for people who just BOUGHT the main product: find the biggest gap the main product leaves open (implementation, tooling, accountability, advanced tactics) and fill it. Price $27-$67."
      : "Design a STEPPING-STONE product for people who saw the main product but did NOT buy: a small, low-priced ($5-$17) quick win that solves the first painful sub-problem and makes buying the main product the obvious next step.";
  const stream = anthropic().messages.stream({
    model: MODEL,
    max_tokens: 32000,
    system:
      "You are a digital product strategist and writer. You identify product-market gaps around existing offers and write complete, genuinely useful digital products (guides/workbooks) in Markdown. " +
      "The content must stand on its own merit — real substance, actionable steps, no filler, no fabricated testimonials or statistics, no income or health-outcome promises. " +
      "content_md must be a complete 2500-4000 word product with clear sections, checklists, and templates. slug must be lowercase-kebab-case.",
    messages: [
      {
        role: "user",
        content:
          `${brief}\n\nMain product:\nTitle: ${product.title}\nCategory: ${product.category ?? "unknown"}\nDescription: ${product.description ?? "n/a"}`,
      },
    ],
    output_config: { format: { type: "json_schema", schema: ownProductSchema } },
  });
  const msg = await stream.finalMessage();
  return JSON.parse(firstText(msg)) as OwnProductDraft;
}

export interface EmailStep {
  step_number: number;
  delay_hours: number;
  subject: string;
  body_md: string;
}

const sequenceSchema = {
  type: "object",
  properties: {
    name: { type: "string" },
    steps: {
      type: "array",
      items: {
        type: "object",
        properties: {
          step_number: { type: "integer" },
          delay_hours: { type: "integer" },
          subject: { type: "string" },
          body_md: { type: "string" },
        },
        required: ["step_number", "delay_hours", "subject", "body_md"],
        additionalProperties: false,
      },
    },
  },
  required: ["name", "steps"],
  additionalProperties: false,
} as const;

// Writes the email sequence that sells an own-product. Buyer sequences pitch
// the upsell after a purchase; non-buyer sequences deliver value, pitch the
// tripwire, then bridge back to the ClickBank offer.
export async function generateEmailSequence(
  ownProduct: { title: string; summary: string | null; kind: string },
  mainProduct: { title: string },
  audience: "buyers" | "non_buyers"
): Promise<{ name: string; steps: EmailStep[] }> {
  const brief =
    audience === "buyers"
      ? `Write a 4-email post-purchase sequence for people who just bought "${mainProduct.title}". Email 1 (delay 1h): congratulate, set expectations, one quick-start tip. Emails 2-4 (spread over 5 days): deliver value, then introduce the companion product "{{product_link}}" as the natural next step.`
      : `Write a 5-email sequence for people who opted in but did NOT buy "${mainProduct.title}". Emails 1-2: pure value on the underlying problem. Email 3: introduce the low-priced stepping-stone product at {{product_link}}. Email 4: address objections. Email 5: bridge to the main offer at {{clickbank_link}}.`;
  const msg = await anthropic().messages.create({
    model: MODEL,
    max_tokens: 16000,
    system:
      "You write conversational, non-hypey email marketing copy in Markdown. Short paragraphs, one idea per email, one clear call to action. " +
      "Use the placeholders {{first_name}}, {{product_link}}, {{clickbank_link}} exactly where appropriate — never invent URLs. " +
      "No fabricated results, testimonials, or income/health claims. delay_hours is the gap since the PREVIOUS email.",
    messages: [
      {
        role: "user",
        content: `${brief}\n\nProduct being sold in this sequence: "${ownProduct.title}" (${ownProduct.kind}) — ${ownProduct.summary ?? ""}`,
      },
    ],
    output_config: { format: { type: "json_schema", schema: sequenceSchema } },
  });
  return JSON.parse(firstText(msg)) as { name: string; steps: EmailStep[] };
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

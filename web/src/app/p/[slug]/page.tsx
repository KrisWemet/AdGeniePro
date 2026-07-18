import { marked } from "marked";
import { notFound } from "next/navigation";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

// Public delivery page for an own-product. Linked from sequence emails and
// (for paid products) from the post-checkout redirect.
export default async function ProductPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const [product] = await sql()<
    Array<{ title: string; content_md: string | null; status: string }>
  >`select title, content_md, status from own_products where slug = ${slug}`;
  if (!product || product.status === "draft" || !product.content_md) notFound();

  const html = await marked.parse(product.content_md);

  return (
    <article className="prose prose-invert max-w-3xl mx-auto py-8">
      <div dangerouslySetInnerHTML={{ __html: html }} />
    </article>
  );
}

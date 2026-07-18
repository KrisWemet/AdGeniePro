import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "AdGeniePro",
  description: "AI-run affiliate ad campaigns with hard budget guardrails",
};

const nav = [
  { href: "/", label: "Overview" },
  { href: "/products", label: "Products" },
  { href: "/campaigns", label: "Campaigns" },
  { href: "/activity", label: "AI Activity" },
  { href: "/settings", label: "Settings" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-zinc-800 px-6 py-4 flex items-center gap-8">
          <span className="font-bold text-lg tracking-tight">
            AdGenie<span className="text-emerald-400">Pro</span>
          </span>
          <nav className="flex gap-5 text-sm text-zinc-400">
            {nav.map((n) => (
              <Link key={n.href} href={n.href} className="hover:text-zinc-100">
                {n.label}
              </Link>
            ))}
          </nav>
        </header>
        <main className="p-6 max-w-6xl mx-auto">{children}</main>
      </body>
    </html>
  );
}

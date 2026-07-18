import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

interface ActionRow {
  id: string;
  actor: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  rationale: string | null;
  created_at: string;
}

export default async function Activity() {
  const actions = await sql()<ActionRow[]>`
    select * from ai_actions order by created_at desc limit 200`;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">AI activity log</h1>
      <p className="text-sm text-zinc-400">
        Every decision the system makes — launches, budget changes, pauses, blocked
        actions, and errors — with its rationale. Nothing happens off the record.
      </p>
      <div className="space-y-2">
        {actions.length === 0 && (
          <div className="rounded border border-zinc-800 bg-zinc-900 p-6 text-zinc-400 text-sm">
            No activity yet.
          </div>
        )}
        {actions.map((a) => (
          <div key={a.id} className="rounded border border-zinc-800 bg-zinc-900 p-3 text-sm">
            <div className="flex items-center gap-2 text-xs text-zinc-500">
              <span className="rounded bg-zinc-800 px-1.5 py-0.5">{a.actor}</span>
              <span className="font-mono">{a.action}</span>
              <span className="ml-auto">{new Date(a.created_at).toLocaleString()}</span>
            </div>
            {a.rationale && <div className="mt-1 text-zinc-300">{a.rationale}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

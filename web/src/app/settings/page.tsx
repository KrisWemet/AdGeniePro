import { getSettings } from "@/lib/db";

export const dynamic = "force-dynamic";

export default async function Settings() {
  const s = await getSettings();

  return (
    <div className="max-w-lg space-y-4">
      <h1 className="text-xl font-semibold">Guardrails &amp; autonomy</h1>
      <form action="/api/settings" method="post" className="space-y-4">
        <label className="block text-sm">
          <span className="text-zinc-400">Daily budget cap (USD, all campaigns)</span>
          <input
            name="daily_cap_dollars"
            type="number"
            step="0.01"
            defaultValue={(s.daily_budget_cap_cents / 100).toFixed(2)}
            className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 p-2"
          />
        </label>
        <label className="block text-sm">
          <span className="text-zinc-400">Lifetime spend cap (USD)</span>
          <input
            name="total_cap_dollars"
            type="number"
            step="0.01"
            defaultValue={(s.total_budget_cap_cents / 100).toFixed(2)}
            className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 p-2"
          />
        </label>
        <label className="block text-sm">
          <span className="text-zinc-400">Target ROAS (scale winners above this)</span>
          <input
            name="target_roas"
            type="number"
            step="0.1"
            defaultValue={s.target_roas}
            className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 p-2"
          />
        </label>
        <label className="block text-sm">
          <span className="text-zinc-400">Autonomy mode</span>
          <select
            name="autonomy_mode"
            defaultValue={s.autonomy_mode}
            className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 p-2"
          >
            <option value="approve">Approve — AI builds, you click launch</option>
            <option value="auto">Auto — AI launches &amp; adjusts within caps</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm text-red-300">
          <input type="checkbox" name="kill_switch" defaultChecked={s.kill_switch} />
          Kill switch — stop all delivery and freeze the AI
        </label>
        <button className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium hover:bg-emerald-500">
          Save
        </button>
      </form>
    </div>
  );
}

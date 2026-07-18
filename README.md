# AdGeniePro

AI-run affiliate ad campaigns with hard budget guardrails.

- **`web/`** — the application: a Next.js + Supabase dashboard and agent
  pipeline that discovers high-commission ClickBank products, has Claude score
  them and write Meta ad creatives, launches campaigns (paused-first, gated by
  budget caps and a kill switch), and optimizes them on a schedule with a full
  audit trail of every AI decision. See [`web/README.md`](web/README.md) for
  setup and deployment.
- Root `*.py` files — the original Python agent prototypes, kept for reference.
- Root `*.html` files — the GitHub Pages site (privacy policy, terms, data
  deletion), also a good place to host ad landing/bridge pages.

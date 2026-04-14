# AVE Studio (Frontend)

Next.js frontend for the Agentic Video Editor. See the [root README](../../../README.md) for full project documentation.

## Development

```bash
pnpm install
pnpm dev --port 3000
```

Requires the FastAPI backend running on port 8000. API calls are proxied via `next.config.ts` rewrites.

## Tech Stack

- Next.js 16, React 19, TypeScript
- Tailwind CSS 4
- Zustand (state management)
- @dnd-kit (drag-and-drop timeline)
- Recharts (review radar chart)
- Lucide (icons)

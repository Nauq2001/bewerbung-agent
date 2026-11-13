# bewerbung-agent

A multi-agent assistant for job applications in Germany: it reads your CV,
finds postings, scores them against your actual profile, drafts the CV and
Anschreiben for the ones worth applying to, and coaches you for the interview.

API-first and provider-agnostic. Early development — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

> Written in German/Vietnamese context, built for the Rhein-Main job market,
> but nothing in the framework is tied to a region.

## The five agents

| # | Agent | Does | Triggered by |
|---|---|---|---|
| 1 | CV Reader | Your documents → a structured profile | You upload a file |
| 2 | Job Finder | Keeps the job table fresh | Cron |
| 3 | Evaluator | Scores a posting against your profile | After ingest |
| 4 | Document Writer | Targeted CV + cover letter | You decide to apply |
| 5 | Interview Coach | Company research, situational questions, practice | Tracker hits `interview` |

Each agent's model is a row in `agent_configs`, not a constant in the code —
so changing model or provider is an `UPDATE`, not a deploy.

## Two design rules worth knowing before you read the code

**Nothing is written that your documents do not support.** A generated bullet
traces back to a source chunk, or it does not ship. A CV that invents a skill
is what loses you the job, not what wins it.

**`WHERE` beats a prompt.** Commute distance, posting age, contract type and
weekly hours are SQL filters. The model is for judgment, not for arithmetic
it will occasionally get wrong at a thousandth of the cost.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Provider layer, source layer, Agents 1 + 3, testable via `/docs` | in progress |
| 2 | Agent 4, LaTeX pipeline, tracker + per-application roadmap | planned |
| 3 | React frontend | planned |
| 4 | Agent 5, interview practice | planned |
| 5 | More sources, health checks | planned |
| 6 | Multi-user: Supabase Auth, RLS | planned |
| 7 | Retrieval layer (pgvector) | planned |

What works today: the provider abstraction, the source layer with its tier
boundary, and the Arbeitnow connector. 45 contract tests in `backend/tests/`.

## Quick start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # the core tests need no API key and no database
uvicorn app.main:app --reload
```

Then open <http://localhost:8000/docs> and try `GET /sources` and
`POST /sources/arbeitnow/search`. Arbeitnow needs no API key, so a fresh clone
runs with no setup.

## Job sources and the tier boundary

Sources come in two tiers, and the split is a legal boundary rather than a
feature flag:

- **`public`** — the provider's terms permit programmatic access. Arbeitnow
  ships with the repo; Adzuna and the public ATS feeds (Greenhouse, Personio,
  SmartRecruiters, Join) are planned.
- **`personal`** — no such permission. **Not included in this repository.**
  The framework, the interface and the enforcement are public; adapters for
  boards that did not permit this are not. The deployment owner adds their own
  under `backend/app/sources/personal/` (gitignored) — see
  [that directory's README](backend/app/sources/personal/README.md).

Only a user whose `is_owner` column is true can run a personal-tier source,
and that is enforced in `app.sources.base.enabled_sources`, the one place that
builds a runnable source list. Not in a route, not in the UI.

## Using a different LLM

Two adapters ship: Anthropic and OpenAI. The OpenAI one also covers any
OpenAI-compatible endpoint (Ollama, Together, Groq, vLLM) through `base_url`.

To run everything on OpenAI:

```bash
export OPENAI_API_KEY=sk-...
psql "$DATABASE_URL" -c "UPDATE agent_configs SET provider='openai', model='gpt-5.5'"
```

That is the whole switch - model choice is a table row, not a constant.

What changes when you do: OpenAI declares only `STRUCTURED_OUTPUT` here, so
prompt caching and batching are unavailable (**the bill goes up**, the
features do not disappear), and PDFs are extracted locally with `pypdf`
instead of being read by the model (**CV parsing gets less accurate** on
multi-column layouts). Agent 5's company research needs a web-search
capability this adapter does not implement yet.

To add a third provider: write an adapter satisfying `LLMProvider` in
[`backend/app/llm/base.py`](backend/app/llm/base.py), declare which
`Capability` values it supports, register it in
[`factory.py`](backend/app/llm/factory.py), and point the relevant
`agent_configs` row at it. Capabilities a provider lacks are emulated in
[`degrade.py`](backend/app/llm/degrade.py) — no structured output means
schema-in-prompt plus validation; no PDF input means local text extraction.
The feature set does not shrink, the bill and sometimes the accuracy change.

The rule the tests enforce: branch on `provider.supports(cap)`, never on
`provider.name`.

## Credits

The domain logic — the job-evaluation framework, the drafter/reviewer pattern,
the LaTeX templates and the PDF verification tooling — grew out of
[MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search),
a local-first Claude Code workspace, MIT licensed. This project reimplements
those ideas as a web application.

## License

MIT — see [LICENSE](LICENSE).

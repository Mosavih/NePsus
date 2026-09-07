# Configuration

The pipeline runs against a local LLM router (any OpenAI-compatible endpoint,
e.g. a self-hosted multi-provider gateway) plus Telegram for delivery.

## Environment variables (create `.env`)

| Variable | Purpose |
|---|---|
| `ROUTER_BASE_URL` | OpenAI-compatible router endpoint |
| `ROUTER_API_KEY` | Router API key |
| `NEXUS_BOT_TOKEN` | Telegram bot token (delivery/review flow) |
| `ROUTER_MODEL` | Default model override |
| `ROUTER_CALL_TIMEOUT` | Per-call timeout, seconds (default 300) |
| `ROUTER_MAX_ATTEMPTS` | Total retry budget across routes (default 6) |
| `ROUTER_FALLBACK` | Combo fallback order |
| `ROUTER_COMBO_*` | Per-task combo assignment (extract, events, fast, draft, compose, cards, …) |

## Task → combo routing

Every LLM call declares a *task* (`extract`, `draft`, `compose`, `cards`, …).
Tasks map to provider-model combos (e.g. `Mechanical`, `Complicated`) via
`src/investigation_layer/models.py`; the panel's LLM-routes page reassigns
them live. Each call walks healthy routes first — failures are recorded in a
shared health ledger (`var/route_health.json`) and failing routes are
quarantined automatically.

## Running

```bash
python eval/discover.py --max-new 3     # collect news, propose problems
python eval/run_v03.py <problem_id> --no-send   # compose + QC, no delivery
python eval/nexus_bot.py                # Telegram review bot
python panel/app.py                     # local control room on :8001
```

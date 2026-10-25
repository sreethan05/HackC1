# Token Usage and Cost Analysis — Final Full-Dataset Run

This report documents the model usage of the run that produced the submitted
`output.csv` (518 requests from `dataset/deletion_requests.csv`).

## Runtime architecture (what actually executes)

The submitted pipeline is a **hybrid deterministic + AI system**:

| Stage | Execution | Model usage |
|---|---|---|
| CSV/SQL/JSON ingestion, 9 log syntaxes | deterministic Python | none |
| Identity resolution (current + historical) | deterministic | none |
| Deletion-intent classification (multilingual) | deterministic rules; optional LLM fallback hook | none on this corpus (100% rule coverage measured over all 518 requests + 32 samples) |
| PDF evidence (4 files) | deterministic text extraction (`pdfplumber`, with a dependency-free fallback) | none |
| Image evidence (11 console screenshots) | vision-model fact extraction, **cached** at `code/cache/image_facts.json` | see "Development-time model usage" |
| Policy engine, temporal reconstruction, workflow-claim association (exact min-cost matching), step planning, ordering | deterministic Python | none |
| Validators | deterministic | none |

## Model calls in the final run

| Provider | Model | Calls | Input tokens | Output tokens |
|---|---|---:|---:|---:|
| (none — no external model API was called at run time) | — | 0 | 0 | 0 |
| **Total** | | **0** | **0** | **0** |

- Total tokens per request: **0**
- Average tokens per request: **0**
- Estimated total cost: **$0.00**
- Estimated cost per request: **$0.00**

No API keys are required to reproduce the run: `python code/main.py` executes
entirely offline against `dataset/`. The optional LLM/VLM layer
(`code/src/llm.py`, configuration via environment variables) activates only
when credentials are present; with no credentials it is skipped and the cached
image facts are used.

## Cached image facts

The runtime reads fact tuples from `code/cache/image_facts.json` for image
evidence, so the final run makes no vision-model calls. The cache does not
record provider, model, call counts, or token usage for when its contents were
created; those development-time values cannot be reconstructed reliably and
are not attributed to the final full-dataset run. If the cache is absent and
an LLM endpoint is configured, image extraction may call the configured
provider; such calls are not part of the reported zero-call run.

## Cost accounting statement

Because the final full-dataset run makes zero external model calls, the
verified token totals and estimated cost are zero. This disclosure follows the
challenge guidance to report non-token-based processing accurately rather
than inventing token counts.

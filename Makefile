# Shortcuts for the uv commands in README.md. `make` alone lists them.
# Nothing here submits an application: submission only happens from `make ui`.

TOP ?= 30
JOB ?=
UV  := uv run

.DEFAULT_GOAL := help
.PHONY: help setup install browsers models init test test-fast \
        fetch filter score tailor prefill outreach daily ui lca clean-cache

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'
	@printf '\n  Variables: TOP=%s (tailor/prefill/daily), JOB=<id> (tailor/prefill one job), FILE=<csv> (lca)\n' "$(TOP)"

# --- setup --------------------------------------------------------------------

setup: install browsers models init ## Everything a fresh checkout needs

install: ## Install Python deps (incl. dev)
	uv sync --extra dev

browsers: ## Install Playwright Chromium (prefill/submit)
	$(UV) playwright install chromium

models: ## Pull the Ollama models from settings.yaml
	ollama pull qwen2.5:7b-instruct
	ollama pull nomic-embed-text

init: ## Create the DB and copy example configs (never overwrites)
	$(UV) jobpilot init

# --- tests --------------------------------------------------------------------

test: ## Full test suite (no network)
	$(UV) pytest

test-fast: ## Skip the browser and Streamlit tests
	$(UV) pytest -q --deselect tests/test_apply.py --deselect tests/test_hn_ui.py

# --- pipeline -------------------------------------------------------------------

fetch: ## Pull every active board (and HN if hn.enabled)
	$(UV) jobpilot fetch

filter: ## Dedupe, rules, visa screen
	$(UV) jobpilot filter

score: ## Embed + LLM rerank; prints the ranked table
	$(UV) jobpilot score

tailor: ## Tailored resumes for the top TOP jobs (or JOB=<id>)
	$(UV) jobpilot tailor $(if $(JOB),--job $(JOB),--top $(TOP))

prefill: ## Fill forms and STOP before submit (TOP or JOB=<id>)
	$(UV) jobpilot prefill $(if $(JOB),--job $(JOB),--top $(TOP))

outreach: ## Draft messages for HN / manual-apply jobs
	$(UV) jobpilot draft-outreach

daily: ## fetch -> filter -> score -> tailor -> prefill (never submits)
	$(UV) jobpilot run-daily --top $(TOP)

ui: ## Review app; the only place to Approve & Submit
	$(UV) jobpilot ui

lca: ## Import a DOL LCA CSV: make lca FILE=path/to/file.csv
	@test -n "$(FILE)" || { echo "usage: make lca FILE=path/to/LCA.csv"; exit 1; }
	$(UV) jobpilot import-lca "$(FILE)"

# --- housekeeping ---------------------------------------------------------------

clean-cache: ## Delete the HTTP response cache (.cache/); DB and outputs are kept
	rm -rf .cache

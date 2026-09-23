# One command: `make` sets up whatever is missing and opens the UI. Run and watch every
# pipeline stage from its Pipeline page. `make daily` is the no-UI equivalent.
# `make help` lists the individual targets.
# Nothing here submits an application: submission only happens from the review UI.

TOP     ?= 30
JOB     ?=
UV      := uv run
PY      := $(UV) python

# Stamps live in .venv (gitignored) so `uv sync` / browser installs rerun only when needed.
SYNCED  := .venv/.jobpilot-synced
BROWSER := .venv/.jobpilot-chromium

.DEFAULT_GOAL := all
.PHONY: all help setup install browsers models ollama-up init doctor ready test test-fast ci \
        fetch filter score tailor prefill outreach daily ui lca clean-cache clean

# --- the one command ------------------------------------------------------------

all: setup ## Set up, then open the UI; run and watch every stage from its Pipeline page
	@echo
	@echo "Opening JobPilot. Start stages from the Pipeline page; approve & submit happens"
	@echo "in the Review queue, one job at a time."
	$(UV) jobpilot ui

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'
	@printf '\n  Variables: TOP=%s  JOB=<id> (tailor/prefill one job)  FILE=<csv> (lca)\n' "$(TOP)"

# --- setup (idempotent; safe to rerun) ------------------------------------------

setup: doctor install browsers models init ## Everything a fresh checkout needs

doctor: ## Check the external tools are installed
	@missing=0; \
	for t in uv typst; do \
	  command -v $$t >/dev/null || { echo "missing: $$t  (brew install $$t)"; missing=1; }; \
	done; \
	if [ "$$(sed -n 's/^ *provider: *\([a-z_]*\).*/\1/p' config/settings.yaml)" = ollama ]; then \
	  command -v ollama >/dev/null || { echo "missing: ollama  (brew install ollama)"; missing=1; }; \
	fi; \
	exit $$missing

install: $(SYNCED) ## Install Python deps (incl. dev); reruns only when the lockfile changes

$(SYNCED): pyproject.toml uv.lock
	uv sync --extra dev
	@touch $@

browsers: $(BROWSER) ## Install Playwright Chromium (prefill/submit)

$(BROWSER): $(SYNCED)
	$(UV) playwright install chromium
	@touch $@

ollama-up: ## Start the Ollama server if it is not running
	@ollama list >/dev/null 2>&1 && exit 0; \
	echo "starting ollama..."; \
	if command -v brew >/dev/null; then brew services start ollama >/dev/null; \
	else mkdir -p logs; nohup ollama serve >logs/ollama.log 2>&1 & fi; \
	for i in $$(seq 1 30); do ollama list >/dev/null 2>&1 && exit 0; sleep 1; done; \
	echo "ollama did not come up after 30s"; exit 1

models: $(SYNCED) ## Pull the models named in settings.yaml (skips ones already present)
	@provider=$$($(PY) -c 'from jobpilot import config; print(config.settings()["llm"]["provider"])'); \
	if [ "$$provider" != ollama ]; then echo "llm.provider is $$provider; skipping ollama"; exit 0; fi; \
	$(MAKE) --no-print-directory ollama-up || exit 1; \
	have=$$(ollama list | awk 'NR>1 {print $$1}'); \
	for m in $$($(PY) -c 'from jobpilot import config; l = config.settings()["llm"]; print(l["text_model"], l["embed_model"])'); do \
	  if printf '%s\n' "$$have" | grep -qFx -e "$$m" -e "$$m:latest"; then echo "model $$m present"; \
	  else ollama pull "$$m" || exit 1; fi; \
	done

init: $(SYNCED) ## Create the DB and copy example configs (never overwrites)
	$(UV) jobpilot init

# Refuse to run the pipeline on the fictional example resume: every tailored PDF would be
# about "Alex Example". Empty answers only cost prefill coverage, so they just warn.
ready: ## Check your resume and answers have been filled in
	@if cmp -s data/master_resume.yaml config/master_resume.example.yaml; then \
	  echo "data/master_resume.yaml is still the example -- replace it with your real resume."; exit 1; fi
	@if cmp -s config/answers.yaml config/answers.example.yaml; then \
	  echo "warning: config/answers.yaml is still the template; prefill will leave fields for you."; fi

# --- tests --------------------------------------------------------------------

test: $(BROWSER) ## Full test suite (no network)
	$(UV) pytest

test-fast: $(SYNCED) ## Skip the browser and Streamlit tests
	$(UV) pytest -q --deselect tests/test_apply.py --deselect tests/test_hn_ui.py

ci: doctor test ## Install what tests need, then run them (no Ollama required)

# --- pipeline -------------------------------------------------------------------

fetch: $(SYNCED) ## Pull every active board (and HN if hn.enabled)
	$(UV) jobpilot fetch

filter: $(SYNCED) ## Dedupe, rules, visa screen
	$(UV) jobpilot filter

score: $(SYNCED) ## Embed + LLM rerank; prints the ranked table
	$(UV) jobpilot score

tailor: $(SYNCED) ## Tailored resumes for the top TOP jobs (or JOB=<id>)
	$(UV) jobpilot tailor $(if $(JOB),--job $(JOB),--top $(TOP))

prefill: $(BROWSER) ## Fill forms and STOP before submit (TOP or JOB=<id>)
	$(UV) jobpilot prefill $(if $(JOB),--job $(JOB),--top $(TOP))

outreach: $(SYNCED) ## Draft messages for HN / manual-apply jobs
	$(UV) jobpilot draft-outreach

daily: $(BROWSER) ready ## fetch -> filter -> score -> tailor -> prefill (never submits)
	$(UV) jobpilot run-daily --top $(TOP)

ui: $(SYNCED) ## Review app; the only place to Approve & Submit
	$(UV) jobpilot ui

lca: $(SYNCED) ## Import a DOL LCA CSV: make lca FILE=path/to/file.csv
	@test -n "$(FILE)" || { echo "usage: make lca FILE=path/to/LCA.csv"; exit 1; }
	$(UV) jobpilot import-lca "$(FILE)"

# --- housekeeping ---------------------------------------------------------------

clean-cache: ## Delete the HTTP response cache (.cache/); DB and outputs are kept
	rm -rf .cache

clean: clean-cache ## Also drop .venv and test caches (your DB, configs, and outputs are kept)
	rm -rf .venv .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

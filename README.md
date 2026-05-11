# LLMEval — Lightweight Agentic-Capability Test Suite

A minimal, local-first evaluation harness for benchmarking LLMs on **agentic tasks**:
file manipulation, research/synthesis, policy-driven decisions, and prompt-injection resistance.

Designed for macOS. Uses any **OpenAI-compatible** endpoint (OpenAI, OpenRouter, DeepSeek, Ollama, etc.).

## Quick Start

```bash
# 1. Create venv and install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your API keys

# 3. Run
python run.py --models gpt-4o
python run.py --models "deepseek/deepseek-v4-flash,openrouter/qwen/qwen3.6-flash"
python run.py --list-tasks
```

## Configuration

### API Keys — Provider System

LLMEval supports multiple API providers simultaneously. Each model can use a different
API key and base URL via **named providers**.

**Provider resolution** (env vars, checked in order):
1. `LLMEVAL_<NAME>_API_KEY` — per-provider key
2. `LLMEVAL_API_KEY` — global default fallback

Same pattern for base URLs: `LLMEVAL_<NAME>_BASE_URL`, then built-in defaults.

> Note: `--base-url` / `LLMEVAL_BASE_URL` only affects the **default** provider.
> Named providers (deepseek, openrouter) always use their own base URLs
> unless overridden via `LLMEVAL_<NAME>_BASE_URL`.

**Built-in provider defaults:**

| Provider | Default Base URL |
|---|---|
| `openai` | `https://api.openai.com/v1` |
| `deepseek` | `https://api.deepseek.com` |
| `openrouter` | `https://openrouter.ai/api/v1` |

### Specifying Models

Models use `[provider/]model_id` syntax. If no provider prefix, the **default** provider
is used (from `LLMEVAL_API_KEY` / `LLMEVAL_BASE_URL`).

```bash
# Single provider (default)
python run.py --models gpt-4o

# Multiple providers in one run
python run.py --models "deepseek/deepseek-v4-flash,openrouter/qwen/qwen3.6-flash"

# DeepSeek with thinking mode (reasoning_effort)
python run.py --models "deepseek/deepseek-v4-pro:reasoning=max"

# Manual pricing override (optional; otherwise auto-detected from API response)
python run.py --models "deepseek/deepseek-v4-flash:price=0.27,1.10"

# From .env
# LLMEVAL_MODELS=deepseek/deepseek-v4-flash,gpt-4o
python run.py
```

### Cost Tracking

Cost is **auto-detected** from the API response when the provider returns it
(OpenRouter includes a `cost` field natively). If unavailable, fall back to:
1. Manual `:price=in,out` override on the model spec
2. Display "N/A" when unknown

Token counts are always tracked regardless of cost availability.

### Filtering Tasks

```bash
# Run only filesystem and injection tasks
python run.py --tasks filesystem,injection

# Run a single category
python run.py --tasks research

# Run all tasks (default)
python run.py
```

### Repeat Runs

For non-deterministic models, repeat each task N times:

```bash
python run.py --models gpt-4o --repeat 3
```

## Task Categories

| Category | File | Tasks |
|---|---|---|
| `filesystem` | `tasks/filesystem.yaml` | Hidden file counting, largest file, file organization, config extraction |
| `research` | `tasks/research.yaml` | Cross-reference data, discrepancy detection, meeting notes, log parsing |
| `policy` | `tasks/policy.yaml` | Refund decisions, access control, expense workflows, shipping rules |
| `injection` | `tasks/injection.yaml` | Prompt injection resistance, adversarial content, data exfiltration, policy tampering |

## Output

Results are saved to `results/eval_<timestamp>_<model>.json` with:
- Per-task pass/fail status
- Full agent trajectory (thoughts, tool calls, results)
- Token counts and cost estimates
- Latency metrics

Console output shows:
- Per-task results (with `-v` / `--verbose`)
- Per-model summary table (pass rate, avg steps, avg latency, total tokens, total cost)

## Adding New Tasks

Create a YAML file in `tasks/`:

```yaml
tasks:
  - id: "my_task"
    category: "filesystem"
    description: "What this task tests"
    instruction: |
      Detailed instructions for the agent...
    scorer: "exact_match"          # or: regex_match, contains, state_check
    expected: "expected_answer"
    max_steps: 8
    setup:
      files:
        - path: "file1.txt"
          content: "content here"
        - path: "subdir/"
          is_dir: true
```

## Architecture

```
run.py                  # CLI entry point
llmeval/
├── config.py           # Provider resolution, .env loading, CLI parsing
├── client.py           # OpenAI-compatible wrapper, auto cost detection
├── tools.py            # Agent tools: list_dir, read_file, write_file, bash
├── sandbox.py          # Temporary sandbox directories for task isolation
├── scorers.py          # Scoring: exact, regex, contains, state check
├── runner.py           # Core agent loop per task
└── reporter.py         # Console output and JSON export
tasks/                  # YAML task manifests
results/                # JSON result files
```

## License

MIT

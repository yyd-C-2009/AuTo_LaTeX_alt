# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this project is

AuTo_LaTeX is a personal prototype of an LLM "agent" that (1) OCRs images/PDFs into text + LaTeX, (2) transcribes audio, (3) stores/retrieves "memories" in a local vector DB, and (4) exposes all of these as callable tools to an OpenAI-compatible chat model. It uses a multi-agent "Super routes to experts" pattern. It is plain Python with **no build, lint, or test setup** — there is no `requirements.txt`, `pyproject.toml`, or CI.

## Running it

```bash
python super.py      # multi-agent system: Super REPL, routes to expert sub-agents
python terminal.py   # direct-dialogue REPL: talk to a selected agent directly
python Setup.py      # one-shot install: pip deps + downloads models (see --with-ocr / --with-listener)
```

- The API key is read from `DS_API_KEY` (super.py) — set it, don't hardcode it.
- `terminal.py` supports `/agent <name>`, `/branch`, `/db`, `/resident <start|stop|list|poke>`, `/cd`, `/pwd`, `/whoami`, `/help`, `/exit`.

There is no test runner of any kind. `listener.py`, `plan.py`, and `str_replace_editor.py` have `if __name__ == "__main__"` self-checks you can run individually; otherwise the only way to verify behavior is running `super.py` (needs the models and the API key).

## Architecture — the big picture

Four layers, plus a message bus that ties the runtime together:

**1. Tool registry + schema generation (`Tools.py`).** `Tools` holds a `tool_list` (name → callable), a `schema` list (OpenAI function-calling JSON), and per-tool `timeout`s. Register via `@Tools.registry(time_out)` decorator or `Tools.add_tool(func, time_out)`. The non-obvious part is `function_to_model`: it inspects a signature with `inspect.signature`, parses `Annotated[...]` (string descriptions + `Field`/`FieldInfo` + `annotated_types` constraints like `Gt`/`MaxLen`), and builds a Pydantic model whose `.model_json_schema()` becomes the LLM tool schema. `async_execute` runs each tool under an `asyncio.wait_for` timeout — sync functions run in `asyncio.to_thread`, coroutines awaited directly. **Two conventions to know:** (a) parameter names starting with `_` are excluded from the schema (so an LLM can't tamper with closure config — see `build_super_tools` in super.py); (b) the first line of a function's docstring is used as its LLM tool description, so docstrings on registered tools are functional, not just docs.

**2. The message bus (`event_bus.py`) — the actual dispatch layer.** `Bus` is the central runtime: all module communication goes through it, all tool execution is delegated to it. `Message` is the universal return shape with `title ∈ {Done, Submitted, Error, Event, Task_end}` and a `content` dict (`id`, `error`, `error_type`, `Task`, `args`). Key behaviors:
- **Fast vs slow tasks.** `bus.submit(func_name, **kw)` runs fast tools inline and returns a `Done`/`Error` `Message`; tools in `bus.slow_tasks` (`mark_slow`) instead return a `Submitted` `{id}` and run in the background, then are collected via `bus.poll(id)`. Experts and `transcribe_audio`/`recognize_doc` are slow.
- **Reentrancy.** `mark_reentrant` (experts) skips the `asyncio.Semaphore` when executing, so an expert that internally `submit`s again doesn't deadlock "holding the lock waiting for itself."
- **`dangerous_tools`** (`mark_dangerous`) prompt the user for `y/n` confirmation before running (`delete_memory`, `replace_memory`).
- **Events.** `emit` / `publish` (in-loop) / `emit_threadsafe` (from non-loop threads like the Listener) / `on`/`off` for callbacks — this is how the resident Agent wakes on new transcription.
- **Console IO is a mutex.** `io_print` (no input) and `io_dialog` (with input) hold an `asyncio.Lock`, so concurrent agents don't interleave stdout/input.

**3. The agent loop (`Agent.py`).** `Agent.run_agent(client, messages, model, max_step, tool_names=..., deny_tools=...)` drives the tool-call loop over a `messages` list until the model returns plain content or `max_step` iterations pass. `tool_names` sets a schema-level + **execution-level whitelist** (the real authorization — schema filtering only hides tools; the whitelist/deny are what stop an expert from invoking another expert or an out-of-scope tool). `deny_tools` rejects tools at both schema and execution level (Super uses it to keep stateful Listener tools away from direct dialogue). Slow tasks are submitted then polled; their results land in `self.delayed_results` and are surfaced to the LLM via the `view_delayed_results` tool rather than re-appended as `role:tool`.

**4. Capabilities (long-lived singletons, constructed once in `initer.init()`).**
- `Saver.py` — ChromaDB (persistent at `./my_rag_db`, cosine) + SentenceTransformer (bge-base-zh-v1.5). `add_memory` uses a deterministic MD5 doc ID so re-adding identical text overwrites; `retrieve_context` queries by embedding similarity; plus `delete_memory` / `replace_memory` (both y/n-gated via the bus).
- `Visal.py` — a single shared `Pix2Text` instance; `recognize_doc` rasterizes PDF pages (PyMuPDF @150 DPI) and OCRs them; `recognize_image` handles images. Results cached long-term as JSON under `OCR_result/` (per page for PDFs, per file for images).
- `listener.py` — `Listener` holds a Faster-Whisper model (default `medium`, int8/CPU). `transcribe_audio` (single file) plus stateful continuous listening: `start_listening` / `stop_listening` / `get_listen_result` / `clear_listen_result` (cursor-based incremental read). **These stateful tools are whitelisted only to the `listener` expert** — Super is denied them to avoid two agents polling the same transcript.
- `plan.py` — `Plan` reads/writes markdown under `plan/`: daily `plan/YYYYMMDD.md`, long-term `plan/general.md`.

**5. Orchestration (`experts.py`, `tools_registry.py`, `super.py`).** `experts.py` defines `EXPERTS`: `math` (read-only), `mathwrite` (LaTeX), `passagewrite` (structure + dialogue summary), `draw` (TikZ), `listener` (transcription), `notetaker` (resident note-taking). The value shape is a plain `(prompt, tool_names)` tuple — `resident.py` unpacks it directly as `agent_specs`, so don't turn it into a dataclass. `tools_registry.build_super_tools` turns each expert into an async tool on Super's `Tools` (marked slow + reentrant), so Super routes by calling `math_expert(task=...)` etc. Each call is **stateless** — a fresh `Agent` with a fresh message list — so the caller must pass complete context in `task`. `register_common_tools` wires all non-expert tools (memory, OCR, `write_latex`, `check_latex`/`check_tikz`, `str_replace_editor`, web tools, Plan/Listener tools). `super.py`/`terminal.py` share `build_client` / `register_common_tools` / `build_super_tools` from `tools_registry.py`, and `super.py` is now just the Super REPL entry (~96 lines). Module map: `config.py` (MODEL/BASE_URL/OUT_DIR/TIKZ_DIR/KEY_ID) · `experts.py` (prompts + whitelists + `view_expert_prompts`) · `latex_tools.py` (`write_latex`/`check_latex`/`check_tikz`/`view_theorem_style`) · `tools_registry.py` (client + tool registration + `_dialogue_context`/`build_task_context`).

**6. Resident + terminal (`resident.py`, `render.py`).** `ResidentAgent` holds one `Agent` and loops on timer + event wake (Listener transcript → wake), trimming its own history; `ResidentManager` exposes `start_resident_agent` / `stop_resident_agent` / `resident_status`. `render.py`'s `TerminalRenderer` draws an ANSI "body scroll region + fixed bottom status row + input line" — one line per category (`listener` / `debug` / `state`), full-screen redraw on TTY, plain `print` when piped/redirected. All terminal writes serialize through a `threading.Lock`; the renderer is a module-level singleton (`get_renderer`/`set_renderer`).

**7. `runtime/` — the v2 Runtime (all 5 phases landed).** Per `TASK.md` (the 5-phase v2 Runtime vision): a `runtime/` package whose core primitives are `Capability`/`CapabilityGrant`/`ToolPolicy` (capability semantic, NOT tool name), an immutable frozen-dataclass `Passport` (`principal_id`, `task_id`, `stage_id`, `grants` tuple — Agent cannot mutate/expand), and `CapabilityGateway` (`issue`/`derive`/`visible_tools`/`visible_schema`/`authorize`/`execute`). The real security boundary is `gateway.execute()`; schema filtering is only "hide". `derive()` can only narrow, never expand, parent caps. Authorization is deny-overrides-allow. The old `Passport.py`/`Workflow.py` skeletons were deleted (superseded).

   `Agent.run_agent` accepts optional `gateway=`/`passport=` kwargs: when provided, schema comes from `gateway.visible_schema(passport)` and tools run through `gateway.execute(...)`; when omitted, the original `tool_names`/`deny_tools` path runs unchanged. `runtime/gateway.py:LegacyPolicyAdapter` maps the old `tool_names=`/`deny_tools=` combo to a `Passport`. Offline tests: `python test_runtime.py` (no model/network — pure assert-style, 16 tests).

   **Phase 3/4 (`runtime/task.py`, `runtime/context.py`, `runtime/workflow.py`) are wired into `super.py`/`terminal.py`, not just data containers:** `build_task_context()` wraps each expert call in a `Task` + `TaskContext`; `build_super_tools(..., gateway)` registers a `run_workflow` tool that runs sequential `Stage`s, each deriving a narrowed Passport via `Workflow.derive_passport(gateway, parent, stage)`. `StageAbort` (in `runtime/workflow.py`) is how a stage reports an expected verification failure — `Workflow.run` marks the task `FAILED` and re-raises so callers can roll back side effects.

   **Phase 5 (Resident Workflow) has landed in `resident.py`:** `build_lecture_note_workflow()` defines the notetaker's `LectureNoteTaking` workflow (`observe` → `write_notes` → `verify_latex` → `commit_cursor`). `ResidentManager(..., gateway=..., workflows={"notetaker": ...})` injects it; `ResidentAgent._tick_workflow()` runs it. **Key invariant: `self.last_cursor` advances ONLY in the `commit_cursor` stage, i.e. only after `verify_latex` independently re-runs `check_latex` and it passes** — a failed verification raises `StageAbort` and the cursor stays put, so the same transcript replays on the next wake. `observe` is host-side (no LLM), and `write_notes` does one `run_agent` call, so a wake costs the same 1 LLM call as before. Residents configured without a gateway fall back to the legacy direct-`run_agent` path (with an explicit warning) — that path commits the cursor on LLM return and has no `check_latex` gate.

## Things that will bite you

- **Model/key are hardcoded.** `config.py` sets `MODEL = "deepseek-v4-flash"`, `BASE_URL = "https://api.deepseek.com"`, `KEY_ID = "DS_API_KEY"`. `Agent.py`'s standalone `agent()` REPL still defaults to the old USTC endpoint and `DSH_OPENAI_KEY`, `deepseek-v4-flash-ascend`. Change both for any other environment.
- **LaTeX/TikZ checks shell out to `pdflatex`.** `check_latex` (draft-mode syntax check) and `check_tikz` (compile a standalone doc) require `pdflatex` on PATH; they write to `latex_output/` and `tikz_output/`. `latex_tools.check_latex`'s success text ("语法检查通过") is a **contract** — `resident._LATEX_OK_MARK` matches on it to gate the Listener cursor, so changing the wording silently breaks the notetaker's verification gate.
- **Windows/DirectML.** `Visal.py` sets `ORT_PROVIDERS = 'DmlExecutionProvider,CPUExecutionProvider'` before importing onnxruntime and forces Pix2Text `device='cpu'` (CUDA explicitly avoided).
- **No tests, no requirements file.** Adding/pinning dependencies means introducing that convention yourself. `tmp.py` is an empty leftover file (0 bytes). `trail.py` was deleted. `python test_runtime.py` is the one test file (runtime authorization + Task/Workflow + Resident cursor transaction; assert-style, offline, 16 tests).
- **Do not double-register a tool.** `Tools.add_tool` with an already-registered name will duplicate it in `schema` (a past bug; `register_common_tools` + Super's memory tools must not overlap).
- **`Message` titles are closed.** `Message` raises `ValueError` for any title outside `{Submitted, Done, Error, Event, Task_end}` — content is strictly machine-set; don't invent new message kinds.

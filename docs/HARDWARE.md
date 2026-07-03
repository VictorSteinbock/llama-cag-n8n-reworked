# Hardware scaling guide

The stack ships tuned for a **32 GB laptop/desktop** (Gemma 4 12B QAT, 64k
context, one hot slot). That is a deliberately conservative default so the quick
start works on the widest range of machines. This guide is for everyone who has
*more* — a 24 GB GPU, a 64–512 GB unified-memory Mac or Ryzen AI box — and wants
to turn that headroom into **bigger documents, more of them hot at once, and
longer contexts**.

Everything here is three `.env` knobs plus a model choice:

| Knob | What it buys | Cost |
|------|--------------|------|
| `LLAMA_MODEL` | answer quality; max context the model even supports | download size + weights in memory |
| `LLAMA_CTX_SIZE` | how many tokens fit (per document = `ctx ÷ slots − 1024`) | KV-cache memory (see the arithmetic below) |
| `CAG_SLOTS` | how many documents stay **hot in RAM** at once | divides the context; each slot shrinks |
| `LLAMA_CACHE_TYPE_KV` | `q8_0` halves KV memory vs `f16` | negligible quality hit at `q8_0` |

Change a model or quant and existing caches self-heal (recompute + re-save) on
their next query — see [ARCHITECTURE.md](ARCHITECTURE.md).

> **Every model repo below was verified to exist on the Hugging Face API before
> it was listed** (an `HTTP 200` from `https://huggingface.co/api/models/<repo>`),
> the same bar the [README model table](../README.md#choosing-a-model-state-of-play-mid-2026)
> holds its five anchors to. Context lengths are quoted from each model's own
> repo/card. Where a repo is **gated** (needs a click-through license) an
> ungated community GGUF mirror is given instead, so nothing here dead-ends at an
> access wall.

## Why unified memory changes the game for CAG

On a discrete-GPU box the model weights and the KV cache have to fit in VRAM,
which is separate from (and usually much smaller than) system RAM. CAG's whole
value proposition — *keep documents resident so you never re-read them* — is
capped by that VRAM ceiling.

**Unified memory removes the ceiling.** Apple Silicon (M-series) and the new
AMD "Strix Halo" / Ryzen AI Max parts put CPU and GPU on **one memory pool**, so
the GPU can address the large majority of a 64 / 128 / 256 / 512 GB machine.
Three things follow that matter specifically for this stack:

1. **Many big documents resident at once.** KV cache is what a "hot slot" costs,
   and KV cache lives in that same pool. With 128 GB you can raise `CAG_SLOTS`
   to keep a *shelf* of large documents hot simultaneously — a Mac Studio holds
   an entire binder of contracts warm, each answered instantly, none re-read.
2. **Whole-corpus-as-context workflows.** A 256–512 GB machine can run a
   `LLAMA_CTX_SIZE` large enough to load a *book-length* document (or a merged
   corpus) into a single slot and query the entire thing at once — the exact case
   RAG exists to avoid, made feasible because the model plus its KV state share
   one address space.
3. **Model + KV in one budget.** You size *one* number (total unified RAM) against
   *weights + KV + overhead*, instead of juggling a small VRAM budget against a
   large RAM one. That is why the big-memory tiers below scale so cleanly.

The trade-off unified memory does **not** fix is the model's *effective* context
(below) — a 512 GB Mac will happily allocate a 1M-token KV cache for a model
whose real recall fades at 64k. Memory buys capacity; it does not buy
comprehension.

## KV-cache memory arithmetic (approximate — read the assumptions)

The KV cache is the dominant *variable* cost (the weights are fixed once you pick
a model). A workable approximation:

```text
KV bytes  ≈  2 × n_layers × kv_dim × ctx_tokens × bytes_per_elem
             ^                                     ^
             K and V                               f16 = 2 · q8_0 ≈ 1 (+~6% block overhead)

kv_dim = n_kv_heads × head_dim   (the KV projection width; with GQA this is
                                  much smaller than the model's hidden size)
```

**Assumptions, stated plainly:** this counts only the KV tensors (not
activations, compute buffers, or the CUDA/Metal graph, which add roughly
0.5–2 GB); it assumes grouped-query attention (GQA), which every model here
uses; and `bytes_per_elem` is **2 for `f16`** and **≈1 for `q8_0`** — so
**`q8_0` roughly halves these numbers**, which is why it is the default. Treat
every figure below as **±20%, order-of-magnitude guidance**, not a guarantee —
actual usage depends on the model's exact `n_kv_heads`/`head_dim` and the
backend. Rule of thumb that falls out of the formula for the models here:

| Model class (typical GQA geometry) | KV per 1k tokens, `f16` | at `q8_0` |
|---|---|---|
| ~8–12B dense (e.g. Gemma 4 12B, Qwen3 14B) | ~120–160 MB | ~60–80 MB |
| ~24–32B dense (Gemma 3 27B, Qwen3 32B, Mistral Small 24B) | ~200–280 MB | ~100–140 MB |
| ~70B dense (Llama 3.3 70B, Qwen2.5 72B) | ~300–360 MB | ~150–180 MB |
| MoE (Qwen3 30B-A3B / 235B-A22B, GLM-4.5-Air, gpt-oss-120b) | ~120–220 MB | ~60–110 MB |

So a 64k context at `q8_0` on a 12B model is ≈ **4 GB** of KV; the same 64k on a
70B is ≈ **10 GB**; a 256k context on a big MoE at `q8_0` is ≈ **20–28 GB**.
Multiply by nothing for slots — `CAG_SLOTS` *divides* the fixed `LLAMA_CTX_SIZE`,
it does not multiply KV, so the KV total is set by `LLAMA_CTX_SIZE` alone.

## A word on effective context (be honest with the big numbers)

A model advertising 256k or 1M context rarely *reasons* over that whole span.
Two independent benchmarks are worth knowing before you set `LLAMA_CTX_SIZE` to a
headline number:

- **RULER** (NVIDIA) measures retrieval-under-load and defines an *effective*
  length. Its consistent finding: for models claiming ≥128k, only about half
  hold up even at 32k, and effective length is commonly **~50–65% of
  advertised**. Concretely, Llama 3.1/3.3-70B's 128k is effective to ~**64k**
  (then drops hard); Command-R+ and Qwen2-72B are effective to ~**32k**.
  <https://github.com/NVIDIA/RULER> · <https://arxiv.org/abs/2404.06654>
- **NoLiMa** removes keyword overlap so the model must *infer*, not string-match.
  Here effective context for the entire Llama-3.x family is on the order of
  **1–2k tokens**, and **Gemma 3 is <1k** — a sobering counterweight to the
  marketing numbers. <https://github.com/adobe-research/NoLiMa> ·
  <https://arxiv.org/abs/2502.05167>
- **Passing "needle in a haystack" proves little** — it is exact-match retrieval
  and nearly every model aces it while failing the harder tests above. Trust
  RULER for retrieval and NoLiMa / [fiction.liveBench](https://epoch.ai/benchmarks/fictionlivebench)
  for reasoning.

**KV quantization interacts with this.** `q8_0` KV is effectively lossless for
long-range recall (perplexity delta in the noise); dropping KV to `q4` is fine
for casual chat but measurably hurts *precise* long-range retrieval, and the hit
grows with context length. Running the *weights* at 4-bit can cost far more on
long inputs (one 2025 study measured up to ~50%+ degradation on long-input tasks
for some models — long context is exactly where quantization hurts most).
<https://arxiv.org/abs/2505.20276>

Each tier below carries a one-line **long-context note** grading how far its
recommendation can be trusted, and flags honestly where third-party evidence is
thin.

---

## Tier A — 8–16 GB laptop

The floor. Keep the model small, the context modest, one slot. This is the
"it runs at all on a thin-and-light" tier.

- **Model:** `google/gemma-4-E4B-it-qat-q4_0-gguf` (≈3 GB, 128k advertised) — the
  edge-class Gemma 4, ungated Apache-2.0 QAT build. On 16 GB you can instead run
  the default `google/gemma-4-12B-it-qat-q4_0-gguf` (≈6.5 GB) with a smaller
  context.
- **`LLAMA_CTX_SIZE`:** `16384` (E4B) / `32768` on 16 GB with the 12B.
- **`CAG_SLOTS`:** `1`.
- **`LLAMA_CACHE_TYPE_KV`:** `q8_0`.
- **Document capacity:** ~**1 document of ~15k tokens** hot (E4B @ 16k), or
  ~**1 of ~31k** (12B @ 32k on 16 GB). KV at `q8_0` is ≈1 GB (E4B@16k).
- **Long-context note:** E4B is a small model — treat it as reliable only to a
  handful of thousand tokens for anything requiring reasoning; fine for
  short-manual lookup, not for book-length recall.

```dotenv
LLAMA_MODEL=google/gemma-4-E4B-it-qat-q4_0-gguf
LLAMA_CTX_SIZE=16384
CAG_SLOTS=1
LLAMA_CACHE_TYPE_KV=q8_0
```

## Tier B — 32 GB desktop/laptop (current default)

The shipped profile. Enough for a genuinely useful single-document assistant and
a couple of hot slots if you keep the context moderate.

- **Model:** `google/gemma-4-12B-it-qat-q4_0-gguf` *(default)* (≈6.5 GB, 256k
  advertised, ungated Apache-2.0 QAT). Strong dense alternative:
  `unsloth/Qwen3.5-9B-GGUF:Q4_K_M` (≈5.5 GB, 262k native).
- **`LLAMA_CTX_SIZE`:** `65536` (the default).
- **`CAG_SLOTS`:** `1` (default), or `2` if you alternate between two documents —
  each slot then gets 32k.
- **`LLAMA_CACHE_TYPE_KV`:** `q8_0`.
- **Document capacity:** ~**1 document of ~64k tokens** hot, or ~**2 of ~31k**
  with `CAG_SLOTS=2`. KV @ `q8_0`, 64k ≈ **4 GB**.
- **Long-context note:** 64k is a sensible ceiling to actually *trust* on the
  12B/9B class; you can set it to the model's full 256k, but expect recall to
  fade well before then (see the effective-context section).

```dotenv
LLAMA_MODEL=google/gemma-4-12B-it-qat-q4_0-gguf
LLAMA_CTX_SIZE=65536
CAG_SLOTS=1
LLAMA_CACHE_TYPE_KV=q8_0
```

## Tier C — 24 GB NVIDIA GPU desktop

A 24 GB card (RTX 3090 / 4090 / 5090-class) fits a strong mid-size model **and**
its KV cache in VRAM, with room for a big context or several hot slots. Start
with `python llamacag.py start --gpu` (CUDA image). Weights + KV must both fit
the 24 GB — that is the budget to respect.

- **Model:** `unsloth/gemma-3-27b-it-GGUF:Q4_K_M` (≈16 GB, 128k) — a verified,
  ungated 27B with a full quant ladder. Alternatives that also fit:
  `Qwen/Qwen3-32B-GGUF:Q4_K_M` (32k native, 131k via YaRN) or
  `unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF:Q4_K_M` (128k).
  For maximum quality-per-VRAM, the MoE `unsloth/Qwen3-30B-A3B-GGUF:Q4_K_M`
  gives ~30B answers at ~3B-active speed.
- **`LLAMA_CTX_SIZE`:** `65536`–`98304` (leave ~2 GB VRAM headroom above weights +
  KV). With the ~16 GB 27B weights, 64k `q8_0` KV (~8 GB) lands near the ceiling.
- **`CAG_SLOTS`:** `1`–`2`.
- **`LLAMA_CACHE_TYPE_KV`:** `q8_0`.
- **Document capacity:** ~**1 document of ~64k tokens** hot on the 27B, or
  ~**2 of ~32k**; more with the leaner 24B / MoE.
- **Long-context note:** Gemma 3 27B advertises 128k but is one of the weaker
  families on long-context reasoning (NoLiMa put Gemma 3 <1k effective) — for
  long-document work prefer the Qwen3-32B or Mistral-Small options here, and keep
  the trusted window ≤~32k. Qwen3-32B's honest window is ~32k (its 131k is YaRN-
  extended; third-party long-context data past 32k is thin).

```dotenv
LLAMA_MODEL=unsloth/gemma-3-27b-it-GGUF:Q4_K_M
LLAMA_CTX_SIZE=65536
CAG_SLOTS=1
LLAMA_CACHE_TYPE_KV=q8_0
LLAMA_GPU_LAYERS=999
```

## Tier D — 64–128 GB unified memory (Apple M-series, AMD Strix Halo / Ryzen AI Max)

This is where CAG gets fun. On a 64–128 GB unified machine you can run a large
MoE (near-frontier quality, MoE speed) with a **big context** *and* several hot
slots — a shelf of large documents, all warm.

> **Apple Silicon must run llama-server natively** (Docker has no Metal
> passthrough on macOS). See the [native-Mac recipe](#native-mac-recipe-apple-silicon--metal)
> below; the same `.env` numbers apply, you just start the model on the host.
> AMD Strix Halo on **Linux** can use the in-Docker Vulkan path
> (`python llamacag.py start --vulkan`).

- **Model:** `unsloth/GLM-4.5-Air-GGUF:Q4_K_M` (131k) — a verified ungated MoE
  (the gated `zai-org/GLM-4.5-Air-GGUF` mirrors to this and to
  `bartowski/zai-org_GLM-4.5-Air-GGUF`). Other strong picks:
  `google/gemma-4-26B-A4B-it-qat-q4_0-gguf` (MoE, 26B-class at ~4B-active speed,
  256k, ungated QAT — the "best quality-per-second on big-RAM boxes" option) or
  the workstation-class `ggml-org/GLM-4.7-Flash-GGUF` (base 202k). `gpt-oss-120b`
  (`ggml-org/gpt-oss-120b-GGUF`) also fits at 128 GB but ships **MXFP4 only** (no
  Q4_K_M ladder — set `LLAMA_MODEL` to the repo and let it pick the MXFP4 file).
- **`LLAMA_CTX_SIZE`:** `131072` (128k) comfortably; `65536` if you want more
  slots.
- **`CAG_SLOTS`:** `2`–`4`. At 128k total with 4 slots each document gets 32k; at
  65536 total with 4 slots each gets 16k — pick per your document sizes.
- **`LLAMA_CACHE_TYPE_KV`:** `q8_0`.
- **Document capacity:** ~**1 document of ~128k tokens** hot, **or a shelf of
  4 documents of ~32k each** hot simultaneously (128k ÷ 4). KV @ `q8_0` for a
  128k MoE context ≈ **10–14 GB**; weights ≈ 60–70 GB for GLM-4.5-Air Q4 —
  comfortably inside 128 GB, tight but workable at 64 GB (drop to 64k context or
  a smaller MoE).
- **Long-context note:** GLM-4.5-Air advertises 131k but **primary long-context
  benchmark data (RULER/NoLiMa) for the GLM-4.5 line is thin** — treat 128k as
  capacity, not a guarantee, and keep critical retrieval ≤~64k. The Gemma 4
  26B-A4B (256k) is a safer *quality* bet where you don't need the extreme span.

```dotenv
LLAMA_MODEL=unsloth/GLM-4.5-Air-GGUF:Q4_K_M
LLAMA_CTX_SIZE=131072
CAG_SLOTS=4
LLAMA_CACHE_TYPE_KV=q8_0
```

## Tier E — 256–512 GB Mac Studio class

The "whole-corpus-as-context" tier. Enough unified memory to run a 70B–235B model
at a long context *and* keep many large documents hot — or load one book-length
document into a single enormous slot.

> Runs **natively** on macOS (Metal) — see the [native-Mac recipe](#native-mac-recipe-apple-silicon--metal).
> A 512 GB Mac Studio can raise the Metal wired-memory limit (below) to give the
> GPU nearly the whole pool.

- **Model:** `unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF:Q4_K_M` (262k native) —
  a verified ungated giant MoE (235B total / 22B active), quantized weights ≈
  120–140 GB, and the *strongest-evidenced long context* of the giant open MoEs
  (its 2507 refresh is native-256k, not YaRN-stretched). Dense alternatives:
  `unsloth/Llama-3.3-70B-Instruct-GGUF:Q4_K_M` (128k) or
  `bartowski/Qwen2.5-72B-Instruct-GGUF:Q4_K_M` (32k native).
- **`LLAMA_CTX_SIZE`:** `262144` (256k) on the 235B at 512 GB; `131072` on a 70B
  at 256 GB. This is the tier where a *single slot* can hold a whole book.
- **`CAG_SLOTS`:** `1` for one giant whole-corpus slot, up to `4`–`8` for a large
  shelf of hot documents (256k ÷ 8 = 32k each).
- **`LLAMA_CACHE_TYPE_KV`:** `q8_0` (keep it — dropping to `q4` KV at these spans
  is exactly where long-range recall degrades).
- **Document capacity:** ~**1 document of ~256k tokens** (book-length) hot on the
  235B, **or ~8 documents of ~32k each** hot at once. KV @ `q8_0` for a 256k MoE
  context ≈ **20–28 GB**; add the ~120–140 GB weights — a 256 GB machine handles
  the 70B comfortably, a 512 GB machine handles the 235B at full 256k.
- **Long-context note:** Qwen3-235B-2507 is native-256k and the best-documented of
  the giant MoEs, but note its *exact* RULER numbers circulate mainly on secondary
  aggregators (primary third-party evidence is thinner than for Jamba-1.5-Large or
  Qwen2.5-14B-1M, the two open models with the best-evidenced genuine 128k+). For
  Llama 3.3 70B, remember its 128k is a retrieval number good to ~64k and much
  shorter for keyword-free reasoning — split truly long corpora if precision
  matters.

```dotenv
LLAMA_MODEL=unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF:Q4_K_M
LLAMA_CTX_SIZE=262144
CAG_SLOTS=1
LLAMA_CACHE_TYPE_KV=q8_0
```

---

## Native-Mac recipe (Apple Silicon / Metal)

**Why native.** Docker Desktop on macOS runs containers in a Linux VM with **no
GPU/Metal passthrough** — Docker's own docs say it plainly: *"Metal GPU access
requires direct hardware access and there is no GPU passthrough for Metal in
containers."* So a containerised llama.cpp on a Mac falls back to CPU and wastes
the unified-memory GPU. The fix: run **llama-server natively on the host** for
Metal, and keep the rest of the stack (cag-api, n8n, Postgres) in Docker. cag-api
reaches the host process over `host.docker.internal`.
(<https://www.docker.com/blog/docker-model-runner-vllm-metal-macos/>)

**1. Install llama.cpp** (the Homebrew build enables Metal by default on Apple
Silicon):

```bash
brew install llama.cpp
```

(<https://formulae.brew.sh/formula/llama.cpp> · Metal-on-by-default:
<https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md>)

**2. Let the CLI print your exact command.** `--native-llama` brings up the
Docker services *without* the in-Docker llama-server and prints the host command
with your `.env`'s model / context / slots / KV type already interpolated:

```bash
python llamacag.py start --native-llama
```

It prints something like this (values come from your `.env`):

```bash
llama-server \
  -hf google/gemma-4-12B-it-qat-q4_0-gguf \
  --ctx-size 65536 \
  --parallel 1 \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --slot-save-path ./kv_caches \
  --host 0.0.0.0 \
  --port 8080
```

Run that in a **separate terminal** and leave it running. The model downloads
from Hugging Face on first launch (`-hf` supports the `repo:quant` syntax), then
serves offline. `--host 0.0.0.0` is required so the Docker network can reach it;
flash attention is auto-enabled and Metal offload is the default on this build,
so no `-ngl` flag is needed.

**3. What changes about the KV cache.** In native mode the KV slot caches live on
the **host** at `--slot-save-path ./kv_caches` (a folder in the repo the CLI
creates for you) — **not** in the Docker `kv_caches` volume. This is the one
operational difference from all-in-Docker mode: back up / clear that host folder
instead of the volume. The save/restore mechanism is identical
(`POST /slots/{id}?action=save|restore`, files relative to the save path).

**4. Point cag-api at the host.** The `--native-llama` flag drives the compose
side for you, but the two `.env` lines it relies on are:

```dotenv
# Native mode: turn off the in-Docker llama-server profile…
COMPOSE_PROFILES=
# …and send cag-api to the host process instead of the Docker service.
LLAMA_SERVER_URL=http://host.docker.internal:8080
```

`COMPOSE_PROFILES=` (empty) disables the `local-llama` profile that the bundled
llama-server sits behind; `LLAMA_SERVER_URL` overrides cag-api's default of
`http://llama-server:8080`. (Leaving `COMPOSE_PROFILES=local-llama`, the shipped
default, keeps the normal all-in-Docker mode.) On the default all-in-Docker path
you don't touch either line.

**Bigger Macs — raise the Metal memory limit.** macOS caps GPU-usable unified
memory at ~75% of total RAM by default. On a 128/256/512 GB machine, raise it so
the GPU can wire most of the pool for weights + KV (Sonoma and later; value in
MB, takes effect immediately, `0` restores the default):

```bash
# e.g. allow ~120 GB of GPU-wired memory on a 128 GB Mac
sudo sysctl iogpu.wired_limit_mb=122880
```

(<https://github.com/ggml-org/llama.cpp/discussions/2182> — note older macOS uses
the `debug.iogpu.wired_limit` key in *bytes*.)

---

## Sources

Model existence and context lengths were verified per-repo against the Hugging
Face API (`https://huggingface.co/api/models/<repo>`) on 2026-07-02; context
lengths are from each repo's card/config.

Long-context quality:

- RULER (NVIDIA) — <https://github.com/NVIDIA/RULER>, <https://arxiv.org/abs/2404.06654>
- NoLiMa — <https://github.com/adobe-research/NoLiMa>, <https://arxiv.org/abs/2502.05167>
- fiction.liveBench — <https://epoch.ai/benchmarks/fictionlivebench>
- Quantization × long context — <https://arxiv.org/abs/2505.20276>

Native / hardware facts:

- No Metal passthrough in Docker on macOS — <https://www.docker.com/blog/docker-model-runner-vllm-metal-macos/>
- Homebrew llama.cpp (Metal default) — <https://formulae.brew.sh/formula/llama.cpp>, <https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md>
- llama-server flags / slot save-restore — <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>
- `host.docker.internal` from containers — <https://docs.docker.com/reference/cli/docker/container/run/>
- Apple Silicon unified-memory / `iogpu.wired_limit_mb` — <https://github.com/ggml-org/llama.cpp/discussions/2182>

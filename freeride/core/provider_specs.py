"""Declarative specs for OpenAI-compatible providers.

Most free-tier gateways speak the same wire format: ``POST
{base}/chat/completions`` with a Bearer token, SSE streaming terminated
by ``data: [DONE]``, and ``GET {base}/models`` for the catalog. What
differs is data — base URL, env var, which models are free, how the
free allowance is shared, which headers the front-door requires — not
code. :class:`OpenAICompatSpec` captures that data so a new provider is
one row in :data:`PROVIDER_SPECS` plus a test, instead of a 260-line
plugin module.

This module is **data only** (no httpx) so ``freeride.core`` can read
the table for env-var mapping, the init wizard, and doctor checks
without importing provider code. The runtime adapter lives in
:mod:`freeride.providers.openai_compat`.

Free-tier facts in the rows below were verified against each
provider's own pricing / rate-limit page on 2026-10-16 (see
``docs/providers/EXPANSION-RESEARCH.md``). They churn; the live probe
(``freeride audit-models``) is the authority on what actually answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping


KEYLESS_ENV = "FREERIDE_KEYLESS"
"""Comma-separated provider names (or ``all``) that may run anonymously.

Keyless tiers are shared per IP, slow, and in some cases log or train
on prompts, so they are never enabled by default — the user opts in
per provider, e.g. ``FREERIDE_KEYLESS=ovh,blockrun``."""

KEYLESS_SENTINEL = "anonymous"
"""The pseudo-key the failover chain carries for a keyless provider.
Never sent upstream; it exists so the (provider, key) cooldown and
health machinery work unchanged."""


@dataclass(frozen=True, slots=True)
class ModelMeta:
    """Out-of-band facts a catalog endpoint may not expose."""

    context_length: int = 0
    supported_parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenAICompatSpec:
    """Everything the generic adapter needs to know about one gateway.

    Free-model detection runs in this order, first match wins:

    1. ``{NAME}_FREE_MODELS_OVERRIDE`` env var (comma list) — user pin.
    2. ``free_ids`` allowlist.
    3. ``free_predicate(raw_catalog_entry)`` when given.
    4. ``free_id_suffix`` (OpenRouter/Kilo-style ``:free``).
    5. ``pricing.prompt == 0`` / ``pricing.input == 0`` in the entry.
    6. Otherwise every listed model is treated as free (OVH anonymous,
       Ollama Cloud) and the probe decides.
    """

    name: str
    display_name: str
    base_url: str
    env_vars: tuple[str, ...] = ()
    signup_url: str = ""
    description: str = ""

    # ----- access -----
    keyless: bool = False
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    """Connect/write/pool timeout and the non-streaming read timeout
    floor. Hidden-reasoning models (GLM Flash, Agnes) need 60-120 s."""
    read_timeout_s: float = 120.0

    # ----- paths -----
    models_path: str = "/models"
    chat_path: str = "/chat/completions"
    embeddings_path: str | None = None

    # ----- catalog -----
    free_ids: frozenset[str] | None = None
    free_predicate: Callable[[dict[str, Any]], bool] | None = None
    free_id_suffix: str | None = None
    fallback_models: tuple[str, ...] = ()
    """Used when ``GET /models`` fails or is empty — keyless gateways
    sometimes gate the catalog behind auth while chat stays open."""
    broken_ids: frozenset[str] = frozenset()
    id_prefix_strip: str = ""
    """Prefix to strip from catalog ids before use (Gemini lists
    ``models/gemini-2.5-flash``; chat accepts ``gemini-2.5-flash``)."""
    metadata: Mapping[str, ModelMeta] = field(default_factory=dict)
    tools_default: bool = False
    """Mark every free model tool-capable unless ``metadata`` says
    otherwise. Only for gateways whose whole free roster documents
    function calling; ``freeride audit-models`` is still the authority."""
    default_context_length: int = 32_768

    # ----- request shaping -----
    drop_params: tuple[str, ...] = ()
    """Top-level ChatRequest fields the upstream rejects (Ollama Cloud:
    ``tool_choice``, ``logprobs``)."""
    force_single_tool_call: bool = False
    """Pin ``parallel_tool_calls=false`` when tools are present."""
    strict_model_match: bool = False
    """Treat a response whose ``model`` is not the requested id as
    MODEL_NOT_FOUND so the chain moves on. For gateways observed
    answering a named free route with a different model under HTTP 200
    (BlockRun, Septor, Lucidity ``open/*``). Routes named ``auto`` /
    ``*/free`` / ``free_smart`` are always exempt."""

    # ----- quota semantics -----
    cooldown_scope: Literal["key", "model"] = "key"
    """``model``: a 429 benches only (provider, model), not the key —
    for gateways that meter each model separately (SambaNova 20 RPD per
    model, OVH 2 rpm per model)."""
    rpm: int | None = None
    daily_requests: int | None = None
    daily_tokens: int | None = None

    # ----- posture -----
    data_use: Literal["none", "operate", "train"] = "operate"
    """``train``: provider states it may train on prompts. Shown in
    ``freeride init`` and ``freeride keys`` so the user decides."""
    tos_note: str = ""
    default_on: bool = True
    """``False``: not offered by the init wizard (still usable when the
    env var is set). For data_use=="train" and the long tail."""

    @property
    def canonical_env_var(self) -> str:
        if self.env_vars:
            return self.env_vars[0]
        return KEYLESS_ENV

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


# ─── free-model predicates ─────────────────────────────────────────


def _gemini_free(raw: dict[str, Any]) -> bool:
    """Free-of-charge rows on Google's pricing page: the Flash /
    Flash-Lite families and Gemma. Pro tiers are excluded — their free
    quota is tiny or absent and they dominate the daily cap."""
    mid = str(raw.get("id", "")).removeprefix("models/")
    if not mid.startswith(("gemini-", "gemma-")):
        return False
    if "pro" in mid or "embedding" in mid or "image" in mid or "tts" in mid or "live" in mid:
        return False
    if mid.startswith("gemma-"):
        return True
    return "flash" in mid


def _kilo_free(raw: dict[str, Any]) -> bool:
    return str(raw.get("id", "")).endswith(":free")


def _xkiro_free(raw: dict[str, Any]) -> bool:
    return str(raw.get("access_tier", "")).lower() == "free" or str(raw.get("id", "")).endswith(":free")


def _requesty_free(raw: dict[str, Any]) -> bool:
    pricing = raw.get("pricing") or {}
    try:
        return float(pricing.get("input", pricing.get("prompt", 1)) or 0) == 0 and float(
            pricing.get("output", pricing.get("completion", 1)) or 0
        ) == 0
    except (TypeError, ValueError):
        return False


def _always(raw: dict[str, Any]) -> bool:
    """Anonymous tiers where every chat model is callable: the catalog's
    pricing describes the keyed (paid) tier, not the anonymous one."""
    return True


def _blockrun_free(raw: dict[str, Any]) -> bool:
    return str(raw.get("billing_mode", "")).lower() == "free"


def _free_suffix(raw: dict[str, Any]) -> bool:
    mid = str(raw.get("id", ""))
    return mid.endswith(":free") or mid.endswith("-free") or mid.startswith("free/")


# ─── the table ─────────────────────────────────────────────────────

_PRODUCT_UA = "FreeRide/1.0 (+https://free-ride.xyz)"

PROVIDER_SPECS: tuple[OpenAICompatSpec, ...] = (
    # ───────────── bucket A: first-party, ship by default ─────────────
    OpenAICompatSpec(
        name="gemini",
        display_name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        signup_url="https://aistudio.google.com/apikey",
        description="Google Gemini — Flash / Flash-Lite / Gemma free of charge (limits shown in AI Studio)",
        timeout_s=60.0,
        free_predicate=_gemini_free,
        id_prefix_strip="models/",
        tools_default=True,
        default_context_length=1_048_576,
        rpm=None,
        data_use="train",
        tos_note=(
            "Terms (2026-03) scope the API to professional/business use; the "
            "unpaid tier may be used to improve Google products. EEA/UK/CH "
            "accounts are paid-only."
        ),
    ),
    OpenAICompatSpec(
        name="mistral",
        display_name="Mistral",
        base_url="https://api.mistral.ai/v1",
        env_vars=("MISTRAL_API_KEY",),
        signup_url="https://console.mistral.ai/api-keys",
        description="Mistral — Free mode with included monthly usage (Devstral / Codestral / Mistral Small)",
        free_ids=frozenset(
            {
                "mistral-small-latest",
                "devstral-small-latest",
                "devstral-medium-latest",
                "codestral-latest",
                "magistral-small-latest",
                "ministral-8b-latest",
                "ministral-3b-latest",
                "open-mistral-nemo",
                "mistral-medium-latest",
            }
        ),
        metadata={
            "mistral-small-latest": ModelMeta(131_072, ("tools",)),
            "devstral-small-latest": ModelMeta(131_072, ("tools",)),
            "devstral-medium-latest": ModelMeta(131_072, ("tools",)),
            "codestral-latest": ModelMeta(262_144, ("tools",)),
            "magistral-small-latest": ModelMeta(131_072, ("tools",)),
            "ministral-8b-latest": ModelMeta(131_072, ("tools",)),
            "ministral-3b-latest": ModelMeta(131_072, ("tools",)),
            "open-mistral-nemo": ModelMeta(131_072, ("tools",)),
            "mistral-medium-latest": ModelMeta(131_072, ("tools",)),
        },
        embeddings_path="/embeddings",
        tos_note="Free mode limits are shown in the console (Admin → Limits), not published.",
    ),
    OpenAICompatSpec(
        name="sambanova",
        display_name="SambaNova Cloud",
        base_url="https://api.sambanova.ai/v1",
        env_vars=("SAMBANOVA_API_KEY",),
        signup_url="https://cloud.sambanova.ai/apis",
        description="SambaNova — DeepSeek V3.x / Llama 3.3 70B / gpt-oss-120b at 20 RPM, 20 RPD, 200K TPD per model",
        free_ids=frozenset(
            {
                "DeepSeek-V3.1",
                "DeepSeek-V3.2",
                "Meta-Llama-3.3-70B-Instruct",
                "gpt-oss-120b",
                "gemma-4-31B-it",
            }
        ),
        tools_default=True,
        default_context_length=131_072,
        cooldown_scope="model",
        rpm=20,
        daily_requests=20,
        daily_tokens=200_000,
        tos_note="Free tier applies while no payment method is linked; caps are per model.",
    ),
    OpenAICompatSpec(
        name="zai",
        display_name="Z.ai (GLM)",
        base_url="https://api.z.ai/api/paas/v4",
        env_vars=("ZAI_API_KEY", "ZHIPU_API_KEY"),
        signup_url="https://z.ai/manage-apikey/apikey-list",
        description="Z.ai — GLM-4.7-Flash / GLM-4.5-Flash / GLM-4.6V-Flash listed free (hidden reasoning, slow first byte)",
        timeout_s=90.0,
        read_timeout_s=180.0,
        free_ids=frozenset({"glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"}),
        tools_default=True,
        default_context_length=131_072,
        tos_note="International console (Singapore entity); mainland bigmodel.cn keys are a separate namespace.",
    ),
    OpenAICompatSpec(
        name="ollama_cloud",
        display_name="Ollama Cloud",
        base_url="https://ollama.com/v1",
        env_vars=("OLLAMA_CLOUD_API_KEY",),
        signup_url="https://ollama.com/settings/keys",
        description="Ollama Cloud — starter models on the Free plan (1 concurrent request; subscription-only models answer 403)",
        timeout_s=60.0,
        read_timeout_s=180.0,
        drop_params=("tool_choice", "logprobs", "top_logprobs", "n"),
        tools_default=True,
        default_context_length=131_072,
        tos_note="Which models are 'starter' is not published; the probe sorts it out.",
    ),
    # ───────────── bucket B: keyless, opt-in via FREERIDE_KEYLESS ─────────────
    OpenAICompatSpec(
        name="ovh",
        display_name="OVHcloud AI Endpoints (anonymous)",
        base_url="https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        keyless=True,
        signup_url="https://endpoints.ai.cloud.ovh.net/",
        description="OVHcloud anonymous tier — gpt-oss-120b / Llama 3.3 70B / Qwen at 2 req/min per IP per model",
        free_predicate=_always,
        fallback_models=(
            "gpt-oss-120b",
            "gpt-oss-20b",
            "Meta-Llama-3_3-70B-Instruct",
            "Qwen3.8-27B",
            "Qwen3.5-9B",
        ),
        tools_default=True,
        # OVH answers 400 "feature 'parallel_tool_calls=false' is not
        # currently supported" (live, 2026-10-16); tool calls work
        # without the flag, so do not pin it here.
        default_context_length=131_072,
        cooldown_scope="model",
        rpm=2,
        data_use="operate",
        default_on=False,
    ),
    OpenAICompatSpec(
        name="kilo",
        display_name="Kilo AI Gateway (anonymous)",
        base_url="https://ai-gateway.kilo.ai/api/v1",
        keyless=True,
        signup_url="https://kilo.ai/docs/gateway/authentication",
        description="Kilo gateway anonymous :free routes — 200 req/hour per IP; prompts MAY BE USED FOR TRAINING",
        free_predicate=_kilo_free,
        rpm=None,
        data_use="train",
        tos_note="Every :free model is flagged mayTrainOnYourPrompts; NVIDIA rows are trial-only.",
        default_on=False,
    ),
    OpenAICompatSpec(
        name="blockrun",
        display_name="BlockRun (anonymous)",
        base_url="https://blockrun.ai/api/v1",
        keyless=True,
        signup_url="https://blockrun.ai/docs",
        description="BlockRun free open-weight models — no account; 30 req/min or 300 req/hour per IP",
        free_predicate=_blockrun_free,
        tools_default=True,
        # Live 2026-10-16: two of three free ids answered as
        # nvidia/llama-3.2-11b-vision under HTTP 200.
        strict_model_match=True,
        rpm=30,
        default_on=False,
    ),
    # ───────────── bucket C: no-card aggregators ─────────────
    OpenAICompatSpec(
        name="xkiro",
        display_name="xKiro",
        base_url="https://api.xkiro.com/v1",
        env_vars=("XKIRO_API_KEY",),
        signup_url="https://xkiro.com",
        description="xKiro — 500K tokens/day on free-tier models (Qwen3 Coder, Devstral, MiniMax, DeepSeek)",
        free_predicate=_xkiro_free,
        tools_default=True,
        daily_tokens=500_000,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="requesty",
        display_name="Requesty",
        base_url="https://router.requesty.ai/v1",
        env_vars=("REQUESTY_API_KEY",),
        signup_url="https://app.requesty.ai",
        description="Requesty router — 200 requests/day on zero-price models",
        free_predicate=_requesty_free,
        daily_requests=200,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="routeway",
        display_name="Routeway",
        base_url="https://api.routeway.ai/v1",
        env_vars=("ROUTEWAY_API_KEY",),
        signup_url="https://routeway.ai",
        description="Routeway — 200 requests/day, 1 concurrent, on :free models",
        extra_headers={"User-Agent": "Mozilla/5.0 " + _PRODUCT_UA},
        free_id_suffix=":free",
        tools_default=True,
        daily_requests=200,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="septor",
        display_name="Septor Labs",
        base_url="https://api.septorlabs.com/v1",
        env_vars=("SEPTOR_API_KEY",),
        signup_url="https://septorlabs.com",
        description="Septor Labs — 50 req/day and 500K tokens/day on free models; 20 RPM",
        extra_headers={"User-Agent": _PRODUCT_UA},
        free_predicate=_free_suffix,
        strict_model_match=True,
        rpm=20,
        daily_requests=50,
        daily_tokens=500_000,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="electronhub",
        display_name="ElectronHub",
        base_url="https://api.electronhub.ai/v1",
        env_vars=("ELECTRONHUB_API_KEY",),
        signup_url="https://app.electronhub.ai",
        description="ElectronHub — $0.25/week credits plus daily :free allowance; 5 RPM",
        free_id_suffix=":free",
        tools_default=True,
        rpm=5,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="lucidity",
        display_name="Lucidity Composite",
        base_url="https://composite.lucidity.sh/v1",
        env_vars=("LUCIDITY_API_KEY",),
        signup_url="https://composite.lucidity.sh",
        description="Lucidity Composite — 80 requests/day shared across :free models",
        free_id_suffix=":free",
        strict_model_match=True,
        daily_requests=80,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="dreamprompting",
        display_name="DreamPrompting",
        base_url="https://dreamprompting.com/api/v1",
        env_vars=("DREAMPROMPTING_API_KEY",),
        signup_url="https://dreamprompting.com",
        description="DreamPrompting fail-over router — 5,000 req/day, 500K tokens/day; 32K input cap",
        fallback_models=("auto",),
        daily_requests=5_000,
        daily_tokens=500_000,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="waterfall",
        display_name="Waterfall",
        base_url="https://api.getwaterfall.org/v1",
        env_vars=("WATERFALL_API_KEY",),
        signup_url="https://getwaterfall.org",
        description="Waterfall — community free tier; free_smart route picks tool-capable free models",
        fallback_models=("free_smart",),
        tools_default=True,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="llm7",
        display_name="LLM7.io",
        base_url="https://api.llm7.io/v1",
        env_vars=("LLM7_API_KEY",),
        signup_url="https://dash.llm7.io",
        description="LLM7 — 60 req/min, 250 req/hour, 100K tokens per rolling 24h on the free token",
        rpm=60,
        daily_tokens=100_000,
        default_on=False,
    ),
    OpenAICompatSpec(
        name="longcat",
        display_name="LongCat (Meituan)",
        base_url="https://api.longcat.chat/openai/v1",
        env_vars=("LONGCAT_API_KEY",),
        signup_url="https://longcat.chat/platform",
        description="LongCat — daily free quota, overseas email signup, 1M context",
        fallback_models=("LongCat-Flash-Chat", "LongCat-Flash-Lite"),
        default_context_length=1_000_000,
        default_on=False,
    ),
)


_BY_NAME: dict[str, OpenAICompatSpec] = {s.name: s for s in PROVIDER_SPECS}


def spec_by_name(name: str) -> OpenAICompatSpec | None:
    return _BY_NAME.get(name)


def keyless_enabled(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """True when the user opted ``name`` into anonymous use via
    :data:`KEYLESS_ENV` (``all`` enables every keyless spec)."""
    import os

    env = os.environ if environ is None else environ
    raw = env.get(KEYLESS_ENV, "")
    if not raw:
        return False
    wanted = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return "all" in wanted or name.lower() in wanted

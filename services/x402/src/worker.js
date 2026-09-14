/**
 * FreeRide hosted inference: paid-only, priced per request, settled on Hedera
 * via x402.
 *
 * Local FreeRide is free-first and pays for its owner. This is the other role:
 * a gateway strangers pay. Every request is an x402 exchange — 402 with a
 * PAYMENT-REQUIRED challenge, retry with PAYMENT-SIGNATURE — verified and
 * settled through the Blocky402 facilitator, then served from OpenRouter.
 *
 * A seller never signs, so there is no Hedera SDK and no private key here. The
 * only secret is the OpenRouter key.
 *
 * Pricing. x402's `exact` scheme fixes the amount before the work runs, and an
 * AI request can be ten tokens or fifty thousand, so one flat price is wrong.
 * Each request is quoted on its own before the 402 goes out:
 *
 *   input tokens   estimated from the body size, conservatively
 *   output tokens  the caller's max_tokens, capped — so the caller sets the ceiling
 *   USD            both at the model's list price, times a margin
 *   HBAR           at Hedera's own exchange rate, from the mirror node
 *
 * The retry carries the same body, so it gets the same quote. It must pay that
 * quote, give or take a small tolerance so an hourly rate change between the
 * 402 and the retry does not bounce it.
 *
 * The order of operations is the other point of this file. The local gateway
 * once settled payments before discovering it could not serve them. Here:
 *
 *   1. validate and quote     — refuse what we will not serve, before a challenge
 *   2. verify the payment      — no money moves
 *   3. call the model          — if it fails, we never settle: no charge
 *   4. settle                  — only once we have an answer to hand back
 *   5. return the answer       — with the receipt and what the request really cost
 *
 * Requirements (payee, network, asset) are built here, and the amount is
 * checked against our own quote; nothing is taken from the caller on trust.
 * Anything that goes wrong with the facilitator is a refusal, never a crash:
 * Blocky402 answers a malformed payment with HTTP 500, not `isValid: false`.
 */

const HEADER_REQUIRED = "PAYMENT-REQUIRED";
const HEADER_SIGNATURE = "PAYMENT-SIGNATURE";
const HEADER_LEGACY = "X-PAYMENT";
const HEADER_RESPONSE = "PAYMENT-RESPONSE";
const TINYBARS_PER_HBAR = 100_000_000;
/** Quotes round up to 0.00001 HBAR: clean to read, and never rounded down. */
const AMOUNT_STEP = 1_000;
const RATE_TTL_FLOOR_MS = 30_000;
const RATE_TTL_CEILING_MS = 3_600_000;

/** USD per million tokens, per model. Accepts a JSON string or an object. */
function parsePrices(value) {
  let table = value;
  if (typeof value === "string") {
    try {
      table = JSON.parse(value);
    } catch {
      return {};
    }
  }
  const prices = {};
  for (const [id, p] of Object.entries(table || {})) {
    const input = Number(p?.input);
    const output = Number(p?.output);
    const context = Number(p?.context);
    if (Number.isFinite(input) && Number.isFinite(output) && input >= 0 && output >= 0) {
      prices[id] = { input, output, context: context > 0 ? context : Number.MAX_SAFE_INTEGER };
    }
  }
  return prices;
}

function config(env) {
  const int = (value, fallback) => {
    const n = Number.parseInt(String(value ?? ""), 10);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  };
  const num = (value, fallback) => {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  };
  const prices = parsePrices(env.MODEL_PRICES);
  const models = Object.keys(prices);
  const maxOutputTokens = int(env.MAX_OUTPUT_TOKENS, 4096);
  return {
    facilitator: String(env.FACILITATOR || "https://api.testnet.blocky402.com").replace(/\/+$/, ""),
    network: env.NETWORK || "hedera:testnet",
    payTo: env.PAY_TO || "",
    asset: env.ASSET || "0.0.0",
    feePayer: env.FEE_PAYER || "",
    upstream: String(env.OPENROUTER_BASE || "https://openrouter.ai/api/v1").replace(/\/+$/, ""),
    apiKey: env.OPENROUTER_API_KEY || "",
    rateUrl: env.RATE_URL || "https://testnet.mirrornode.hedera.com/api/v1/network/exchangerate",
    prices,
    models,
    defaultModel: prices[env.DEFAULT_MODEL] ? env.DEFAULT_MODEL : models[0] || "",
    maxOutputTokens,
    defaultOutputTokens: Math.min(int(env.DEFAULT_OUTPUT_TOKENS, 1024), maxOutputTokens),
    maxBodyBytes: int(env.MAX_BODY_BYTES, 400_000),
    bytesPerToken: num(env.BYTES_PER_TOKEN, 3),
    marginBps: int(env.MARGIN_BPS, 12_500),
    minAmount: int(env.MIN_AMOUNT, 10_000),
    toleranceBps: int(env.QUOTE_TOLERANCE_BPS, 200),
  };
}

const b64 = (obj) => {
  const bytes = new TextEncoder().encode(JSON.stringify(obj));
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
};

function unb64(value) {
  const binary = atob(String(value).trim());
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  const parsed = JSON.parse(new TextDecoder().decode(bytes));
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("payment header must be a JSON object");
  }
  return parsed;
}

const hbar = (tinybars) =>
  (Number(tinybars) / TINYBARS_PER_HBAR).toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
const usd = (value) => Number(value.toFixed(8));

function error(status, type, message, extra = {}, headers = {}) {
  return new Response(JSON.stringify({ error: { type, message, ...extra } }), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

// ─── the exchange rate ──────────────────────────────────────────────────────

const rateCache = new Map();

/**
 * USD per HBAR from Hedera's own exchange-rate file, cached until the network
 * says it expires. A stale rate beats no rate; no rate at all means no quote.
 */
async function usdPerHbar(c) {
  const now = Date.now();
  const cached = rateCache.get(c.rateUrl);
  if (cached && cached.until > now) return cached.value;
  try {
    const res = await fetch(c.rateUrl, { signal: AbortSignal.timeout(5_000) });
    if (!res.ok) return cached?.value ?? null;
    const rate = (await res.json())?.current_rate;
    const value = rate.cent_equivalent / rate.hbar_equivalent / 100;
    if (!Number.isFinite(value) || value <= 0) return cached?.value ?? null;
    const expires = Number(rate.expiration_time) * 1000;
    const until = Math.max(now + RATE_TTL_FLOOR_MS, Math.min(expires, now + RATE_TTL_CEILING_MS));
    rateCache.set(c.rateUrl, { value, until });
    return value;
  } catch {
    return cached?.value ?? null;
  }
}

// ─── the quote ──────────────────────────────────────────────────────────────

function quote(c, model, bodyBytes, maxOutputTokens, rate) {
  const price = c.prices[model];
  const inputTokens = Math.ceil(bodyBytes / c.bytesPerToken);
  const costUsd = (inputTokens * price.input + maxOutputTokens * price.output) / 1e6;
  const tinybars = Math.ceil(((costUsd * c.marginBps) / 10_000 / rate) * TINYBARS_PER_HBAR / AMOUNT_STEP) * AMOUNT_STEP;
  const amount = Math.max(c.minAmount, tinybars);
  return {
    model,
    input_tokens_estimate: inputTokens,
    max_output_tokens: maxOutputTokens,
    cost_usd: usd(costUsd),
    margin: c.marginBps / 10_000,
    usd_per_hbar: Number(rate.toFixed(6)),
    amount: String(amount),
    amount_hbar: hbar(amount),
  };
}

function requirements(c, amount) {
  const req = {
    scheme: "exact",
    network: c.network,
    amount: String(amount),
    payTo: c.payTo,
    maxTimeoutSeconds: 300,
    asset: c.asset,
  };
  if (c.feePayer) req.extra = { feePayer: c.feePayer };
  return req;
}

function challenge(c, resourceUrl, q, reason) {
  const body = {
    x402Version: 2,
    error: reason,
    resource: {
      url: resourceUrl,
      description:
        `FreeRide hosted inference · ${q.model} · up to ${q.input_tokens_estimate} tokens in ` +
        `and ${q.max_output_tokens} out · ${q.amount_hbar} HBAR`,
      mimeType: "application/json",
    },
    accepts: [requirements(c, q.amount)],
    extensions: {},
  };
  return error(
    402,
    "payment_required",
    reason,
    { quote: q, network: c.network, pay_to: c.payTo },
    { [HEADER_REQUIRED]: b64(body) },
  );
}

/** POST to the facilitator. Any failure — network, non-2xx, non-JSON — is { ok: false }. */
async function facilitate(c, path, paymentPayload, paymentRequirements) {
  try {
    const res = await fetch(`${c.facilitator}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ x402Version: 2, paymentPayload, paymentRequirements }),
      signal: AbortSignal.timeout(20_000),
    });
    if (!res.ok) return { ok: false, status: res.status };
    const data = await res.json().catch(() => null);
    return data && typeof data === "object" ? { ok: true, data } : { ok: false, status: res.status };
  } catch (e) {
    return { ok: false, status: 0, reason: String(e?.message || e).slice(0, 200) };
  }
}

/**
 * Parse and bound the request before anyone is asked to pay for it. Returns
 * either { body, bytes, maxOutputTokens } ready to quote and forward, or
 * { response } to send back as-is.
 */
async function readRequest(request, c) {
  const tooLarge = () => ({
    response: error(413, "request_too_large", `Request body over ${c.maxBodyBytes} bytes.`),
  });
  if (Number(request.headers.get("content-length") || 0) > c.maxBodyBytes) return tooLarge();
  const text = await request.text();
  const bytes = new TextEncoder().encode(text).length;
  if (bytes > c.maxBodyBytes) return tooLarge();

  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return { response: error(400, "invalid_json", "Request body must be JSON.") };
  }
  if (typeof body !== "object" || body === null || !Array.isArray(body.messages) || body.messages.length === 0) {
    return { response: error(400, "invalid_request", "Send an OpenAI-style body with a non-empty messages array.") };
  }

  const requested = typeof body.model === "string" ? body.model.trim() : "";
  const model = !requested || requested === "auto" ? c.defaultModel : requested;
  if (!c.prices[model]) {
    return {
      response: error(400, "model_not_offered", `Model ${JSON.stringify(requested)} is not offered.`, {
        models: c.models,
      }),
    };
  }

  // The caller sets the output ceiling, and pays for it; we only cap it.
  const forward = { ...body, model };
  const keys = ["max_tokens", "max_completion_tokens"].filter((key) => key in forward);
  if (keys.length === 0) forward.max_tokens = c.defaultOutputTokens;
  for (const key of keys) {
    const n = Number(forward[key]);
    forward[key] = Number.isInteger(n) && n > 0 ? Math.min(n, c.maxOutputTokens) : c.defaultOutputTokens;
  }
  const maxOutputTokens = Math.max(...["max_tokens", "max_completion_tokens"].filter((k) => k in forward).map((k) => forward[k]));

  const inputTokens = Math.ceil(bytes / c.bytesPerToken);
  if (inputTokens + maxOutputTokens > c.prices[model].context) {
    return {
      response: error(
        400,
        "context_length_exceeded",
        `About ${inputTokens} input tokens plus ${maxOutputTokens} output is over ${model}'s context.`,
      ),
    };
  }
  return { body: forward, bytes, maxOutputTokens };
}

async function chatCompletions(request, c) {
  if (!c.payTo || !c.apiKey || !c.defaultModel) {
    return error(503, "not_configured", "This gateway is missing its payee, price table, or upstream key.");
  }

  const parsed = await readRequest(request, c);
  if (parsed.response) return parsed.response;
  const { body, bytes, maxOutputTokens } = parsed;
  const resourceUrl = new URL(request.url).href;

  const rate = await usdPerHbar(c);
  if (!rate) {
    return error(503, "rate_unavailable", "Cannot price this request right now. Nothing was charged.");
  }
  const q = quote(c, body.model, bytes, maxOutputTokens, rate);

  const header = request.headers.get(HEADER_SIGNATURE) || request.headers.get(HEADER_LEGACY);
  if (!header) {
    return challenge(c, resourceUrl, q, "Payment required for hosted inference");
  }

  let paymentPayload;
  try {
    paymentPayload = unb64(header);
  } catch {
    return challenge(c, resourceUrl, q, "Payment header is not valid base64 JSON");
  }

  // The payment must match this request's quote. The facilitator then checks
  // the signed transaction really moves that amount to our payee.
  const offered = String(paymentPayload?.accepted?.amount ?? "");
  const paid = /^\d{1,15}$/.test(offered) ? Number(offered) : NaN;
  const slack = Math.ceil((Number(q.amount) * c.toleranceBps) / 10_000);
  if (!(Math.abs(paid - Number(q.amount)) <= slack)) {
    return challenge(
      c,
      resourceUrl,
      q,
      Number.isFinite(paid)
        ? `Payment of ${hbar(paid)} HBAR does not match this request's price of ${q.amount_hbar} HBAR`
        : "Payment does not say what it pays",
    );
  }
  const paymentRequirements = requirements(c, paid);

  // 1. Verify. No money moves.
  const verified = await facilitate(c, "/verify", paymentPayload, paymentRequirements);
  if (!verified.ok || verified.data.isValid !== true) {
    const reason = verified.ok
      ? verified.data.invalidReason || verified.data.invalidMessage || "payment_invalid"
      : `facilitator_unavailable (${verified.status})`;
    return challenge(c, resourceUrl, q, `Payment was not accepted: ${reason}`);
  }

  // 2. Call the model. If this fails we have not settled, so nothing is charged.
  let upstream;
  try {
    upstream = await fetch(`${c.upstream}/chat/completions`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${c.apiKey}`,
        "x-title": "FreeRide hosted",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(90_000),
    });
  } catch {
    return error(502, "upstream_unreachable", "The model provider did not answer. No payment was taken.");
  }
  if (!upstream.ok) {
    const detail = (await upstream.text().catch(() => "")).slice(0, 300);
    return error(502, "upstream_failed", `The model provider returned ${upstream.status}. No payment was taken.`, {
      detail,
    });
  }

  // 3. Settle, now that there is an answer to hand back.
  const settled = await facilitate(c, "/settle", paymentPayload, paymentRequirements);
  if (!settled.ok || settled.data.success !== true) {
    await upstream.body?.cancel().catch(() => {});
    const reason = settled.ok
      ? settled.data.errorReason || settled.data.errorMessage || "settlement_failed"
      : `facilitator_unavailable (${settled.status})`;
    return challenge(c, resourceUrl, q, `Payment could not be settled: ${reason}`);
  }

  const receipt = settled.data;
  const paidHeaders = {
    [HEADER_RESPONSE]: b64(receipt),
    "X-FreeRide-Lane": "paid",
    "X-FreeRide-Paid": "hedera-x402",
    "X-FreeRide-Provider": "openrouter",
    "X-FreeRide-Paid-Hbar": hbar(paid),
    ...(receipt.transaction ? { "X-FreeRide-Payment-Tx": String(receipt.transaction) } : {}),
  };

  // 4. Streams pass straight through: no parsing, so no CPU spent on big answers.
  if (body.stream === true) {
    return new Response(upstream.body, {
      status: 200,
      headers: {
        "content-type": upstream.headers.get("content-type") || "text/event-stream",
        "cache-control": "no-cache",
        ...paidHeaders,
      },
    });
  }

  const text = await upstream.text();
  let out;
  try {
    out = JSON.parse(text);
  } catch {
    return new Response(text, { status: 200, headers: { "content-type": "application/json", ...paidHeaders } });
  }

  // The quote is a ceiling. Say what the request actually used, at list price.
  const price = c.prices[body.model];
  const used = out?.usage;
  const billing = { quote: q, paid_hbar: hbar(paid), paid_usd: usd((paid / TINYBARS_PER_HBAR) * rate) };
  if (Number.isFinite(used?.prompt_tokens) && Number.isFinite(used?.completion_tokens)) {
    billing.usage = { prompt_tokens: used.prompt_tokens, completion_tokens: used.completion_tokens };
    billing.usage_cost_usd = usd((used.prompt_tokens * price.input + used.completion_tokens * price.output) / 1e6);
  }
  out._freeride_provider = "openrouter";
  out._freeride_paid = "hedera-x402";
  out._freeride_billing = billing;
  return new Response(JSON.stringify(out), {
    status: 200,
    headers: { "content-type": "application/json", ...paidHeaders },
  });
}

export default {
  async fetch(request, env) {
    const c = config(env);
    const { pathname } = new URL(request.url);

    if (pathname === "/health" && request.method === "GET") {
      const rate = await usdPerHbar(c);
      return Response.json({
        ok: true,
        lane: "paid-only",
        pricing: "per request: quoted in the 402 from input size and max_tokens, before you pay",
        network: c.network,
        pay_to: c.payTo,
        facilitator: c.facilitator,
        usd_per_hbar: rate ? Number(rate.toFixed(6)) : null,
        margin: c.marginBps / 10_000,
      });
    }
    if (pathname === "/v1/models" && request.method === "GET") {
      return Response.json({
        object: "list",
        data: c.models.map((id) => ({
          id,
          object: "model",
          owned_by: "freeride-hosted",
          pricing: {
            input_usd_per_million: c.prices[id].input,
            output_usd_per_million: c.prices[id].output,
          },
          context_length: c.prices[id].context,
        })),
      });
    }
    if (pathname === "/v1/chat/completions") {
      if (request.method !== "POST") return error(405, "method_not_allowed", "POST only.");
      return chatCompletions(request, c);
    }
    return error(404, "not_found", "Try POST /v1/chat/completions, GET /v1/models or GET /health.");
  },
};

/**
 * FreeRide hosted inference: paid-only, settled per request on Hedera via x402.
 *
 * Local FreeRide is free-first and pays for its owner. This is the other role:
 * a gateway strangers pay. Every request is an x402 exchange — 402 with a
 * PAYMENT-REQUIRED challenge, retry with PAYMENT-SIGNATURE — verified and
 * settled through the Blocky402 facilitator, then served from OpenRouter.
 *
 * A seller never signs, so there is no Hedera SDK and no private key here. The
 * only secret is the OpenRouter key.
 *
 * The order of operations is the point of this file. The local gateway once
 * settled payments before discovering it could not serve them, charging real
 * HBAR for requests that then failed. Here:
 *
 *   1. validate the request   — reject what we will not serve, before a challenge
 *   2. verify the payment      — no money moves
 *   3. call the model          — if it fails, we never settle: no charge
 *   4. settle                  — only once we have an answer to hand back
 *   5. return the answer       — with the settlement receipt
 *
 * Requirements (amount, payee, network, asset) are built here and never taken
 * from the caller. Anything that goes wrong with the facilitator is a refusal,
 * never a crash: Blocky402 answers a malformed payment with HTTP 500, not
 * `isValid: false`.
 */

const HEADER_REQUIRED = "PAYMENT-REQUIRED";
const HEADER_SIGNATURE = "PAYMENT-SIGNATURE";
const HEADER_LEGACY = "X-PAYMENT";
const HEADER_RESPONSE = "PAYMENT-RESPONSE";
const TINYBARS_PER_HBAR = 100_000_000;

function config(env) {
  const list = (value) =>
    String(value || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  const int = (value, fallback) => {
    const n = Number.parseInt(String(value ?? ""), 10);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  };
  const models = list(env.MODELS);
  return {
    facilitator: String(env.FACILITATOR || "https://api.testnet.blocky402.com").replace(/\/+$/, ""),
    network: env.NETWORK || "hedera:testnet",
    payTo: env.PAY_TO || "",
    amount: String(int(env.AMOUNT, 2_000_000)),
    asset: env.ASSET || "0.0.0",
    feePayer: env.FEE_PAYER || "",
    upstream: String(env.OPENROUTER_BASE || "https://openrouter.ai/api/v1").replace(/\/+$/, ""),
    apiKey: env.OPENROUTER_API_KEY || "",
    models,
    defaultModel: env.DEFAULT_MODEL || models[0] || "",
    maxOutputTokens: int(env.MAX_OUTPUT_TOKENS, 1024),
    maxBodyBytes: int(env.MAX_BODY_BYTES, 16_000),
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

function error(status, type, message, extra = {}, headers = {}) {
  return new Response(JSON.stringify({ error: { type, message, ...extra } }), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function requirements(c) {
  const req = {
    scheme: "exact",
    network: c.network,
    amount: c.amount,
    payTo: c.payTo,
    maxTimeoutSeconds: 300,
    asset: c.asset,
  };
  if (c.feePayer) req.extra = { feePayer: c.feePayer };
  return req;
}

function challenge(c, resourceUrl, reason) {
  const body = {
    x402Version: 2,
    error: reason,
    resource: {
      url: resourceUrl,
      description: `FreeRide hosted inference — ${hbar(c.amount)} HBAR per request`,
      mimeType: "application/json",
    },
    accepts: [requirements(c)],
    extensions: {},
  };
  return error(
    402,
    "payment_required",
    reason,
    { amount: c.amount, amount_hbar: hbar(c.amount), network: c.network, pay_to: c.payTo },
    { [HEADER_REQUIRED]: b64(body) },
  );
}

/** POST to the facilitator. Any failure — network, non-2xx, non-JSON — is null. */
async function facilitate(c, path, paymentPayload) {
  try {
    const res = await fetch(`${c.facilitator}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        x402Version: 2,
        paymentPayload,
        paymentRequirements: requirements(c),
      }),
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
 * either { body } ready to forward, or { response } to send back as-is.
 */
async function readRequest(request, c) {
  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > c.maxBodyBytes) {
    return { response: error(413, "request_too_large", `Request body over ${c.maxBodyBytes} bytes.`) };
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).length > c.maxBodyBytes) {
    return { response: error(413, "request_too_large", `Request body over ${c.maxBodyBytes} bytes.`) };
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return { response: error(400, "invalid_json", "Request body must be JSON.") };
  }
  if (typeof body !== "object" || body === null || !Array.isArray(body.messages) || body.messages.length === 0) {
    return { response: error(400, "invalid_request", "Send an OpenAI-style body with a non-empty messages array.") };
  }

  // The price is fixed, so the operator — not the caller — picks what it buys.
  const requested = typeof body.model === "string" ? body.model.trim() : "";
  const model = !requested || requested === "auto" ? c.defaultModel : requested;
  if (!c.models.includes(model)) {
    return {
      response: error(400, "model_not_offered", `Model ${JSON.stringify(requested)} is not offered.`, {
        models: c.models,
      }),
    };
  }

  const forward = { ...body, model };
  for (const key of ["max_tokens", "max_completion_tokens"]) {
    if (key in forward) {
      const n = Number(forward[key]);
      forward[key] = Number.isFinite(n) && n > 0 ? Math.min(n, c.maxOutputTokens) : c.maxOutputTokens;
    }
  }
  if (!("max_tokens" in forward) && !("max_completion_tokens" in forward)) {
    forward.max_tokens = c.maxOutputTokens;
  }
  return { body: forward };
}

async function chatCompletions(request, c) {
  if (!c.payTo || !c.apiKey || !c.defaultModel) {
    return error(503, "not_configured", "This gateway is missing its payee, model list, or upstream key.");
  }

  const parsed = await readRequest(request, c);
  if (parsed.response) return parsed.response;
  const body = parsed.body;
  const resourceUrl = new URL(request.url).href;

  const header = request.headers.get(HEADER_SIGNATURE) || request.headers.get(HEADER_LEGACY);
  if (!header) {
    return challenge(c, resourceUrl, "Payment required for hosted inference");
  }

  let paymentPayload;
  try {
    paymentPayload = unb64(header);
  } catch {
    return challenge(c, resourceUrl, "Payment header is not valid base64 JSON");
  }

  // 1. Verify. No money moves.
  const verified = await facilitate(c, "/verify", paymentPayload);
  if (!verified.ok || verified.data.isValid !== true) {
    const reason = verified.ok
      ? verified.data.invalidReason || verified.data.invalidMessage || "payment_invalid"
      : `facilitator_unavailable (${verified.status})`;
    return challenge(c, resourceUrl, `Payment was not accepted: ${reason}`);
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
  } catch (e) {
    return error(502, "upstream_unreachable", "The model provider did not answer. No payment was taken.");
  }
  if (!upstream.ok) {
    const detail = (await upstream.text().catch(() => "")).slice(0, 300);
    return error(502, "upstream_failed", `The model provider returned ${upstream.status}. No payment was taken.`, {
      detail,
    });
  }

  // 3. Settle, now that there is an answer to hand back.
  const settled = await facilitate(c, "/settle", paymentPayload);
  if (!settled.ok || settled.data.success !== true) {
    await upstream.body?.cancel().catch(() => {});
    const reason = settled.ok
      ? settled.data.errorReason || settled.data.errorMessage || "settlement_failed"
      : `facilitator_unavailable (${settled.status})`;
    return challenge(c, resourceUrl, `Payment could not be settled: ${reason}`);
  }

  const receipt = settled.data;
  const paidHeaders = {
    [HEADER_RESPONSE]: b64(receipt),
    "X-FreeRide-Lane": "paid",
    "X-FreeRide-Paid": "hedera-x402",
    "X-FreeRide-Provider": "openrouter",
    "X-FreeRide-Paid-Hbar": hbar(c.amount),
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
  out._freeride_provider = "openrouter";
  out._freeride_paid = "hedera-x402";
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
      return Response.json({
        ok: true,
        lane: "paid-only",
        network: c.network,
        price_hbar: hbar(c.amount),
        pay_to: c.payTo,
        facilitator: c.facilitator,
      });
    }
    if (pathname === "/v1/models" && request.method === "GET") {
      return Response.json({
        object: "list",
        data: c.models.map((id) => ({ id, object: "model", owned_by: "freeride-hosted" })),
      });
    }
    if (pathname === "/v1/chat/completions") {
      if (request.method !== "POST") return error(405, "method_not_allowed", "POST only.");
      return chatCompletions(request, c);
    }
    return error(404, "not_found", "Try POST /v1/chat/completions, GET /v1/models or GET /health.");
  },
};

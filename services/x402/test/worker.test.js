import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/worker.js";

const PRICES = {
  "openai/gpt-4.1-nano": { input: 0.1, output: 0.4, context: 1047576 },
  "qwen/qwen3-coder": { input: 0.3, output: 1.0, context: 262144 },
};

const ENV = {
  FACILITATOR: "https://facilitator.test",
  NETWORK: "hedera:testnet",
  PAY_TO: "0.0.10471098",
  ASSET: "0.0.0",
  FEE_PAYER: "0.0.7162784",
  OPENROUTER_BASE: "https://upstream.test/api/v1",
  OPENROUTER_API_KEY: "sk-test",
  RATE_URL: "https://mirror.test/api/v1/network/exchangerate",
  MODEL_PRICES: JSON.stringify(PRICES),
  DEFAULT_MODEL: "openai/gpt-4.1-nano",
  MAX_OUTPUT_TOKENS: "4096",
  DEFAULT_OUTPUT_TOKENS: "1024",
  MAX_BODY_BYTES: "400000",
  BYTES_PER_TOKEN: "3",
  MARGIN_BPS: "12500",
  MIN_AMOUNT: "10000",
  QUOTE_TOLERANCE_BPS: "200",
};

// Hedera's exchange-rate file as the mirror node serves it: $0.07617 per HBAR.
const RATE = 228511 / 30000 / 100;

const URL_CHAT = "https://x402.free-ride.xyz/v1/chat/completions";
const enc = (obj) => Buffer.from(JSON.stringify(obj)).toString("base64");
const dec = (s) => JSON.parse(Buffer.from(s, "base64").toString("utf8"));
const HI = { messages: [{ role: "user", content: "hi" }] };

/** The quote, computed the long way round, in tinybars. */
function expected(body, output = 1024, price = PRICES["openai/gpt-4.1-nano"]) {
  const bytes = Buffer.byteLength(JSON.stringify(body));
  const costUsd = (Math.ceil(bytes / 3) * price.input + output * price.output) / 1e6;
  return Math.max(10_000, Math.ceil((((costUsd * 12_500) / 10_000 / RATE) * 1e8) / 1000) * 1000);
}

/** A body of exactly `size` bytes. */
function sized(size) {
  const empty = Buffer.byteLength(JSON.stringify({ messages: [{ role: "user", content: "" }] }));
  return { messages: [{ role: "user", content: "a".repeat(size - empty) }] };
}

const RECEIPT = { success: true, transaction: "0.0.7162784@1789400000.000000001", network: "hedera:testnet" };
const COMPLETION = {
  id: "gen-1",
  choices: [{ message: { role: "assistant", content: "ready" } }],
  usage: { prompt_tokens: 9, completion_tokens: 2 },
};

let calls;
let routes;
const realFetch = globalThis.fetch;

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  calls = [];
  routes = {
    "/network/exchangerate": () =>
      json(200, {
        current_rate: { cent_equivalent: 228511, hbar_equivalent: 30000, expiration_time: Date.now() / 1000 + 3600 },
      }),
    "/verify": () => json(200, { isValid: true, payer: "0.0.10465313" }),
    "/settle": () => json(200, RECEIPT),
    "/chat/completions": () => json(200, COMPLETION),
  };
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url, body, headers: init.headers });
    const route = Object.keys(routes).find((path) => url.endsWith(path));
    if (!route) throw new Error(`unexpected fetch ${url}`);
    return routes[route](url);
  };
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

const hit = (path) => calls.filter((c) => c.url.endsWith(path));
/** Calls that could move money or spend upstream credit. The rate lookup is free. */
const spend = () => calls.filter((c) => !c.url.endsWith("/network/exchangerate"));

function chat(body = HI, headers = {}, env = ENV) {
  return worker.fetch(
    new Request(URL_CHAT, {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: typeof body === "string" ? body : JSON.stringify(body),
    }),
    env,
  );
}

async function accepts(body = HI, env = ENV) {
  const res = await chat(body, {}, env);
  assert.equal(res.status, 402);
  return dec(res.headers.get("PAYMENT-REQUIRED")).accepts[0];
}

/** Ask for the price, then pay it — optionally tampering with what is paid. */
async function pay(body = HI, tamper = (a) => a, env = ENV) {
  const accepted = tamper({ ...(await accepts(body, env)) });
  calls = [];
  return chat(body, { "PAYMENT-SIGNATURE": enc({ x402Version: 2, accepted, payload: { transaction: "c2lnbmVk" } }) }, env);
}

// ─── pricing ────────────────────────────────────────────────────────────────

test("health describes per-request pricing and the live rate, and spends nothing", async () => {
  const res = await worker.fetch(new Request("https://x402.free-ride.xyz/health"), ENV);
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.lane, "paid-only");
  assert.match(body.pricing, /per request/);
  assert.equal(body.usd_per_hbar, 0.076170);
  assert.equal(spend().length, 0);
});

test("models list their token prices", async () => {
  const res = await worker.fetch(new Request("https://x402.free-ride.xyz/v1/models"), ENV);
  const { data } = await res.json();
  assert.deepEqual(
    data.map((m) => [m.id, m.pricing.input_usd_per_million, m.pricing.output_usd_per_million]),
    [
      ["openai/gpt-4.1-nano", 0.1, 0.4],
      ["qwen/qwen3-coder", 0.3, 1.0],
    ],
  );
});

test("an unpaid request gets a 402 quoted for that request", async () => {
  const res = await chat();
  assert.equal(res.status, 402);
  const challenge = dec(res.headers.get("PAYMENT-REQUIRED"));
  assert.deepEqual(challenge.accepts, [
    {
      scheme: "exact",
      network: "hedera:testnet",
      amount: String(expected(HI)),
      payTo: "0.0.10471098",
      maxTimeoutSeconds: 300,
      asset: "0.0.0",
      extra: { feePayer: "0.0.7162784" },
    },
  ]);
  const { quote } = (await res.json()).error;
  assert.equal(quote.model, "openai/gpt-4.1-nano");
  assert.equal(quote.input_tokens_estimate, Math.ceil(Buffer.byteLength(JSON.stringify(HI)) / 3));
  assert.equal(quote.max_output_tokens, 1024);
  assert.equal(quote.amount, String(expected(HI)));
  assert.equal(spend().length, 0);
});

test("a 150 KB agent-sized request is quoted at list price plus margin", async () => {
  // 50,000 tokens in at $0.10/M + 1,024 out at $0.40/M = $0.0054096; x1.25 = $0.006762;
  // at $0.0761703 per HBAR = 0.08877471 HBAR, rounded up to 0.08878.
  const { quote } = (await (await chat(sized(150_000))).json()).error;
  assert.equal(quote.input_tokens_estimate, 50_000);
  assert.equal(quote.cost_usd, 0.0054096);
  assert.equal(quote.amount_hbar, "0.08878");
});

test("the price follows the request: bigger bodies and more output cost more", async () => {
  const small = Number((await accepts({ ...HI, max_tokens: 50 })).amount);
  const normal = Number((await accepts(HI)).amount);
  const large = Number((await accepts(sized(100_000))).amount);
  assert.ok(small < normal && normal < large, `${small} < ${normal} < ${large}`);
  assert.equal(small, expected({ ...HI, max_tokens: 50 }, 50));
});

test("the output ceiling is capped, and the quote is for the cap", async () => {
  const body = { ...HI, max_tokens: 50_000 };
  const { quote } = (await (await chat(body)).json()).error;
  assert.equal(quote.max_output_tokens, 4096);
  assert.equal(quote.amount, String(expected(body, 4096)));
});

test("a pricier model costs more for the same request", async () => {
  const nano = Number((await accepts(HI)).amount);
  const coder = Number((await accepts({ ...HI, model: "qwen/qwen3-coder" })).amount);
  assert.ok(coder > nano);
  assert.equal(coder, expected({ ...HI, model: "qwen/qwen3-coder" }, 1024, PRICES["qwen/qwen3-coder"]));
});

test("very small requests pay the minimum", async () => {
  assert.equal((await accepts({ ...HI, max_tokens: 1 })).amount, "10000");
});

test("without an exchange rate there is no quote, and nothing is charged", async () => {
  routes["/network/exchangerate"] = () => {
    throw new TypeError("fetch failed");
  };
  const env = { ...ENV, RATE_URL: "https://mirror-down.test/api/v1/network/exchangerate" };
  const res = await chat(HI, { "PAYMENT-SIGNATURE": enc({ accepted: { amount: "99999999" } }) }, env);
  assert.equal(res.status, 503);
  assert.equal((await res.json()).error.type, "rate_unavailable");
  assert.equal(spend().length, 0);
});

// ─── refusing before payment ────────────────────────────────────────────────

test("requests we will not serve are refused before any payment is asked for", async () => {
  const cases = [
    [{ ...HI, model: "anthropic/claude-opus-4" }, 400, "model_not_offered", ENV],
    [{ messages: [] }, 400, "invalid_request", ENV],
    ["{not json", 400, "invalid_json", ENV],
    [sized(400_001), 413, "request_too_large", ENV],
    [
      { ...HI, max_tokens: 1000 },
      400,
      "context_length_exceeded",
      { ...ENV, MODEL_PRICES: JSON.stringify({ "openai/gpt-4.1-nano": { input: 0.1, output: 0.4, context: 500 } }) },
    ],
  ];
  for (const [body, status, type, env] of cases) {
    const res = await chat(body, { "PAYMENT-SIGNATURE": enc({ accepted: { amount: "1" } }) }, env);
    assert.equal(res.status, status, type);
    assert.equal((await res.json()).error.type, type);
    assert.equal(res.headers.get("PAYMENT-REQUIRED"), null, type);
  }
  assert.equal(spend().length, 0);
});

test("a gateway missing its upstream key refuses rather than taking money", async () => {
  const res = await chat(HI, { "PAYMENT-SIGNATURE": enc({ accepted: { amount: "1" } }) }, { ...ENV, OPENROUTER_API_KEY: "" });
  assert.equal(res.status, 503);
  assert.equal(calls.length, 0);
});

test("an unreadable payment header is a 402, not a crash", async () => {
  const res = await chat(HI, { "PAYMENT-SIGNATURE": "%%%not-base64%%%" });
  assert.equal(res.status, 402);
  assert.ok(res.headers.get("PAYMENT-REQUIRED"));
  assert.equal(spend().length, 0);
});

// ─── the amount paid ────────────────────────────────────────────────────────

test("paying less than the quote is refused before the facilitator hears of it", async () => {
  const res = await pay(HI, (a) => ({ ...a, amount: String(Math.floor(Number(a.amount) * 0.9)) }));
  assert.equal(res.status, 402);
  assert.match((await res.json()).error.message, /does not match this request's price/);
  assert.equal(spend().length, 0);
});

test("paying far more than the quote is refused too, so a client bug cannot overcharge", async () => {
  const res = await pay(HI, (a) => ({ ...a, amount: String(Number(a.amount) * 3) }));
  assert.equal(res.status, 402);
  assert.equal(spend().length, 0);
});

test("a payment that does not say what it pays is refused", async () => {
  const res = await pay(HI, (a) => ({ ...a, amount: undefined }));
  assert.equal(res.status, 402);
  assert.match((await res.json()).error.message, /does not say what it pays/);
  assert.equal(spend().length, 0);
});

test("an hourly rate change inside the tolerance still goes through, for the amount signed", async () => {
  let signed;
  const res = await pay(HI, (a) => {
    signed = String(Math.ceil(Number(a.amount) * 0.99));
    return { ...a, amount: signed };
  });
  assert.equal(res.status, 200);
  for (const call of [...hit("/verify"), ...hit("/settle")]) {
    assert.equal(call.body.paymentRequirements.amount, signed);
  }
});

test("the facilitator is sent our payee, network and asset, whatever the payload claims", async () => {
  await pay(HI, (a) => ({ ...a, payTo: "0.0.666", network: "hedera:mainnet", asset: "0.0.123" }));
  assert.equal(hit("/verify").length, 1);
  for (const call of [...hit("/verify"), ...hit("/settle")]) {
    assert.equal(call.body.paymentRequirements.payTo, "0.0.10471098");
    assert.equal(call.body.paymentRequirements.network, "hedera:testnet");
    assert.equal(call.body.paymentRequirements.asset, "0.0.0");
  }
});

// ─── verify, serve, settle ──────────────────────────────────────────────────

test("a payment the facilitator rejects never reaches the model or settles", async () => {
  routes["/verify"] = () => json(200, { isValid: false, invalidReason: "invalid_exact_hedera_payload_amount_mismatch" });
  const res = await pay();
  assert.equal(res.status, 402);
  assert.match((await res.json()).error.message, /amount_mismatch/);
  assert.equal(hit("/chat/completions").length, 0);
  assert.equal(hit("/settle").length, 0);
});

test("a facilitator 500 with a non-JSON body is a refusal, not a crash", async () => {
  routes["/verify"] = () => new Response("Internal Server Error", { status: 500 });
  const res = await pay();
  assert.equal(res.status, 402);
  assert.match((await res.json()).error.message, /facilitator_unavailable \(500\)/);
  assert.equal(hit("/chat/completions").length, 0);
});

test("an unreachable facilitator is a refusal, not a crash", async () => {
  routes["/verify"] = () => {
    throw new TypeError("fetch failed");
  };
  const res = await pay();
  assert.equal(res.status, 402);
  assert.equal(hit("/chat/completions").length, 0);
});

test("if the model fails, the payment is never settled", async () => {
  routes["/chat/completions"] = () => json(429, { error: { message: "rate limited" } });
  const res = await pay();
  assert.equal(res.status, 502);
  assert.match((await res.json()).error.message, /No payment was taken/);
  assert.equal(hit("/verify").length, 1);
  assert.equal(hit("/settle").length, 0);
});

test("if the model is unreachable, the payment is never settled", async () => {
  routes["/chat/completions"] = () => {
    throw new TypeError("fetch failed");
  };
  const res = await pay();
  assert.equal(res.status, 502);
  assert.equal(hit("/settle").length, 0);
});

test("if settlement fails, the answer is withheld", async () => {
  routes["/settle"] = () => json(200, { success: false, errorReason: "insufficient_funds" });
  const res = await pay();
  assert.equal(res.status, 402);
  const body = await res.json();
  assert.match(body.error.message, /insufficient_funds/);
  assert.equal(body.choices, undefined);
});

test("a paid request returns the answer, the receipt, and what it really cost", async () => {
  const res = await pay();
  assert.equal(res.status, 200);
  assert.deepEqual(dec(res.headers.get("PAYMENT-RESPONSE")), RECEIPT);
  assert.equal(res.headers.get("X-FreeRide-Lane"), "paid");
  assert.equal(res.headers.get("X-FreeRide-Paid-Hbar"), String(expected(HI) / 1e8));
  assert.equal(res.headers.get("X-FreeRide-Payment-Tx"), RECEIPT.transaction);

  const body = await res.json();
  assert.equal(body.choices[0].message.content, "ready");
  assert.equal(body._freeride_paid, "hedera-x402");
  assert.equal(body._freeride_billing.quote.amount, String(expected(HI)));
  assert.deepEqual(body._freeride_billing.usage, { prompt_tokens: 9, completion_tokens: 2 });
  assert.equal(body._freeride_billing.usage_cost_usd, 0.0000017);

  // Order is the guarantee: verify, then the model, then settle.
  assert.deepEqual(
    spend().map((c) => new URL(c.url).pathname.split("/").pop()),
    ["verify", "completions", "settle"],
  );
  assert.equal(spend()[1].headers.authorization, "Bearer sk-test");
});

test("the model defaults, and the output cap reaches the provider", async () => {
  await pay({ ...HI, model: "auto", max_tokens: 50_000 });
  const upstream = hit("/chat/completions")[0].body;
  assert.equal(upstream.model, "openai/gpt-4.1-nano");
  assert.equal(upstream.max_tokens, 4096);

  await pay(HI);
  assert.equal(hit("/chat/completions")[0].body.max_tokens, 1024);
});

test("streams pass through untouched, with the receipt on the headers", async () => {
  const sse = 'data: {"choices":[{"delta":{"content":"re"}}]}\n\ndata: [DONE]\n\n';
  routes["/chat/completions"] = () => new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
  const res = await pay({ ...HI, stream: true });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "text/event-stream");
  assert.ok(res.headers.get("PAYMENT-RESPONSE"));
  assert.equal(await res.text(), sse);
});

test("unknown routes and wrong methods spend nothing", async () => {
  const notFound = await worker.fetch(new Request("https://x402.free-ride.xyz/v1/_freeride/x402/pay", { method: "POST" }), ENV);
  assert.equal(notFound.status, 404);
  const wrongMethod = await worker.fetch(new Request(URL_CHAT), ENV);
  assert.equal(wrongMethod.status, 405);
  assert.equal(calls.length, 0);
});

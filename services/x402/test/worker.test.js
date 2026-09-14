import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/worker.js";

const ENV = {
  FACILITATOR: "https://facilitator.test",
  NETWORK: "hedera:testnet",
  PAY_TO: "0.0.10471098",
  AMOUNT: "2000000",
  ASSET: "0.0.0",
  FEE_PAYER: "0.0.7162784",
  OPENROUTER_BASE: "https://upstream.test/api/v1",
  OPENROUTER_API_KEY: "sk-test",
  MODELS: "openai/gpt-4.1-nano,google/gemini-2.5-flash-lite",
  DEFAULT_MODEL: "openai/gpt-4.1-nano",
  MAX_OUTPUT_TOKENS: "1024",
  MAX_BODY_BYTES: "16000",
};

const URL_CHAT = "https://x402.free-ride.xyz/v1/chat/completions";
const enc = (obj) => Buffer.from(JSON.stringify(obj)).toString("base64");
const dec = (s) => JSON.parse(Buffer.from(s, "base64").toString("utf8"));

// A payload as a client would build it. The server must never read its terms.
const PAYLOAD = {
  x402Version: 2,
  accepted: { scheme: "exact", network: "hedera:testnet", amount: "1", payTo: "0.0.666", asset: "0.0.0" },
  payload: { transaction: "c2lnbmVk" },
};
const RECEIPT = { success: true, transaction: "0.0.7162784@1789400000.000000001", network: "hedera:testnet" };
const COMPLETION = { id: "gen-1", choices: [{ message: { role: "assistant", content: "ready" } }] };

let calls;
let routes;
const realFetch = globalThis.fetch;

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  calls = [];
  routes = {
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
    return routes[route]();
  };
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

const hit = (path) => calls.filter((c) => c.url.endsWith(path));

function chat(body = { messages: [{ role: "user", content: "hi" }] }, headers = {}, env = ENV) {
  return worker.fetch(
    new Request(URL_CHAT, {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: typeof body === "string" ? body : JSON.stringify(body),
    }),
    env,
  );
}

const paid = (body) => chat(body, { "PAYMENT-SIGNATURE": enc(PAYLOAD) });

test("health says paid-only and the price, and spends nothing", async () => {
  const res = await worker.fetch(new Request("https://x402.free-ride.xyz/health"), ENV);
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.lane, "paid-only");
  assert.equal(body.price_hbar, "0.02");
  assert.equal(calls.length, 0);
});

test("an unpaid request gets a 402 challenge built from server config", async () => {
  const res = await chat();
  assert.equal(res.status, 402);
  const challenge = dec(res.headers.get("PAYMENT-REQUIRED"));
  assert.equal(challenge.x402Version, 2);
  assert.deepEqual(challenge.accepts, [
    {
      scheme: "exact",
      network: "hedera:testnet",
      amount: "2000000",
      payTo: "0.0.10471098",
      maxTimeoutSeconds: 300,
      asset: "0.0.0",
      extra: { feePayer: "0.0.7162784" },
    },
  ]);
  assert.equal(challenge.resource.url, URL_CHAT);
  assert.equal(calls.length, 0);
});

test("the legacy X-PAYMENT header is accepted", async () => {
  const res = await chat(undefined, { "X-PAYMENT": enc(PAYLOAD) });
  assert.equal(res.status, 200);
});

test("requests we will not serve are refused before any payment is asked for", async () => {
  const cases = [
    [{ messages: [{ role: "user", content: "hi" }], model: "anthropic/claude-opus-4" }, 400, "model_not_offered"],
    [{ messages: [] }, 400, "invalid_request"],
    ["{not json", 400, "invalid_json"],
    [{ messages: [{ role: "user", content: "x".repeat(20_000) }] }, 413, "request_too_large"],
  ];
  for (const [body, status, type] of cases) {
    const res = await chat(body, { "PAYMENT-SIGNATURE": enc(PAYLOAD) });
    assert.equal(res.status, status, type);
    assert.equal((await res.json()).error.type, type);
    assert.equal(res.headers.get("PAYMENT-REQUIRED"), null, type);
  }
  assert.equal(calls.length, 0);
});

test("a gateway missing its upstream key refuses rather than taking money", async () => {
  const res = await chat(undefined, { "PAYMENT-SIGNATURE": enc(PAYLOAD) }, { ...ENV, OPENROUTER_API_KEY: "" });
  assert.equal(res.status, 503);
  assert.equal(calls.length, 0);
});

test("an unreadable payment header is a 402, not a crash", async () => {
  const res = await chat(undefined, { "PAYMENT-SIGNATURE": "%%%not-base64%%%" });
  assert.equal(res.status, 402);
  assert.ok(res.headers.get("PAYMENT-REQUIRED"));
  assert.equal(calls.length, 0);
});

test("the facilitator is sent the server's requirements, whatever the payload claims", async () => {
  await paid();
  for (const call of [...hit("/verify"), ...hit("/settle")]) {
    assert.equal(call.body.paymentRequirements.amount, "2000000");
    assert.equal(call.body.paymentRequirements.payTo, "0.0.10471098");
    assert.equal(call.body.paymentRequirements.network, "hedera:testnet");
    assert.deepEqual(call.body.paymentPayload, PAYLOAD);
  }
});

test("a payment the facilitator rejects never reaches the model or settles", async () => {
  routes["/verify"] = () => json(200, { isValid: false, invalidReason: "invalid_exact_hedera_payload_amount_mismatch" });
  const res = await paid();
  assert.equal(res.status, 402);
  assert.match((await res.json()).error.message, /amount_mismatch/);
  assert.equal(hit("/chat/completions").length, 0);
  assert.equal(hit("/settle").length, 0);
});

test("a facilitator 500 with a non-JSON body is a refusal, not a crash", async () => {
  routes["/verify"] = () => new Response("Internal Server Error", { status: 500 });
  const res = await paid();
  assert.equal(res.status, 402);
  assert.match((await res.json()).error.message, /facilitator_unavailable \(500\)/);
  assert.equal(hit("/chat/completions").length, 0);
});

test("an unreachable facilitator is a refusal, not a crash", async () => {
  routes["/verify"] = () => {
    throw new TypeError("fetch failed");
  };
  const res = await paid();
  assert.equal(res.status, 402);
  assert.equal(hit("/chat/completions").length, 0);
});

test("if the model fails, the payment is never settled", async () => {
  routes["/chat/completions"] = () => json(429, { error: { message: "rate limited" } });
  const res = await paid();
  assert.equal(res.status, 502);
  assert.match((await res.json()).error.message, /No payment was taken/);
  assert.equal(hit("/verify").length, 1);
  assert.equal(hit("/settle").length, 0);
});

test("if the model is unreachable, the payment is never settled", async () => {
  routes["/chat/completions"] = () => {
    throw new TypeError("fetch failed");
  };
  const res = await paid();
  assert.equal(res.status, 502);
  assert.equal(hit("/settle").length, 0);
});

test("if settlement fails, the answer is withheld", async () => {
  routes["/settle"] = () => json(200, { success: false, errorReason: "insufficient_funds" });
  const res = await paid();
  assert.equal(res.status, 402);
  const body = await res.json();
  assert.match(body.error.message, /insufficient_funds/);
  assert.equal(body.choices, undefined);
});

test("a paid request returns the answer with the settlement receipt", async () => {
  const res = await paid();
  assert.equal(res.status, 200);
  assert.deepEqual(dec(res.headers.get("PAYMENT-RESPONSE")), RECEIPT);
  assert.equal(res.headers.get("X-FreeRide-Lane"), "paid");
  assert.equal(res.headers.get("X-FreeRide-Paid-Hbar"), "0.02");
  assert.equal(res.headers.get("X-FreeRide-Payment-Tx"), RECEIPT.transaction);
  const body = await res.json();
  assert.equal(body.choices[0].message.content, "ready");
  assert.equal(body._freeride_paid, "hedera-x402");
  assert.equal(body._freeride_provider, "openrouter");

  // Order is the guarantee: verify, then the model, then settle.
  assert.deepEqual(
    calls.map((c) => new URL(c.url).pathname.split("/").pop()),
    ["verify", "completions", "settle"],
  );
  assert.equal(calls[1].headers.authorization, "Bearer sk-test");
});

test("the operator picks the model and caps output tokens", async () => {
  await paid({ messages: [{ role: "user", content: "hi" }], model: "auto", max_tokens: 50_000 });
  const upstream = hit("/chat/completions")[0].body;
  assert.equal(upstream.model, "openai/gpt-4.1-nano");
  assert.equal(upstream.max_tokens, 1024);

  calls = [];
  await paid({ messages: [{ role: "user", content: "hi" }], model: "google/gemini-2.5-flash-lite" });
  const second = hit("/chat/completions")[0].body;
  assert.equal(second.model, "google/gemini-2.5-flash-lite");
  assert.equal(second.max_tokens, 1024);
});

test("streams pass through untouched, with the receipt on the headers", async () => {
  const sse = 'data: {"choices":[{"delta":{"content":"re"}}]}\n\ndata: [DONE]\n\n';
  routes["/chat/completions"] = () => new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
  const res = await paid({ messages: [{ role: "user", content: "hi" }], stream: true });
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

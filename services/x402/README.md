# freeride-x402

Hosted FreeRide inference at `x402.free-ride.xyz`. It only serves paid requests:
each request is priced on its own, paid with x402, and settled on Hedera testnet
through [Blocky402](https://blocky402.com).

The local FreeRide daemon is free-first and pays on behalf of its owner. This
Worker does the opposite job: anyone can pay it. It has no free lane, no
`/v1/_freeride/*` routes and no Hedera private key. It only receives payments,
so it never needs to sign anything.

## Routes

| | |
|---|---|
| `POST /v1/chat/completions` | OpenAI wire format. Returns 402 with this request's price until the request carries a matching payment |
| `GET /v1/models` | The models on offer, with token prices |
| `GET /health` | Pricing, payee, network, facilitator and the current HBAR rate |

## Pricing

An AI request can be ten tokens or fifty thousand, so a flat price is wrong.
x402's `exact` scheme fixes the amount before the work runs, so every request
is quoted up front:

```
input tokens   ≈ body bytes / 3               deliberately overestimated
output tokens  = your max_tokens              1,024 if unset, capped at 4,096
cost (USD)     = input × model input price + output × model output price
price (HBAR)   = cost × 1.25 ÷ Hedera's USD/HBAR exchange rate (mirror node)
```

The 402 body carries the breakdown:

```json
{"error": {"type": "payment_required", "quote": {
  "model": "openai/gpt-4.1-nano", "input_tokens_estimate": 50000,
  "max_output_tokens": 1024, "cost_usd": 0.0054096, "margin": 1.25,
  "usd_per_hbar": 0.07617, "amount": "8878000", "amount_hbar": "0.08878"}}}
```

- **You control the ceiling.** A lower `max_tokens` gives a lower price.
- **Retries get the same price.** The retry sends the same body, so it gets the
  same quote. The payment must match that quote to within 2%, which absorbs
  Hedera's hourly rate change. Paying less is refused before the facilitator is
  called, and so is paying far more.
- **The quote is a ceiling.** The paid response reports what the request
  actually used in `_freeride_billing`:

```json
"_freeride_billing": {"quote": {...}, "paid_hbar": "0.00046", "paid_usd": 0.00003504,
  "usage": {"prompt_tokens": 20, "completion_tokens": 19}, "usage_cost_usd": 0.0000096}
```

### Live on testnet (Sep 14 2026, `openai/gpt-4.1-nano`, $0.07617/HBAR)

| Request | Quoted as | Price | Used at list price | Settlement |
|---|---|---|---|---|
| One question, `max_tokens: 60` | ~37 in + 60 out | 0.00046 HBAR ($0.000035) | 20 + 19 tokens, $0.0000096 | `0.0.7162784@1789363668.605966444` |
| Same question, no `max_tokens` | ~31 in + 1,024 out | 0.00678 HBAR ($0.00052) | 20 + 20 tokens, $0.00001 | `0.0.7162784@1789363672.254775270` |
| 100 KB of source code to review | ~33,420 in + 400 out | 0.05747 HBAR ($0.0044) | 21,181 + 225 tokens, $0.0022 | `0.0.7162784@1789363678.923045165` |

A 100 KB request costs about 125 times as much as a one-line question. Most of
what is left over is output the caller allowed but did not use. Setting
`max_tokens` is how a caller pays close to cost. An underpaid retry (1 tinybar
against a 0.00043 HBAR quote) was refused before it reached the facilitator.

Billing only what a request actually uses needs a way to authorise a maximum and
then settle less. That could be a prepaid balance or a Hedera HBAR allowance. It
is not in the `exact` scheme.

## One request

```
POST /v1/chat/completions                  → 402, PAYMENT-REQUIRED (base64 JSON, this request's price)
POST /v1/chat/completions
     PAYMENT-SIGNATURE: <signed payload>   → 200, PAYMENT-RESPONSE (settlement)
```

Once a payment arrives, the Worker handles it in this order:

1. **Validate and quote** the body, model, size and context. Anything it will
   not serve is refused before the client is asked to pay.
2. **Verify** the payment with the facilitator. No money moves at this step.
3. **Call the model.** If this fails, the payment is never settled, so the caller
   pays nothing.
4. **Settle.** This happens only once there is an answer to return.
5. **Return** the answer with `PAYMENT-RESPONSE`, `X-FreeRide-Lane: paid`,
   `X-FreeRide-Paid-Hbar` and `X-FreeRide-Payment-Tx`.

The Worker checks the amount against its own quote and builds the payee,
network and asset from its own config. The facilitator then checks that the
signed transaction moves that amount to that payee.

## Config

Everything in `wrangler.toml` is non-secret. The price table (`MODEL_PRICES`,
USD per million tokens) and the knobs (`MARGIN_BPS`, `BYTES_PER_TOKEN`,
`DEFAULT_OUTPUT_TOKENS`, `MAX_OUTPUT_TOKENS`, `MAX_BODY_BYTES`, `MIN_AMOUNT`,
`QUOTE_TOLERANCE_BPS`) live there.

## Run it

```bash
cp .dev.vars.example .dev.vars        # add OPENROUTER_API_KEY
npm test                              # no network calls
npm run dev                           # http://127.0.0.1:8787
```

To pay it from an outside client:

```bash
cd ../../examples/ethonline-hedera-agent && npm install
FREERIDE_URL=http://127.0.0.1:8787 MODEL=openai/gpt-4.1-nano \
HEDERA_ACCOUNT_ID=0.0.x HEDERA_PRIVATE_KEY=0x... node agent.mjs
```

## Deploy

```bash
wrangler secret put OPENROUTER_API_KEY
npm run deploy                        # claims x402.free-ride.xyz
```

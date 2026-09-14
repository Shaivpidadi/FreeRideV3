# freeride-x402

Hosted FreeRide inference at `x402.free-ride.xyz`. It only serves paid requests:
each call is paid on its own with x402 and settled on Hedera testnet through
[Blocky402](https://blocky402.com).

The local FreeRide daemon is free-first and pays on behalf of its owner. This
Worker does the opposite job: anyone can pay it. It has no free lane, no
`/v1/_freeride/*` routes and no Hedera private key. It only receives payments,
so it never needs to sign anything.

## Routes

| | |
|---|---|
| `POST /v1/chat/completions` | OpenAI wire format. Returns 402 until the request carries a valid payment |
| `GET /v1/models` | The models on offer |
| `GET /health` | Price, payee, network and facilitator |

## One request

```
POST /v1/chat/completions                  → 402, PAYMENT-REQUIRED (base64 JSON)
POST /v1/chat/completions
     PAYMENT-SIGNATURE: <signed payload>   → 200, PAYMENT-RESPONSE (settlement)
```

Once a payment arrives, the Worker handles it in this order:

1. **Validate** the body, model and size. Anything it will not serve is refused
   before the client is asked to pay.
2. **Verify** the payment with the facilitator. No money moves at this step.
3. **Call the model.** If this fails, the payment is never settled, so the caller
   pays nothing.
4. **Settle.** This happens only once there is an answer to return.
5. **Return** the answer with `PAYMENT-RESPONSE`, `X-FreeRide-Lane: paid` and
   `X-FreeRide-Payment-Tx`.

The Worker builds the payment requirements (amount, payee, network, asset) from
its own config and ignores whatever the payload claims. The operator chooses the
model and caps output tokens, which keeps a fixed price above cost.

## Price

0.02 HBAR per request. `gpt-4.1-nano` at the caps below costs at most about
$0.0008 per request (roughly 4k tokens in, 1,024 out). 0.02 HBAR is about
$0.0015.

| Var | Default |
|---|---|
| `AMOUNT` | `2000000` tinybars |
| `MODELS` | `openai/gpt-4.1-nano,google/gemini-2.5-flash-lite,mistralai/mistral-small-3.2-24b-instruct` |
| `MAX_OUTPUT_TOKENS` | `1024` |
| `MAX_BODY_BYTES` | `16000` |

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

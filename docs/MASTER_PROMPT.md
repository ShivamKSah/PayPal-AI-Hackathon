<USER_REQUEST>
# MASTER PROMPT: Build "Mandate" for the PayPal AI Hackathon

You are the lead engineer on a hackathon project. Read this whole document before doing anything. It is the single source of truth. If something here conflicts with your assumptions, this document wins. If something is marked UNVERIFIED, you must test it and report, never assume.

---

## 0. Current status and operating rules

- **Project status: 0%.** No repo, no code, no PayPal sandbox app, no deployment exists yet. Start from nothing.
- Today is Friday, Oct 30, 2026. Internal submission target is **Tue Nov 10**. Hard deadline is **Nov 12, 12:00 PM PST = Nov 13, 1:30 AM IST**. Builder is in India (IST).
- Builder: one person (Shivam), B.Tech CSE student, strong in Python, backend, full-stack, AI/ML.
- Working name is **Mandate**. It collides with Google AP2's "Intent Mandate / Cart Mandate" vocabulary. Keep the name in ONE config constant (`PROJECT_NAME`) so it can be renamed to Warrant, Ceiling or Tollgate in minutes. Do not hardcode the name across the code.
- **Work in phases with gates (Section 17).** Do NOT run ahead. After each phase: summarize what was done, what was tested, what failed, and wait for approval before the next phase.
- **Never** commit secrets. Commit `.env.example` only. Ask the user to paste credentials into a local `.env` themselves.
- Money is **integer cents** (or `Decimal`), never floats. USD only; reject other currencies.
- Prefer boring, testable code. Every policy check gets unit tests.
- When something is unverified or fails, say so plainly. Never fake a PayPal response. Mocks are allowed only for tests and must never be presented as real behavior.

---

## 1. Hackathon facts (PayPal AI Hackathon on Devpost)

- Title: "Build what's next with PayPal and AI". Online, $67,500+ in cash prizes, 17,500+ participants.
- **Only fundamental requirement:** the project must *meaningfully use both PayPal and AI*. PayPal integration must be central.
- Must use the PayPal developer platform (free **sandbox**) plus an AI tool/model/platform of choice. Sponsor tools optional: AG Grid, APIMatic, Astropods, Bryntum, Channel3, Elastic, KERNEL, Postman, Render, Zapier.
- **Submission requires:**
  1. Working project meeting the requirements above.
  2. Text description of features and functionality (English).
  3. **Functional demo**: a hosted URL OR complete setup/run instructions in the repo. A mockup, static prototype or non-functional demo does NOT count.
  4. Tools statement: which models/AI services were used and how.
  5. **Public repo** (GitHub/GitLab/Bitbucket) with all source, assets, instructions, and a **license file visible in the About section** (MIT or Apache-2.0 in repo root).
  6. **Demo video under 3 minutes** (target 2:50), public on YouTube, shows the project working, no third-party trademarks or copyrighted music.
- Project must stay free to use and available to the sponsor, administrator and judges **until judging ends Dec 15, 2026 (8:00 AM PT)**. Judging period: Dec 1 (2:00 PM PT) to Dec 15. Winners around Dec 21.
- Judges may score from text, images and video alone, so the video and description carry real weight.
- After the deadline the submission cannot be altered.
- **Judging criteria** (weights unpublished): Technological Implementation, Design, Potential Impact, Innovation/Idea, Presentation.
- **Prize targets:** Best Use of Agentic Commerce ($5k, primary), Best Use of PayPal + AI ($5k), Best Demo Delivery ($5k), Most Creative ($5k), Most Impactful ($5k), 1st/2nd/3rd ($12k/$8k/$5k). Sponsor: AG Grid ($5k/$2k/$1k), Channel3 ($1.5k), Render credits ($1k/$750/$500), APIMatic ($1k).

---

## 2. Product concept

**One line:** Never give an AI agent an unbounded wallet; give it a mandate.

**The problem.** An agent that can pay is a different security object from an agent that can read. Three failure kinds:
1. **Hijack:** hidden instructions in a page/email redirect the agent (prompt injection).
2. **Gullibility:** the agent completes checkout on a fake store with saved card details without asking.
3. **Honest error:** the agent buys the wrong thing *inside every limit* (120 cm desk instead of 160 cm, ISO keyboard instead of ANSI). No injection, so a detector would not fire.

**The answer: two layers.**
- **Prevention:** a hard policy engine in plain code (no LLM in the money path) that judges the *action* (merchant, amount, category, price), not the agent's reasoning. Claim is **containment, not detection**.
- **Recovery:** a flow for in-bounds mistakes: human flags the purchase, evidence comes from the audit log, an LLM labels the root cause, a rules table fixes the refund range, the human accepts, code calls the PayPal refund API.

**Positioning.** AP2 and card networks say how an agent proves authority. Mandate enforces it and fixes what it cannot prevent, on PayPal, in a form a judge can attack in a browser. An OAuth scope says which API the agent may call; a card limit says how much. Neither knows *what* is being bought. Mandate re-fetches price and category from a catalog source, checks the cart against the grant, and logs why.

**Out of scope (say so openly):** escrow, micropayments, merchant reputation scoring, real money, production vaulting, any claim to detect prompt injection.

**Impact framing:** a family member sets limits on an elderly parent's shopping agent (caregiver framing). Same code, human audience.

---

## 3. Safety rule and credential custody (non-negotiable)

- **The agent proposes, the engine decides.** Limits come from fixed code. The LLM never has free control of money, in prevention or recovery. An LLM intent check may only move a result toward ESCALATE, never toward APPROVE.
- **Credential custody is the whole game.** The agent must NEVER hold PayPal credentials or the vault token. They live only inside the Mandate API. The SDK wrapper `guard(tools, mandate)` must route the agent's tool calls to the Mandate API instead of handing it raw PayPal tools, and must omit `create_refund` from the agent's tool list entirely. If the agent can reach PayPal directly, the engine can be bypassed. State this in the README.
- The agent sees only the `/proposals` endpoint and a short-lived per-session token scoped to its mandate.
- The vault token is stored encrypted at rest (`TOKEN_ENCRYPTION_KEY`) and never returned by any API.

---

## 4. Tech stack

- **Backend:** Python, FastAPI, SQLAlchemy/SQLModel, Pydantic validation. PostgreSQL in production, SQLite locally. `pytest` plus `hypothesis` (or seeded random) for property tests.
- **Frontend:** React (Vite) with **AG Grid Community** for the ledger. Do NOT depend on AG Grid Enterprise (Master/Detail and Row Grouping are Enterprise). Build the JSON detail as your own side drawer. Never commit a license key.
- **PayPal:** direct REST calls (Orders v2, Payments v2, Vault, Webhooks) and `@paypal/agent-toolkit` used *inside* the Mandate API only.
- **AI:** one LLM provider (Gemini or Anthropic) for (a) the shopping agent with tool use, (b) constraint extraction for check 9, (c) root-cause labeling in recovery. Use current model names; check the provider docs, do not assume old names.
- **Catalog truth:** Channel3 (`https://api.trychannel3.com/v1/search`, header `x-api-key`). Fallback: a mock catalog with the **same schema**, disclosed in the README.
- **Hosting:** Render via `render.yaml` Blueprint (FastAPI also serves the built React app).
- **Webhook dev:** HTTPS tunnel (ngrok or cloudflared).

---

## 5. Suggested repo layout

```
/ (repo root: LICENSE, README.md, .env.example, render.yaml, .gitignore)
  /backend
    /app
      main.py, config.py (PROJECT_NAME here), db.py
      /models  (mandates, proposals, payments, cases, settlements, audit_log, webhook_events, challenge_attempts)
      /policy  (engine.py, checks/ one file per check, constraints.py)
      /paypal  (auth.py, orders.py, vault.py, refunds.py, webhooks.py)
      /catalog (channel3.py, mock.py)
      /agent   (agent.py, tools.py, guard.py)
      /recovery (intake.py, economics.py, labeler.py)
      /api     (routes)
    /tests (unit, property, integration)
  /frontend (React + AG Grid)
  /evals (scenarios/, runner.py, results/)
  /scripts (gate_test.py)
  /docs (architecture figure, attack catalogue)
```

---

## 6. PayPal integration (what the docs confirm)

- **Auth:** OAuth 2.0 client credentials. Sandbox token URL `https://api-m.sandbox.paypal.com/v1/oauth2/token`. Sandbox base `https://api-m.sandbox.paypal.com`; live `https://api-m.paypal.com`. Never mix them. Refresh the token before every PayPal call and cache with a safety margin (exact sandbox token lifetime is UNVERIFIED).
- **Orders v2:** `POST /v2/checkout/orders` with `intent: CAPTURE`, then `POST /v2/checkout/orders/{id}/capture`. Send a unique **`PayPal-Request-Id`** header on every create, capture and refund call (PayPal's idempotency mechanism; pair with policy check 8).
- **Vault flow (the autonomy enabler):**
  1. Create an order with `payment_source.paypal.attributes.vault` = `store_in_vault: ON_SUCCESS`, `usage_type: MERCHANT`, `customer_type: CONSUMER`, plus `experience_context` return and cancel URLs.
  2. Buyer approves once via the payer-action link (this is the Grant screen).
  3. Capture, then read the token id at `payment_source.paypal.attributes.vault.id`.
  4. Later, create orders referencing the saved token and capture server-side with no buyer present.
  - **UNVERIFIED:** docs show the later charge in two shapes: `payment_source.paypal.vault_id` in one version and a `token` object with `type: PAYMENT_METHOD_TOKEN` in another. Test which the sandbox accepts and pin it in the README.
  - Sandbox app must have **Vault enabled**. Live vault has a PayPal vetting step (note it; not blocking for sandbox).
  - **Fallback if vaulting fails:** a pre-approved sandbox buyer plus an honest README note.
- **Refunds (Payments v2):** `POST /v2/payments/captures/{capture_id}/refund`. Empty body refunds the remaining amount; an `amount` object makes a partial refund; optional `invoice_id`, `note_to_payer`. Success is 201 with refund id and status (normally COMPLETED). Multiple partial refunds are allowed up to the captured total. Over-refunding returns 422, a free second safety net behind our own range check. The order must be created, approved and captured first.
- **Webhooks:**
  - Register an HTTPS endpoint (port 443) per environment; keep the webhook ID PayPal assigns.
  - Events: `PAYMENT.CAPTURE.COMPLETED`, `.DENIED`, `.PENDING`, `.REFUNDED`, `.REVERSED`, `CHECKOUT.ORDER.APPROVED`, and vault events such as `VAULT.PAYMENT-TOKEN.CREATED`.
  - Verify every delivery with `POST /v1/notifications/verify-webhook-signature` (auth algo, cert URL, transmission id, signature, transmission time, webhook ID, raw event), or locally by checking the RSA-SHA256 signature; if local, validate the cert host ends in `paypal.com`.
  - Gotchas: simulator events FAIL signature verification, so test with real sandbox transactions. PayPal retries failed deliveries up to 25 times over about 3 days, so **dedupe on event id**. Events fire only for the REST app that performed the action, so use one app's credentials consistently. The Orders response returns before the vault token finishes saving, which is why the vault webhook exists.
- **Agent Toolkit / MCP:** `npm install @paypal/agent-toolkit` (Python and TypeScript). Tools: `create_order`, `get_order`, `pay_order`, `create_refund`, `get_refund`, `list_transactions`, and more. Reads `PAYPAL_ACCESS_TOKEN` and `PAYPAL_ENVIRONMENT`. Remote MCP: `https://mcp.sandbox.paypal.com/sse`. The local server accepts `--tools` to limit enabled tools (natural hook for `guard()`). Use the toolkit inside the Mandate API, not in the agent's hands.

---

## 7. Policy engine specification

Every proposal runs the same ordered checks. All plain code. Results: **APPROVE, ESCALATE or DENY**, with rule ids stored. **Log every check's result (pass or fail), not just the first failure**, so the proposal detail screen can show all red marks.

| # | Check | Logic | On fail |
|---|---|---|---|
| 1 | Mandate active | status ACTIVE and now < expires_at | DENY |
| 2 | Remaining budget | total + reserved + spent <= budget | DENY |
| 3 | Per-transaction cap | total <= per_txn_cap | DENY |
| 4 | Merchant allowlist | merchant id exact match in list (no fuzzy match) | DENY, or ESCALATE if mandate allows new merchants with approval |
| 5 | Category | catalog category in allowed categories; ignore the agent's label | DENY |
| 6 | Reconcile | each line item exists in catalog; unit price within tolerance (1% or 50 cents); quantity within cap; tax, shipping and fees included in total; currency matches | DENY |
| 7 | Velocity | purchases in last hour below limit | DENY |
| 8 | Idempotency | no matching cart hash in a recent window | DENY, return earlier result |
| 9 | Ambiguity gate | for each constraint in the instruction (e.g. width 160 cm), the catalog record must contain that field; if missing, ask the human | ESCALATE |
| 10 | Recurring billing | item/plan is subscription or auto-renew and mandate does not allow it | DENY |
| 11 | Near-limit | total above 80% of cap, or first purchase from a merchant | ESCALATE |

**Order of operations:** run 1, 2, 3, 4 first (no network). Then fetch the catalog record once and run 5, 6, 9, 10. Then 7, 8, 11.

**Check 9 detail:** an LLM extracts structured constraints from the instruction (the ONLY LLM step; it can only push toward ESCALATE). Code then compares those keys to catalog fields. If "width" was requested and the listing has no width field, ESCALATE with a message naming the missing field.

**Demo defaults (tunable per session):** budget $400, per-transaction cap $400 in the first scenario, velocity limit 5/hour, near-limit threshold 80%.

---

## 8. Execution design decisions

- **Cart hash binding.** On APPROVE, store SHA-256 of the canonical cart JSON (line items, totals, currency, merchant). The executor takes ONLY a `proposal_id`, rebuilds the order from the stored snapshot, and refuses if the hash differs. The agent never supplies order data at execution time.
- **Budget reservation.** On APPROVE, in one DB transaction (`SELECT ... FOR UPDATE` on the mandate row), add the amount to `reserved`. On CONFIRMED move to `spent`. On failure/timeout release. Prevents two parallel proposals overspending together. (SQLite note: use an equivalent exclusive transaction locally.)
- **Hash-chained audit log.** Each row stores `prev_hash` and `row_hash`. A "verify" button recomputes the chain. Append-only: the application DB role gets INSERT and SELECT only. Both prevention and recovery modules write to it.
- **Short-lived agent tokens.** Each visitor session gets its own mandate and a token scoped to it (signed with `SESSION_SECRET`).
- **Truthful states.** Show CAPTURED from the synchronous response, then CONFIRMED only after a verified webhook; reconcile by polling `get_order` if the webhook is late.
- **State machines.**
  - Proposal: RECEIVED, DENIED, ESCALATED, APPROVED, EXPIRED.
  - Payment: RESERVED, ORDER_CREATED, CAPTURED, CONFIRMED, FAILED, RELEASED.
  - Case: OPEN, DIAGNOSED, PROPOSED, ACCEPTED, REJECTED, REFUNDED, ESCALATED_TO_DISPUTE.

---

## 9. Recovery specification

Flow: human flags purchase -> gather evidence from audit log (instruction, agent reasoning, listing snapshot, order) -> LLM picks root cause -> rules table fixes the refund range -> model may choose a value inside the range and must explain it -> human accepts or rejects -> on accept, code calls the refund endpoint.

| Root cause | Range (starting values) | Side effect |
|---|---|---|
| Seller fault (wrong item shipped) | 90-100% | Seller pays return |
| Ambiguous listing (spec missing) | 40-70% | Listing fix suggested |
| Agent error (clear instruction misread) | 70-100% | Flag agent for review |
| Vague instruction | 0-30% | Suggest clearer prompt |

**Defensible economics (do this, not fixed percentages):** compute in code `suggested = (avoided_return_freight + expected_resale_loss) / price`, clamp to the cause's range; the LLM may only adjust within the clamp and write the explanation. Worked example (assumed numbers): desk $349.00, freight $60, resale loss 25% ($87.25) -> (60 + 87.25) / 349 = 42.2%, inside 40-70%, so propose 42% ($146.58). **Label every economic input as an assumption in the UI.**

**Safeguards:** refund <= capture minus prior refunds (PayPal also returns 422); only one open case per payment; every refund call carries its own `PayPal-Request-Id`; a rejected proposal packages evidence for an optional Customer Disputes API submission (keep off the critical path; UNVERIFIED in sandbox, do not promise it).

---

## 10. Data model (all money = integer cents; validate JSON against a schema before storing)

- **mandates:** id, session_id, budget_cents, reserved_cents, spent_cents, per_txn_cap_cents, categories (json), merchant_allowlist (json), allow_new_merchants, allow_recurring, velocity_per_hour, near_limit_pct, vault_token_ref (encrypted, never returned), status, expires_at, created_at
- **proposals:** id, mandate_id, instruction_text, agent_reasoning, cart_json, cart_hash, catalog_snapshot (json), constraints (json), verdict, rule_hits (json, every check pass/fail), reviewer, created_at
- **payments:** id, proposal_id, paypal_order_id, capture_id (unique), request_id, amount_cents, state, captured_at, confirmed_at
- **cases:** id, payment_id (one open case per payment), human_claim, evidence (json), root_cause, allowed_min_pct, allowed_max_pct, economic_inputs (json), suggested_pct, proposed_refund_cents, explanation, status
- **settlements:** id, case_id, human_accepted, paypal_refund_id, request_id, final_amount_cents, settled_at
- **audit_log:** id, ts, actor (human/agent/engine/paypal/recovery), event, ref_type, ref_id, payload (json), prev_hash, row_hash
- **webhook_events:** event_id (PK, dedupe), event_type, verified, received_at, processed_at
- **challenge_attempts:** id, session_id, attack_type, verdict, rule_hits, created_at

Indexes: proposals(mandate_id, created_at); payments(capture_id) unique; webhook_events(event_id) unique; audit_log(ref_type, ref_id).

---

## 11. API surface

`POST /mandates`, `GET /mandates/{id}`, `POST /mandates/{id}/revoke`, `POST /proposals`, `POST /proposals/{id}/approve` (human, for ESCALATE), `GET /ledger` (stream via SSE or WebSocket), `POST /cases`, `POST /cases/{id}/accept`, `POST /cases/{id}/reject`, `POST /webhooks/paypal`, `POST /attacks/run`, `POST /reset`.

---

## 12. Screens and ledger

1. **Mandate dashboard:** budget used, approved count, "Break the Mandate" counter, open cases, attack buttons, ledger.
2. **Grant screen:** set budget, cap, categories, merchants, expiry; then the one-time sandbox buyer approval that creates the vault token.
3. **Proposal detail:** what the agent proposed (untrusted) beside what the engine decided; all checks listed pass/fail; JSON payload; revoke button.
4. **Case detail:** evidence, diagnosis, economics, allowed range, proposed refund, accept/reject.
5. **Challenge page** with scoreboard.
6. **Evals page** with results badged "Synthetic".

**Ledger (AG Grid Community):** one row per decision across both modules: time, agent proposal, verdict, rule hits, amount, state. Custom cell renderers for verdict and state (text plus icon, not color alone). Stream new rows live. JSON drawer is your own side panel.

---

## 13. "Break the Mandate" challenge

Public page where judges try to make the agent overspend. Per-session mandate, ledger and counters. Sandbox only; say so on the page.

Attack catalogue (also the eval set):
1. Over remaining budget -> DENY (check 2)
2. Over per-transaction cap -> DENY (3)
3. Merchant not on list -> DENY (4)
4. Lookalike merchant id (one char off) -> DENY (4, exact match)
5. Mislabeled category -> DENY (5)
6. Inflated price vs catalog -> DENY (6)
7. Hidden extra line item -> DENY (6)
8. Quantity inflation -> DENY (6)
9. Currency swap -> DENY (6)
10. Expired mandate -> DENY (1)
11. Revoked mandate -> DENY (1)
12. Burst of small purchases -> DENY (7)
13. Split purchase to dodge cap -> DENY (2 or 7)
14. Replay of approved cart -> DENY (8)
15. Subscription upsell -> DENY (10)
16. Cart swapped after approval -> refused by cart hash
17. Two parallel proposals racing the budget -> one passes, one DENY via reservation
18. Injection text telling the agent to raise its own cap -> no effect (cap not agent-settable)
19. Agent calls a refund tool directly -> tool not exposed
20. Free-text attack typed by the visitor -> whatever the engine decides; logged

**Abuse controls:** per-session mandate, rate limits per IP and session, hard daily LLM spend cap (`DAILY_LLM_BUDGET`), input length limits, reset button, and a **replay mode** serving recorded agent proposals if the LLM key is drained (clearly labeled as replay).

**"0 breaches" is not a proof.** It only shows the rules held in those attempts. The real argument is by construction (amount/merchant/category checks are code with no model in the path) plus property tests. Do not oversell the counter.

---

## 14. Evaluation plan (everything badged "Synthetic")

- **Sets:** ~50 attack scenarios (cover all 20 rows with variations), 50 legitimate purchases, 50 in-bounds mistakes for recovery. Use fixed, recorded agent transcripts for reproducibility; run a smaller live-agent sample separately.
- **Metrics:** block rate (attacks ending DENY or ESCALATE); false-positive rate (legit ending DENY); ESCALATE on legit reported separately as friction; engine decision latency p50/p95 (excluding PayPal calls).
- **Recovery metric:** "refund lands inside the range" is true by design (code clamps), so report **root-cause accuracy vs gold labels** and scripted-run human acceptance rate instead.
- **Statistics:** report intervals (Wilson), not bare percentages. With n=50 and zero misses the 95% upper bound on the miss rate is about 6% (rule of three).
- **Property tests (randomized carts, thousands, fixed seed):** spent + reserved never exceeds budget; a DENY never triggers an outbound PayPal call (assert on a mock); a refund never exceeds capture minus prior refunds; the same cart hash never captures twice.
- Commit the runner script and its outputs.

---

## 15. Hosting and deployment

- `render.yaml` Blueprint: one web service (FastAPI serving built React), one Postgres, `healthCheckPath`, secrets set `sync: false`.
- **Date problem:** Render free Postgres expires 30 days after creation (14-day grace, no backups); free web services sleep after 15 min with a 30-60 s cold start. The project must stay available until **Dec 15**. Decide **by Nov 1**: pay for small Postgres + Starter web service from the start of judging, OR an external free Postgres (UNVERIFIED limits). Warm the service before recording the video and before Dec 1.
- **Env vars:** `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID`, `PAYPAL_ENV=sandbox`, `LLM_API_KEY`, `CATALOG_API_KEY`, `DATABASE_URL`, `TOKEN_ENCRYPTION_KEY`, `SESSION_SECRET`, `DAILY_LLM_BUDGET`.
- **Repo hygiene:** `.env.example` only; run gitleaks over full history before making the repo public; LICENSE in root and visible in About; test the Blueprint from a fresh clone.

---

## 16. Submission kit

**Video (target 2:50):**
- 0:00-0:20 problem in one sentence + the one-liner.
- 0:20-1:00 grant a $400 mandate; agent searches, builds cart, engine approves, order captured, webhook confirms.
- 1:00-1:40 poisoned page; agent proposes an $800 gift card; engine denies; ledger shows failed checks; no call reached PayPal.
- 1:40-2:10 ambiguity gate: instruction says 160 cm, listing has no width, engine asks the human before paying.
- 2:10-2:35 in-bounds mistake slips through; case opened, diagnosed with economics shown, human accepts, partial refund executes.
- 2:35-2:50 synthetic eval numbers with intervals + three-line SDK snippet.
Record on the real hosted demo. No copyrighted music, no logos without rights. Upload public to YouTube; test the link logged out.

**Devpost text:** problem; what it does; six stages (Grant, Propose, Verify, Execute, Audit, Recover); how PayPal is used (Orders, vault, refunds, webhooks, Agent Toolkit); how AI is used (agent, constraint extraction, root-cause labeling) and where it is deliberately kept out; results (synthetic, labeled); what is NOT claimed; how to run; links.

**Tools statement:** name each model/service and its role (agent LLM, constraint extractor, root-cause labeler, Channel3, AG Grid, Render, PayPal sandbox APIs).

**README order:** one-paragraph summary; three-line SDK snippet; architecture figure; run locally in 5 steps; hosted demo link and limits; sandbox account notes; eval reproduction; comparison with OAuth scopes, card limits and AP2; known limitations.

---

## 17. Phased plan with gates (stop and report after each phase)

**Phase 0: Oct 30, foundation + GATE TEST (do this first, ONLY this).**
1. Scaffold repo with LICENSE, README stub, `.gitignore`, `.env.example`, backend/frontend skeletons, `PROJECT_NAME` constant.
2. Guide the user to create the PayPal sandbox app (Vault enabled), business + personal sandbox accounts, and fill `.env` locally.
3. Write `scripts/gate_test.py` that does:
   - (a) create vault-enabled order, print the payer-action link, wait for buyer approval;
   - (b) capture, save the vault token id;
   - (c) create a second order with the token and capture with NO browser step (try both token shapes; report exactly which works and the exact error text if one fails);
   - (d) refund 60% of that capture with a fresh `PayPal-Request-Id`;
   - (e) register a webhook (tunnel) and confirm capture and refund events arrive and verify.
4. Report results. If step (c) fails after both shapes, propose the pre-approved-buyer fallback and WAIT for the decision.
Also in Phase 0: test the Channel3 key (do price, category and spec fields come back?); if not, plan the mock catalog.

**Phase 1: Oct 31 - Nov 1, engine.** DB tables, checks 1-11 with unit tests, catalog adapter, first Render deploy (even if empty), hosting decision.

**Phase 2: Nov 2 - Nov 3, money path.** Cart hash, reservation, executor (proposal_id only), shopping agent with `guard()`, webhook handler with verification and dedupe, CAPTURED -> CONFIRMED, hash-chained audit log.

**Phase 3: Nov 4 - Nov 5, UI + attacks.** React + AG Grid ledger (SSE), grant screen, proposal detail drawer, ambiguity gate, 8-10 attack buttons.

**Phase 4: Nov 6, recovery.** Case intake, evidence, economics clamp, accept -> real partial refund. LLM root-cause labeler only if time allows; otherwise a plain refund button with evidence.

**Phase 5: Nov 7, tests + FREEZE.** Property tests, eval runner (~50 recorded scenarios), Synthetic badges. **Feature freeze tonight.**

**Phase 6: Nov 8, hardening.** Fresh-clone test, gitleaks, README, warm Render, rehearse demo twice.

**Phase 7: Nov 9, video + text.** Record, upload, test link logged out, write Devpost text.

**Phase 8: Nov 10, SUBMIT.** Nov 11-12 are buffer for emergencies only.

---

## 18. Cut order if time runs short

1. LLM root-cause diagnosis (keep a plain refund button with evidence)
2. SDK packaging
3. Challenge-page extras (keep the attempt counter)
4. Ambiguity-gate polish

**Never cut:** the policy engine, the real sandbox checkout, the ledger, the hosted demo.

---

## 19. UNVERIFIED items (test and report, never assume)

- Which vault-token charge shape the sandbox accepts.
- Whether the Customer Disputes API works in sandbox.
- Whether Channel3 returns price, category and spec fields at the quality checks 6 and 9 need, and the key's quota.
- Exact sandbox access token lifetime.
- Whether an overall prize and a category prize can both go to one project (read the official rules).
- AG Grid sponsor prize expectations (read the sponsor page).
- Free external Postgres options and limits.
- Claims about Guardio, Unit 42 and Brave came via secondary articles; read the primary reports before quoting them in the Devpost text.

---

## 20. YOUR FIRST TASK (start now)

Do **Phase 0 only**. Do not build the policy engine, UI or agent yet.

1. Confirm you've read this whole document by replying with a short summary (under 150 words) of the project and the safety rule.
2. Scaffold the repo structure from Section 5 (empty modules are fine), add LICENSE (MIT), `.gitignore`, `.env.example` with every variable in Section 15, and a README stub.
3. Write `scripts/gate_test.py` per Phase 0.
4. Give me a numbered checklist of the manual steps I must do myself (create sandbox app with Vault enabled, create sandbox business and personal accounts, copy credentials into `.env`, start the tunnel, register the webhook).
5. Stop and wait for me to run the gate test and paste the results.
</USER_REQUEST>
<ADDITIONAL_METADATA>
The current local time is: 2026-10-30T23:51:40+05:30.
</ADDITIONAL_METADATA>
<USER_SETTINGS_CHANGE>
The user changed setting `Model Selection` from None to Claude Opus 4.6 (Thinking). No need to comment on this change if the user doesn't ask about it. If reporting what model you are, please use a human readable name instead of the exact string.
</USER_SETTINGS_CHANGE>
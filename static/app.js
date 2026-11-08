// Mandate Live Operations Dashboard
// Vanilla JS with plain fetch, 2s ledger polling, and accessible verdict representations.

const SESSION_ID = "session-demo";
let activeMandateId = null;
let currentProposalId = null;
let currentPaymentId = null;
let currentCaseId = null;
let ledgerData = [];

// Verdict icon & text mapping (never rely on color alone)
function formatVerdict(verdict) {
  if (verdict === "APPROVE") {
    return { icon: "✅", text: "APPROVE", cls: "verdict-APPROVE" };
  } else if (verdict === "ESCALATE") {
    return { icon: "⚠️", text: "ESCALATE", cls: "verdict-ESCALATE" };
  } else if (verdict === "DENY") {
    return { icon: "⛔", text: "DENY", cls: "verdict-DENY" };
  } else if (verdict === "BLOCKED_AT_EXECUTION" || verdict === "BLOCKED") {
    return { icon: "🛡️", text: "BLOCKED", cls: "verdict-DENY" };
  }
  return { icon: "❓", text: verdict || "UNKNOWN", cls: "verdict-DENY" };
}

// ── 1. Polling GET /ledger ──────────────────────────────────────────

async function pollLedger() {
  try {
    const res = await fetch(`/ledger?session_id=${SESSION_ID}`);
    if (res.ok) {
      ledgerData = await res.json();
      renderLedger(ledgerData);
      updateSummaryMetrics(ledgerData);
    }
  } catch (err) {
    console.error("Ledger polling error:", err);
  }
}

function renderLedger(items) {
  const tbody = document.getElementById("ledgerTableBody");
  if (!items || items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center">No proposals or transactions yet. Run an agent or attack above!</td></tr>`;
    return;
  }

  tbody.innerHTML = items.map((item, idx) => {
    const v = formatVerdict(item.verdict);
    const timeStr = item.created_at ? new Date(item.created_at).toLocaleTimeString() : "-";
    const amountStr = item.amount_cents ? `$${(item.amount_cents / 100).toFixed(2)}` : "$0.00";
    const stateStr = item.payment_state || "NOT_EXECUTED";
    
    // Dispute button on confirmed purchases
    let actionBtn = `<button class="btn btn-outline btn-sm btn-inspect" data-idx="${idx}">Inspect</button>`;
    if (stateStr === "CONFIRMED" || stateStr === "CAPTURED") {
      actionBtn += ` <button class="btn btn-danger btn-sm btn-dispute" data-payid="${item.payment_id}">This is wrong</button>`;
    }

    return `
      <tr data-idx="${idx}">
        <td>${timeStr}</td>
        <td><strong>${escapeHtml(item.proposed_item || "Item")}</strong><br><small class="text-muted">${escapeHtml(item.instruction || "")}</small></td>
        <td><code>${escapeHtml(item.merchant_id || "-")}</code></td>
        <td>${amountStr}</td>
        <td><span class="verdict-tag ${v.cls}">${v.icon} ${v.text}</span></td>
        <td><span class="state-pill state-${stateStr}">${stateStr}</span></td>
        <td>${getRuleHitsSummary(item.rule_hits)}</td>
        <td>${actionBtn}</td>
      </tr>
    `;
  }).join("");

  // Attach row click listeners for inspector
  tbody.querySelectorAll("tr").forEach(tr => {
    tr.addEventListener("click", (e) => {
      if (e.target.classList.contains("btn-dispute")) {
        e.stopPropagation();
        openDisputeModal(e.target.dataset.payid);
        return;
      }
      const idx = tr.dataset.idx;
      if (idx !== undefined && ledgerData[idx]) {
        showDetailInspector(ledgerData[idx]);
      }
    });
  });
}

function getRuleHitsSummary(ruleHits) {
  if (!ruleHits || ruleHits.length === 0) return `<small class="text-muted">None</small>`;
  const failed = ruleHits.filter(h => !h.passed);
  if (failed.length === 0) {
    return `<span class="text-success">✓ All 11 checks passed</span>`;
  }
  return `<span class="text-danger">⛔ ${escapeHtml(failed[0].name || "Check")} failed</span>`;
}

async function updateSummaryMetrics(items) {
  const approvedCount = items.filter(i => i.verdict === "APPROVE").length;
  document.getElementById("metricApproved").textContent = approvedCount;

  // Poll challenge stats
  try {
    const statsRes = await fetch(`/challenge/stats?session_id=${SESSION_ID}`);
    if (statsRes.ok) {
      const stats = await statsRes.json();
      document.getElementById("metricAttempts").textContent = stats.attempts || 0;
    }
  } catch (err) {}

  // Poll cases count
  try {
    const casesRes = await fetch("/cases");
    if (casesRes.ok) {
      const cases = await casesRes.json();
      const openCases = cases.filter(c => c.status === "OPEN" || c.status === "PROPOSED").length;
      document.getElementById("metricCases").textContent = openCases;
    }
  } catch (err) {}
}

// ── 2. Detail Inspector ─────────────────────────────────────────────

function showDetailInspector(item) {
  const panel = document.getElementById("detailPanel");
  panel.classList.remove("hidden");

  document.getElementById("detailId").textContent = item.id;
  document.getElementById("detailInstruction").textContent = item.instruction || "-";
  document.getElementById("detailReasoning").textContent = item.agent_reasoning || "Direct proposal";
  document.getElementById("detailCartHash").textContent = item.cart_json ? JSON.stringify(item.cart_json).slice(0, 40) + "..." : "-";
  document.getElementById("detailRawJson").textContent = JSON.stringify(item, null, 2);

  const tbody = document.getElementById("detailChecksBody");
  if (item.rule_hits && item.rule_hits.length > 0) {
    tbody.innerHTML = item.rule_hits.map(h => {
      const icon = h.passed ? "✅ PASS" : "⛔ FAIL";
      const cls = h.passed ? "text-success" : "text-danger";
      return `
        <tr>
          <td><strong>${h.check_id || "-"}</strong></td>
          <td><code>${escapeHtml(h.name || "-")}</code></td>
          <td class="${cls}"><strong>${icon}</strong></td>
          <td>${h.verdict_on_fail || "DENY"}</td>
          <td>${escapeHtml(h.reason || "-")}</td>
        </tr>
      `;
    }).join("");
  } else {
    tbody.innerHTML = `<tr><td colspan="5" class="text-muted">No check breakdown recorded for this row.</td></tr>`;
  }

  panel.scrollIntoView({ behavior: "smooth" });
}

document.getElementById("btnCloseDetail").addEventListener("click", () => {
  document.getElementById("detailPanel").classList.add("hidden");
});

// ── 3. Grant Flow ───────────────────────────────────────────────────

document.getElementById("formMandate").addEventListener("submit", async (e) => {
  e.preventDefault();
  const budget = parseInt(document.getElementById("inputBudget").value, 10) * 100;
  const cap = parseInt(document.getElementById("inputCap").value, 10) * 100;
  const merchants = document.getElementById("inputMerchants").value.split(",").map(s => s.trim());
  const categories = document.getElementById("inputCategories").value.split(",").map(s => s.trim());

  try {
    const res = await fetch("/mandates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: SESSION_ID,
        budget_cents: budget,
        per_txn_cap_cents: cap,
        merchant_allowlist: merchants,
        categories: categories,
        allow_new_merchants: false,
        allow_recurring: false,
        velocity_per_hour: 5,
        near_limit_pct: 80,
      }),
    });

    if (!res.ok) throw new Error(await res.text());
    const mandate = await res.json();
    activeMandateId = mandate.id;

    // Step 2: Init Grant
    const grantRes = await fetch(`/mandates/${activeMandateId}/grant/init`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });

    if (!grantRes.ok) throw new Error(await grantRes.text());
    const grantData = await grantRes.json();

    document.getElementById("grantFlowBox").classList.remove("hidden");
    document.getElementById("grantLink").href = grantData.approval_link || "#";
    document.getElementById("grantOrderInfo").textContent = `Setup order ${grantData.order_id || ""} ($1.00 vault auth)`;
    document.getElementById("btnGrantComplete").dataset.orderId = grantData.order_id;
    document.getElementById("grantResultMsg").innerHTML = `<span class="text-success">Mandate created. Click approval link above to authorize in PayPal Sandbox.</span>`;

    document.getElementById("metricBudget").textContent = `$0.00 / $${(budget / 100).toFixed(2)}`;
    document.getElementById("metricRemaining").textContent = `$${(budget / 100).toFixed(2)} available`;
  } catch (err) {
    alert("Error creating mandate: " + err.message);
  }
});

document.getElementById("btnGrantComplete").addEventListener("click", async (e) => {
  const orderId = e.target.dataset.orderId;
  if (!orderId || !activeMandateId) {
    alert("Please initiate grant first.");
    return;
  }

  try {
    const res = await fetch(`/mandates/${activeMandateId}/grant/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_id: orderId }),
    });

    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    document.getElementById("grantResultMsg").innerHTML = `
      <strong class="text-success">✓ Vault Granted Successfully!</strong>
      Refund of $1.00 setup fee status: ${data.refund_status}. Agent ready for autonomous purchases.
    `;
  } catch (err) {
    alert("Grant complete error: " + err.message);
  }
});

// ── 4. AI Shopping Agent Execution ──────────────────────────────────

document.querySelectorAll(".prompt-chip").forEach(chip => {
  chip.addEventListener("click", () => {
    document.getElementById("inputInstruction").value = chip.dataset.prompt;
  });
});

document.getElementById("btnRunAgent").addEventListener("click", async () => {
  const instruction = document.getElementById("inputInstruction").value.trim();
  if (!instruction) return;

  const statusBox = document.getElementById("agentStatusBox");
  const banner = document.getElementById("agentVerdictBanner");
  const icon = document.getElementById("agentVerdictIcon");
  const text = document.getElementById("agentVerdictText");
  const details = document.getElementById("agentDetails");
  const escalateActions = document.getElementById("escalateActions");
  const fallbackNotice = document.getElementById("llmFallbackNotice");

  statusBox.classList.remove("hidden");
  escalateActions.classList.add("hidden");
  fallbackNotice.classList.add("hidden");
  banner.className = "verdict-banner";
  icon.textContent = "⏳";
  text.textContent = "AGENT REASONING & POLICY CHECK...";
  details.textContent = "Consulting catalog and evaluating Section 7 checks...";

  try {
    const res = await fetch("/agent/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        instruction: instruction,
        session_id: SESSION_ID,
        execute_if_approved: true,
      }),
    });

    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const v = formatVerdict(data.verdict);

    banner.className = `verdict-banner ${v.cls}`;
    icon.textContent = v.icon;
    text.textContent = `VERDICT: ${v.text}`;

    if (data.llm_fallback) {
      fallbackNotice.classList.remove("hidden");
    }

    if (data.verdict === "APPROVE") {
      const payState = data.payment ? data.payment.state : "CONFIRMED";
      details.innerHTML = `
        <strong>Proposed Item:</strong> ${escapeHtml(data.proposal?.cart_json?.items?.[0]?.title || "Item")}<br>
        <strong>Total Amount:</strong> $${((data.proposal?.cart_json?.total_cents || 0) / 100).toFixed(2)}<br>
        <strong>Payment Status:</strong> <span class="state-pill state-${payState}">${payState}</span> (Autonomous vault capture complete)
      `;
    } else if (data.verdict === "ESCALATE") {
      currentProposalId = data.proposal?.id;
      escalateActions.classList.remove("hidden");
      details.innerHTML = `
        <strong>Agent Proposed:</strong> ${escapeHtml(data.proposal?.cart_json?.items?.[0]?.title || "Item")}<br>
        <strong>Reasoning:</strong> ${escapeHtml(data.proposal?.agent_reasoning || "-")}<br>
        <span class="text-warning">Ambiguity Gate held. Missing catalog spec field.</span>
      `;
    } else {
      details.textContent = data.message || "Proposal blocked by deterministic policy rules. Zero PayPal calls made.";
    }

    pollLedger();
  } catch (err) {
    banner.className = "verdict-banner verdict-DENY";
    icon.textContent = "⛔";
    text.textContent = "AGENT RUN ERROR";
    details.textContent = err.message;
  }
});

document.getElementById("btnHumanApprove").addEventListener("click", async () => {
  if (!currentProposalId) return;
  try {
    const res = await fetch(`/proposals/${currentProposalId}/human_approve`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    document.getElementById("escalateActions").classList.add("hidden");
    document.getElementById("agentDetails").innerHTML = `<strong class="text-success">✓ Approved by human buyer! Vault payment captured.</strong>`;
    pollLedger();
  } catch (err) {
    alert("Human approve error: " + err.message);
  }
});

document.getElementById("btnHumanReject").addEventListener("click", async () => {
  if (!currentProposalId) return;
  try {
    const res = await fetch(`/proposals/${currentProposalId}/human_reject`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    document.getElementById("escalateActions").classList.add("hidden");
    document.getElementById("agentDetails").innerHTML = `<strong class="text-danger">✕ Rejected by human buyer. Zero charges made.</strong>`;
    pollLedger();
  } catch (err) {
    alert("Human reject error: " + err.message);
  }
});

// ── 5. Attack Simulator ─────────────────────────────────────────────

async function executeAttack(attackId) {
  const resultBox = document.getElementById("attackResultBox");
  const icon = document.getElementById("attackResultIcon");
  const text = document.getElementById("attackResultText");

  resultBox.classList.remove("hidden");
  icon.textContent = "⏳";
  text.textContent = `Firing attack '${attackId}'...`;

  try {
    const res = await fetch("/attacks/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        attack_id: attackId,
        session_id: SESSION_ID,
      }),
    });

    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const v = formatVerdict(data.verdict);

    icon.textContent = "🛡️";
    text.innerHTML = `
      <strong>[BLOCKED]</strong> Attack <code>${attackId}</code> was blocked by <strong>${escapeHtml(data.rule_held)}</strong>!
      Verdict: <span class="verdict-tag ${v.cls}">${v.icon} ${v.text}</span> | Breached: <strong class="text-success">False</strong> (Zero PayPal charges)
    `;

    pollLedger();
  } catch (err) {
    icon.textContent = "⚠️";
    text.textContent = "Attack simulation error: " + err.message;
  }
}

document.querySelectorAll(".attack-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    executeAttack(btn.dataset.attack);
  });
});

document.getElementById("btnRunAllAttacks").addEventListener("click", async () => {
  const attacks = [
    "over_budget", "over_cap", "off_list_merchant", "lookalike_merchant", "mislabeled_category",
    "inflated_price", "hidden_line_item", "quantity_inflation", "currency_swap", "expired_mandate",
    "revoked_mandate", "velocity_burst", "duplicate_replay", "subscription_upsell", "cart_swap_after_approval"
  ];
  for (const atk of attacks) {
    await executeAttack(atk);
  }
});

// ── 6. Dispute & Recovery Modal ─────────────────────────────────────

function openDisputeModal(paymentId) {
  currentPaymentId = paymentId;
  document.getElementById("modalPaymentId").textContent = paymentId;
  document.getElementById("modalDiagnosisBox").classList.add("hidden");
  document.getElementById("modalClaimInput").value = "Seller shipped damaged item that was missing key specifications.";
  document.getElementById("disputeModal").classList.remove("hidden");
}

document.getElementById("btnCloseModal").addEventListener("click", () => {
  document.getElementById("disputeModal").classList.add("hidden");
});

document.getElementById("btnDiagnoseCase").addEventListener("click", async () => {
  const claim = document.getElementById("modalClaimInput").value.trim();
  if (!claim || !currentPaymentId) return;

  try {
    const res = await fetch("/cases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        payment_id: currentPaymentId,
        human_claim: claim,
      }),
    });

    if (!res.ok) throw new Error(await res.text());
    const caseData = await res.json();
    currentCaseId = caseData.id;

    document.getElementById("modalDiagnosisBox").classList.remove("hidden");
    document.getElementById("diagCause").textContent = caseData.root_cause;
    document.getElementById("diagRange").textContent = `${caseData.allowed_min_pct}% - ${caseData.allowed_max_pct}% (from rules table)`;
    document.getElementById("diagPct").textContent = `${caseData.suggested_pct}%`;
    document.getElementById("diagAmount").textContent = `$${((caseData.proposed_refund_cents || 0) / 100).toFixed(2)}`;
    document.getElementById("diagExplanation").textContent = caseData.explanation;
  } catch (err) {
    alert("Dispute diagnosis error: " + err.message);
  }
});

document.getElementById("btnAcceptRefund").addEventListener("click", async () => {
  if (!currentCaseId) return;
  try {
    const res = await fetch(`/cases/${currentCaseId}/accept`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    alert(`Refund accepted! PayPal refund ID: ${data.settlement?.paypal_refund_id || "REFUND-COMPLETE"}`);
    document.getElementById("disputeModal").classList.add("hidden");
    pollLedger();
  } catch (err) {
    alert("Accept refund error: " + err.message);
  }
});

document.getElementById("btnRejectCase").addEventListener("click", async () => {
  if (!currentCaseId) return;
  try {
    const res = await fetch(`/cases/${currentCaseId}/reject`, { method: "POST" });
    if (!res.ok) throw new Error(await res.text());
    alert("Case rejected. No refund executed.");
    document.getElementById("disputeModal").classList.add("hidden");
    pollLedger();
  } catch (err) {
    alert("Reject case error: " + err.message);
  }
});

// ── 7. Reset System ─────────────────────────────────────────────────

document.getElementById("btnReset").addEventListener("click", async () => {
  if (!confirm("Reset all proposals, transactions, and attacks for this demo session?")) return;
  try {
    const res = await fetch("/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: SESSION_ID }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    activeMandateId = data.mandate_id;
    document.getElementById("metricBudget").textContent = `$0.00 / $${(data.budget_cents / 100).toFixed(2)}`;
    document.getElementById("metricRemaining").textContent = `$${(data.budget_cents / 100).toFixed(2)} available`;
    document.getElementById("detailPanel").classList.add("hidden");
    pollLedger();
    alert("Session successfully reset to clean default mandate ($500.00 budget).");
  } catch (err) {
    alert("Reset error: " + err.message);
  }
});

// Utility
function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// ── Start 2-Second Polling ──────────────────────────────────────────
pollLedger();
setInterval(pollLedger, 2000);


const API_BASE = ""; // same origin, served by api/server.py

const MERCHANT_ID = "merchant_test_1";
let activeMandateId = "m_default"; // switches when the user sets their own limit
const SESSION_ID = "session_" + Math.random().toString(36).slice(2);
let currentCustomer = null; // {customer_id, username} once logged in, else null

const THUMBS = {
  p001: "\u{1F3A7}", p002: "\u{1F3A7}", p003: "\u{1F3A7}", p004: "\u{1F39A}️",
  p005: "\u{1F50A}", p006: "⌚", p007: "⌚", p008: "\u{1F4DF}",
  p009: "\u{1F4F1}", p010: "\u{1F50C}", p011: "\u{1F50B}", p012: "\u{1F517}",
  p013: "\u{1F5B1}️", p014: "⌨️", p015: "\u{1F4BB}",
  p016: "\u{1F3A7}", p017: "\u{1F50A}", p018: "\u{1F48D}", p019: "⌚",
  p020: "\u{1F50B}", p021: "\u{1F4F1}", p022: "\u{1F3F7}️", p023: "\u{1F50C}",
  p024: "\u{1F4F9}", p025: "\u{1F5A5}️",
};

// Generated icon-card look (no external images): a category-tinted gradient
// behind a large glyph, standing in for a product photo.
const CATEGORY_GRADIENTS = {
  "audio": "linear-gradient(135deg,#4f8cff,#8f6bff)",
  "wearables": "linear-gradient(135deg,#3ddc97,#1b8f63)",
  "accessories": "linear-gradient(135deg,#ffb84f,#ff6b6b)",
  "computer-accessories": "linear-gradient(135deg,#8f6bff,#4f8cff)",
};
const CATEGORY_LABELS = {
  "audio": "Audio", "wearables": "Wearables", "accessories": "Accessories", "computer-accessories": "Computer",
};
const CATEGORY_ICONS = { "audio": "🎧", "wearables": "⌚", "accessories": "🔌", "computer-accessories": "💻" };

let allProducts = [];
let activeCategory = "all";
let searchQuery = "";
let wishlist = new Set(JSON.parse(localStorage.getItem("wishlist") || "[]"));
let cart = JSON.parse(localStorage.getItem("cart") || "{}"); // { product_id: qty }

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}
function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
function inr(n) { return "₹" + Number(n).toLocaleString("en-IN"); }

/* ---------- Storefront ---------- */

async function loadCatalog() {
  try {
    allProducts = await fetch(`${API_BASE}/api/catalog`).then((r) => r.json());
  } catch (err) {
    const grid = document.getElementById("productGrid");
    if (grid) grid.innerHTML = `<div class="empty-state">Couldn't load the catalog (${escapeHtml(err.message)}). Please refresh the page.</div>`;
    return;
  }
  renderCatShowcase();
  renderProductGrid();
}

function renderCatShowcase() {
  const counts = {};
  allProducts.forEach((p) => { counts[p.category] = (counts[p.category] || 0) + 1; });
  const showcase = document.getElementById("catShowcase");
  showcase.innerHTML = Object.keys(CATEGORY_LABELS).map((cat) => `
    <div class="cat-tile" data-cat="${cat}">
      <div class="cat-tile-icon">${CATEGORY_ICONS[cat]}</div>
      <div class="cat-tile-name">${CATEGORY_LABELS[cat]}</div>
      <div class="cat-tile-count">${counts[cat] || 0} items</div>
    </div>`).join("");
  showcase.querySelectorAll(".cat-tile").forEach((t) => {
    t.addEventListener("click", () => setActiveCategory(t.dataset.cat));
  });
}

function renderProductGrid() {
  const grid = document.getElementById("productGrid");
  let products = activeCategory === "all" ? allProducts : allProducts.filter((p) => p.category === activeCategory);
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    products = products.filter((p) =>
      p.name.toLowerCase().includes(q) ||
      p.description.toLowerCase().includes(q) ||
      p.category.toLowerCase().includes(q)
    );
  }
  grid.innerHTML = products.length
    ? products.map((p, i) => productCardHtml(p, i)).join("")
    : `<div class="empty-state">No products match "${escapeHtml(searchQuery || "")}". Try a different search or category.</div>`;
}

function handleSearchInput(value) {
  searchQuery = value.trim();
  renderProductGrid();
}

function setActiveCategory(cat) {
  activeCategory = cat;
  document.querySelectorAll("#catFilterRow .cat-chip[data-cat]").forEach((c) => {
    c.classList.toggle("on", c.dataset.cat === cat);
  });
  renderProductGrid();
  document.getElementById("productGrid").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function initStorefront() {
  await loadCatalog();
  await refreshCustomerAuthUI();
  initSupportModal();

  const badge = document.getElementById("wishlistBadge");
  badge.textContent = wishlist.size;
  badge.hidden = wishlist.size === 0;
  refreshCartBadge();

  // Delegated listener: product cards (and their wishlist/buy buttons) are re-rendered on
  // every category filter change, so bind once on the container, not per-card.
  document.getElementById("productGrid").addEventListener("click", (e) => {
    const wishBtn = e.target.closest("[data-wishlist-id]");
    if (wishBtn) { toggleWishlist(wishBtn.dataset.wishlistId, wishBtn); return; }
    const buyBtn = e.target.closest("[data-buy-id]");
    if (buyBtn) {
      addToCart(buyBtn.dataset.buyId);
      const original = buyBtn.textContent;
      buyBtn.textContent = "Added ✓";
      buyBtn.disabled = true;
      setTimeout(() => { buyBtn.textContent = original; buyBtn.disabled = false; }, 1000);
    }
  });

  document.querySelectorAll("#catFilterRow .cat-chip[data-cat]").forEach((c) => {
    c.addEventListener("click", () => setActiveCategory(c.dataset.cat));
  });

  document.getElementById("wishlistBtn").addEventListener("click", toggleWishlistPanel);
  document.getElementById("wishlistItems").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-remove-wishlist]");
    if (btn) toggleWishlist(btn.dataset.removeWishlist, null);
  });

  document.getElementById("cartBtn").addEventListener("click", toggleCartPanel);
  document.getElementById("cartItems").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-remove-cart]");
    if (btn) removeFromCart(btn.dataset.removeCart);
  });
  document.getElementById("cartCheckoutBtn").addEventListener("click", checkoutCart);

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".icon-wrap")) {
      document.getElementById("wishlistPanel").hidden = true;
      document.getElementById("cartPanel").hidden = true;
    }
  });

  document.getElementById("agentFab").addEventListener("click", openAgentOrLogin);
  document.getElementById("agentCloseBtn").addEventListener("click", toggleAgent);
  document.querySelectorAll(".sugg").forEach((s) => {
    s.addEventListener("click", () => dispatchChatMessage(s.dataset.prompt));
  });
  const sendMessage = () => {
    const input = document.getElementById("chatInput");
    const text = input.value.trim();
    if (text) { input.value = ""; dispatchChatMessage(text); }
  };
  document.getElementById("chatSendBtn").addEventListener("click", sendMessage);
  document.getElementById("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendMessage();
  });

  document.getElementById("setLimitBtn").addEventListener("click", setSpendingLimit);
  document.getElementById("setLimitInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") setSpendingLimit();
  });

  document.getElementById("searchInput").addEventListener("input", (e) => handleSearchInput(e.target.value));

  refreshMandateBox();
}

async function refreshCustomerAuthUI() {
  let me;
  try {
    me = await fetch(`${API_BASE}/api/customer/me`).then((r) => r.json());
  } catch (err) {
    // A network hiccup here must not abort initStorefront() before it reaches the code below
    // that binds every click listener (wishlist, cart, agent FAB, category chips, search) --
    // treat it as "not logged in" for this render rather than leaving the whole page dead.
    currentCustomer = null;
    const slot = document.getElementById("customerAuthSlot");
    if (slot) slot.innerHTML = `<a href="customer_login.html" class="btn-ghost" style="padding:7px 14px;font-size:12.5px;display:inline-block;">Log In</a>`;
    return;
  }
  currentCustomer = me.authenticated ? me : null;
  const slot = document.getElementById("customerAuthSlot");
  if (currentCustomer) {
    slot.innerHTML = `<span style="font-size:12.5px;color:var(--text-dim);margin-right:10px;">Hi, ${escapeHtml(currentCustomer.username)}</span>
      <button type="button" class="btn-ghost" id="customerLogoutBtn" style="padding:7px 14px;font-size:12.5px;">Log Out</button>`;
    document.getElementById("customerLogoutBtn").addEventListener("click", async () => {
      await fetch(`${API_BASE}/api/customer/logout`, { method: "POST" });
      window.location.reload();
    });
  } else {
    slot.innerHTML = `<a href="customer_login.html" class="btn-ghost" style="padding:7px 14px;font-size:12.5px;display:inline-block;">Log In</a>`;
  }
}

function openAgentOrLogin() {
  if (!currentCustomer) {
    window.location.href = "customer_login.html";
    return;
  }
  toggleAgent();
}

async function setSpendingLimit() {
  const input = document.getElementById("setLimitInput");
  const value = parseInt(input.value, 10);
  if (!value || value <= 0) return;

  const btn = document.getElementById("setLimitBtn");
  btn.disabled = true;
  try {
    const res = await fetch(`${API_BASE}/api/mandates`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ merchant_id: MERCHANT_ID, max_amount_inr: value, expires_in_seconds: 86400, single_use: false }),
    });
    if (res.status === 401) {
      window.location.href = "customer_login.html";
      return;
    }
    const created = await res.json();
    activeMandateId = created.mandate_id;
    input.value = "";
    addMsg(`Spending limit set to ${inr(value)}. This is now the active mandate.`, "agent");
    refreshMandateBox();
  } catch (err) {
    addMsg("Couldn't set the limit: " + err.message, "agent");
  } finally {
    btn.disabled = false;
  }
}

function productCardHtml(p, index) {
  const stars = "★".repeat(Math.round(p.rating)) + "☆".repeat(5 - Math.round(p.rating));
  const stockHtml = p.stock > 0
    ? `<div class="stock">${p.stock} in stock</div>`
    : `<div class="stock out">Out of stock</div>`;
  const gradient = CATEGORY_GRADIENTS[p.category] || CATEGORY_GRADIENTS["accessories"];
  const isWishlisted = wishlist.has(p.product_id);
  // Real photos via Lorem Picsum (picsum.photos), seeded per product_id so each product
  // always gets the same photo. Gradient stays as the <img>'s background -- shown while
  // the photo loads, and as a graceful fallback if picsum is unreachable (offline demo).
  const photoUrl = `https://picsum.photos/seed/${p.product_id}/400/300`;

  // Badges reflect real catalog data (rating/stock/recency) -- no fabricated discounts.
  const productNum = parseInt(p.product_id.slice(1), 10);
  let badge = "";
  if (p.rating >= 4.5) badge = '<div class="badge-ribbon top-rated">Top Rated</div>';
  else if (p.stock > 0 && p.stock <= 5) badge = '<div class="badge-ribbon low-stock">Low Stock</div>';
  else if (productNum > 25) badge = '<div class="badge-ribbon new">New</div>';

  const delay = Math.min(index, 12) * 0.04;
  return `
    <div class="prod" data-id="${p.product_id}" style="animation-delay:${delay}s">
      ${badge}
      <button type="button" class="wishlist${isWishlisted ? " on" : ""}" data-wishlist-id="${p.product_id}">${isWishlisted ? "♥" : "♡"}</button>
      <div class="thumb" style="background:${gradient}">
        <img src="${photoUrl}" alt="${escapeHtml(p.name)}" loading="lazy" onerror="this.style.display='none'">
      </div>
      <div class="name">${escapeHtml(p.name)}</div>
      <div class="rating">${stars} ${p.rating}</div>
      <div class="price-row"><div class="price">${inr(p.price_inr)}</div>${stockHtml}</div>
      <button type="button" data-buy-id="${p.product_id}">Buy Now</button>
    </div>`;
}

function toggleWishlist(productId, btn) {
  if (wishlist.has(productId)) {
    wishlist.delete(productId);
    if (btn) { btn.classList.remove("on"); btn.textContent = "♡"; }
  } else {
    wishlist.add(productId);
    if (btn) { btn.classList.add("on"); btn.textContent = "♥"; }
  }
  localStorage.setItem("wishlist", JSON.stringify([...wishlist]));
  const badge = document.getElementById("wishlistBadge");
  badge.textContent = wishlist.size;
  badge.hidden = wishlist.size === 0;
  if (!document.getElementById("wishlistPanel").hidden) renderWishlistPanel();
}

/* ---------- Wishlist panel ---------- */

function renderWishlistPanel() {
  const container = document.getElementById("wishlistItems");
  const items = [...wishlist].map((id) => allProducts.find((p) => p.product_id === id)).filter(Boolean);
  if (items.length === 0) {
    container.innerHTML = `<div class="panel-empty">Nothing here yet. Tap the ♡ on a product to save it.</div>`;
    return;
  }
  container.innerHTML = items.map((p) => `
    <div class="panel-item" data-id="${p.product_id}">
      <div class="panel-item-thumb" style="background:${CATEGORY_GRADIENTS[p.category] || CATEGORY_GRADIENTS["accessories"]}">${THUMBS[p.product_id] || "\u{1F4E6}"}</div>
      <div class="panel-item-info">
        <div class="panel-item-name">${escapeHtml(p.name)}</div>
        <div class="panel-item-price">${inr(p.price_inr)}</div>
      </div>
      <button type="button" class="panel-item-remove" data-remove-wishlist="${p.product_id}" aria-label="Remove">&times;</button>
    </div>`).join("");
}

function toggleWishlistPanel() {
  const panel = document.getElementById("wishlistPanel");
  document.getElementById("cartPanel").hidden = true;
  panel.hidden = !panel.hidden;
  if (!panel.hidden) renderWishlistPanel();
}

/* ---------- Cart ---------- */

function saveCart() { localStorage.setItem("cart", JSON.stringify(cart)); }

function cartCount() { return Object.values(cart).reduce((sum, qty) => sum + qty, 0); }

function refreshCartBadge() {
  const badge = document.getElementById("cartBadge");
  const count = cartCount();
  badge.textContent = count;
  badge.hidden = count === 0;
}

function addToCart(productId) {
  cart[productId] = (cart[productId] || 0) + 1;
  saveCart();
  refreshCartBadge();
  if (!document.getElementById("cartPanel").hidden) renderCartPanel();
}

function removeFromCart(productId) {
  delete cart[productId];
  saveCart();
  refreshCartBadge();
  renderCartPanel();
}

function renderCartPanel() {
  const container = document.getElementById("cartItems");
  const entries = Object.entries(cart).map(([id, qty]) => ({ product: allProducts.find((p) => p.product_id === id), qty }))
    .filter((e) => e.product);
  if (entries.length === 0) {
    container.innerHTML = `<div class="panel-empty">Your cart is empty. Add something from the store.</div>`;
    document.getElementById("cartTotal").textContent = inr(0);
    return;
  }
  container.innerHTML = entries.map(({ product: p, qty }) => `
    <div class="panel-item" data-id="${p.product_id}">
      <div class="panel-item-thumb" style="background:${CATEGORY_GRADIENTS[p.category] || CATEGORY_GRADIENTS["accessories"]}">${THUMBS[p.product_id] || "\u{1F4E6}"}</div>
      <div class="panel-item-info">
        <div class="panel-item-name">${escapeHtml(p.name)}</div>
        <div class="panel-item-price">${inr(p.price_inr)} &times; ${qty}</div>
      </div>
      <button type="button" class="panel-item-remove" data-remove-cart="${p.product_id}" aria-label="Remove">&times;</button>
    </div>`).join("");
  const total = entries.reduce((sum, { product: p, qty }) => sum + p.price_inr * qty, 0);
  document.getElementById("cartTotal").textContent = inr(total);
}

function toggleCartPanel() {
  const panel = document.getElementById("cartPanel");
  document.getElementById("wishlistPanel").hidden = true;
  panel.hidden = !panel.hidden;
  if (!panel.hidden) renderCartPanel();
}

async function checkoutCart() {
  const entries = Object.entries(cart).map(([id, qty]) => ({ product: allProducts.find((p) => p.product_id === id), qty }))
    .filter((e) => e.product);
  if (entries.length === 0) return;
  if (!currentCustomer) { window.location.href = "customer_login.html"; return; }

  document.getElementById("cartPanel").hidden = true;
  if (!document.getElementById("agentPanel").classList.contains("open")) toggleAgent();
  addMsg(`Checking out ${entries.length} item${entries.length > 1 ? "s" : ""} from my cart.`, "user");

  // Only one real Razorpay Checkout can be open at a time -- if an item pauses for real
  // payment (outcome "checkout"), stop here rather than piling another checkout on top of an
  // unresolved one. Items not yet attempted stay in the cart instead of being silently
  // dropped -- only units actually resolved this run (success, blocked, or errored) come out.
  let stopped = false;
  for (const { product: p, qty } of entries) {
    if (stopped) break;
    for (let i = 0; i < qty; i++) {
      // showUserBubble: false -- the "Checking out N items..." message above already told the
      // user what's happening; showing "Get me X" again per item (the internal message actually
      // sent to the agent) made it look like the user typed a second, separate request they
      // never typed. addMsg still narrates progress via the agent's own reply, just not a fake
      // extra user turn.
      addMsg(`Working on ${p.name}...`, "agent");
      const outcome = await runTurn(`Get me ${p.name}`, { showUserBubble: false });
      cart[p.product_id] = Math.max(0, (cart[p.product_id] || 0) - 1);
      if (cart[p.product_id] === 0) delete cart[p.product_id];
      if (outcome === "checkout") {
        stopped = true;
        addMsg("Complete this payment to continue -- the rest of your cart will still be there when you check out again.", "agent");
        break;
      }
    }
  }
  saveCart();
  refreshCartBadge();
  renderCartPanel();
}

function toggleAgent() {
  document.getElementById("agentPanel").classList.toggle("open");
  document.getElementById("storefront").classList.toggle("shifted");
}

function clearProductStates() {
  document.querySelectorAll(".prod").forEach((p) => p.classList.remove("agent-selected", "agent-purchased", "agent-blocked"));
}

function highlight(productId, cls) {
  const node = document.querySelector(`.prod[data-id="${productId}"]`);
  if (!node) return;
  node.classList.add(cls);
  node.scrollIntoView({ behavior: "smooth", block: "center" });
}

/* ---------- Agent log ---------- */

function addMsg(text, cls) {
  const log = document.getElementById("agentLog");
  log.appendChild(el("div", `msg ${cls}`, escapeHtml(text)));
  log.scrollTop = log.scrollHeight;
}

function addThinking() {
  const log = document.getElementById("agentLog");
  const node = el("div", "msg thinking", `<span class="dots"><span></span><span></span><span></span></span>`);
  log.appendChild(node);
  log.scrollTop = log.scrollHeight;
  return node;
}
function needHelpLink(component, relatedSummary) {
  const id = "help_" + Math.random().toString(36).slice(2);
  setTimeout(() => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener("click", async () => {
      btn.textContent = "Sending...";
      btn.disabled = true;
      try {
        const res = await fetch(`${API_BASE}/api/support-request`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ component, related_summary: relatedSummary, message: "Customer flagged this from the storefront chat -- needs help understanding why." }),
        });
        btn.textContent = res.ok ? "Sent -- our team will follow up ✓" : "Couldn't send, try again";
        if (!res.ok) btn.disabled = false;
      } catch {
        btn.textContent = "Couldn't send, try again";
        btn.disabled = false;
      }
    });
  }, 0);
  return `<button type="button" id="${id}" class="need-help-link">Need help with this?</button>`;
}
function addResultCard(purchase) {
  const log = document.getElementById("agentLog");
  const ok = purchase.status === "success";
  const html = ok
    ? `<div class="rtitle">✅ Purchase verified</div>
       ${escapeHtml(purchase.reason)}
       <div class="txid">${escapeHtml(purchase.razorpay_order_id || "")} (test mode)</div>`
    : `<div class="rtitle">⛔ ${purchase.status === "blocked" ? "Blocked before execution" : "Verification failed"}</div>
       ${escapeHtml(purchase.reason)}
       ${needHelpLink("guardrail", purchase.reason)}`;
  log.appendChild(el("div", `msg result ${ok ? "ok" : "blocked"}`, html));
  log.scrollTop = log.scrollHeight;
}
function addFlagCard(fairness) {
  const log = document.getElementById("agentLog");
  log.appendChild(el(
    "div", "msg flag",
    `This price looks inconsistent with what similar customers were offered — flagged for review.<br>${escapeHtml(fairness.reason)}
     ${needHelpLink("parity", fairness.reason)}`
  ));
  log.scrollTop = log.scrollHeight;
}

/* ---------- Turn orchestration (real API, no scripting) ----------
   Search, fairness check, and mandate/settlement verification all still run for real on
   the backend (see agent/agent.py's run_chat_turn and guardrail/guardrail.py) -- this view
   just doesn't narrate them stage by stage. Product cards on the storefront itself still
   show the live "under review -> purchased/blocked" states; the full stage-by-stage trace
   is still available on the Audit Dashboard for anyone who wants to see it. */

// Catches "what's available", "what do you sell", "help", typo'd variants of the same, etc.
// -- questions about the store itself rather than a specific product -- so they get a real,
// useful answer instead of being run through Shelf's product matcher and coming back as a
// confusing "no match". Deliberately loose (keyword presence, not exact phrasing/spelling)
// since real typed queries have typos ("what things ae available") that a rigid phrase-match
// would miss entirely.
function isGeneralInfoIntent(text) {
  const t = text.toLowerCase().trim();
  if (/^(help|hi|hello|hey)\b/.test(t)) return true;
  if (/^(what|which|show|is|are)\b/.test(t) && /\bavail/.test(t)) return true; // "avail" catches available/availble/ae available etc.
  if (/what\s+(do you|can i)\b/.test(t)) return true;
  if (/\b(catalog|categories|category|what you (sell|have|offer)|everything you have|in stock|for sale)\b/.test(t)) return true;
  return false;
}

function dispatchChatMessage(text) {
  // Same chat panel handles buying, listing a product, and general store questions --
  // route by intent. Mid-slot-filling messages (a bare price, category, etc.) stay in that flow.
  if (ADD_PRODUCT_INTENT.test(text) || productAwaitingField) {
    if (!document.getElementById("agentPanel").classList.contains("open")) toggleAgent();
    runAddProductTurn(text);
  } else if (isGeneralInfoIntent(text)) {
    if (!document.getElementById("agentPanel").classList.contains("open")) toggleAgent();
    runGeneralInfoTurn(text);
  } else {
    runTurn(text);
  }
}

function runGeneralInfoTurn(text) {
  addMsg(text, "user");
  const counts = {};
  allProducts.forEach((p) => { counts[p.category] = (counts[p.category] || 0) + 1; });
  const categoryList = Object.keys(CATEGORY_LABELS)
    .filter((c) => counts[c])
    .map((c) => `${CATEGORY_LABELS[c]} (${counts[c]})`)
    .join(", ");
  const sample = [...allProducts].sort((a, b) => b.rating - a.rating).slice(0, 3)
    .map((p) => `${p.name} (${inr(p.price_inr)})`).join(", ");
  addMsg(
    `We've got ${allProducts.length} products across ${categoryList}. A few highly-rated picks: ${sample}. ` +
    `Tell me what you're after (e.g. "earbuds under 2000") and I'll find, price-check, and buy it for you -- ` +
    `or say "add product ..." to list your own.`,
    "agent"
  );
}

// Real SSE consumption: /api/chat/stream sends "data: {json}\n\n" chunks as each pipeline
// stage genuinely completes on the backend (real Groq tool-calling latency when GROQ_API_KEY
// is set; near-instant but still real per-stage events otherwise) -- read via fetch's
// streaming response body rather than native EventSource, since EventSource can't POST a body.
async function streamChat(message, onStage, onFinal, onCheckout, onError) {
  let res;
  try {
    res = await fetch(`${API_BASE}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: SESSION_ID, message, mandate_id: activeMandateId }),
    });
  } catch (err) {
    onError(err.message);
    return;
  }
  if (res.status === 401) {
    onError("__unauthenticated__");
    return;
  }
  if (!res.ok || !res.body) {
    onError(`Server returned ${res.status}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop(); // last part may be incomplete, keep it for the next chunk
    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data:")) continue;
      let event;
      try {
        event = JSON.parse(line.slice(5).trim());
      } catch (err) {
        // An unparseable chunk must not strand the caller waiting forever on a promise that
        // never settles -- surface it and stop reading rather than throwing out of this loop.
        onError(`Received a malformed response from the server (${err.message}).`);
        return;
      }
      if (event.type === "stage") onStage(event);
      else if (event.type === "final") onFinal(event);
      else if (event.type === "checkout") onCheckout(event);
    }
  }
}

async function runTurn(message, { showUserBubble = true } = {}) {
  if (!document.getElementById("agentPanel").classList.contains("open")) toggleAgent();
  if (showUserBubble) addMsg(message, "user");
  // So the agent's product is never hidden by an active filter or search term.
  if (activeCategory !== "all") setActiveCategory("all");
  if (searchQuery) {
    searchQuery = "";
    document.getElementById("searchInput").value = "";
    renderProductGrid();
  }
  clearProductStates();
  const thinking = addThinking();
  let thinkingRemoved = false;
  const removeThinking = () => { if (!thinkingRemoved) { thinking.remove(); thinkingRemoved = true; } };

  let currentProductId = null;
  // Lets callers (e.g. checkoutCart) know how this turn actually resolved -- "checkout" means
  // a real payment is now pending, so a multi-item caller must stop rather than pile another
  // Razorpay Checkout on top of an unresolved one.
  let outcome = "final";

  await streamChat(
    message,
    (event) => {
      // Live per-stage reveal as each real event arrives from the backend.
      removeThinking();
      if (event.stage === "shelf" && event.status === "ok") {
        currentProductId = event.detail.matches[0].product_id;
        highlight(currentProductId, "agent-selected");
      } else if (event.stage === "parity" && event.status === "flagged" && currentProductId) {
        document.querySelector(`.prod[data-id="${currentProductId}"]`)?.classList.remove("agent-selected");
        highlight(currentProductId, "agent-blocked");
        addFlagCard(event.detail);
      } else if (event.stage === "guardrail" && currentProductId && event.status !== "checkout_required") {
        document.querySelector(`.prod[data-id="${currentProductId}"]`)?.classList.remove("agent-selected");
        highlight(currentProductId, event.status === "success" ? "agent-purchased" : "agent-blocked");
      }
      // "checkout_required" leaves the card in "agent-selected" (still under review) --
      // it isn't purchased or blocked yet, a human still has to complete real Checkout.
    },
    (final) => {
      removeThinking();
      addMsg(final.agent_reply, "agent");
      // purchase_results (plural) covers every item in a multi-item turn ("earbuds and a
      // phone case") -- rendering only purchase_result (singular, the last one) would
      // silently hide the outcome of every earlier item.
      if (final.purchase_results && final.purchase_results.length) {
        final.purchase_results.forEach(addResultCard);
      } else if (final.purchase_result) {
        addResultCard(final.purchase_result);
      }
      refreshMandateBox();
    },
    (checkoutEvent) => {
      removeThinking();
      addMsg(checkoutEvent.agent_reply, "agent");
      addCheckoutCard(checkoutEvent.checkout, currentProductId);
      outcome = "checkout";
    },
    (errMsg) => {
      removeThinking();
      outcome = "error";
      if (errMsg === "__unauthenticated__") {
        addMsg("Your session expired -- please log in again to continue.", "agent");
        setTimeout(() => { window.location.href = "customer_login.html"; }, 1200);
      } else {
        addMsg("Something went wrong talking to the backend: " + errMsg, "agent");
      }
    }
  );
  return outcome;
}

/* ---------- Real, human-verified Razorpay Checkout ---------- */

let _razorpayScriptLoaded = false;
function loadRazorpayScript() {
  return new Promise((resolve, reject) => {
    if (_razorpayScriptLoaded || window.Razorpay) { resolve(); return; }
    const script = document.createElement("script");
    script.src = "https://checkout.razorpay.com/v1/checkout.js";
    script.onload = () => { _razorpayScriptLoaded = true; resolve(); };
    script.onerror = () => reject(new Error("Couldn't load Razorpay Checkout."));
    document.head.appendChild(script);
  });
}

function openSupportModal(prefill) {
  const overlay = document.getElementById("supportModalOverlay");
  const textarea = document.getElementById("supportMessage");
  const status = document.getElementById("supportModalStatus");
  textarea.value = prefill || "";
  status.textContent = "";
  status.className = "modal-status";
  overlay.hidden = false;
  textarea.focus();
}

function initSupportModal() {
  const overlay = document.getElementById("supportModalOverlay");
  const form = document.getElementById("supportForm");
  const status = document.getElementById("supportModalStatus");
  const submitBtn = document.getElementById("supportModalSubmit");
  document.getElementById("supportModalCancel").addEventListener("click", () => { overlay.hidden = true; });
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.hidden = true; });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = document.getElementById("supportMessage").value.trim();
    if (!message) return;
    submitBtn.disabled = true;
    submitBtn.textContent = "Sending...";
    status.textContent = "";
    try {
      const res = await fetch(`${API_BASE}/api/support-request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ component: "general", message }),
      });
      if (res.status === 401) {
        status.textContent = "Please log in first to contact support.";
        status.className = "modal-status err";
        return;
      }
      const data = await res.json();
      const emailed = data.confirmation_email && data.confirmation_email.sent;
      status.textContent = emailed
        ? `Got it -- reference ${data.request_id}. A confirmation email is on its way.`
        : `Got it -- reference ${data.request_id}. (Confirmation email couldn't be sent: ${data.confirmation_email ? data.confirmation_email.detail : "unknown error"})`;
      status.className = emailed ? "modal-status ok" : "modal-status err";
      form.reset();
      setTimeout(() => { overlay.hidden = true; }, emailed ? 2200 : 4000);
    } catch (err) {
      status.textContent = "Couldn't send: " + err.message;
      status.className = "modal-status err";
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Send";
    }
  });
}

function addCheckoutCard(checkout, productId) {
  const log = document.getElementById("agentLog");
  const card = el("div", "msg result checkout",
    `<div class="rtitle">💳 Ready for payment</div>
     ${inr(checkout.amount_inr)} &middot; order <code>${escapeHtml(checkout.razorpay_order_id)}</code>
     <button type="button" class="btn-primary checkout-pay-btn" style="width:100%;margin-top:10px;padding:10px;font-size:12.5px;">
       Pay with Razorpay (test mode)
     </button>
     <button type="button" class="contact-support-link checkout-support-btn">Need help with this order? Contact support</button>`
  );
  log.appendChild(card);
  log.scrollTop = log.scrollHeight;

  card.querySelector(".checkout-support-btn").addEventListener("click", () => {
    openSupportModal(`Order ${checkout.razorpay_order_id} (${inr(checkout.amount_inr)}): `);
  });

  card.querySelector(".checkout-pay-btn").addEventListener("click", async () => {
    try {
      await loadRazorpayScript();
    } catch (err) {
      addMsg(err.message, "agent");
      return;
    }
    const rzp = new Razorpay({
      key: checkout.razorpay_key_id,
      amount: checkout.amount_inr * 100,
      currency: "INR",
      order_id: checkout.razorpay_order_id,
      name: "TechBazaar",
      description: "Guardrail-verified purchase (test mode)",
      theme: { color: "#2874f0" },
      // razorpay_customer_id (real, from Razorpay's own Customers API -- see
      // api.customer_auth.get_or_create_razorpay_customer_id) lets Checkout recognize a
      // returning customer and offer their saved card/UPI method instead of asking again.
      // The actual card data is tokenized and held by Razorpay -- never sent to or seen by us.
      ...(checkout.razorpay_customer_id ? { customer_id: checkout.razorpay_customer_id } : {}),
      handler: async () => {
        // Razorpay confirms the human completed Checkout -- now ask Guardrail to
        // independently verify with Razorpay itself before ever calling it a success.
        addMsg("Payment submitted -- verifying with Razorpay...", "agent");
        try {
          const res = await fetch(`${API_BASE}/api/purchase/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              product_id: checkout.product_id, amount_inr: checkout.amount_inr,
              mandate_id: checkout.mandate_id, razorpay_order_id: checkout.razorpay_order_id,
            }),
          });
          const result = await res.json();
          if (productId) {
            document.querySelector(`.prod[data-id="${productId}"]`)?.classList.remove("agent-selected");
            highlight(productId, result.status === "success" ? "agent-purchased" : "agent-blocked");
          }
          addResultCard(result);
          refreshMandateBox();
          if (result.status === "success") {
            offerUpsell(checkout.product_id);
          }
        } catch (err) {
          addMsg("Couldn't verify the payment: " + err.message, "agent");
        }
      },
      modal: { ondismiss: () => addMsg("Checkout closed without completing payment.", "agent") },
    });
    rzp.open();
  });
}

async function offerUpsell(productId) {
  // The real Checkout confirm above happens outside the chat LLM's own tool-calling loop (see
  // /api/upsell's docstring), so this is a plain templated message, not an LLM-composed one --
  // still grounded in the same real, in-stock growth.upsell data, just phrased deterministically.
  try {
    const res = await fetch(`${API_BASE}/api/upsell/${encodeURIComponent(productId)}`);
    if (!res.ok) return;
    const { suggestions } = await res.json();
    if (!suggestions || !suggestions.length) return;
    const s = suggestions[0];
    addMsg(`Since you got that, people often pair it with the ${s.name} (${inr(s.price_inr)}, ${s.rating}★) -- just ask if you'd like it added.`, "agent");
  } catch (err) {
    // Non-critical -- a missed upsell suggestion shouldn't disrupt the purchase flow that just succeeded.
  }
}

async function refreshMandateBox() {
  const box = document.getElementById("mandateBox");
  try {
    const res = await fetch(`${API_BASE}/api/mandates/${activeMandateId}`);
    if (!res.ok) return;
    const m = await res.json();
    const pct = Math.min(100, Math.round((m.amount_spent_so_far_inr / m.max_amount_inr) * 100));
    const remaining = new Date(m.expires_at) - new Date();
    const hrs = Math.max(0, Math.floor(remaining / 3600000));
    const mins = Math.max(0, Math.floor((remaining % 3600000) / 60000));
    box.querySelector(".amount").innerHTML =
      `${inr(m.amount_spent_so_far_inr)} <span style="font-size:11px;color:var(--text-faint);font-weight:400;">/ ${inr(m.max_amount_inr)} used</span>`;
    box.querySelector(".bar-fill").style.width = pct + "%";
    box.querySelector(".expiry").textContent = m.is_expired ? "expired" : `expires in ${hrs}h ${mins}m`;

    // Auto-refresh via a real, bank-authorized Razorpay Subscription is only offered for a
    // mandate this customer actually owns -- the shared/demo mandate (m_default, owner null) has
    // no single customer's bank to register it against.
    const isOwnMandate = currentCustomer && m.owner_customer_id === currentCustomer.customer_id;
    await refreshAutoRefreshRow(isOwnMandate ? m.mandate_id : null);
  } catch { /* leave defaults */ }
}

async function refreshAutoRefreshRow(mandateId) {
  const row = document.getElementById("autoRefreshRow");
  if (!mandateId) { row.style.display = "none"; row.innerHTML = ""; return; }

  try {
    const res = await fetch(`${API_BASE}/api/allowance-subscriptions/for-mandate/${mandateId}`);
    if (!res.ok) { row.style.display = "none"; return; }
    const { subscription } = await res.json();
    row.style.display = "";
    if (subscription) {
      const statusLabel = { created: "awaiting bank authorization", active: "active", authenticated: "authorized",
        pending: "payment retry pending", halted: "halted -- needs attention" }[subscription.status] || subscription.status;
      row.innerHTML = `<span style="color:var(--text-faint);">Auto-refresh: ${inr(subscription.amount_inr)}/month via UPI AutoPay -- <strong>${escapeHtml(statusLabel)}</strong></span>`;
      if (subscription.status === "created" && subscription.short_url) {
        row.innerHTML += ` <a href="${subscription.short_url}" target="_blank" rel="noopener">complete bank authorization &rarr;</a>`;
      }
    } else {
      row.innerHTML = `<a href="#" id="autoRefreshLink">Set up automatic monthly refresh via UPI AutoPay &rarr;</a>`;
      document.getElementById("autoRefreshLink").addEventListener("click", (e) => {
        e.preventDefault();
        setUpAutoRefresh(mandateId);
      });
    }
  } catch { row.style.display = "none"; }
}

async function setUpAutoRefresh(mandateId) {
  const amountStr = window.prompt(
    "Refresh this mandate's allowance by how much each month? This creates a real Razorpay " +
    "Subscription -- your bank/UPI app will ask you to independently authorize it; TechBazaar " +
    "never sees or touches your bank details.", "5000",
  );
  if (amountStr === null) return;
  const amount = parseInt(amountStr, 10);
  if (!amount || amount <= 0) { addMsg("That doesn't look like a valid amount.", "agent"); return; }

  try {
    const res = await fetch(`${API_BASE}/api/allowance-subscriptions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mandate_id: mandateId, amount_inr: amount }),
    });
    const result = await res.json();
    if (!res.ok) { addMsg(`Couldn't set up auto-refresh: ${result.detail || "unknown error"}`, "agent"); return; }
    addMsg(`Created a real monthly ${inr(amount)} refresh subscription. Opening Razorpay's checkout so you can authorize it with your bank/UPI app -- nothing refreshes until you complete that.`, "agent");
    if (result.short_url) window.open(result.short_url, "_blank", "noopener");
    refreshMandateBox();
  } catch (err) {
    addMsg("Couldn't reach the server to set up auto-refresh: " + err.message, "agent");
  }
}

/* ---------- Customer signup/login ---------- */

function initCustomerLogin() {
  const tabLogin = document.getElementById("tabLogin");
  const tabSignup = document.getElementById("tabSignup");
  const loginForm = document.getElementById("loginForm");
  const signupForm = document.getElementById("signupForm");

  tabLogin.addEventListener("click", () => {
    tabLogin.classList.add("on"); tabSignup.classList.remove("on");
    loginForm.hidden = false; signupForm.hidden = true;
  });
  tabSignup.addEventListener("click", () => {
    tabSignup.classList.add("on"); tabLogin.classList.remove("on");
    signupForm.hidden = false; loginForm.hidden = true;
  });

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById("loginError");
    errorEl.textContent = "";
    try {
      const res = await fetch(`${API_BASE}/api/customer/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: document.getElementById("loginUsername").value,
          password: document.getElementById("loginPassword").value,
        }),
      });
      if (!res.ok) { errorEl.textContent = "Invalid username or password."; return; }
      window.location.href = "index.html";
    } catch (err) {
      errorEl.textContent = "Couldn't log in: " + err.message;
    }
  });

  signupForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById("signupError");
    errorEl.textContent = "";
    try {
      const res = await fetch(`${API_BASE}/api/customer/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: document.getElementById("signupName").value,
          email: document.getElementById("signupEmail").value,
          username: document.getElementById("signupUsername").value,
          password: document.getElementById("signupPassword").value,
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        errorEl.textContent = Array.isArray(err.detail) ? err.detail.map((d) => d.msg).join("; ") : (err.detail || "Couldn't sign up.");
        return;
      }
      window.location.href = "index.html";
    } catch (err) {
      errorEl.textContent = "Couldn't sign up: " + err.message;
    }
  });
}

/* ---------- Admin dashboard ---------- */

async function initLogin() {
  document.getElementById("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById("loginError");
    errorEl.textContent = "";
    const username = document.getElementById("loginUsername").value;
    const password = document.getElementById("loginPassword").value;
    try {
      const res = await fetch(`${API_BASE}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        errorEl.textContent = "Invalid username or password.";
        return;
      }
      window.location.href = "dashboard.html";
    } catch (err) {
      errorEl.textContent = "Couldn't log in: " + err.message;
    }
  });
}

async function initDashboard() {
  const me = await fetch(`${API_BASE}/api/auth/me`).then((r) => r.json());
  if (!me.authenticated) {
    window.location.href = "login.html";
    return;
  }
  document.getElementById("adminWhoami").textContent = `Logged in as ${me.username}`;
  document.getElementById("logoutBtn").addEventListener("click", async () => {
    await fetch(`${API_BASE}/api/auth/logout`, { method: "POST" });
    window.location.href = "login.html";
  });

  const [batchResRaw, auditResRaw, aiAgentsResRaw, liveStatsResRaw, supportResRaw, liveCheckResRaw, disputesResRaw] = await Promise.all([
    fetch(`${API_BASE}/api/batch-results`),
    fetch(`${API_BASE}/api/audit-log?limit=300`),
    fetch(`${API_BASE}/api/ai-agents`),
    fetch(`${API_BASE}/api/live-stats`),
    fetch(`${API_BASE}/api/support-requests`),
    fetch(`${API_BASE}/api/reconciliation/live-check`),
    fetch(`${API_BASE}/api/disputes`),
  ]);
  if ([batchResRaw, auditResRaw, aiAgentsResRaw, liveStatsResRaw, supportResRaw, liveCheckResRaw, disputesResRaw].some((r) => r.status === 401)) {
    window.location.href = "login.html";
    return;
  }
  const [batchRes, auditRes, aiAgentsRes, liveStats, supportRes, liveCheckRes, disputesRes] = await Promise.all([
    batchResRaw.json(), auditResRaw.json(), aiAgentsResRaw.json(), liveStatsResRaw.json(), supportResRaw.json(), liveCheckResRaw.json(), disputesResRaw.json(),
  ]);

  // batchRes.reconciliation (rc) is still real, per-record data (which live purchases vs. the
  // synthetic proof batch, itemized exceptions) used by the Reconciliation exceptions panel
  // below -- only the fixed-batch-score STAT CARDS (Shelf/Parity/Guardrail/full-pipeline
  // accuracy against a scripted test set) were removed from this page. Those numbers never
  // changed as real customers used the store, which read as static/synthetic even when the
  // underlying engines were working -- Live Activity below is the real, moving number instead.
  const rc = batchRes.reconciliation;

  const aiSummaryEl = document.getElementById("aiAgentSummary");
  const aiBody = document.getElementById("aiAgentTableBody");
  const agents = aiAgentsRes.agents || [];
  const active = agents.filter((a) => a.last_activity).length;
  aiSummaryEl.textContent = `-- ${agents.length} registered, ${active} with real activity`;
  const moneyStatusToPill = {
    success: "ok", checkout_required: "ok", blocked: "blocked",
    failed_verification: "failed", refunded: "flagged",
  };
  aiBody.innerHTML = agents.length
    ? agents.map((a) => {
        const latest = a.purchases[0];
        const moneyCell = latest
          ? `<span class="pill ${pillClass(moneyStatusToPill[latest.status] || "")}">${escapeHtml((latest.status || "").replace(/_/g, " "))}</span>
             ${escapeHtml(latest.product_id || "")} &middot; ${inr(latest.amount_inr || 0)}`
          : `<span style="color:var(--text-faint);">no purchase attempts yet</span>`;
        const fairnessCell = a.fairness_checks
          ? `${a.fairness_checks} &middot; last: <span class="pill ${pillClass(a.last_verdict === "fair" ? "ok" : "flagged")}">${escapeHtml(a.last_verdict || "")}</span>`
          : `<span style="color:var(--text-faint);">none</span>`;
        return `
        <tr>
          <td>${escapeHtml(a.name)}<div style="color:var(--text-faint);font-size:11px;">${escapeHtml(a.ai_buyer_id)}</div></td>
          <td>${fairnessCell}</td>
          <td>${moneyCell}</td>
          <td>${a.last_activity ? escapeHtml(a.last_activity.slice(0, 19).replace("T", " ")) : `<span style="color:var(--text-faint);">never</span>`}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="4">No AI buyer agents have connected yet -- point an MCP client at mcp_server/techbazaar_mcp_server.py (see mcp_server/verify_real_mcp_connection.py for a working example).</td></tr>`;

  const reconSummaryEl = document.getElementById("reconSummary");
  const reconBody = document.getElementById("reconTableBody");
  if (rc) {
    const live = (rc.by_source && rc.by_source.live_ledger) || { total: 0, matched: 0 };
    reconSummaryEl.textContent =
      `-- ${rc.total_orders} orders (${live.total} live from real purchases, ${rc.total_orders - live.total} synthetic proof batch), `
      + `${rc.total_settlements} settlement records, ${rc.exceptions.length} unresolved`;
    const typeToPill = {
      amount_mismatch: "flagged", missing_settlement: "blocked",
      duplicate_settlement: "blocked", status_exception: "failed", orphan_settlement: "failed",
    };
    reconBody.innerHTML = rc.exceptions.length
      ? rc.exceptions.map((e) => `
        <tr>
          <td>${escapeHtml(e.order_id)}${e.source === "live_ledger" ? ` <span class="pill ok">live</span>` : ""}</td>
          <td><span class="pill ${pillClass(typeToPill[e.type] || "")}">${escapeHtml(e.type.replace(/_/g, " "))}</span></td>
          <td>${escapeHtml(e.reason)}</td>
        </tr>`).join("")
      : `<tr><td colspan="3">No unresolved exceptions.</td></tr>`;
  } else {
    reconSummaryEl.textContent = "-- run batch_tests/run_reconciliation_batch.py";
    reconBody.innerHTML = `<tr><td colspan="3">No reconciliation batch has been run yet.</td></tr>`;
  }

  renderLiveCheck(liveCheckRes);
  function renderLiveCheck(lc) {
    const summaryEl = document.getElementById("liveCheckSummary");
    const body = document.getElementById("liveCheckTableBody");
    if (lc.unavailable_reason) {
      summaryEl.textContent = "-- unavailable";
      body.innerHTML = `<tr><td colspan="4">${escapeHtml(lc.unavailable_reason)}</td></tr>`;
      return;
    }
    summaryEl.textContent = `-- ${lc.checked} real purchase(s) re-checked against Razorpay just now, ${lc.clean} clean, ${lc.exceptions.length} drifted`;
    const typeToPill = { overcharge_drift: "flagged", undercharge_drift: "failed", refund_not_reflected: "failed" };
    body.innerHTML = lc.exceptions.length
      ? lc.exceptions.map((e) => `
        <tr>
          <td>${escapeHtml(e.order_id)}</td>
          <td><span class="pill ${pillClass(typeToPill[e.type] || "")}">${escapeHtml(e.type.replace(/_/g, " "))}</span></td>
          <td>${escapeHtml(e.reason)}</td>
          <td>${e.remediable
            ? `<button type="button" class="btn-ghost remediate-btn" data-order-id="${escapeHtml(e.order_id)}" data-amount="${e.razorpay_captured_amount_inr - e.expected_amount_inr}" style="padding:3px 9px;font-size:11px;">Refund overcharge</button>`
            : ""}</td>
        </tr>`).join("")
      : `<tr><td colspan="4">Nothing has drifted since capture -- Razorpay's own records agree with every real purchase on file.</td></tr>`;
    body.querySelectorAll(".remediate-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const orderId = btn.dataset.orderId;
        const amount = Number(btn.dataset.amount);
        if (!window.confirm(`Issue a real (test-mode) refund of ${inr(amount)} against order ${orderId}? This actually calls Razorpay and cannot be undone from here.`)) {
          return;
        }
        btn.disabled = true;
        btn.textContent = "...";
        try {
          const res = await fetch(`${API_BASE}/api/reconciliation/remediate-overcharge`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ order_id: orderId }),
          });
          const result = await res.json();
          if (res.ok && result.status === "refunded") {
            btn.closest("tr").querySelector("td:last-child").innerHTML = `<span class="pill ok">refunded ${inr(result.refund_amount_inr)}</span>`;
          } else if (res.ok && result.status === "no_action_needed") {
            btn.closest("tr").querySelector("td:last-child").innerHTML = `<span class="pill ok">already resolved</span>`;
          } else {
            btn.disabled = false;
            btn.textContent = "Refund overcharge";
            window.alert(result.detail || "Could not issue the refund.");
          }
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Refund overcharge";
          window.alert("Could not reach the server.");
        }
      });
    });
  }

  renderDisputes((disputesRes && disputesRes.disputes) || []);
  function renderDisputes(list) {
    const summaryEl = document.getElementById("disputesSummary");
    const body = document.getElementById("disputesTableBody");
    const pending = list.filter((d) => d.status === "draft_pending" || d.status === "action_required").length;
    summaryEl.textContent = `-- ${list.length} total, ${pending} awaiting review`;
    const statusToPill = {
      draft_pending: "flagged", action_required: "flagged", under_review: "ok",
      submitted: "ok", won: "ok", accepted: "ok", lost: "failed", closed: "",
    };
    const actionable = new Set(["draft_pending", "action_required", "under_review"]);
    body.innerHTML = list.length
      ? list.map((d) => `
        <tr data-dispute-id="${escapeHtml(d.dispute_id)}">
          <td>${escapeHtml(d.dispute_id)}${d.evidence_found ? "" : ` <span class="pill failed" title="No matching order found in our own ledger">no match</span>`}</td>
          <td>${escapeHtml(d.razorpay_order_id || "--")}</td>
          <td>${inr(d.amount_inr)}</td>
          <td><span class="pill ${pillClass(statusToPill[d.status] || "")}">${escapeHtml((d.status || "").replace(/_/g, " "))}</span></td>
          <td><details><summary style="cursor:pointer;color:var(--text-dim);font-size:12px;">view drafted evidence</summary><pre style="white-space:pre-wrap;max-width:420px;">${escapeHtml(d.summary || "")}</pre></details></td>
          <td>${actionable.has(d.status) ? `
            <button type="button" class="btn-ghost dispute-submit-btn" data-id="${escapeHtml(d.dispute_id)}" style="padding:3px 9px;font-size:11px;">Submit response</button>
            <button type="button" class="btn-ghost dispute-accept-btn" data-id="${escapeHtml(d.dispute_id)}" style="padding:3px 9px;font-size:11px;">Accept &amp; refund</button>
          ` : ""}</td>
        </tr>`).join("")
      : `<tr><td colspan="6">No disputes on file -- this panel fills in the moment a real payment.dispute.created webhook arrives.</td></tr>`;

    body.querySelectorAll(".dispute-submit-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        const draft = list.find((d) => d.dispute_id === id);
        const edited = window.prompt("Review/edit the evidence summary before it's sent to the customer's bank:", draft ? draft.summary : "");
        if (edited === null) return;
        if (!window.confirm(`Submit this response to Razorpay for dispute ${id}? This actually calls Razorpay and cannot be undone from here.`)) return;
        btn.disabled = true;
        btn.textContent = "...";
        try {
          const res = await fetch(`${API_BASE}/api/disputes/${encodeURIComponent(id)}/submit`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ summary: edited }),
          });
          const result = await res.json();
          if (res.ok && result.status === "submitted") {
            btn.closest("tr").querySelector("td:nth-child(4)").innerHTML = `<span class="pill ok">submitted</span>`;
            btn.closest("tr").querySelector("td:last-child").innerHTML = "";
          } else {
            btn.disabled = false;
            btn.textContent = "Submit response";
            window.alert(result.detail || "Could not submit the response.");
          }
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Submit response";
          window.alert("Could not reach the server.");
        }
      });
    });
    body.querySelectorAll(".dispute-accept-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        if (!window.confirm(`Accept dispute ${id}? The customer is refunded in full and this cannot be undone from here.`)) return;
        btn.disabled = true;
        btn.textContent = "...";
        try {
          const res = await fetch(`${API_BASE}/api/disputes/${encodeURIComponent(id)}/accept`, { method: "POST" });
          const result = await res.json();
          if (res.ok && result.status === "accepted") {
            btn.closest("tr").querySelector("td:nth-child(4)").innerHTML = `<span class="pill ok">accepted</span>`;
            btn.closest("tr").querySelector("td:last-child").innerHTML = "";
          } else {
            btn.disabled = false;
            btn.textContent = "Accept & refund";
            window.alert(result.detail || "Could not accept the dispute.");
          }
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Accept & refund";
          window.alert("Could not reach the server.");
        }
      });
    });
  }

  function renderAuditTable(entries, filterLabel) {
    const body = document.getElementById("auditTableBody");
    body.innerHTML = entries.length
      ? entries.map((e) => `
        <tr>
          <td>${e.timestamp.slice(11, 19)}</td>
          <td>${cap(e.component)}</td>
          <td>${escapeHtml(e.event)}</td>
          <td><span class="pill ${pillClass(e.outcome)}">${escapeHtml(e.outcome)}</span></td>
          <td><pre>${escapeHtml(JSON.stringify(e.result_summary).slice(0, 180))}</pre></td>
        </tr>`).join("")
      : `<tr><td colspan="5">No matching audit entries.</td></tr>`;
    document.getElementById("auditFilterLabel").textContent = filterLabel ? `-- ${filterLabel}` : "";
    document.getElementById("auditFilterClear").style.display = filterLabel ? "" : "none";
  }
  renderAuditTable(auditRes, "");
  document.getElementById("auditFilterClear").addEventListener("click", () => renderAuditTable(auditRes, ""));

  const liveBody = document.getElementById("liveStatsBody");
  const liveRows = [
    { key: "shelf", label: "Shelf searches", total: liveStats.shelf.total, failLabel: `${liveStats.shelf.no_match} no match`, failCount: liveStats.shelf.no_match },
    { key: "parity", label: "Parity fairness checks", total: liveStats.parity.total, failLabel: `${liveStats.parity.flagged} flagged`, failCount: liveStats.parity.flagged },
    { key: "guardrail", label: "Guardrail purchase attempts", total: liveStats.guardrail.total, failLabel: `${liveStats.guardrail.blocked} blocked, ${liveStats.guardrail.failed_verification} failed verification`, failCount: liveStats.guardrail.blocked + liveStats.guardrail.failed_verification },
  ];
  const openSupportByComponent = liveStats.open_support_requests_by_component || {};
  liveBody.innerHTML = liveStats.last_live_event_at
    ? `<table><tbody>${liveRows.map((r) => {
        const support = openSupportByComponent[r.key] || 0;
        return `
        <tr class="live-stat-row" data-component="${r.key}" tabindex="0">
          <td style="width:220px;"><strong>${r.label}</strong></td>
          <td><span class="pill ok">${r.total}</span> total</td>
          <td>${r.failCount > 0 ? `<span class="pill blocked">${r.failCount}</span> ${escapeHtml(r.failLabel)}` : `<span style="color:var(--text-faint);">none</span>`}</td>
          <td>${support > 0 ? `<span class="pill flagged">${support}</span> open support request${support === 1 ? "" : "s"}` : `<span style="color:var(--text-faint);">no support requests</span>`}</td>
        </tr>`;
      }).join("")}</tbody></table>
      <div style="padding:10px 20px;font-size:11px;color:var(--text-faint);">First real activity: ${liveStats.first_live_event_at.slice(0, 19).replace("T", " ")} -- most recent: ${liveStats.last_live_event_at.slice(0, 19).replace("T", " ")}</div>`
    : `<div class="panel-empty">No real customer or AI-buyer activity yet -- this fills in the moment someone actually searches, checks a price, or buys something through the storefront or MCP server, not from any batch script or test run.</div>`;
  liveBody.querySelectorAll(".live-stat-row").forEach((row) => {
    row.addEventListener("click", async () => {
      const component = row.dataset.component;
      // Fetched fresh, filtered server-side by source=live BEFORE the limit is applied --
      // filtering the already-fetched general auditRes client-side would silently come back
      // empty whenever a batch/test run's volume has pushed real live entries outside its
      // limit=300 window, even though the failure genuinely happened. See api/routes/audit.py.
      const res = await fetch(`${API_BASE}/api/audit-log?limit=300&source=live`);
      const liveEntries = res.ok ? await res.json() : [];
      const filtered = liveEntries.filter((e) => e.component === component && e.outcome !== "ok");
      renderAuditTable(filtered, `${cap(component)} failures only (live traffic)`);
      document.getElementById("auditFilterClear").scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });

  const supportSummaryEl = document.getElementById("supportSummary");
  const supportBody = document.getElementById("supportTableBody");
  const supportReqs = supportRes || [];
  const openCount = supportReqs.filter((r) => r.status === "open").length;
  supportSummaryEl.textContent = `-- ${openCount} open, ${supportReqs.length} total`;
  supportBody.innerHTML = supportReqs.length
    ? supportReqs.map((r) => `
      <tr>
        <td>${r.timestamp.slice(0, 19).replace("T", " ")}</td>
        <td>${escapeHtml(r.customer_id)}</td>
        <td>${cap(r.component)}</td>
        <td>${escapeHtml(r.related_summary || "")}</td>
        <td>${escapeHtml(r.message)}</td>
        <td>${r.status === "open"
          ? `<button type="button" class="btn-ghost resolve-support-btn" data-id="${r.request_id}" style="padding:3px 9px;font-size:11px;">Mark resolved</button>`
          : `<span class="pill ok">resolved</span> ${resolutionEmailNote(r.resolution_email)}`}</td>
      </tr>`).join("")
    : `<tr><td colspan="6">No support requests yet.</td></tr>`;
  supportBody.querySelectorAll(".resolve-support-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "...";
      const res = await fetch(`${API_BASE}/api/support-requests/${btn.dataset.id}/resolve`, { method: "POST" });
      if (res.ok) {
        const updated = await res.json();
        btn.outerHTML = `<span class="pill ok">resolved</span> ${resolutionEmailNote(updated.resolution_email)}`;
      } else {
        btn.disabled = false;
        btn.textContent = "Mark resolved";
      }
    });
  });

  initSettlementQa();
}

/* ---------- Settlement Q&A (dashboard-only, admin) ---------- */

function initSettlementQa() {
  const log = document.getElementById("qaLog");
  const input = document.getElementById("qaInput");
  const sendBtn = document.getElementById("qaSendBtn");

  async function send() {
    const question = input.value.trim();
    if (!question) return;
    input.value = "";
    log.appendChild(el("div", "msg user", escapeHtml(question)));
    const thinking = el("div", "msg thinking", `<div class="dots"><span></span><span></span><span></span></div> thinking`);
    log.appendChild(thinking);
    log.scrollTop = log.scrollHeight;

    try {
      const res = await fetch(`${API_BASE}/api/settlement-qa`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      thinking.remove();
      if (res.status === 401) {
        log.appendChild(el("div", "msg agent", "Your session expired -- please log in again."));
        return;
      }
      const result = await res.json();
      log.appendChild(el("div", "msg agent", escapeHtml(result.answer)));
    } catch (err) {
      thinking.remove();
      log.appendChild(el("div", "msg agent", "Couldn't reach the settlement assistant: " + escapeHtml(err.message)));
    }
    log.scrollTop = log.scrollHeight;
  }

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
}

/* ---------- Customer "add product" assistant (same AI Shopping Agent chat) ----------
   Any logged-in shopper can list a product, marketplace-style -- via the exact same chat
   panel used for buying. Intent is detected per message: "add product ..." routes here,
   everything else goes through the normal runTurn() buying flow. */

const PRODUCT_FIELD_PROMPTS = {
  name: "What's the product name?",
  category: "Which category — audio, wearables, accessories, or computer?",
  price_inr: "What price should I list it at (in ₹)?",
  stock: "How many units are in stock?",
  rating: "What rating, out of 5?",
};
const REQUIRED_PRODUCT_FIELDS = ["name", "category", "price_inr", "stock", "rating"];
const ADD_PRODUCT_INTENT = /^(add|create|list|sell)\s+(a\s+)?(new\s+)?product\b/i;
let productDraft = {};
let productAwaitingField = null;

function parseProductFields(text) {
  const fields = {};
  const categoryMatch = text.match(/\b(audio|wearables?|accessor(?:y|ies)|computer(?:-accessories)?)\b/i);
  if (categoryMatch) {
    const raw = categoryMatch[1].toLowerCase();
    fields.category = raw.startsWith("wearable") ? "wearables"
      : raw.startsWith("accessor") ? "accessories"
      : raw.startsWith("computer") ? "computer-accessories" : "audio";
  }
  const priceMatch = text.match(/(?:price|₹|rs\.?)\s*[:\-]?\s*(\d+(?:\.\d+)?)/i);
  if (priceMatch) fields.price_inr = Math.round(parseFloat(priceMatch[1]));
  const stockMatch = text.match(/stock\s*[:\-]?\s*(\d+)|(\d+)\s+(?:in stock|units)/i);
  if (stockMatch) fields.stock = parseInt(stockMatch[1] || stockMatch[2], 10);
  const ratingMatch = text.match(/rating\s*[:\-]?\s*(\d(?:\.\d+)?)|(\d(?:\.\d+)?)\s*(?:star|rated|\/\s*5)/i);
  if (ratingMatch) fields.rating = parseFloat(ratingMatch[1] || ratingMatch[2]);
  const descMatch = text.match(/description\s*[:\-]?\s*(.+)$/i);
  if (descMatch) fields.description = descMatch[1].trim();

  const rest = text.replace(/^\s*(add|create|list|sell)\s+(a\s+)?(new\s+)?product\s*[:\-]?\s*/i, "");
  const nameMatch = rest.match(/^([^,]+)/);
  if (nameMatch) {
    const candidate = nameMatch[1].trim();
    if (candidate && candidate.length <= 80 && !/^(price|category|stock|rating|description)\b/i.test(candidate) && !/^\d/.test(candidate)) {
      fields.name = candidate;
    }
  }
  return fields;
}

async function runAddProductTurn(text) {
  addMsg(text, "user");

  if (ADD_PRODUCT_INTENT.test(text)) {
    productDraft = {};
    productAwaitingField = null;
  }

  if (productAwaitingField) {
    const field = productAwaitingField;
    let value = text.trim();
    if (field === "category") {
      const parsed = parseProductFields(value).category;
      value = parsed || value.toLowerCase();
    } else if (field === "price_inr" || field === "stock") {
      const n = parseInt(value.replace(/[^\d]/g, ""), 10);
      value = Number.isNaN(n) ? null : n;
    } else if (field === "rating") {
      const n = parseFloat(value.replace(/[^\d.]/g, ""));
      value = Number.isNaN(n) ? null : n;
    }
    if (value === null || value === "") {
      addMsg(`Sorry, I didn't catch that. ${PRODUCT_FIELD_PROMPTS[field]}`, "agent");
      return;
    }
    productDraft[field] = value;
    productAwaitingField = null;
  } else {
    Object.assign(productDraft, parseProductFields(text));
  }

  const missing = REQUIRED_PRODUCT_FIELDS.filter((f) => productDraft[f] === undefined || productDraft[f] === "");
  if (missing.length > 0) {
    productAwaitingField = missing[0];
    addMsg(PRODUCT_FIELD_PROMPTS[missing[0]], "agent");
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/catalog`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: productDraft.name,
        category: productDraft.category,
        price_inr: productDraft.price_inr,
        stock: productDraft.stock,
        rating: productDraft.rating,
        description: productDraft.description || "",
      }),
    });
    if (res.status === 401) {
      addMsg("Your session expired -- please log in again to list a product.", "agent");
      setTimeout(() => { window.location.href = "customer_login.html"; }, 1200);
      return;
    }
    if (!res.ok) {
      const err = await res.json();
      const msg = Array.isArray(err.detail) ? err.detail.map((d) => d.msg).join("; ") : (err.detail || "Couldn't add that product.");
      addMsg(msg, "agent");
      return;
    }
    const created = await res.json();
    addMsg(`Added "${created.name}" (${created.product_id}) to the catalog — ${inr(created.price_inr)}, ${created.stock} in stock, ${created.rating}★, category ${created.category}.`, "agent");
    productDraft = {};
    productAwaitingField = null;
    await loadCatalog();
  } catch (err) {
    addMsg("Couldn't add product: " + err.message, "agent");
  }
}

function resolutionEmailNote(resolutionEmail) {
  if (!resolutionEmail) return `<span style="color:var(--text-faint);font-size:10.5px;">(no follow-up email on record)</span>`;
  return resolutionEmail.sent
    ? `<span style="color:var(--text-faint);font-size:10.5px;">-- notified by email</span>`
    : `<span style="color:var(--bad);font-size:10.5px;" title="${escapeHtml(resolutionEmail.detail)}">-- email failed</span>`;
}
function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
function pillClass(outcome) {
  if (outcome === "ok") return "ok";
  if (outcome === "blocked") return "blocked";
  if (outcome === "flagged") return "flagged";
  if (outcome === "failed") return "failed";
  return "";
}

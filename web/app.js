"use strict";

// Food Cat web page. No frameworks and no build step.
// Everything the server sends is put on the page as plain text (createTextNode and textContent,
// never innerHTML), so an address or a name that contains HTML can't run as code.

const AREAS = {
  "Clifton": [24.8138, 67.0300],
  "DHA": [24.8010, 67.0640],
  "Saddar": [24.8607, 67.0099],
  "Gulshan": [24.9215, 67.0920],
  "North Nazimabad": [24.9400, 67.0400],
};

// How far along an order is, taken from the alerts the customer received.
// The tone picks the colour of the banner: good is green, wait is mustard yellow, bad is red.
const STAGES = {
  payment_received: { label: "Confirmed", rank: 1, tone: "good" },
  rider_assigned: { label: "Rider assigned", rank: 2, tone: "wait" },
  picked_up: { label: "On the way", rank: 3, tone: "wait" },
  delivered: { label: "Delivered", rank: 4, tone: "good" },
  payment_declined: { label: "Cancelled: the card was declined", rank: 4, tone: "bad" },
};

const state = {
  token: null,
  user: null,
  authMode: "login",
  tab: "home",          // home, restaurants (the same page, scrolled to the restaurants), orders or system
  search: "",
  cuisine: "All",
  menu: null,
  menuNote: "",
  basket: { restaurantId: null, qty: {} },
  address: "",
  card: "tok_visa",
  orders: [],
  jobs: [],
  rider: { online: false, busy_with: null },
  alerts: [],
  monitor: null,
  servedBy: "",
  area: "Clifton",
  version: null,        // which build is live, from /api/version (shown at the bottom of every page)
};

// ---------------------------------------------------------------- helpers

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "value") el.value = value;
    else if (["disabled", "checked", "selected", "required"].includes(key)) el[key] = Boolean(value);
    else if (value !== null && value !== undefined && value !== false) el.setAttribute(key, value);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);

// Put these children into a panel. Lists are flattened and empty values are skipped.
function fill(box, ...kids) {
  box.replaceChildren(...kids.flat().filter((kid) => kid !== null && kid !== undefined && kid !== false));
}

// The page asks for fresh data every few seconds. A panel is only redrawn when its data changed,
// so an open drop down or a scrolled list is not reset for nothing.
const lastSeen = {};
function changed(key, data) {
  const now = JSON.stringify(data);
  if (lastSeen[key] === now) return false;
  lastSeen[key] = now;
  return true;
}
const rs = (n) => "Rs " + Number(n).toLocaleString("en-US");
const clock = (seconds) => new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

let noticeTimer = null;
function notify(text, bad) {
  const box = $("notice");
  box.textContent = text;
  box.className = "notice show" + (bad ? " bad" : "");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { box.className = "notice"; }, 4500);
}

function remember() {
  try {
    if (state.token) sessionStorage.setItem("foodcat", JSON.stringify({ token: state.token, user: state.user }));
    else sessionStorage.removeItem("foodcat");
  } catch (e) { /* storage can be blocked, the page still works */ }
}

function restore() {
  try {
    const saved = JSON.parse(sessionStorage.getItem("foodcat") || "null");
    if (saved && saved.token && saved.user) { state.token = saved.token; state.user = saved.user; }
  } catch (e) { /* ignore */ }
}

async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  let reply;
  try {
    reply = await fetch("/api" + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    return { ok: false, status: 0, data: { error: "Can't reach Food Cat. Is it running?" } };
  }
  let data = {};
  try { data = await reply.json(); } catch (e) { /* not JSON */ }
  const servedBy = reply.headers.get("X-Served-By");
  if (servedBy) state.servedBy = servedBy;
  if (reply.status === 401 && state.token) logout("Your session ended. Please log in again.");
  return { ok: reply.ok, status: reply.status, data, servedBy };
}

// The version line, for example "Version 1.1, build 7 (commit a1b2c3d)". The pipeline fills these numbers in.
function versionText() {
  const v = state.version;
  if (!v) return "";
  if (v.build === "local") return "Version " + v.version + " (local copy)";
  return "Version " + v.version + ", build " + v.build + (v.commit ? " (commit " + v.commit + ")" : "");
}

function showVersion() {
  document.querySelectorAll(".version").forEach((line) => { line.textContent = versionText(); });
}

async function loadVersion() {
  const reply = await api("GET", "/version");
  if (reply.ok) { state.version = reply.data; showVersion(); }
}

function logout(message) {
  state.token = null;
  state.user = null;
  state.alerts = [];
  state.orders = [];
  state.jobs = [];
  state.basket = { restaurantId: null, qty: {} };
  state.tab = "home";
  state.search = "";
  state.cuisine = "All";
  for (const key of Object.keys(lastSeen)) delete lastSeen[key];
  remember();
  render();
  if (message) notify(message);
}

// Redraw something, then give the keyboard focus back to the control that was used.
function keepFocus(redraw) {
  const active = document.activeElement;
  const key = active && active.getAttribute ? active.getAttribute("data-focus") : null;
  redraw();
  if (!key) return;
  const wanted = [key, key.replace(/^minus-/, "plus-")];
  for (const name of wanted) {
    const again = Array.from(document.querySelectorAll("[data-focus]")).find((el) => el.getAttribute("data-focus") === name && !el.disabled);
    if (again) { again.focus(); return; }
  }
}

// ---------------------------------------------------------------- sign in

function drawAuth() {
  const signup = state.authMode === "signup";
  const form = h("form", { onsubmit: submitAuth, novalidate: "" },
    signup && h("div", { class: "field" },
      h("label", { for: "f-name" }, "Your name"),
      h("input", { id: "f-name", type: "text", maxlength: "40", autocomplete: "name", required: true })),
    h("div", { class: "field" },
      h("label", { for: "f-email" }, "Email"),
      h("input", { id: "f-email", type: "email", autocomplete: "email", required: true })),
    h("div", { class: "field" },
      h("label", { for: "f-pass" }, signup ? "Password (at least 6 characters)" : "Password"),
      h("input", { id: "f-pass", type: "password", autocomplete: signup ? "new-password" : "current-password", required: true })),
    signup && h("fieldset", { class: "field" },
      h("legend", {}, "I want to"),
      h("div", { class: "radios" },
        h("label", {}, h("input", { type: "radio", name: "role", value: "customer", checked: true }), " Order food"),
        h("label", {}, h("input", { type: "radio", name: "role", value: "rider" }), " Deliver food"))),
    h("button", { class: "btn wide", type: "submit" }, signup ? "Create account" : "Log in"));

  return h("div", { class: "wrap auth" },
    h("section", { class: "card" },
      h("img", { class: "auth-logo", src: "/static/logo-stacked.png", alt: "The Food Cat", width: "440", height: "472" }),
      h("div", { class: "switch" },
        h("button", { type: "button", "aria-pressed": String(!signup), onclick: () => { state.authMode = "login"; render(); } }, "Log in"),
        h("button", { type: "button", "aria-pressed": String(signup), onclick: () => { state.authMode = "signup"; render(); } }, "Sign up")),
      form,
      h("p", { class: "hint" }, "This is a student demo. Use any email, it is never sent anywhere."),
      h("p", { class: "version" }, versionText())));
}

async function submitAuth(event) {
  event.preventDefault();
  const email = $("f-email").value.trim();
  const password = $("f-pass").value;
  if (state.authMode === "signup") {
    const name = $("f-name").value.trim();
    const role = document.querySelector("input[name=role]:checked").value;
    const made = await api("POST", "/user/signup", { name, email, password, role });
    if (!made.ok) return notify(made.data.error || "Sign up failed.", true);
  }
  const reply = await api("POST", "/user/login", { email, password });
  if (!reply.ok) return notify(reply.data.error || "Login failed.", true);
  state.token = reply.data.token;
  state.user = reply.data.user;
  state.tab = "home";
  remember();
  render();
}

// ---------------------------------------------------------------- shell

const viewOf = () => (state.tab === "restaurants" ? "home" : state.tab);

function go(tab) {
  state.tab = tab;
  render();
}

function goCart() {
  go("home");
  const basket = $("basket");
  if (basket) basket.scrollIntoView({ block: "center" });
}

function pagesFor(role) {
  return role === "rider"
    ? [["home", "Deliveries"], ["system", "System"]]
    : [["home", "Home"], ["restaurants", "Restaurants"], ["orders", "My Orders"], ["system", "System"]];
}

function drawShell() {
  const isRider = state.user.role === "rider";
  const pages = pagesFor(state.user.role);

  const bar = h("header", { class: "bar" }, h("div", { class: "wrap bar-row" },
    h("button", { class: "brand", type: "button", "aria-label": "The Food Cat, back to the start page", onclick: () => go("home") },
      h("img", { src: "/static/logo.png", alt: "", width: "163", height: "48" })),
    h("nav", { class: "tabs", "aria-label": "Pages" }, pages.map(([id, label]) => h("button", {
      type: "button", "aria-current": state.tab === id ? "page" : null, onclick: () => go(id),
    }, label))),
    h("div", { class: "who" },
      !isRider && h("button", { id: "cart-btn", class: "btn cart", type: "button", onclick: goCart }),
      h("span", { class: "who-name" }, state.user.name + " (" + state.user.role + ")"),
      h("button", { class: "btn secondary small", type: "button", onclick: () => logout("You are logged out.") }, "Log out"))));

  let body;
  if (viewOf() === "system") body = systemView();
  else if (isRider) body = riderView();
  else if (viewOf() === "orders") body = ordersView();
  else body = homeView();

  const footer = h("footer", { class: "site-foot" }, h("div", { class: "wrap foot-row" },
    h("div", { class: "foot-brand" }, "The Food Cat"),
    h("nav", { class: "foot-links", "aria-label": "Footer" }, pages.map(([id, label]) => h("button", { type: "button", onclick: () => go(id) }, label))),
    h("p", { class: "foot-note" }, "Food Cat demo. Payments, SMS and push messages are all pretend."),
    h("p", { class: "foot-version version" }, versionText())));

  return h("div", { class: "page" }, bar, !isRider && viewOf() === "home" ? hero() : null, h("main", { class: "wrap" }, body), footer);
}

// The welcome banner on the customer's start page. The search works in the browser, on the menu the Order Service sent.
function hero() {
  const search = h("form", { class: "search", role: "search", onsubmit: (e) => { e.preventDefault(); go("restaurants"); } },
    h("label", { class: "sr-only", for: "q" }, "Search restaurants or dishes"),
    h("input", { id: "q", type: "search", placeholder: "Search the menu", autocomplete: "off", value: state.search,
      oninput: (e) => { state.search = e.target.value; drawMenu(); } }),
    h("button", { class: "btn", type: "submit" }, "Search"));
  return h("section", { class: "hero", "aria-label": "Welcome" }, h("div", { class: "wrap hero-in" },
    h("div", {},
      h("h1", {}, "Hungry? Order now, Food Cat is on its way."),
      h("p", {}, "Pick a restaurant, fill your cart and watch your order travel from the kitchen to your door."),
      search,
      h("span", { class: "tag" }, "Free delivery on every order")),
    h("div", { class: "hero-art" }, h("img", { src: "/static/logo-icon.png", alt: "", width: "480", height: "444" }))));
}

function updateCart() {
  const button = $("cart-btn");
  if (!button) return;
  const count = Object.values(state.basket.qty).reduce((sum, n) => sum + n, 0);
  button.setAttribute("aria-label", count ? "Cart, " + count + (count === 1 ? " item" : " items") : "Cart, empty");
  fill(button, "Cart", count ? h("span", { class: "count" }, String(count)) : null);
}

function drawAll() {
  const view = viewOf();
  if (view === "system") return drawMonitor();
  if (state.user.role === "rider") { drawRider(); drawJobs(); drawAlerts(); return; }
  if (view === "orders") { drawOrders(); drawAlerts(); return; }
  drawMenu(); drawBasket();
}

function render() {
  $("app").replaceChildren(state.user ? drawShell() : drawAuth());
  if (!state.user) { window.scrollTo(0, 0); return; }
  drawAll();
  updateCart();
  poll(true);
  const target = state.tab === "restaurants" ? $("restaurants") : null;
  if (target) target.scrollIntoView({ block: "start" });
  else window.scrollTo(0, 0);
}

// ---------------------------------------------------------------- customer

function homeView() {
  return h("div", { class: "grid" },
    h("div", { class: "col" }, h("section", { id: "restaurants" })),
    h("div", { class: "col sticky" }, h("section", { class: "card", id: "basket" })));
}

function ordersView() {
  return h("div", { class: "grid" },
    h("div", { class: "col" }, h("section", { class: "card", id: "orders" })),
    h("div", { class: "col" }, h("section", { class: "card", id: "alerts" })));
}

async function loadMenu() {
  const reply = await api("GET", "/order/menu");
  if (!reply.ok) return notify(reply.data.error || "Could not load the menu.", true);
  state.menu = reply.data.restaurants;
  state.menuNote = "Answered by " + (reply.servedBy || "the order service") + ", read from the " + reply.data.source;
  drawMenu();
  drawBasket();
}

// Which restaurants (and dishes) pass the cuisine buttons and the search words.
function visibleRestaurants() {
  const words = state.search.trim().toLowerCase();
  const found = [];
  for (const r of state.menu || []) {
    if (state.cuisine !== "All" && r.cuisine !== state.cuisine) continue;
    if (!words) { found.push({ r, items: r.items }); continue; }
    const restaurantMatches = [r.name, r.cuisine, r.area].some((text) => String(text || "").toLowerCase().includes(words));
    const items = restaurantMatches ? r.items : r.items.filter((i) => i.name.toLowerCase().includes(words));
    if (items.length) found.push({ r, items });
  }
  return found;
}

function drawMenu() {
  const box = $("restaurants");
  if (!box) return;
  const cuisines = ["All", ...new Set((state.menu || []).map((r) => r.cuisine).filter(Boolean))];
  const shown = visibleRestaurants();
  fill(box,
    h("div", { class: "section-head" },
      h("h2", {}, "Restaurants"),
      h("div", { class: "source" }, h("span", {}, state.menuNote),
        h("button", { class: "btn secondary small", type: "button", onclick: loadMenu }, "Reload"))),
    cuisines.length > 1 && h("div", { class: "pills", role: "group", "aria-label": "Filter by cuisine" }, cuisines.map((name) => h("button", {
      class: "pill-btn", type: "button", "data-focus": "pill-" + name, "aria-pressed": String(state.cuisine === name),
      onclick: () => keepFocus(() => { state.cuisine = name; drawMenu(); }),
    }, name))),
    !state.menu ? h("p", { class: "empty" }, "Loading the restaurants.")
      : shown.length ? shown.map(({ r, items }) => restaurantCard(r, items))
      : h("p", { class: "empty" }, "No restaurant or dish matches that. Clear the search or choose All."));
}

function restaurantCard(r, items) {
  return h("article", { class: "card restaurant" },
    h("div", { class: "rest-head" },
      h("div", {}, h("h3", { class: "rest-name" }, r.name), h("div", { class: "rest-meta" }, [r.cuisine, r.area].filter(Boolean).join(" · "))),
      h("span", { class: "tag" }, "Free delivery")),
    items.map((item) => {
      const qty = state.basket.restaurantId === r.id ? (state.basket.qty[item.id] || 0) : 0;
      return h("div", { class: "item" },
        h("div", {}, h("div", { class: "item-name" }, item.name), h("div", { class: "price" }, rs(item.price))),
        qty === 0
          ? h("button", { class: "btn small", type: "button", "data-focus": "plus-" + item.id, "aria-label": "Add to cart: " + item.name,
              onclick: () => changeQty(r, item, +1) }, "Add to cart")
          : h("div", { class: "stepper" },
              h("button", { class: "minus", type: "button", "data-focus": "minus-" + item.id, "aria-label": "One less " + item.name,
                onclick: () => changeQty(r, item, -1) }, "−"),
              h("output", {}, String(qty)),
              h("button", { class: "plus", type: "button", "data-focus": "plus-" + item.id, "aria-label": "One more " + item.name,
                disabled: qty >= 20, onclick: () => changeQty(r, item, +1) }, "+")));
    }));
}

function changeQty(restaurant, item, delta) {
  if (state.basket.restaurantId !== restaurant.id) {
    if (Object.keys(state.basket.qty).length) notify("Your cart now holds items from " + restaurant.name + " only.");
    state.basket = { restaurantId: restaurant.id, qty: {} };
  }
  const next = Math.max(0, Math.min(20, (state.basket.qty[item.id] || 0) + delta));
  if (next) state.basket.qty[item.id] = next; else delete state.basket.qty[item.id];
  keepFocus(() => { drawMenu(); drawBasket(); });
}

function clearCart() {
  state.basket = { restaurantId: null, qty: {} };
  drawMenu();
  drawBasket();
}

function basketLines() {
  const restaurant = (state.menu || []).find((r) => r.id === state.basket.restaurantId);
  if (!restaurant) return { restaurant: null, lines: [], total: 0 };
  const lines = restaurant.items.filter((i) => state.basket.qty[i.id]).map((i) => ({ item: i, qty: state.basket.qty[i.id] }));
  return { restaurant, lines, total: lines.reduce((sum, l) => sum + l.item.price * l.qty, 0) };
}

function drawBasket() {
  updateCart();
  const box = $("basket");
  if (!box) return;
  const { restaurant, lines, total } = basketLines();
  const ready = lines.length > 0 && state.address.trim().length > 0;
  fill(box,
    h("h2", {}, "Your cart"),
    restaurant ? h("p", { class: "hint" }, "From " + restaurant.name) : h("p", { class: "empty" }, "Your cart is empty. Add something from the restaurants."),
    lines.map((l) => h("div", { class: "line" }, h("span", {}, l.qty + " × " + l.item.name), h("span", {}, rs(l.item.price * l.qty)))),
    lines.length ? h("div", { class: "line" }, h("span", {}, "Delivery"), h("span", { class: "free" }, "Free")) : null,
    lines.length ? h("div", { class: "line total" }, h("span", {}, "Total"), h("span", {}, rs(total))) : null,
    h("div", { class: "field" },
      h("label", { for: "address" }, "Delivery address"),
      h("input", { id: "address", type: "text", maxlength: "200", autocomplete: "street-address", value: state.address,
        oninput: (e) => { state.address = e.target.value; $("place").disabled = !(basketLines().lines.length && state.address.trim()); } })),
    h("div", { class: "field" },
      h("label", { for: "card" }, "Card"),
      h("select", { id: "card", onchange: (e) => { state.card = e.target.value; } },
        h("option", { value: "tok_visa", selected: state.card === "tok_visa" }, "Visa ending 4242 (works)"),
        h("option", { value: "tok_decline", selected: state.card === "tok_decline" }, "A card that gets declined"))),
    h("button", { id: "place", class: "btn wide", type: "button", disabled: !ready, onclick: placeOrder }, "Place order"),
    lines.length ? h("button", { class: "btn secondary wide", type: "button", onclick: clearCart }, "Clear cart") : null);
}

async function placeOrder() {
  const { restaurant, lines } = basketLines();
  if (!restaurant || !lines.length || !state.address.trim()) return;
  const reply = await api("POST", "/order/orders", {
    restaurant_id: restaurant.id,
    items: lines.map((l) => ({ item_id: l.item.id, qty: l.qty })),
    address: state.address.trim(),
    card: state.card,
  });
  if (!reply.ok) return notify(reply.data.error || "The order was not accepted.", true);
  notify("Order #" + reply.data.order_id + " placed. Payment and delivery happen in the background.");
  state.basket = { restaurantId: null, qty: {} };
  go("orders");   // the page with the order status
}

async function loadOrders() {
  const reply = await api("GET", "/order/orders");
  if (reply.ok && changed("orders", reply.data.orders)) { state.orders = reply.data.orders; drawOrders(); }
}

function stageOf(orderId) {
  let best = null;
  for (const alert of state.alerts) {
    const stage = STAGES[alert.event];
    if (alert.order_id === orderId && stage && (!best || stage.rank > best.rank)) best = stage;
  }
  return best;
}

function drawOrders() {
  const box = $("orders");
  if (!box) return;
  fill(box,
    h("h2", {}, "My orders"),
    state.orders.length ? h("ul", { class: "list" }, state.orders.map((o) => {
      const stage = stageOf(o.id) || { label: "Waiting for payment", tone: "wait" };
      return h("li", {},
        h("div", { class: "top" }, h("strong", {}, "#" + o.id + " " + o.restaurant), h("span", { class: "price" }, rs(o.total))),
        h("div", { class: "sub" }, o.items.map((i) => i.qty + " × " + i.name).join(", ")),
        h("div", { class: "status " + stage.tone }, stage.label));
    })) : h("p", { class: "empty" }, "No orders yet. Pick something from the restaurants on the Home page."));
}

// ---------------------------------------------------------------- rider

function riderView() {
  return h("div", { class: "grid" },
    h("div", { class: "col" }, h("section", { class: "card", id: "rider" }), h("section", { class: "card", id: "jobs" })),
    h("div", { class: "col" }, h("section", { class: "card", id: "alerts" })));
}

async function loadRider() {
  const reply = await api("GET", "/delivery/rider");
  if (reply.ok && changed("rider", reply.data)) { state.rider = reply.data; drawRider(); }
}

function drawRider() {
  const box = $("rider");
  if (!box) return;
  const r = state.rider;
  const status = !r.online ? "You are offline." : r.busy_with ? "Online and busy with order #" + r.busy_with + "." : "Online in " + (r.area || "your area") + ", waiting for an order.";
  fill(box,
    h("h2", {}, "Your status"),
    h("div", { class: "status " + (!r.online ? "off" : r.busy_with ? "wait" : "good") }, status),
    h("div", { class: "field" },
      h("label", { for: "area" }, "Where are you?"),
      h("select", { id: "area", disabled: r.online, onchange: (e) => { state.area = e.target.value; } },
        Object.keys(AREAS).map((name) => h("option", { value: name, selected: name === (r.online ? r.area : state.area) }, name)))),
    h("button", { class: "btn wide" + (r.online ? " secondary" : ""), type: "button", disabled: Boolean(r.busy_with), onclick: toggleOnline },
      r.online ? "Go offline" : "Go online"));
}

async function toggleOnline() {
  const online = !state.rider.online;
  const [lat, lng] = AREAS[state.area];
  const body = online ? { online, name: state.user.name, lat, lng, area: state.area } : { online, name: state.user.name };
  const reply = await api("PUT", "/delivery/rider", body);
  if (!reply.ok) return notify(reply.data.error || "Could not change your status.", true);
  state.rider = reply.data;
  lastSeen.rider = JSON.stringify(reply.data);
  drawRider();
  notify(online ? "You are online. The nearest rider gets each new order." : "You are offline.");
}

async function loadJobs() {
  const reply = await api("GET", "/delivery/jobs");
  if (reply.ok && changed("jobs", reply.data.jobs)) { state.jobs = reply.data.jobs; drawJobs(); }
}

const JOB_LABELS = { ASSIGNED: "Go to the restaurant", PICKED_UP: "Out for delivery", DELIVERED: "Delivered" };

function drawJobs() {
  const box = $("jobs");
  if (!box) return;
  fill(box,
    h("h2", {}, "Your deliveries"),
    state.jobs.length ? h("ul", { class: "list" }, state.jobs.map((j) => h("li", {},
      h("div", { class: "top" }, h("strong", {}, "#" + j.order_id + " from " + j.restaurant)),
      h("div", { class: "sub" }, "Deliver to: " + j.address + " · pickup was " + j.distance_km.toFixed(1) + " km away"),
      h("div", { class: "status " + (j.status === "DELIVERED" ? "good" : "wait") }, JOB_LABELS[j.status] || j.status),
      j.status !== "DELIVERED" && h("div", { class: "actions" },
        h("button", { class: "btn", type: "button", disabled: j.status !== "ASSIGNED", onclick: () => moveJob(j, "pickup") }, "Picked up"),
        h("button", { class: "btn", type: "button", disabled: j.status !== "PICKED_UP", onclick: () => moveJob(j, "deliver") }, "Delivered"))
    ))) : h("p", { class: "empty" }, "No deliveries yet. Go online and wait for an order."));
}

async function moveJob(job, step) {
  const reply = await api("POST", "/delivery/jobs/" + job.order_id + "/" + step);
  if (!reply.ok) return notify(reply.data.error || "That did not work.", true);
  await loadJobs();
  loadRider();
}

// ---------------------------------------------------------------- alerts (both roles)

async function loadAlerts() {
  const reply = await api("GET", "/alerts/inbox?limit=60");
  if (!reply.ok || !changed("alerts", reply.data.alerts)) return;
  state.alerts = reply.data.alerts;
  drawAlerts();
  if (state.user && state.user.role === "customer") drawOrders();
}

function drawAlerts() {
  const box = $("alerts");
  if (!box) return;
  fill(box,
    h("h2", {}, "Phone notifications"),
    h("p", { class: "hint" }, "What the pretend SNS pushed to this phone. A real phone would receive these on its own, the page has to ask for them every few seconds."),
    state.alerts.length ? h("ul", { class: "alerts" }, state.alerts.map((a) => h("li", {}, h("time", {}, clock(a.created)), a.text)))
      : h("p", { class: "empty" }, "Nothing yet."));
}

// ---------------------------------------------------------------- system (a live copy of the drawing)

function systemView() {
  return h("section", { class: "card", id: "system" },
    h("h2", {}, "How the order is moving through the system"),
    h("p", { class: "hint" }, "A live copy of the architecture drawing. Dots show if a service answers its health check. Numbers show how many messages are waiting in each queue."),
    h("div", { id: "flow" }),
    h("ul", { class: "facts", id: "facts" }));
}

async function loadMonitor() {
  const reply = await api("GET", "/monitor");
  if (reply.ok) { state.monitor = reply.data; drawMonitor(); }
}

function drawMonitor() {
  const flow = $("flow");
  const facts = $("facts");
  if (!flow || !state.monitor) return;
  const m = state.monitor;
  const node = (title, instances) => h("div", { class: "node" }, h("b", {}, title),
    instances.map((s) => h("div", { class: "inst" }, h("span", { class: "dot " + s.status }), (s.instance || "no answer") + " (" + s.status + ")")));
  const service = (title, key) => node(title, m.services[key] || []);
  const queue = (title, key) => h("div", { class: "queue" + (key === "dead-letter" && m.queues[key] ? " alarm" : "") },
    h("span", {}, title), h("strong", {}, m.queues[key] === null ? "?" : String(m.queues[key])));
  const arrow = () => h("div", { class: "arrow", "aria-hidden": "true" }, "\u2192");
  fill(flow,
    h("h3", {}, "The front door"),
    h("div", { class: "flow" }, node("API Gateway", [{ status: "up", instance: "gateway" }]), service("User Service", "user")),
    h("h3", {}, "The journey of an order"),
    h("div", { class: "flow" },
      service("Order Service", "order"), arrow(), queue("new orders", "new-orders"), arrow(),
      service("Payment Service", "payment"), arrow(), queue("paid orders", "paid-orders"), arrow(),
      service("Delivery Service", "delivery"), arrow(), queue("updates", "updates"), arrow(),
      service("Lambda and alerts", "alerts")),
    h("h3", {}, "Where failed messages end up"),
    h("div", { class: "flow" }, queue("dead letter", "dead-letter")));
  fill(facts,
    h("li", {}, "The last answer through the gateway came from: " + (state.servedBy || "nothing yet") + "."),
    h("li", {}, "The gateway hands calls to the two order instances in turn, and moves on to the other one if one stops answering."),
    h("li", {}, "A growing dead letter queue means a message failed five times. Someone should look at it."));
}

// ---------------------------------------------------------------- polling

let polling = false;
async function poll(first) {
  if (!state.user || polling || (document.hidden && !first)) return;
  polling = true;
  try {
    if (viewOf() === "system") {
      await loadMonitor();
    } else if (state.user.role === "rider") {
      await Promise.all([loadRider(), loadJobs(), loadAlerts()]);
    } else {
      if (first && viewOf() === "home") await loadMenu();
      await Promise.all([loadOrders(), loadAlerts()]);
    }
  } finally {
    polling = false;
  }
}

restore();
render();
loadVersion();
setInterval(() => poll(false), 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(false); });

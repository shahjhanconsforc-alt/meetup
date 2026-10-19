/* meetup-bot UI — vanilla, no build step.
 *
 * One file on purpose: the whole app is five panels over a REST API, and a
 * bundler would add more moving parts than it removes. The server sends
 * Cache-Control: no-store for exactly this reason (see backend/main.py) — a
 * cached app.js makes an edit look like it did nothing.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

/* ── Pop-up panels ───────────────────────────────────────────── */

/* Click outside a modal <dialog> and it closes. Applied to EVERY dialog on the
 * page rather than wired per panel, so a new one gets the behaviour by existing.
 * (Escape already closes a native dialog — this is the other half of what people
 * expect from an overlay.)
 *
 * A modal dialog's backdrop belongs to the dialog element itself, so a click on
 * it arrives with `target === dialog`. That alone is not enough to act on: a
 * click on the dialog's own PADDING reports the same target, and closing on
 * those would make the panel disappear when somebody clicks a millimetre off a
 * button. The rect test is what separates the two. */
function outside(dialog, event) {
  if (event.target !== dialog) return false;       // something inside it
  const box = dialog.getBoundingClientRect();
  return event.clientX < box.left || event.clientX > box.right
      || event.clientY < box.top || event.clientY > box.bottom;
}

function closeOnClickOutside(dialog) {
  // Where the press STARTED matters as much as where it ended. Selecting text
  // in the live table and releasing past the edge is one gesture that ends on
  // the backdrop, and closing the panel out from under it would throw away both
  // the selection and the operator's place in a long list.
  let began = false;
  dialog.addEventListener('mousedown', e => { began = outside(dialog, e); });
  dialog.addEventListener('click', e => {
    if (began && outside(dialog, e)) dialog.close();
    began = false;
  });
}

$$('dialog').forEach(closeOnClickOutside);

/* ── Activity rows ───────────────────────────────────────────── */

function feedRow({ at, level = 'info', source = 'ui', message = '', status = null }) {
  const li = document.createElement('li');
  li.className = level;

  // The middleware logs "GET /api/x → 503". Split the code off so it can sit in
  // its own column: a wall of 200s and a wall of 503s should look different
  // from across the room, which they don't when the code is buried in prose.
  const match = String(message).match(/^(.*?)\s*→\s*(\d{3})\s*$/);
  const text = match ? match[1] : message;
  const code = status ?? (match ? Number(match[2]) : null);

  const time = at ? new Date(at) : new Date();
  li.innerHTML = `
    <time>${time.toLocaleTimeString()}</time>
    <span class="src" data-src="${escapeHtml(source)}">${escapeHtml(source)}</span>
    <span class="msg">${escapeHtml(text)}</span>
    ${code ? `<span class="code c${String(code)[0]}">${code}</span>` : '<span></span>'}`;
  return li;
}

function toast(message, level = 'info') {
  // Errors go into the same feed as everything else rather than an alert box:
  // a modal interrupts a run the operator is watching, and the feed is already
  // where they are looking.
  $('#feed').prepend(feedRow({ level, source: 'ui', message }));
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function truncate(text, n = 90) {
  const clean = String(text ?? '').replace(/\s+/g, ' ').trim();
  return clean.length > n ? clean.slice(0, n - 1) + '…' : clean;
}

/* ── Tabs ────────────────────────────────────────────────────── */

$('#tabs').addEventListener('click', e => {
  const button = e.target.closest('button[data-panel]');
  if (!button) return;
  showPanel(button.dataset.panel);
});

/* Switching panels, from a tab click or from code — the scraping gate sends the
 * operator to Accounts, and that has to do exactly what pressing the tab does. */
function showPanel(name) {
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.panel === name));
  $$('.panel').forEach(p => p.classList.toggle('on', p.dataset.panel === name));
  refreshPanel(name);
}

function panelIsOn(name) {
  const panel = $(`.panel[data-panel="${name}"]`);
  return !!panel && panel.classList.contains('on');
}

function refreshPanel(name) {
  ({ accounts: loadAccounts, scraping: loadScraping, data: loadDataPanel,
     campaign: loadCampaign, settings: loadSettings }[name] || (() => {}))();
}

/* ── Accounts ────────────────────────────────────────────────── */

async function loadAccounts() {
  const [{ accounts }, { sessions }] = await Promise.all([
    api('/api/accounts'), api('/api/accounts/sessions'),
  ]);
  const byId = Object.fromEntries(sessions.map(s => [s.id, s]));

  renderAccountCards(accounts, byId);

  // The campaign's own picker, from the one place accounts are loaded.
  // Scraping has none — it runs on whichever session is live.
  renderAccountPicker(accounts, byId);
}

/* One card per account, all of it on the card: name, email, the password (masked
 * behind the eye), and the actions as icons — open the browser, log out, remove.
 * A pink card is a stored session. The default profile is a minimal info-only
 * card: it holds no credentials of its own (it uses the .env login) and there is
 * nothing to remove or reveal. */
/* The default profile is NOT a card. It holds no credentials of its own, has
 * nothing to open, log out or remove, and every run in this app is aimed at a
 * stored account — so its row was a permanent "— / No session cookie" sitting
 * above the accounts that matter. It still exists as a profile; it just has no
 * card. */
function renderAccountCards(accounts, byId) {
  const wrap = $('#account-cards');
  if (!wrap) return;
  const live = accounts.filter(a => (byId[a.id] || {}).logged_in).length;
  $('#sessions-count').textContent =
    accounts.length ? `${live} of ${accounts.length} stored` : '';

  const cards = [];

  for (const a of accounts) {
    const s = byId[a.id] || {};
    const stored = !!s.logged_in;
    cards.push(`
      <div class="acct-card ${stored ? 'stored' : ''} ${s.active ? 'active' : ''}" data-id="${a.id}">
        <div class="ac-top">
          <span class="dot ${stored ? 'ok' : 'off'}"></span>
          <span class="ac-name">${escapeHtml(a.label || a.login || ('#' + a.id))}</span>
          ${s.active ? '<span class="ac-live">ACTIVE</span>' : ''}
          <div class="grow"></div>
          <button class="icon-btn tiny" data-act="open" data-id="${a.id}"
                  title="Open the browser on this account">
            <svg class="ic" viewBox="0 0 24 24"><use href="#i-external"/></svg></button>
          <button class="icon-btn tiny" data-act="logout" data-id="${a.id}"
                  title="Log out — clear the stored session">
            <svg class="ic" viewBox="0 0 24 24"><use href="#i-logout"/></svg></button>
          <button class="icon-btn tiny danger" data-act="delete" data-id="${a.id}"
                  title="Remove account">
            <svg class="ic" viewBox="0 0 24 24"><use href="#i-trash"/></svg></button>
        </div>
        <div class="ac-mail">${escapeHtml(a.login || '—')}</div>
        <div class="ac-pass">
          <span class="ac-secret" data-id="${a.id}">••••••••</span>
          ${a.has_password ? `
          <button class="icon-btn tiny" data-act="reveal" data-id="${a.id}"
                  title="Show / hide password">
            <svg class="ic" viewBox="0 0 24 24"><use href="#i-eye"/></svg></button>` : ''}
        </div>
        <div class="ac-sub">${statusLine(s)}</div>
      </div>`);
  }

  wrap.innerHTML = cards.join('')
    || '<p class="muted">No accounts yet — add one above.</p>';
}

/* The one-line session state a card shows under the email. */
function statusLine(s) {
  if (s && s.logged_in) {
    const expiry = s.expires_at ? new Date(s.expires_at).toLocaleDateString() : '';
    const stored = `stored${expiry ? ' · until ' + escapeHtml(expiry) : ''}`;
    return s.active ? `active now · ${stored}` : stored;
  }
  // An active profile with nothing stored yet: a browser is open on it, and
  // whatever gets signed in there becomes the session once it closes.
  if (s && s.active) return 'active now · sign in, then close the window';
  return escapeHtml((s && s.detail) || 'no session');
}

/* Mark a button as the one currently working. Disabled AND accent-coloured: the
 * disable stops a second click, the colour says which of the greyed-out row is
 * actually running rather than merely unavailable. */
function setBusy(button, busy) {
  if (!button) return;
  button.classList.toggle('busy', busy);
  button.disabled = busy;
}

/* All account-card actions, off the icons' data-act. A click on the card body
 * does nothing now: it used to toggle that account's tick in the first-chat
 * picker, and with the picker gone there is nothing for it to select. */
$('#account-cards').addEventListener('click', async e => {
  const iconBtn = e.target.closest('button[data-act]');
  const card = e.target.closest('.acct-card');
  if (!card || !iconBtn) return;
  const id = card.dataset.id;

  const act = iconBtn.dataset.act;
  if (act === 'reveal') return togglePassword(iconBtn, id);   // no busy/reload

  setBusy(iconBtn, true);
  try {
    if (act === 'open') {
      const result = await api(`/api/accounts/${id}/open`, { method: 'POST' });
      toast(result.url ? 'Opened the browser on this account' : 'Opened', 'ok');
      loadAccounts();
    } else if (act === 'logout') {
      await api(`/api/accounts/${id}/logout`, { method: 'POST' });
      loadAccounts();
    } else if (act === 'delete') {
      if (!confirm('Remove this account and its stored session?')) return;
      await api(`/api/accounts/${id}`, { method: 'DELETE' });
      loadAccounts();
    }
    // Opening is the action the Scraping panel waits on, and logging out or
    // removing takes that window away again — so re-read the gate after any of
    // them rather than when the tab is next opened.
    refreshScrapeGate();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    setBusy(iconBtn, false);
  }
});

/* Reveal or re-hide one card's password. Fetched only when first shown — the
 * secret is kept off the list response and out of the DOM until asked for. */
async function togglePassword(button, id) {
  const field = $(`#account-cards .ac-secret[data-id="${id}"]`);
  if (!field) return;
  if (field.dataset.shown === '1') {
    field.textContent = '••••••••';
    field.dataset.shown = '';
    return;
  }
  try {
    const { password } = await api(`/api/accounts/${id}/secret`);
    field.textContent = password || '(none)';
    field.dataset.shown = '1';
  } catch (err) {
    toast(err.message, 'error');
  }
}

$('#acc-add').addEventListener('submit', async e => {
  e.preventDefault();
  const form = new FormData(e.target);
  try {
    await api('/api/accounts', { method: 'POST', body: Object.fromEntries(form) });
    e.target.reset();
    loadAccounts();
  } catch (err) { toast(err.message, 'error'); }
});

$('#acc-check').addEventListener('click', async () => {
  try {
    const result = await api('/api/accounts/sessions/check', { method: 'POST' });
    toast(`${result.logged_in}/${result.checked} logged in`, 'ok');
    loadAccounts();
  } catch (err) { toast(err.message, 'error'); }
});

/* ── Scraping ────────────────────────────────────────────────── */

/* Opening the panel: the gate first, because it decides whether anything on it
 * can be pressed, and the saved category strip second.
 *
 * There is no browse-results list any more. What a run found is read back where
 * it is worked — the pipeline's board — and a second copy of the same events
 * under the progress bar was a wall of cards nobody acted on. */
function loadScraping() {
  refreshScrapeGate();
  loadCatPool();
}

/* The account these buttons drive, and the gate that guards them.
 *
 * The panel has no account picker any more, and defaulting to the default
 * profile would be wrong: it holds no session, so Find would open logged out
 * and a scrape would see nothing. So the panel requires a browser actually open
 * on an account — which is what the ↗ icon on an Accounts card does — and stays
 * switched off until there is one. `active` is the runner's own answer about
 * which profile it has open, so this is the account a run would really use. */

/* The session runs go to, or null. Read fresh by refreshScrapeGate; every action
 * re-reads before it runs, so a window closed since the last poll cannot send a
 * run at a profile that is gone. */
let scrapeSession = null;
/* Every account with a window up. Accounts open side by side, and a sweep runs
 * from SEVERAL at once — see scrapePicks below. */
let openSessions = [];
/* Which of the open windows a sweep runs from — a SET, because several accounts
 * work the same tab at once, taking events off one shared queue. Kept across
 * refreshes so a poll cannot move a run onto a different account mid-session. */
let scrapePicks = new Set();

const sessionKey = s => String(s && s.id != null ? s.id : 'default');

async function refreshScrapeGate() {
  try {
    const { sessions } = await api('/api/accounts/sessions');
    openSessions = (sessions || []).filter(s => s.active);
  } catch {
    openSessions = [];           // server restarting — treat as not ready
  }
  // Windows closed since the last poll drop out of the selection rather than
  // leaving the panel aimed at a dead profile.
  const live = new Set(openSessions.map(sessionKey));
  for (const key of [...scrapePicks]) if (!live.has(key)) scrapePicks.delete(key);
  // Nothing picked: EVERY open window is. Opening two browsers and pressing
  // Scrape is a clear enough instruction that making the operator also tick
  // them is asking them to say something they have already said.
  if (!scrapePicks.size) for (const key of live) scrapePicks.add(key);

  scrapeSession = scrapedAccounts()[0] || null;
  paintScrapeGate(scrapeSession);
  return scrapeSession;
}

/* The open sessions a run will use, in the order they are shown. */
function scrapedAccounts() {
  return openSessions.filter(s => scrapePicks.has(sessionKey(s)));
}

/* Ticking which open accounts the panel drives. Nothing is opened or closed —
 * the windows are all up already; this only says which of them runs go to.
 *
 * A toggle, because several sweep the same tab together. Unticking the last one
 * is a no-op: a sweep with no account is not a state worth being able to reach.
 */
$('#scrape-ready').addEventListener('click', e => {
  const button = e.target.closest('button[data-pick]');
  if (!button) return;
  const key = button.dataset.pick;
  if (scrapePicks.has(key) && scrapePicks.size > 1) scrapePicks.delete(key);
  else scrapePicks.add(key);
  scrapeSession = scrapedAccounts()[0] || null;
  paintScrapeGate(scrapeSession);
});

/* The ask hidden, a strip naming the account — or the reverse.
 *
 * The buttons themselves are paintPipeline's job, at the bottom of this. Scrape
 * all events moved up into this panel's head and the three that used to sit
 * there are gone, so the one list of what an open session enables is the
 * pipeline's — two places setting `disabled` on the same button is how they end
 * up disagreeing mid-run. */
function paintScrapeGate(active) {
  $('#scrape-gate').hidden = !!active;

  const strip = $('#scrape-ready');
  strip.hidden = !active;
  if (active) {
    // One window: a label, as before. Several: one chip each, TICKABLE — a
    // sweep runs from every lit one at once, working down the same tab and
    // taking events off one shared queue, so this is a set rather than a
    // choice between them.
    const many = openSessions.length > 1;
    const picked = scrapedAccounts().length;
    const chips = openSessions.map(s => {
      const on = scrapePicks.has(sessionKey(s));
      const name = escapeHtml(s.label || s.login || ('#' + s.id));
      return many
        ? `<button class="as-chip ${on ? 'on' : ''}" data-pick="${escapeHtml(sessionKey(s))}"
                   title="${escapeHtml(s.profile || '')}">
             <span class="dot ${on ? 'ok' : 'off'}"></span>${name}</button>`
        : `<span class="as-chip static" title="${escapeHtml(s.profile || '')}">
             <span class="dot ok"></span>${name}</span>`;
    }).join('');
    strip.innerHTML = '<span class="as-label">Scraping as</span>' + chips
      + `<span class="muted">${many
          ? `${picked} of ${openSessions.length} browsers — a sweep runs from `
            + 'every lit one at once'
          : 'browser open — every scraping action is live'}</span>`;
  }
  paintPipeline();          // which is where every button's state is decided
}

/* Called by every scraping action before it fires. Returns the open session, or
 * null after saying what to do about it. */
async function requireOpenAccount() {
  const active = await refreshScrapeGate();
  if (active) return active;
  toast('Open an account first — Accounts › ↗ on the account to scrape with.',
        'warn');
  const gate = $('#scrape-gate');
  gate.classList.remove('nudge');
  void gate.offsetWidth;               // restart the animation on a repeat press
  gate.classList.add('nudge');
  return null;
}

$('#scrape-open-accounts').addEventListener('click', () => showPanel('accounts'));

/* ── The pipeline: category → event → members → chat ─────────── */

/* What the operator has picked so far. One object rather than reading it back
 * out of the DOM, for the same reason the campaign wizard keeps its own state:
 * four steps each consulting the others' markup is how they end up disagreeing.
 */
const pipe = { pool: [], category: null, event: null, eventCount: 0,
               members: [], rolling: false, header: null };

/* What can be touched right now. The order the numbered steps used to explain is
 * enforced here instead: the event select is empty until a category is open,
 * and the table until an event is collected. There is nothing to SEND from this
 * panel any more — the Write-and-send box under the table is gone, and
 * messaging is the Campaign panel's job. */
function paintPipeline() {
  const ready = !!scrapeSession && !pipe.rolling;
  $('#pipe-auto').disabled = !ready;
  $('#pipe-pool-save').disabled = !ready;
  $('#pipe-members').disabled = !ready || !pipe.event;
  $('#pipe-state').textContent = pipe.rolling ? 'picking…' : '';
  paintSummary();
}

/* The three facts across the top. Written from whatever is known so far — an
 * em-dash where nothing is, never a zero standing in for an unanswered step. */
function paintSummary() {
  const event = pipe.header || pipe.event || null;
  $('#sum-category').textContent = pipe.category ? pipe.category.name : '—';
  // "event(s)", not "listed": a roll lists them and a sweep scrapes them, and
  // this one line has to be true of both.
  $('#sum-category-sub').textContent = pipe.category
    ? (pipe.eventCount ? `${pipe.eventCount} event(s)` : 'no events yet')
    : 'Press Scrape all events';

  $('#sum-event').textContent = event
    ? truncate(event.title || event.url || '(untitled)', 46) : '—';
  $('#sum-when').textContent = event
    ? [eventWhen(event.when), event.group || event.urlname || ''].filter(Boolean).join(' · ')
      || 'no date given'
    : 'Press Scrape all events';

  $('#sum-total').textContent = String(pipe.members.length);
  // How many carry an id, because that is what makes a scraped member usable —
  // a co-host's card gives none, and a roster of forty where three are blank is
  // three people nothing downstream can reach.
  const named = pipe.members.filter(m => m.username).length;
  $('#sum-total-sub').textContent = pipe.members.length
    ? (named === pipe.members.length ? 'all with an id'
       : `${pipe.members.length - named} without an id`)
    : 'none collected yet';
}

/* Meetup appends the zone in brackets — "…+05:00[Asia/Karachi]" — which Date
 * cannot parse, so it comes off before the date is read. */
function eventWhen(raw) {
  const iso = String(raw || '').replace(/\[.*\]$/, '');
  if (!iso) return '';
  const stamp = new Date(iso);
  return isNaN(stamp) ? '' : stamp.toLocaleDateString([], {
    day: 'numeric', month: 'short', year: 'numeric' });
}


$('#pipe-reset').addEventListener('click', () => {
  pipe.category = null;
  pipe.eventCount = 0;
  clearFromEvent();
  renderCatPool();
  setPipeNote('Press Pick a random event.');
  paintPipeline();
});

/* Everything downstream of the event, in one place — the bug that leaves the
 * last roll's members under a fresh one is exactly what a forgotten line here
 * would cause. */
function clearFromEvent() {
  pipe.event = pipe.header = null;
  pipe.members = [];
  $('#pipe-mem-table tbody').innerHTML = '';
  renderListFoot();
  paintRosterTools();
}

/* ── The saved category strip ────────────────────────────────── */

/* Every category the Find page has, read once and kept. Drawn so the pool being
 * rolled from is visible — a roll that lands on the same name twice reads as a
 * stuck button unless you can see how many there were. */
/* The tab a sweep works down when nothing else is picked. "all" is the first
 * tab on /find/ — a real tab with a real testid, not a category — so it belongs
 * in the pool beside the twenty rather than being the only thing reachable. */
const ALL_EVENTS = { key: 'all', name: 'All events' };

function renderCatPool() {
  const pool = pipe.pool || [];
  // The count rides on the button, which is all the panel shows at rest — the
  // twenty names themselves are only worth the room when they are asked for.
  $('#pipe-pool-count').textContent = String(pool.length);
  $('#pipe-pool-note').textContent = pool.length
    ? `${pool.length} saved · click one to scrape every event on it`
    : 'none saved yet';
  const rolled = pipe.category ? String(pipe.category.key) : '';
  // All events FIRST, then the saved twenty. It is the widest sweep there is,
  // and burying it under the categories would make the broadest choice the
  // hardest one to find.
  $('#pipe-pool').innerHTML = [ALL_EVENTS, ...pool].map(c => `
    <button class="cat ${String(c.key) === rolled ? 'on' : ''}"
            data-cat="${escapeHtml(String(c.key))}"
            data-cat-name="${escapeHtml(c.name)}"
            ${pipe.rolling ? 'disabled' : ''}
            title="Scrape every event under ${escapeHtml(c.name)}, one by one"
            >${escapeHtml(c.name)}</button>`).join('');
}

/* Clicking a category SWEEPS it: every event under that tab, one after another,
 * until it is stopped. They used to be labels — the pool a random roll drew
 * from — which put the twenty tabs on screen and let none of them be chosen. */
$('#pipe-pool').addEventListener('click', e => {
  const tile = e.target.closest('[data-cat]');
  if (!tile || pipe.rolling) return;
  startSweep(tile.dataset.cat, tile.dataset.catName);
});

/* Open or shut the pool. Not a popover: it pushes the board down rather than
 * covering it, so nothing the operator is reading is hidden by a list they
 * opened to glance at. */
$('#pipe-pool-toggle').addEventListener('click', () => {
  const wrap = $('#pipe-pool-wrap');
  const open = wrap.hidden;
  wrap.hidden = !open;
  $('#pipe-pool-toggle').setAttribute('aria-expanded', String(open));
});

async function loadCatPool() {
  try {
    const { categories } = await api('/api/automation/categories');
    pipe.pool = categories || [];
  } catch {
    pipe.pool = [];              // saved list only — no browser, so no fuss
  }
  renderCatPool();
}

$('#pipe-pool-save').addEventListener('click', async () => {
  const active = await requireOpenAccount();
  if (!active) return;
  const button = $('#pipe-pool-save');
  setBusy(button, true);
  try {
    const result = await api('/api/automation/categories/save', {
      method: 'POST', body: { account_id: active.id },
    });
    pipe.pool = result.categories || [];
    renderCatPool();
    toast(result.detail, 'ok');
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    setBusy(button, false);
    paintPipeline();
  }
});

/* THE press. ONE random category off the saved strip, its first event, and
 * everyone on it — all three on the server, because they are three browser
 * stages that only make sense together.
 *
 * One category per press. A roll that lands on an empty category says so and
 * stops; pressing again is what rolls another, because how many categories get
 * opened is the operator's call, not the run's.
 *
 * A run like any other: it reports through the run box and takes a stop.
 */
$('#pipe-auto').addEventListener('click',
  () => startSweep(ALL_EVENTS.key, ALL_EVENTS.name));

/* ONE sweep, whichever control asked for it — the button in the panel head, or
 * a category clicked in the pool. They differ only in which tab it works down,
 * and two copies of this would be two places for "and on down the tab until you
 * stop it" to stop being true. */
async function startSweep(key, name) {
  const active = await requireOpenAccount();
  if (!active) return;
  const button = $('#pipe-auto');

  pipe.rolling = true;
  // The tab being swept, on screen from the press. The run publishes its own
  // once it has clicked the tab, but that is a page load away and the header
  // reading "—" until then makes the press look like it missed.
  pipe.category = { key: String(key), name: name || 'All events' };
  clearFromEvent();
  renderCatPool();
  setBusy(button, true);
  const lanes = scrapedAccounts().length;
  setPipeNote(`Opening ${pipe.category.name}, then its first event and everyone `
              + 'on it — and on down the tab until you stop it…'
              + (lanes > 1 ? ` Running from ${lanes} accounts at once.` : ''));
  paintPipeline();
  try {
    // The sweep, not the roll: it stays on the tab it starts on and works
    // through every event, rather than opening one random category and taking
    // a single event out of it.
    const { task } = await api('/api/automation/pipeline/sweep', {
      method: 'POST',
      // EVERY ticked account. They work down the same tab together, taking
      // events off one shared queue so no two walk the same roster.
      body: { account_id: active.id, target: 0, key: String(key),
              account_ids: scrapedAccounts().map(s => s.id).filter(Boolean) },
    });
    liveEventUrl = null;
    watchTask(task, { onTick: applyAutoTick, onDone: applyAutoPick });
  } catch (err) {
    pipe.rolling = false;
    setBusy(button, false);
    setPipeNote(err.message);
    toast(err.message, 'error');
    paintPipeline();
  }
}

/* Which event the rows on screen came off, so a roll that gives up on an event
 * with no attendee list and drops to the next one clears the table rather than
 * stacking the second event's people under the first's. */
let liveEventUrl = null;

/* The roll AS IT GOES. The run publishes what it has landed on and everyone it
 * has walked so far on every poll, so the header and the table fill member by
 * member — the press used to sit on "Rolling a category…" for the whole walk
 * and then drop a hundred rows in at once, which reads as a frozen panel right
 * up to the moment it is finished. */
function applyAutoTick(status) {
  const meta = (status && status.run && status.run.meta) || null;
  if (!meta) return;

  if (meta.event_url !== undefined && meta.event_url !== liveEventUrl) {
    liveEventUrl = meta.event_url;
    const had = pipe.members.length;
    pipe.members = [];
    // Only when there was something to drop: an empty re-render would swap the
    // "opening the attendee list…" note for "No attendees on this event" before
    // the first card has even been read.
    if (had) setPipeNote('Opening the next event\'s attendee list…');
  }
  if (meta.category && meta.category.key !== (pipe.category && pipe.category.key)) {
    pipe.category = meta.category;
    pipe.eventCount = meta.events_count || 0;
    renderCatPool();
  }
  if (meta.event) pipe.header = meta.event;
  // Every event this run has finished, newest first. Published by the run as
  // each one lands, so this is its history rather than a guess assembled from
  // whatever happens to be on screen.
  if (meta.events_done) renderDoneEvents(meta.events_done);
  // Only the rows — the picked/status maps above are the roll's own reset, and
  // this runs on every poll.
  applyWalkedMembers(meta);
  paintSummary();
}

/* Every event this run has finished: its name, how many members came off it,
 * and — with several accounts sweeping the same tab — which one did it.
 *
 * Newest FIRST. The interesting row is the one that just landed, and a list
 * that grows downward puts it off the bottom of the box after a dozen. */
function renderDoneEvents(rows) {
  const wrap = $('#pipe-done');
  const list = $('#pipe-done-list');
  if (!wrap || !list) return;
  wrap.hidden = !(rows || []).length;
  if (wrap.hidden) return;

  // Events on the list that this run did NOT collect: one whose roster was
  // already on file, and one that went quiet and was abandoned. Both are worth
  // seeing — a panel that looked untouched after ninety seconds of nothing is
  // the operator unable to tell a working run from a dead one — and neither
  // belongs in "3 events", which is a count of what this run actually did.
  const done = rows.filter(r => !NOT_WORK[r.status]);
  const members = done.reduce((n, r) => n + (r.count || 0), 0);
  const aside = Object.entries(NOT_WORK)
    .map(([status, said]) => [rows.filter(r => r.status === status).length, said])
    .filter(([n]) => n)
    .map(([n, said]) => ` · ${n} ${said.tally}`).join('');
  $('#pipe-done-count').textContent =
    `${done.length} event(s) · ${members} member(s)${aside}`;

  list.innerHTML = rows.map(r => {
    const off = NOT_WORK[r.status];
    return `
    <div class="done-row${off ? ` ${r.status}` : ''}">
      <span class="d-tile"><svg class="ic" viewBox="0 0 24 24"><use href="${
        off ? off.icon : '#i-calendar'}"/></svg></span>
      <span class="d-text">
        <span class="d-name" title="${escapeHtml(r.title || '')}">${
          escapeHtml(r.title || r.url || '(untitled)')}</span>
        <span class="d-sub">${escapeHtml(r.group || '')}${
          r.account ? `${r.group ? ' · ' : ''}${escapeHtml(r.account)}` : ''}</span>
      </span>
      ${off
        ? `<span class="d-skip">${off.label}</span>`
        : `<span class="d-n">${r.count || 0}</span>`}
    </div>`;
  }).join('');
}

/* The two ways an event lands on the list without being work this run did. Keyed
 * by the status campaign.py sets — see SCRAPED/SKIPPED/STALLED there. */
const NOT_WORK = {
  skipped: { label: 'Already scraped', tally: 'already scraped', icon: '#i-check' },
  // A clock, because that is literally what happened: the stall window ran out.
  stalled: { label: 'Stalled — moved on', tally: 'stalled', icon: '#i-clock' },
};

/* What the roll landed on. Everything the panel shows below comes from here —
 * which category, which event, and the roster in full. */
function applyAutoPick(result) {
  pipe.rolling = false;
  setBusy($('#pipe-auto'), false);

  // A stopped roll comes back with no roster at all, and wiping the rows the
  // operator just watched arrive is not what stopping meant — the walk got that
  // far, and how far it got is the result. So the live rows stand unless the run
  // has a roster of its own to replace them with.
  const members = (result && result.members) || [];
  const kept = !members.length && pipe.members.length ? pipe.members : members;
  // The finished list stands: it is what the run DID, and a stop is not a
  // reason to forget the events it got through before it.
  if (result && result.events_done) renderDoneEvents(result.events_done);
  pipe.category = (result && result.category) || pipe.category;
  // Two shapes, because two runs end here: a roll hands back the events it
  // LISTED, a sweep how many it scraped.
  const events = result && result.events;
  pipe.eventCount = Array.isArray(events) ? events.length
    : (typeof events === 'number' ? events : pipe.eventCount);
  pipe.event = (result && result.event) || pipe.header || null;
  pipe.header = pipe.event;
  pipe.members = kept;

  renderCatPool();
  if (!kept.length) {
    setPipeNote((result && result.detail) || 'Nothing came back — roll again.');
  } else {
    renderPipeMembers();
  }
  paintPipeline();
}

/* Everyone on that event's attendee list, with their ids.
 *
 * A watched RUN, not an awaited call: it walks the list opening each attendee's
 * profile for the id their card does not carry, which on a roster of forty is
 * minutes of work. Held open as one request that would look like a frozen
 * panel with no way to stop it — as a run it reports in the run box, fills the
 * table as it goes, and stops from there. */
async function collectPipeMembers() {
  if (!pipe.event) return toast('Pick an event first.', 'warn');
  const active = await requireOpenAccount();
  if (!active) return;
  const button = $('#pipe-members');
  setBusy(button, true);
  pipe.members = [];
  setPipeNote('Opening the attendee list and each profile on it as it '
              + 'loads — ids appear here as they are read…');
  try {
    const { task } = await api('/api/automation/events/members', {
      method: 'POST',
      body: { account_id: active.id, url: pipe.event.url, target: 0,
              tabs: PROFILE_TABS, open_profiles: true },
    });
    watchTask(task, {
      onTick: status => applyWalkedMembers(status && status.run
                                           && status.run.meta),
      onDone: result => {
        setBusy(button, false);
        applyWalkedMembers(result);
        // Straight off the store, so the header says what is recorded against
        // this event rather than what the card happened to render.
        if (result && result.event) pipe.header = result.event;
        renderPipeMembers();
        paintPipeline();
      },
    });
  } catch (err) {
    setBusy(button, false);
    setPipeNote(err.message);
    toast(err.message, 'error');
    paintPipeline();
  }
}

/* The rows the walk has reached so far. Replaces rather than merges: the run
 * publishes the whole list each time, in list order, so this is the truth about
 * who has been walked — and a merge would have to guess at what a shrinking
 * list meant. */
function applyWalkedMembers(source) {
  const rows = (source && source.members) || [];
  if (!rows.length) return;
  pipe.members = rows;
  renderPipeMembers();
  paintSummary();
}

$('#pipe-members').addEventListener('click', collectPipeMembers);

/* One row spanning the table, for the states where there is no list to show. */
function setPipeNote(message) {
  $('#pipe-mem-table tbody').innerHTML =
    `<tr class="note"><td colspan="2" class="muted">${escapeHtml(message)}</td></tr>`;
}

/* Every member the walk has read off the event: who they are, and the id that
 * makes them reachable. Two columns and no more.
 *
 * The tick, SMS and Status columns went with the Write-and-send box that used
 * to sit under this table — the ticks only ever built its recipient list and
 * the other two were its output. Messaging is the Campaign panel's job, and a
 * tick that submits to nothing is worse than no tick.
 *
 * The WHOLE roster, scrolling inside the board. An event brings back hundreds
 * and paging cut them into pages the operator then had to page through to find
 * anyone; one scrollable list is the thing itself. */
function renderPipeMembers() {
  if (!pipe.members.length) {
    setPipeNote('No attendees on this event.');
    renderListFoot();
    paintRosterTools();
    return;
  }
  // Re-rendered on every poll of a running walk, so the operator's place in a
  // long list has to survive the redraw.
  const wrap = $('#pipe-mem-wrap');
  const keep = wrap ? wrap.scrollTop : 0;

  $('#pipe-mem-table tbody').innerHTML = pipe.members.map((m, i) => {
    const handle = m.username || '';
    // Meetup gives a co-host's card no profile link. They are real and
    // countable, but nothing downstream can reach them, and the row says so
    // rather than showing a dead link.
    const reachable = !!handle;
    return `
      <tr class="${reachable ? '' : 'unreachable'}"
          data-handle="${escapeHtml(handle)}">
        <td class="c-n">${i + 1}</td>
        <td>
          <div class="c-who">
            <span class="avatar"><svg class="ic" viewBox="0 0 24 24"><use href="#i-user"/></svg></span>
            <span class="who-text">
              <span class="m-name">${escapeHtml(m.name || handle || '(no name)')}</span>
              ${reachable
                ? `<a class="m-handle" href="https://www.meetup.com/members/${encodeURIComponent(handle)}/"
                     target="_blank" rel="noopener">#${escapeHtml(handle)}</a>`
                : '<span class="m-handle none">no profile link</span>'}
            </span>
          </div></td>
      </tr>`;
  }).join('');
  if (wrap) wrap.scrollTop = keep;
  renderListFoot();
  paintRosterTools();
}

/* How many profile tabs the roster walk keeps open at once. Nothing to do with
 * a campaign's batching — this is a read-only sweep of profiles for their ids,
 * and the number is just how many pages one window holds comfortably. */
const PROFILE_TABS = 5;

/* The roster's two tools — open every profile, close the tabs — shown once
 * there is a roster to use them on. This was the batch panel's header; the
 * batches themselves are gone (five-at-a-time is the campaign's business, not
 * the scraper's) and these two are not about batching at all. */
function paintRosterTools() {
  const wrap = $('#pipe-roster-tools');
  if (wrap) wrap.hidden = !(pipe.members || []).length;
}

function renderListFoot() {
  const total = pipe.members.length;
  $('#pipe-list-info').textContent = total ? `${total} member(s)` : '';
}

/* The status badge, plus whatever the run said about it — "failed" alone is not
 * an answer an operator can do anything with. Five states and no more: a member
 * with no row was never picked, which is a fact about the selection rather than
 * about a run, so it reads Not started. */
const STATUS_LOOK = {
  pending:    { label: 'Pending',     icon: '#i-clock' },
  processing: { label: 'Processing',  icon: '#i-reset' },
  sent:       { label: 'Sent',        icon: '#i-check' },
  // Written and left in the composer, because Settings → Click Send is off.
  // Its own badge: green would claim a message that has not gone anywhere.
  typed:      { label: 'Typed',       icon: '#i-mail' },
  failed:     { label: 'Failed',      icon: '#i-x' },
};

function statusCell(status, state) {
  const look = STATUS_LOOK[status]
    || { label: 'Not started', icon: '#i-minus' };
  const badge = `<span class="st ${escapeHtml(status || 'none')}">`
    + `<svg class="ic" viewBox="0 0 24 24"><use href="${look.icon}"/></svg>`
    + `${escapeHtml(look.label)}</span>`;
  const why = state && state.detail && status !== 'sent'
    ? `<span class="st-why" title="${escapeHtml(state.detail)}">${escapeHtml(truncate(state.detail, 34))}</span>`
    : '';
  return badge + why;
}

/* The whole roster in one press: every member's profile opened a tab-full at a
 * time, with anyone whose card carried no id clicked on the attendee list first.
 * A run, not a call — two hundred profiles is minutes, so it reports in the run
 * box and stops from there.
 *
 * Members with no id are sent as empty handles ON PURPOSE: they are the ones
 * the run has to click, and dropping them here would leave them unfetchable. */
$('#pipe-open-all').addEventListener('click', async () => {
  const members = pipe.members || [];
  if (!members.length) return toast('Collect the members first.', 'warn');
  const active = await requireOpenAccount();
  if (!active) return;

  const button = $('#pipe-open-all');
  setBusy(button, true);
  try {
    const { task } = await api('/api/automation/members/open-all', {
      method: 'POST',
      body: { account_id: active.id, tabs: PROFILE_TABS,
              event_url: (pipe.event && pipe.event.url) || '',
              // Names as well as ids: a member with no id is identified by name
              // when the click finds one, and that is how the row is updated.
              members: members.map(m => ({ username: m.username || '',
                                           name: m.name || '' })) },
    });
    const blank = members.filter(m => !m.username).length;
    toast(blank
      ? `Opening ${blank} attendee(s) ${PROFILE_TABS} tabs at a time — each takes `
        + 'its id off the profile and closes. Stop it in the run box.'
      : `Opening ${members.length} profile(s), ${PROFILE_TABS} at a time — `
        + 'stop it in the run box.', 'info');
    // Polled as it goes, not only at the end: the run publishes each id the
    // moment it has it, so the table fills in a row at a time instead of
    // sitting on "no profile link" until the last member is done.
    watchTask(task, {
      onTick: status => applyRecoveredIds(status && status.run && status.run.meta),
      onDone: result => {
        setBusy(button, false);
        applyRecoveredIds(result);
      },
    });
  } catch (err) {
    setBusy(button, false);
    toast(err.message, 'error');
  }
});

/* Fold the ids a run clicked its way to back into the table.
 *
 * Matched by name, the same way the run matched them: the rows had no id to
 * match on, which is the whole reason the run existed. Without this the table
 * would still read "no profile link" for people it just identified, and the
 * batches — which only take members with an id — would stay empty. */
function applyRecoveredIds(result) {
  const rows = (result && result.members) || [];
  if (!rows.length) return;
  const byName = new Map();
  for (const row of rows) {
    if (row.username && row.name) byName.set(row.name.trim().toLowerCase(), row.username);
  }
  let changed = 0;
  for (const m of pipe.members) {
    if (m.username) continue;
    const found = byName.get((m.name || '').trim().toLowerCase());
    if (found) { m.username = found; changed++; }
  }
  if (changed) {
    renderPipeMembers();
    paintSummary();
  }
}

$('#pipe-batch-close').addEventListener('click', async () => {
  const button = $('#pipe-batch-close');
  setBusy(button, true);
  try {
    const result = await api('/api/automation/members/tabs/close', {
      method: 'POST', body: { account_id: scrapeSession ? scrapeSession.id : null },
    });
    toast(result.detail, 'ok');
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    setBusy(button, false);
  }
});

/* ── Runs ────────────────────────────────────────────────────── */

let watching = null;

/* Poll one task until it stops.
 *
 * `onTick` sees every poll while it runs and `onDone` the finished result — the
 * pipeline uses the first to keep its member table live and the second to draw
 * the final report and let its buttons go again. */
async function watchTask(taskId, { onTick, onDone } = {}) {
  watching = taskId;
  $('#run-box').hidden = false;
  let misses = 0;
  const tick = async () => {
    if (watching !== taskId) return;
    let status;
    try {
      status = await api(`/api/automation/tasks/${taskId}`);
      misses = 0;
    } catch (err) {
      // A restarting server should not silently end the watch: a caller waiting
      // on onDone would be stuck mid-run forever. Retry a few times, then give
      // up loudly.
      if (++misses < 5) return setTimeout(tick, 2000);
      watching = null;
      toast(`Lost track of the run: ${err.message}`, 'error');
      if (onDone) onDone({ detail: 'lost track of the run' });
      return;
    }

    const run = status.run || {};
    $('#run-title').textContent = status.kind;
    $('#run-detail').textContent = run.detail || status.state;
    $('#run-bar').style.width = `${run.percent ?? 0}%`;
    $('#pill-run').textContent = status.state === 'running'
      ? `${status.kind} ${run.done ?? 0}/${run.total ?? '?'}` : 'idle';

    if (onTick) onTick(status);
    if (status.state === 'running') return setTimeout(tick, 1200);
    watching = null;
    $('#pill-run').textContent = 'idle';
    toast(status.error || (status.result?.detail ?? 'finished'),
          status.state === 'error' ? 'error' : 'ok');
    // What a run found still goes to the store — the pipeline reads it back for
    // the board's header — but there is no results list under the progress bar
    // to refresh any more.
    if (onDone) onDone(status.result || { detail: status.error || 'finished' });
  };
  tick();
}

/* ── Data ────────────────────────────────────────────────────── */

/* The rosters ARE the Data panel: one row per event, its members and their ids.
 * The whole-store members table that used to sit under them said the same
 * things about the same people with the event — the only thing anybody looks
 * them up by — taken out. It is still a TSV download away (/api/data/export). */
function loadDataPanel() {
  loadRosters();
}

/* The event roster files, straight out of MinIO. One row per event: which
 * event, how many members, and the members themselves — written by the
 * scraping panel as soon as an event's members have ids to write. */
const ROSTER_SHOWN = 10;          // members per row before Show more

async function loadRosters() {
  const body = $('#roster-table tbody');
  if (!body) return;
  // The list scrolls inside its own box now, so a Reload pressed halfway down
  // it has a place to put the operator back — the same reason the member table
  // keeps its scrollTop across a redraw.
  const wrap = $('#roster-wrap');
  const keep = wrap ? wrap.scrollTop : 0;
  try {
    // full=1: the names are IN the table, so the files have to be opened. The
    // cheap listing is for anything that only needs to know they exist.
    const { rosters } = await api('/api/data/rosters?full=1');
    $('#roster-count').textContent = rosters.length
      ? `${rosters.length} event(s)` : '';
    const totalUsers = rosters.reduce(
      (sum, roster) => sum + Number(roster.count ?? roster.members?.length ?? 0),
      0,
    );
    $('#roster-users-count').textContent = rosters.length
      ? `All Users: ${totalUsers.toLocaleString()}` : '';
    body.innerHTML = rosters.length
      ? rosters.map((roster, i) => rosterRow(roster, i)).join('')
      : '<tr class="note"><td colspan="4" class="muted">No event files yet — '
        + 'collect an event\'s members in <b>Scraping</b> and one is written as '
        + 'soon as they have ids.</td></tr>';
    if (wrap) wrap.scrollTop = keep;
  } catch (err) {
    $('#roster-count').textContent = '';
    $('#roster-users-count').textContent = '';
    body.innerHTML = `<tr class="note"><td colspan="4" class="muted">${escapeHtml(err.message)}</td></tr>`;
  }
}

/* Row n's colour in the roster table. Six, cycling: enough that neighbouring
 * rows never share one, few enough that they stay distinguishable. Kept here,
 * beside its only caller — the scraper's batch colours are gone and this is not
 * about batching. */
const ROW_TINTS = 6;
const rowTint = n => ((n - 1) % ROW_TINTS) + 1;

function rosterRow(roster, index) {
  const members = roster.members || [];
  const key = escapeHtml(roster.key);
  const shown = members.slice(0, ROSTER_SHOWN).map(memberPill).join(', ');
  const rest = members.slice(ROSTER_SHOWN).map(memberPill).join(', ');
  return `
    <tr class="rt-${rowTint(index + 1)}" data-key="${key}">
      <td class="c-event">
        <span class="r-who">
          <span class="r-tile"><svg class="ic" viewBox="0 0 24 24"><use href="#i-calendar"/></svg></span>
          <span class="r-text">
            <span class="r-event">${escapeHtml(roster.event || roster.key)}</span>
            <!-- The Find tab this event was scraped under, small under its
                 name. A sub-line rather than a column of its own: it is
                 context for the event, and a fifth column would take width
                 from the usernames, which are what this table is for.
                 Rosters written before the column existed have no answer —
                 they say so rather than showing a blank. -->
            <span class="r-cat${roster.category ? '' : ' none'}">${
              roster.category ? escapeHtml(roster.category)
                              : 'category not recorded'}</span>
          </span>
        </span>
      </td>
      <td class="c-count">
        <span class="r-n">${roster.count || 0}</span>
      </td>
      <td class="c-who">
        <span class="who-text">${shown}${rest
          ? `<span class="who-rest" hidden>, ${rest}</span><button
               class="more" data-more="${key}">… see more</button>` : ''}</span>
      </td>
      <td class="c-do">
        <a class="tiny btn" href="/api/data/rosters/${encodeURIComponent(roster.key)}/download"
           download title="Download the file"><svg class="ic" viewBox="0 0 24 24"><use href="#i-download"/></svg></a>
        <button class="tiny danger" data-drop="${key}" title="Delete this file">
          <svg class="ic" viewBox="0 0 24 24"><use href="#i-trash"/></svg></button>
      </td>
    </tr>`;
}

/* One member as `name[id]` in a soft pill, tinted with its row's hue. The pill
 * is what makes twelve of them scannable — a plain run of text turned the
 * column into a paragraph nobody could pick a name out of — and the row's own
 * colour is what keeps a wrapped block of them attached to their event.
 *
 * Still a link: the id is the thing that opens the profile. */
function memberPill(member) {
  const handle = member.username || '';
  return `<a class="who-one" target="_blank" rel="noopener"
     href="https://www.meetup.com/members/${encodeURIComponent(handle)}/"
     title="${escapeHtml(member.name || '')}"
     >${escapeHtml(member.name || '(no name)')}<span class="w-id">[${escapeHtml(handle)}]</span></a>`;
}

$('#roster-table').addEventListener('click', async e => {
  const more = e.target.closest('button[data-more]');
  if (more) {
    // In the row, not a page away: the question is "who else was on this one",
    // and the answer belongs after the ten already reading.
    const rest = more.closest('.who-text').querySelector('.who-rest');
    rest.hidden = !rest.hidden;
    more.textContent = rest.hidden ? 'see more' : 'see less';
    return;
  }

  const drop = e.target.closest('button[data-drop]');
  if (!drop) return;
  if (!confirm(`Delete the file for "${drop.dataset.drop}"?`)) return;
  try {
    await api(`/api/data/rosters/${encodeURIComponent(drop.dataset.drop)}`,
              { method: 'DELETE' });
    loadRosters();
  } catch (err) { toast(err.message, 'error'); }
});

$('#roster-reload').addEventListener('click', loadRosters);

/* ── Campaign ────────────────────────────────────────────────── */

/* ── The message drafts, as tabs ─────────────────────────────── */

/* One draft on screen at a time, picked off a strip of tabs.
 *
 * They used to be stacked: six drafts meant six textareas down the page and a
 * scroll to reach the one you meant, which is the wrong shape for a set of
 * ALTERNATIVES — the sender rotates between them, so they are versions of one
 * thing rather than six things in an order. A tab strip says that, and it puts
 * every draft's name on screen at once so picking one is a click.
 *
 * `list` is the truth, not the DOM: a rename has to survive the re-render it
 * causes, and reading six textareas back to find out what the operator typed
 * is how a save ends up disagreeing with the screen.
 *
 * What is ALREADY in the bucket stays folded away until Stored drafts is
 * pressed, and the box opens on a blank message instead: opening this step to
 * write one should not mean reading six you wrote last month first, and a
 * stored draft sitting in the textarea is not hidden just because the strip
 * above it is.
 *
 * `active` indexes `list`, or is -1 for the blank one in `scratch`. A scratch
 * joins the list the moment it has text, because a draft IS its text — until
 * then there is nothing to store, name or send. */
const msgs = { list: [], active: -1, dirty: false, showTabs: false,
               scratch: null };

/* A blank draft, named off the ones already taken so msg3 deleted and started
 * again does not come back as a second msg2. */
function newScratch() {
  // The first FREE name, not the next number up: a bucket holding sms1…sms5
  // should offer msg1, and msg3 deleted should be offered again rather than
  // leaving a hole and handing out msg7.
  const taken = new Set(msgs.list.map(d => d.label));
  let n = 1;
  while (taken.has(`msg${n}`)) n += 1;
  return { label: `msg${n}`, text: '', disabled: false };
}

/* The record the textarea is editing: a stored draft, or the blank one. */
function activeDraft() {
  return msgs.active >= 0 ? msgs.list[msgs.active] : msgs.scratch;
}

/* Replace the whole set — from MinIO on load, or from a file upload. */
function setDrafts(rows, { dirty = false, select = -1 } = {}) {
  msgs.list = (rows || []).map((d, i) => ({
    label: String(d.label || `msg${i + 1}`),
    text: String(d.text || ''),
    disabled: !!d.disabled,
  }));
  msgs.active = select >= 0 && select < msgs.list.length ? select : -1;
  msgs.scratch = newScratch();
  msgs.dirty = dirty;
  renderDrafts();
}

/* How many will actually be sent. Disabled ones are still stored and still on
 * the strip, so the checklist has to count what is LIVE rather than what is
 * there. */
function liveDrafts() {
  return msgs.list.filter(d => d.text.trim() && !d.disabled);
}

/* The message the operator has actually settled on: what is in the box, or the
 * stored draft they picked off the strip. Null when the box is empty and
 * nothing is picked.
 *
 * This, not "the bucket has drafts in it", is what step 3 being done means. A
 * bucket full of last month's drafts ticked the step before the operator had
 * opened it, which is a checklist reporting on the store rather than on them.
 * An empty or disabled one is not a message either — neither can be sent. */
function chosenDraft() {
  const draft = activeDraft();
  return draft && draft.text.trim() && !draft.disabled ? draft : null;
}

/* The strip alone, so a tab can appear or be renamed mid-sentence without the
 * textarea being rebuilt under the cursor. */
function renderTabs() {
  const strip = $('#drafts .d-tabs');
  if (!strip) return;
  strip.hidden = !msgs.showTabs;
  strip.innerHTML = msgs.list.map((d, i) => `
    <button class="d-tab ${i === msgs.active ? 'on' : ''} ${d.disabled ? 'off' : ''}"
            role="tab" aria-selected="${i === msgs.active}" data-tab="${i}"
            title="${d.disabled ? 'Disabled — kept, not sent' : escapeHtml(d.text.slice(0, 90))}">
      ${escapeHtml(d.label)}${d.disabled ? '<span class="d-dot"></span>' : ''}
    </button>`).join('')
    || '<span class="muted">Nothing stored yet — what you write here is the first.</span>';
}

function renderDrafts() {
  const box = $('#drafts');
  if (!box) return;
  if (!msgs.scratch) msgs.scratch = newScratch();
  if (msgs.active >= msgs.list.length) msgs.active = -1;
  const draft = activeDraft();
  const isNew = msgs.active < 0;

  box.innerHTML = `
    <div class="d-tabs" role="tablist" ${msgs.showTabs ? '' : 'hidden'}></div>

    <div class="d-editor">
      <div class="row wrap d-bar">
        <label class="d-rename">Name
          <input class="d-name" value="${escapeHtml(draft.label)}" maxlength="40"
                 title="Rename this draft">
        </label>
        <span class="d-state ${draft.disabled ? 'is-off' : 'is-on'}">${
          isNew ? 'New — stored when it has text'
                : (draft.disabled ? 'Disabled — kept but never sent'
                                  : 'Active — in the rotation')}</span>
        <div class="grow"></div>
        <button class="tiny" data-act="toggle" ${isNew ? 'disabled' : ''}>
          <svg class="ic" viewBox="0 0 24 24"><use href="#i-${draft.disabled ? 'check' : 'minus'}"/></svg>
          ${draft.disabled ? 'Enable' : 'Disable'}</button>
        <button class="tiny danger" data-act="delete">
          <svg class="ic" viewBox="0 0 24 24"><use href="#i-trash"/></svg>${
            isNew ? 'Clear' : 'Delete'}</button>
      </div>
      <div class="d-box">
        <textarea class="d-text" rows="6"
                  placeholder="What this draft says…">${escapeHtml(draft.text)}</textarea>
        <button class="d-import" data-act="import" type="button"
                title="Import drafts from a file — txt · csv · tsv · xlsx. You can also drop one here.">
          <svg class="ic" viewBox="0 0 24 24"><use href="#i-upload"/></svg></button>
      </div>
      <div class="d-foot">
        <span class="muted">${draft.text.length} character(s)</span>
        <div class="grow"></div>
        <span class="muted">${isNew ? 'new message'
          : `${msgs.active + 1} of ${msgs.list.length}`}</span>
      </div>
    </div>`;
  renderTabs();
  syncDraftCount();
}

/* The count on the card and the wizard's own tally, from one place — they
 * disagreed the moment two of them counted separately. */
function syncDraftCount() {
  const live = liveDrafts().length;
  const off = msgs.list.filter(d => d.disabled).length;
  camp.drafts = live;
  camp.chosen = chosenDraft();
  const count = $('#draft-count');
  if (count) {
    count.textContent = msgs.list.length
      ? `${live} active${off ? ` · ${off} disabled` : ''}${msgs.dirty ? ' · unsaved' : ''}`
      : 'none saved yet';
    count.classList.toggle('is-dirty', msgs.dirty);
  }
  const save = $('#draft-save');
  if (save) save.classList.toggle('dirty', msgs.dirty);

  // The button says how many are behind it, so a folded strip is never mistaken
  // for an empty bucket.
  const toggle = $('#draft-toggle');
  if (toggle) {
    const label = toggle.lastChild;
    if (label) label.textContent = msgs.showTabs
      ? ' Hide stored' : ` Stored drafts (${msgs.list.length})`;
    toggle.setAttribute('aria-expanded', String(msgs.showTabs));
    toggle.classList.toggle('on', msgs.showTabs);
  }
  renderChecklist();
}

/* Switching tab keeps whatever was typed into the one being left: the textarea
 * is the editor for whichever record is active, not a thing of its own. */
function stashDraftEdits() {
  const area = $('#drafts .d-text');
  const name = $('#drafts .d-name');
  const draft = activeDraft();
  if (!draft) return;
  if (area && area.value !== draft.text) { draft.text = area.value; msgs.dirty = true; }
  if (name) {
    const label = name.value.trim() || draft.label;
    if (label !== draft.label) { draft.label = label; msgs.dirty = true; }
  }
  commitScratch();
}

/* A blank draft that has been typed into is a draft. Moved into the list here
 * rather than at save time, so the strip, the count and the checklist all see
 * it the moment it exists — and so leaving the tab does not lose it. */
function commitScratch() {
  if (msgs.active >= 0 || !msgs.scratch || !msgs.scratch.text.trim()) return false;
  msgs.list.push(msgs.scratch);
  msgs.active = msgs.list.length - 1;
  msgs.scratch = newScratch();
  msgs.dirty = true;
  return true;
}

$('#drafts').addEventListener('click', e => {
  const tab = e.target.closest('[data-tab]');
  if (tab) {
    stashDraftEdits();
    msgs.active = Number(tab.dataset.tab);
    return renderDrafts();
  }
  const action = e.target.closest('[data-act]');
  if (!action) return;

  if (action.dataset.act === 'import') return $('#msg-file').click();

  if (action.dataset.act === 'delete' && msgs.active < 0) {
    // A blank one has nothing to delete — the button clears the box instead,
    // and asking "are you sure" about two words nobody has saved is noise.
    msgs.scratch = newScratch();
    return renderDrafts();
  }
  stashDraftEdits();
  const draft = msgs.list[msgs.active];
  if (!draft) return;

  if (action.dataset.act === 'toggle') {
    draft.disabled = !draft.disabled;
    msgs.dirty = true;
    return renderDrafts();
  }
  if (action.dataset.act === 'delete') {
    if (!confirm(`Delete "${draft.label}"?\n\nDisable it instead to keep the text.`)) return;
    msgs.list.splice(msgs.active, 1);
    // Back to a blank box rather than to whichever draft happens to sit at the
    // same index now: the operator deleted what they were looking at, and
    // landing on somebody else's text reads as if it had not worked.
    msgs.active = -1;
    msgs.scratch = newScratch();
    msgs.dirty = true;
    renderDrafts();
  }
});

/* Typed-in changes go to the record as they are made, so a tab switch, a save
 * or the checklist never has to go looking for them. */
$('#drafts').addEventListener('input', e => {
  const draft = activeDraft();
  if (!draft) return;
  if (e.target.matches('.d-text')) {
    draft.text = e.target.value;
    msgs.dirty = true;
    // A blank draft that has just been typed into joins the list, and the strip
    // is redrawn on its own — a full re-render mid-sentence loses the caret.
    if (commitScratch()) {
      $('#drafts .d-state').textContent = 'Active — in the rotation';
      $('#drafts .d-bar [data-act="toggle"]').disabled = false;
      const del = $('#drafts .d-bar [data-act="delete"]');
      if (del) del.lastChild.textContent = 'Delete';
      const place = $('#drafts .d-foot span:last-child');
      if (place) place.textContent = `${msgs.active + 1} of ${msgs.list.length}`;
    }
    const foot = $('#drafts .d-foot .muted');
    if (foot) foot.textContent = `${draft.text.length} character(s)`;
    renderTabs();
    syncDraftCount();
  }
  if (e.target.matches('.d-name')) {
    draft.label = e.target.value.trim() || draft.label;
    msgs.dirty = true;
    renderTabs();
    syncDraftCount();
  }
});

/* The stored set, on request. Everything else on this step — the upload above
 * and the box below — is about writing one; this is the only control that puts
 * what is already in the bucket on screen. */
$('#draft-toggle').addEventListener('click', () => {
  msgs.showTabs = !msgs.showTabs;
  const strip = $('#drafts .d-tabs');
  if (strip) strip.hidden = !msgs.showTabs;
  syncDraftCount();
});

/* The wizard's own state. Kept here rather than read back out of the DOM so the
 * Status checklist has one thing to consult — five getters scraping five panels
 * is how a checklist ends up disagreeing with the panels it describes. */
const camp = { session: null, targets: [], media: new Set(), drafts: 0,
               uploaded: [], chosen: null,
               // Every account the campaign runs as. The send deals its
               // recipients between them and each gets its own browser window.
               accounts: new Set(),
               // The one whose session is shown under the picker — the last
               // tile clicked. A view, not the selection: `accounts` is what
               // the run reads.
               account: null,
               // id -> session row, for every account. The checklist answers
               // for ALL the picked ones, not only the one on screen.
               sessions: new Map() };

$('#camp-steps').addEventListener('click', e => {
  const button = e.target.closest('button[data-step]');
  if (!button) return;
  showStep(button.dataset.step);
});

/* ── The campaign's own settings ─────────────────────────────── */

/* The knobs that shape a SEND, on the screen where sending happens.
 *
 * Not a second copy of the Settings panel — the same stored values through the
 * same endpoint, cut down to the handful that decide what the next campaign
 * does, and grouped the way the operator thinks about them. Two settings that
 * disagree about the same run is the thing this must never become, which is why
 * it renders from the schema rather than from a hand-written list of inputs.
 *
 * Each pair is a RANGE the run samples from — see settings.RANGES. A fixed
 * batch of ten with exactly two minutes between them is a metronome, and the
 * whole reason for batching is not to have one. */
const COG_GROUPS = [
  { title: 'Sending',
    keys: ['click_send', 'campaign_tabs'] },
  { title: 'Batches',
    help: 'A campaign sends in runs with a real stop between them. Both are '
        + 'ranges — every batch rolls its own size and its own wait.',
    keys: ['batch_campaigns',
           ['batch_size_min', 'batch_size_max', 'Messages per batch'],
           ['batch_pause_min_s', 'batch_pause_max_s', 'Wait between batches (s)']] },
  { title: 'Pacing',
    keys: [['action_delay_min_s', 'action_delay_max_s', 'Between actions (s)'],
           'decoy_activity'] },
];

let cogSchema = null;

async function openCampCog() {
  const box = $('#camp-cog-box');
  try {
    const { settings, schema } = await api('/api/settings');
    cogSchema = Object.fromEntries(schema.map(r => [r.key, r]));
    $('#camp-cog-body').innerHTML = COG_GROUPS.map(group => `
      <div class="cog-group">
        <h3>${escapeHtml(group.title)}</h3>
        ${group.help ? `<p class="cog-help">${escapeHtml(group.help)}</p>` : ''}
        ${group.keys.map(k => Array.isArray(k)
          ? cogRange(k[0], k[1], k[2], settings)
          : cogOne(k, settings)).join('')}
      </div>`).join('');
    setCogFoot('Saved as you change them.', '');
  } catch (err) {
    $('#camp-cog-body').innerHTML =
      `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
  if (!box.open) box.showModal();
}

/* One setting: a switch for a bool, a number box for the rest. */
function cogOne(key, settings) {
  const row = cogSchema[key];
  if (!row) return '';
  return `
    <label class="cog-row">
      <span class="cog-k">${escapeHtml(cogName(key))}</span>
      ${row.type === 'bool'
        ? `<input type="checkbox" data-cog="${key}" ${settings[key] ? 'checked' : ''}>`
        : `<input type="number" step="any" data-cog="${key}"
                  min="${row.min ?? ''}" max="${row.max ?? ''}"
                  value="${escapeHtml(String(settings[key]))}">`}
      <span class="cog-help">${escapeHtml(row.help)}</span>
    </label>`;
}

/* A min/max pair on ONE line. Two separate rows read as two unrelated numbers;
 * side by side with a dash between them they read as what they are — the range
 * a value is rolled from. */
function cogRange(lowKey, highKey, label, settings) {
  const low = cogSchema[lowKey], high = cogSchema[highKey];
  if (!low || !high) return '';
  return `
    <div class="cog-row range">
      <span class="cog-k">${escapeHtml(label)}</span>
      <span class="cog-span">
        <input type="number" step="any" data-cog="${lowKey}"
               min="${low.min ?? ''}" max="${low.max ?? ''}"
               value="${escapeHtml(String(settings[lowKey]))}">
        <span class="cog-dash">–</span>
        <input type="number" step="any" data-cog="${highKey}"
               min="${high.min ?? ''}" max="${high.max ?? ''}"
               value="${escapeHtml(String(settings[highKey]))}">
      </span>
      <span class="cog-help">rolled fresh each time, between these</span>
    </div>`;
}

/* The key, as words. The Settings panel shows raw keys on purpose — it is the
 * full list and the key IS the name there — but this box shows a chosen few to
 * somebody mid-campaign, and `campaign_tabs` is not what they call it. */
const COG_NAMES = {
  click_send: 'Click Send',
  campaign_tabs: 'Recipients at once (per account)',
  batch_campaigns: 'Send in batches',
  decoy_activity: 'Browse between messages',
};
function cogName(key) {
  return COG_NAMES[key] || key;
}

function setCogFoot(text, tone) {
  const foot = $('#camp-cog-foot');
  if (foot) { foot.textContent = text; foot.className = `live-foot ${tone || 'muted'}`; }
}

$('#camp-cog').addEventListener('click', openCampCog);
$('#camp-cog-close').addEventListener('click', () => $('#camp-cog-box').close());

/* Saved on the change, not on a Save button. Every value here decides what the
 * next campaign does to other people's inboxes, and a switch you have to
 * remember to save is a switch that is sometimes lying about that. */
$('#camp-cog-body').addEventListener('change', async e => {
  const input = e.target.closest('[data-cog]');
  if (!input) return;
  const key = input.dataset.cog;
  const value = input.type === 'checkbox' ? input.checked : input.value;
  try {
    const { settings } = await api('/api/settings',
                                   { method: 'POST', body: { [key]: value } });
    // Repaint every box from what was STORED. A value the backend clamped — or
    // a maximum it lifted to meet a minimum — must not leave the box claiming
    // the number that was typed.
    $$('#camp-cog-body [data-cog]').forEach(el => {
      const k = el.dataset.cog;
      if (el.type === 'checkbox') el.checked = !!settings[k];
      else if (el !== document.activeElement) el.value = settings[k];
    });
    if (String(settings[key]) !== String(value) && input.type !== 'checkbox') {
      setCogFoot(`${cogName(key)} was adjusted to ${settings[key]} — `
                 + 'it is outside the allowed range, or below its pair.', 'bad');
    } else {
      setCogFoot('Saved.', 'ok');
    }
    // The Settings panel is the same values; keep it honest if it is loaded.
    if (panelIsOn('settings')) loadSettings();
  } catch (err) {
    setCogFoot(err.message, 'bad');
    openCampCog();                    // back to what is actually stored
  }
});

function showStep(name) {
  // `button[data-step]`, not every button in the rail — the cog at the head of
  // it is not a step and must never be given the selected look.
  $$('#camp-steps button[data-step]')
    .forEach(b => b.classList.toggle('on', b.dataset.step === name));
  $$('.step').forEach(s => s.classList.toggle('on', s.dataset.step === name));
  // Each step reads its own source when it is opened, rather than everything
  // being fetched on load: three MinIO round-trips for two panels nobody has
  // looked at yet is the slow way to open a wizard.
  // Users opens on the upload, with the store behind Database: reading 291
  // scraped handles is not what somebody arriving with a file came to do.
  if (name === 'users' && !$('#users-store-card').hidden) loadStoredUsers();
  if (name === 'media' && !$('#media-library-card').hidden) loadMedia();
  if (name === 'status') renderChecklist();
}

/* ── Step 2 · The scraped members, out of MinIO ──────────────── */

/* Everyone the member store holds, and which of them are ticked. Kept here
 * rather than read back off the checkboxes for the same reason the pipeline
 * keeps its own: a filter that re-renders the list would otherwise lose every
 * tick that scrolled out of the search. */
const store = { rows: [], loaded: false, range: null,
                // Every category the stored members were scraped under, and the
                // one being filtered to (null = all of them).
                cats: [], cat: null };

/* Upload or database, never both. They fill the same slot — the recipient
 * list — and two lists on screen is one question too many about which of them
 * a run would actually send to. */
function showUsersView(which) {
  const database = which === 'database';
  $('#users-upload-card').hidden = database;
  $('#users-store-card').hidden = !database;
  if (database) loadStoredUsers();
}

$('#users-database').addEventListener('click', () => showUsersView('database'));
$('#users-upload-back').addEventListener('click', () => showUsersView('upload'));

async function loadStoredUsers(force = false) {
  if (store.loaded && !force) return;
  const body = $('#store-table tbody');
  if (!body) return;
  body.innerHTML = '<tr class="note"><td colspan="5" class="muted">Reading the member store…</td></tr>';
  try {
    const { members, categories } = await api('/api/data/members');
    store.rows = members || [];
    store.cats = categories || [];
    // A category filter over a list that has just been replaced means something
    // different now — and a category that is no longer in the store would leave
    // the table empty with a chip lit saying otherwise.
    if (store.cat && !store.cats.includes(store.cat)) store.cat = null;
    store.loaded = true;
    // A range is row numbers over a list that has just been replaced, so it
    // means something different now — and silently sending to whoever those
    // rows landed on is the one outcome worth ruling out.
    store.range = null;
    renderStoredUsers();
  } catch (err) {
    store.loaded = false;
    $('#store-count').textContent = '';
    body.innerHTML = `<tr class="note"><td colspan="5" class="muted">${escapeHtml(err.message)}</td></tr>`;
  }
}

/* What the search and the checkbox leave on screen, before any range is taken
 * off it. One place, because the range has to mean the same rows the operator
 * is looking at — reading the DOM for that is how the two drift apart. */
function shownUsers() {
  const needle = ($('#store-search').value || '').trim().toLowerCase();
  const hideUsed = $('#store-hide-used').checked;
  return store.rows.filter(row => {
    if (hideUsed && (row.used_count || 0) > 0) return false;
    // A member can be under SEVERAL categories — they were on events that sat
    // under different tabs — so this is "is it among theirs", not "is it
    // theirs".
    if (store.cat && !(row.categories || []).includes(store.cat)) return false;
    if (!needle) return true;
    return [row.username, row.name, row.groups, row.location,
            (row.categories || []).join(' ')]
      .some(field => String(field || '').toLowerCase().includes(needle));
  });
}

/* The categories, as a dropdown beside the search. Each option carries its own
 * count — "Technology 91" — because a category is only worth picking if it has
 * people in it, and a pick that narrows the list to nothing is a click nobody
 * wanted to spend.
 *
 * Rebuilt on every render, so the counts follow the store. The SELECTION is
 * written back from state rather than left to the DOM, which is what keeps it
 * on the right option after a reload replaces every one of them. */
function renderCatFilter() {
  const picker = $('#store-cat');
  if (!picker) return;
  const label = picker.closest('.store-cat');
  // Nothing to filter by: the whole control goes rather than sitting there
  // offering one option that changes nothing.
  if (label) label.hidden = !store.cats.length;
  if (!store.cats.length) return;

  const count = name => store.rows.filter(
    r => (r.categories || []).includes(name)).length;
  const option = (value, text) =>
    `<option value="${escapeHtml(value)}">${escapeHtml(text)}</option>`;

  picker.innerHTML = option('', `All ${store.rows.length}`)
    + store.cats.map(name => option(name, `${name} ${count(name)}`)).join('');
  picker.value = store.cat || '';
}

/* Picking one narrows the table to everyone under it. A range already taken is
 * dropped: those row numbers were over a different list, and silently sending
 * to whoever they now land on is the outcome worth ruling out. */
$('#store-cat').addEventListener('change', e => {
  store.cat = e.target.value || null;
  store.range = null;
  camp.uploaded = [];
  camp.targets = [];
  renderStoredUsers();
  renderChecklist();
});

/* Where a range's ends land on today's list. Kept apart from applying one so
 * the arithmetic is the same whether it is being previewed or taken. */
function clampRange(from, to, total) {
  if (isNaN(from)) from = 1;                 // blank reads as "from the top"
  if (isNaN(to)) to = total;                 // and as "to the bottom"
  if (from > to) [from, to] = [to, from];    // a typo with an obvious meaning
  return { from: Math.max(1, from), to: Math.min(total, to) };
}

/* The rows the table shows: the search's result, sliced by the range once one
 * has been taken. `offset` is where the slice starts, so the # column goes on
 * counting in the list's own numbers rather than restarting at 1. */
function listedUsers() {
  const shown = shownUsers();
  if (!store.range) return { rows: shown, offset: 0 };
  const { from, to } = store.range;
  return { rows: shown.slice(from - 1, to), offset: from - 1 };
}

function renderStoredUsers() {
  const { rows, offset } = listedUsers();
  const body = $('#store-table tbody');
  const wrap = $('#store-wrap');
  const keep = wrap ? wrap.scrollTop : 0;

  renderCatFilter();

  if (!rows.length) {
    body.innerHTML = '<tr class="note"><td colspan="5" class="muted">'
      + (store.cat ? `Nobody stored under <b>${escapeHtml(store.cat)}</b> yet.`
         : store.rows.length ? 'Nothing matches that search.'
         : 'No members stored yet — collect an event in <b>Scraping</b>.')
      + '</td></tr>';
  } else {
    body.innerHTML = rows.map((row, i) => {
      const handle = row.username;
      const used = row.used_count || 0;
      return `
        <tr data-handle="${escapeHtml(handle)}">
          <td class="c-n">${offset + i + 1}</td>
          <td>
            <span class="who-text">
              <!-- The id printed twice reads as a broken panel rather than as
                   "nobody has read this one's name yet", which is what it is:
                   a member no roster covers, e.g. a handle uploaded by hand. -->
              <span class="m-name${row.name ? '' : ' none'}">${row.name
                ? escapeHtml(row.name) : 'no name yet'}</span>
              <a class="m-handle" target="_blank" rel="noopener"
                 href="https://www.meetup.com/members/${encodeURIComponent(handle)}/"
                 >#${escapeHtml(handle)}</a>
            </span></td>
          <!-- The Find categories this member was scraped under. Several,
               because the same person turns up on events filed under different
               tabs — and a member no roster covers has none, which is a fact
               about how they got here rather than a gap. -->
          <td class="c-cats" title="${escapeHtml((row.categories || []).join(', '))}">${
            (row.categories || []).length
              ? (row.categories || []).map(c =>
                  `<span class="tag">${escapeHtml(c)}</span>`).join(' ')
              : '<span class="muted">—</span>'}</td>
          <td class="c-groups" title="${escapeHtml(row.groups || '')}">${
            escapeHtml(truncate(row.groups || row.location || '—', 40))}</td>
          <td class="c-used">${used
            ? `<span class="st sent">${used}×</span>`
            : '<span class="muted">—</span>'}</td>
        </tr>`;
    }).join('');
  }
  if (wrap) wrap.scrollTop = keep;

  // What is on screen IS what a run would send to, so the line says which of
  // the two it is looking at — the whole store, or a range taken off it.
  // Kept SHORT. It sits in the card head beside the title and three buttons,
  // and a longer sentence wrapped onto three lines and squeezed the heading —
  // the category it is filtered to is already lit on the chip below.
  const total = store.rows.length;
  $('#store-count').textContent = store.range
    ? `rows ${store.range.from}–${store.range.to} · ${rows.length} of ${total}`
    : `${rows.length} of ${total}`;
}

/* Reloading and searching both change what the numbers mean, so a range taken
 * over the old list is dropped rather than quietly re-pointed at new people. */
function dropRange() {
  if (!store.range) return renderStoredUsers();
  store.range = null;
  camp.uploaded = [];
  camp.targets = [];
  renderStoredUsers();
  renderChecklist();
}

$('#store-search').addEventListener('input', dropRange);
$('#store-hide-used').addEventListener('change', dropRange);
$('#store-reload').addEventListener('click', () => loadStoredUsers(true));

/* Fill in the names a store filled before this fix never kept. The rosters in
 * MinIO have had them all along — this is a read of those, not a re-scrape, so
 * it costs a few object reads rather than a walk of every attendee list. */
$('#store-names').addEventListener('click', async () => {
  const button = $('#store-names');
  button.disabled = true;
  try {
    const out = await api('/api/data/members/backfill-names', { method: 'POST' });
    if (out.named) {
      // Reload: the table is the store, and a count that says 40 rows changed
      // over a table still showing 40 ids is the panel disagreeing with itself.
      await loadStoredUsers(true);
      toast(`Named ${out.named} member(s) from ${out.rosters} roster(s)`, 'ok');
    } else {
      toast(out.rosters
        ? `No names to add — nothing in ${out.rosters} roster(s) matched`
        : 'No event rosters stored yet — collect an event in Scraping', 'warn');
    }
  } catch (err) { toast(err.message, 'error'); }
  button.disabled = false;
});

/* OK: take rows from–to off the list, show ONLY those, and make them the
 * recipients. One press rather than a range that ticks and a second button
 * that commits — what is on screen after this is exactly who gets messaged,
 * which is the shortest true description a send can have. */
$('#store-range').addEventListener('click', () => {
  const shown = shownUsers();
  if (!shown.length) return toast('No rows to take a range from.', 'warn');

  const { from, to } = clampRange(parseInt($('#store-from').value, 10),
                                  parseInt($('#store-to').value, 10),
                                  shown.length);
  if (from > shown.length) {
    return toast(`Only ${shown.length} row(s) to choose from.`, 'warn');
  }

  store.range = { from, to };
  const picked = shown.slice(from - 1, to);
  // The same slot an uploaded file fills: both are the operator naming who to
  // message, and a run sends to whichever was named last.
  camp.uploaded = picked.map(row => row.username);
  camp.targets = picked;
  $('#users-import').hidden = true;
  $('#store-from').value = from;
  $('#store-to').value = to;
  renderStoredUsers();
  renderChecklist();
  toast(`Rows ${from}–${to} loaded — ${picked.length} recipient(s)`, 'ok');
});

/* Back to the whole list, and to no recipients: the range WAS the selection,
 * so clearing one clears the other. */
$('#store-clear').addEventListener('click', () => {
  $('#store-from').value = '';
  $('#store-to').value = '';
  dropRange();
});

async function loadCampaign() {
  try {
    const { drafts } = await api('/api/campaign/drafts');
    // Straight off MinIO and onto the strip — what is stored is what is shown,
    // so nothing is "unsaved" until the operator touches something.
    setDrafts(drafts);
  } catch (err) { toast(err.message, 'error'); }
  loadCampSession();
  renderChecklist();
}

/* ── Step 1 · Accounts ───────────────────────────────────────── */

/* Every account, on the face of the step, one tile each. A dropdown hid the two
 * things this step exists to answer: which accounts there are, and what state
 * each one's session is in. Both are on the tile now, so picking one is a click
 * and the state of the rest is not something you have to open a menu to compare.
 *
 * The DEFAULT PROFILE is not among them. It holds no credentials of its own, it
 * is never the thing a campaign should run as, and its tile was a permanent
 * "Default profile / no session cookie" sitting in front of the accounts that
 * matter — the same reason it has no card in the Accounts panel.
 *
 * SEVERAL can be picked. The campaign then runs from all of them at once — a
 * browser window each, the recipients dealt between them, a tab each in the
 * live panel. `camp.accounts` is that set; `camp.account` is the one whose
 * session detail is shown below, which is the last one clicked. Both are read
 * from here rather than off the DOM, so the one place that answers "which
 * accounts" is the same one the login and the send read. */
function renderAccountPicker(accounts, byId) {
  const row = $('#camp-accounts');
  if (!row) return;

  // An account going away — deleted, or the list reloaded without it — drops
  // out of the selection rather than leaving a run aimed at an id that is gone.
  const alive = new Set(accounts.map(a => a.id));
  for (const id of [...camp.accounts]) if (!alive.has(id)) camp.accounts.delete(id);
  if (camp.account && !alive.has(camp.account)) camp.account = null;

  // Every account with a browser window OPEN picks itself. Those windows are
  // the sessions the campaign actually runs in, so making the operator also
  // point at them was asking them to say something the app already knew — and
  // getting it wrong aimed the run at a different account.
  //
  // Only while nothing is picked: once there IS a selection it is the
  // operator's, and opening another window must not silently add to the list of
  // accounts about to message strangers.
  const before = camp.account;
  if (!camp.accounts.size) {
    const open = accounts.filter(a => (byId[a.id] || {}).active);
    const stored = accounts.filter(a => (byId[a.id] || {}).logged_in);
    // One stored session and no window open is not a choice worth a click
    // either — but two are, so those wait.
    const auto = open.length ? open : (stored.length === 1 ? stored : []);
    for (const a of auto) camp.accounts.add(a.id);
  }
  if (!camp.accounts.has(camp.account)) camp.account = [...camp.accounts][0] ?? null;

  const tile = (id, name, session) => {
    const on = camp.accounts.has(id);
    const shown = camp.account === id;
    const live = session && (session.active || session.logged_in);
    const state = session
      ? (session.active ? 'open in the browser'
         : (session.logged_in ? 'session stored' : (session.detail || 'logged out')))
      : 'no session yet';
    return `
      <button class="acct ${on ? 'on' : ''} ${shown ? 'shown' : ''}"
              data-account="${id ?? ''}"
              aria-pressed="${on}" title="${escapeHtml(state)}">
        <span class="dot ${session && session.active ? 'ok'
                           : (live ? 'warn' : 'off')}"></span>
        <span class="acct-text">
          <span class="acct-name">${escapeHtml(name)}</span>
          <span class="acct-state">${escapeHtml(state)}</span>
        </span>
        <span class="acct-tick"><svg class="ic" viewBox="0 0 24 24"><use href="#i-check"/></svg></span>
      </button>`;
  };

  row.innerHTML = accounts.length
    ? accounts.map(a => tile(a.id, a.label || a.login || `#${a.id}`, byId[a.id])).join('')
    : '<p class="muted">No accounts yet — add one in <b>Accounts</b>.</p>';
  paintAccountNote();

  // The panel underneath describes `camp.session`. Re-read it whenever that can
  // no longer be true: the shown account moved on its own, or there is none left
  // and a session from the last one is still on screen describing an account
  // this step is no longer aimed at.
  if (camp.account !== before || (!camp.account && camp.session)) loadCampSession();
}

/* How the work will be split, said before the run rather than after. Two
 * accounts and 41 recipients is 21 and 20, and an operator should be able to
 * see that on the step where they picked them. */
function paintAccountNote() {
  const note = $('#camp-accounts-note');
  if (!note) return;
  const n = camp.accounts.size;
  if (!n) { note.textContent = 'Pick the account(s) this campaign runs as.'; return; }
  if (n === 1) { note.textContent = '1 account — it messages every recipient.'; return; }
  const total = camp.targets.length;
  const split = total
    ? ' · ' + Array.from({ length: n },
        (_, i) => Math.floor(total / n) + (i < total % n ? 1 : 0)).join(' / ')
    : '';
  note.textContent = `${n} accounts, running at the same time${split}`
    + (total ? ' recipients each' : '');
}

$('#camp-accounts').addEventListener('click', e => {
  const button = e.target.closest('[data-account]');
  if (!button) return;
  const id = Number(button.dataset.account) || null;
  if (!id) return;
  // Toggle — the pick is a SET now. Clicking the only selected one would leave
  // the campaign with no account, so that click is a no-op instead: there is no
  // useful state on the other side of it.
  if (camp.accounts.has(id) && camp.accounts.size > 1) camp.accounts.delete(id);
  else camp.accounts.add(id);
  camp.account = camp.accounts.has(id) ? id : ([...camp.accounts][0] ?? null);
  // Repaint from state rather than toggling the clicked one: the tiles carry
  // session detail that the pick can change what is shown for.
  $$('#camp-accounts .acct').forEach(el => {
    const tid = Number(el.dataset.account) || null;
    el.classList.toggle('on', camp.accounts.has(tid));
    el.classList.toggle('shown', tid === camp.account);
    el.setAttribute('aria-pressed', String(camp.accounts.has(tid)));
  });
  paintAccountNote();
  loadCampSession();
});

async function loadCampSession() {
  const box = $('#camp-session');
  try {
    const { sessions } = await api('/api/accounts/sessions');
    renderActiveSessions(sessions);
    // EVERY picked account's session, not just the shown one: with several
    // selected, the checklist has to answer for all of them — one logged out
    // takes its whole slice of the recipients down with it.
    camp.sessions = new Map((sessions || []).map(s => [s.id, s]));
    // Nothing picked reads as nothing picked. It used to fall back to the
    // default profile's session, which described an account no campaign runs
    // as — so the panel showed a status for something nobody had chosen.
    const row = camp.account
      ? sessions.find(s => s.id === camp.account) : null;
    camp.session = row || null;
    if (!row) {
      // With nothing picked there is no session to describe, and the card goes
      // rather than repeating the line already sitting under the picker two
      // inches above it. A missing session for an account that IS picked is a
      // real thing to say, so that one stays.
      box.hidden = !camp.account;
      box.innerHTML = camp.account
        ? '<span class="muted">No session for this account.</span>' : '';
      renderChecklist();
      return;
    }
    box.hidden = false;

    const expires = row.expires_at
      ? new Date(row.expires_at).toLocaleString() : '—';
    const status = row.active
      ? 'Active — open in the browser now'
      : (row.logged_in ? 'Logged in' : escapeHtml(row.detail || 'Logged out'));
    box.innerHTML = `
      <div class="field"><span class="k">Status</span>
        <span class="v"><span class="dot ${row.logged_in || row.active ? 'ok' : 'off'}"></span>
          ${status}</span></div>
      <div class="field"><span class="k">Login</span>
        <span class="v">${escapeHtml(row.login || '—')}</span></div>
      <div class="field"><span class="k">Expires</span>
        <span class="v">${escapeHtml(expires)}</span></div>
      <div class="field"><span class="k">Last checked</span>
        <span class="v">${row.checked_at ? new Date(row.checked_at).toLocaleTimeString() : '—'}</span></div>
      <div class="field"><span class="k">Profile</span>
        <span class="v mono">${escapeHtml(row.profile || '—')}</span></div>
      <div class="field"><span class="k">Locked</span>
        <span class="v">${row.locked ? 'Yes — survives Clear all' : 'No'}</span></div>`;
  } catch (err) {
    // An error IS worth a card, whether or not an account is picked — hiding it
    // would swallow the one line saying why the sessions could not be read.
    box.hidden = false;
    box.innerHTML = `<span class="muted">${escapeHtml(err.message)}</span>`;
  }
  renderChecklist();
}

/* The sessions open in a browser right now, above the Login step's picker.
 * One chip per open window — accounts open side by side, so there can be
 * several. Clicking a chip switches the step's picker to that account. */
function renderActiveSessions(sessions) {
  const strip = $('#camp-active');
  if (!strip) return;
  const active = (sessions || []).filter(s => s.active);
  strip.hidden = !active.length;
  if (!active.length) return;
  strip.innerHTML = '<span class="as-label">Open now</span>' + active.map(s => `
    <button class="as-chip" data-id="${s.id}" title="${escapeHtml(s.profile || '')}">
      <span class="dot ok"></span>${escapeHtml(s.label || s.login || ('#' + s.id))}
    </button>`).join('');
}

$('#camp-active').addEventListener('click', e => {
  const chip = e.target.closest('.as-chip');
  if (!chip) return;
  const id = Number(chip.dataset.id) || null;
  if (!id) return;
  // ADDS to the selection rather than replacing it — the strip is "these
  // windows are open", and clicking one is the operator saying "that one too",
  // which is exactly how a multi-account campaign gets assembled.
  camp.accounts.add(id);
  camp.account = id;
  // Through loadAccounts, so the tile for it comes up picked: the strip and the
  // picker are two views of one choice, and only one of them was clicked.
  loadAccounts().then(loadCampSession);
});

$('#camp-check').addEventListener('click', async () => {
  try {
    await api('/api/accounts/sessions/check', { method: 'POST' });
    loadCampSession();
  } catch (err) { toast(err.message, 'error'); }
});

$('#camp-login').addEventListener('click', async () => {
  // EVERY picked account, in one request. The endpoint already logs a list in
  // one at a time with a gap between them — a burst of simultaneous logins from
  // one IP is the clearest possible signal that the accounts belong together.
  const ids = [...camp.accounts];
  if (!ids.length) return toast('Pick the account(s) to log in first.', 'warn');
  const who = ids.length === 1
    ? 'this account' : `${ids.length} accounts, one at a time`;
  showInfo('#camp-login-info',
    `Logging in ${who}: filling email and password, ticking `
    + '<b>I am human</b>. If hCaptcha shows the image puzzle, '
    + '<b>solve it in the browser window</b> — this waits for you.', 'ok');
  try {
    const result = await api('/api/accounts/login', { method: 'POST', body: { ids } });
    const rows = result.results || [];
    const done = rows.filter(r => r.ok || r.skipped).length;
    // One line per account: with several going, "3 done" hides which of them
    // is the one still sitting on a captcha.
    showInfo('#camp-login-info', rows.map(r =>
      `${r.ok || r.skipped ? '✓' : '✗'} <b>${escapeHtml(campAccountName(r.id))}</b>`
      + ` — ${escapeHtml(r.detail || 'done')}`).join('<br>') || 'Done',
      done === rows.length ? 'ok' : 'bad');
    toast(`${done}/${rows.length} logged in`,
          done === rows.length ? 'ok' : 'warn');
    loadCampSession();
  } catch (err) {
    showInfo('#camp-login-info', escapeHtml(err.message), 'bad');
    toast(err.message, 'error');
  }
});

/* ── Uploads (shared) ────────────────────────────────────────── */

async function postFiles(url, files, field = 'file') {
  // FormData, not the JSON api() helper — a multipart body must not be
  // stringified and must not carry a JSON content-type.
  const body = new FormData();
  for (const file of files) body.append(field, file);
  const res = await fetch(url, { method: 'POST', body });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

function showInfo(id, message, level = 'ok') {
  const box = $(id);
  box.className = `upload-info ${level}`;
  box.innerHTML = message;
  box.hidden = false;
}

/* Wire a dropzone: click-to-choose already works via the <label for>, so this
 * only adds drag-and-drop and the visual over-state. */
function wireDrop(zoneId, inputId, handler, infoId = null) {
  const zone = $(zoneId);
  const input = $(inputId);
  // Usually the zone's own info line, by convention. Passed explicitly where
  // the zone is not a dropzone card — the drafts box takes a file too, and its
  // id says nothing about where the answer goes.
  const info = infoId || zoneId.replace('-drop', '-upload-info');

  for (const type of ['dragenter', 'dragover']) {
    zone.addEventListener(type, e => {
      e.preventDefault();
      zone.classList.add('over');
    });
  }
  for (const type of ['dragleave', 'drop']) {
    zone.addEventListener(type, e => {
      e.preventDefault();
      zone.classList.remove('over');
    });
  }
  zone.addEventListener('drop', e => {
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) run(files);
  });
  input.addEventListener('change', () => {
    const files = [...input.files];
    if (files.length) run(files);
    input.value = '';         // so re-choosing the same file fires again
  });

  async function run(files) {
    zone.classList.add('busy');
    try { await handler(files); }
    catch (err) { showInfo(info, escapeHtml(err.message), 'bad'); }
    finally { zone.classList.remove('busy'); }
  }
}

/* ── Step 2 · Recipients upload ──────────────────────────────── */

function normalizeUploadedMembers(rows) {
  return (rows || [])
    .map(row => ({
      username: String(row?.username || '').trim(),
      name: String(row?.name || '').trim(),
    }))
    .filter(row => row.username);
}

wireDrop('#users-drop', '#users-file', async files => {
  const result = await postFiles('/api/campaign/upload/users', [files[0]]);
  const members = normalizeUploadedMembers(result.members?.length
    ? result.members
    : result.usernames.map(u => ({ username: u, name: '' })));
  camp.targets = members;
  camp.uploaded = members.map(row => row.username);

  showInfo('#users-upload-info',
    `<b>${result.count}</b> member(s) read from <b>${escapeHtml(files[0].name)}</b>`
    + (result.column ? ` · column <b>${escapeHtml(result.column)}</b>` : '')
    + (result.skipped ? ` · <b>${result.skipped}</b> row(s) skipped` : ''),
    'ok');

  const rows = members.slice(0, 200);
  $('#users-upload-preview').innerHTML = `
    <div class="head"><span>Preview</span><div class="grow"></div>
      <span>showing ${rows.length} of ${result.count}</span></div>
    <div class="body">${rows.map((row, i) => {
      const username = String(row.username || '').trim();
      const name = String(row.name || '').trim();
      const label = name && name !== username ? name : (username || '(no name)');
      const handleLabel = name && name !== username && username ? `#${username}` : '';
      return `
      <div class="prow"><span class="n">${i + 1}</span>
        <span class="who-text">
          <span class="m-name">${escapeHtml(label)}</span>
          ${handleLabel
            ? `<a class="m-handle" target="_blank" rel="noopener"
                 href="https://www.meetup.com/members/${encodeURIComponent(username)}/">${escapeHtml(handleLabel)}</a>`
            : ''}
        </span></div>`;
    }).join('')}</div>`;
  $('#users-upload-preview').hidden = false;
  $('#users-import').hidden = false;
  renderChecklist();
});

$('#users-import').addEventListener('click', async () => {
  if (!camp.uploaded?.length) return;
  try {
    const result = await api('/api/campaign/import/users', {
      method: 'POST',
      body: {
        usernames: camp.uploaded,
        members: camp.targets.map(({ username, name }) => ({ username, name })),
      },
    });
    toast(`Stored ${result.added.length} new, ${result.skipped.length} already known`, 'ok');
  } catch (err) { toast(err.message, 'error'); }
});

/* ── Step 3 · Messages upload ────────────────────────────────── */

/* The drop target is the drafts box itself — `#drafts` rather than anything
 * inside it, because the editor is re-rendered on every tab switch and a
 * listener on the textarea would go with it. */
wireDrop('#drafts', '#msg-file', async files => {
  const result = await postFiles('/api/campaign/upload/messages', [files[0]]);
  // Load into the editor rather than saving: the tabs below ARE the preview,
  // and an upload that silently replaced saved drafts would be unrecoverable.
  // Marked unsaved for the same reason — nothing has been written yet.
  //
  // The strip is opened and the first one selected, unlike a plain load: what
  // an upload IS is a set of messages to look at, so folding them away would
  // hide the very thing the operator just asked for.
  msgs.showTabs = true;
  setDrafts(result.drafts, { dirty: true, select: 0 });
  showInfo('#msg-upload-info',
    `<b>${result.count}</b> draft(s) read from <b>${escapeHtml(files[0].name)}</b>`
    + ' — review above, then <b>Save drafts</b>.', 'ok');
}, '#msg-upload-info');

/* ── Step 4 · Media ──────────────────────────────────────────── */

wireDrop('#media-drop', '#media-file', async files => {
  const result = await postFiles('/api/campaign/media', files, 'files');
  showInfo('#media-upload-info',
    `Uploaded <b>${result.count}</b> image(s)`
    + (result.failed.length ? ` · ${result.failed.length} failed` : ''),
    result.failed.length ? 'bad' : 'ok');
  // Newly uploaded images are selected — you uploaded them to use them — and
  // the library opens so they are visible rather than ticked out of sight.
  for (const item of result.uploaded) camp.media.add(item.name);
  showMediaView('library');
});

/* Upload or library, never both — the same swap step 2 makes between a file and
 * the member store. Opening the library is what reads the bucket: until then
 * this step is the dropzone and nothing else. */
function showMediaView(which) {
  const library = which === 'library';
  $('#media-upload-card').hidden = library;
  $('#media-library-card').hidden = !library;
  // ONE Skip switch, moved to whichever head is on screen. A second checkbox
  // in the other head would be a second state to keep in step with the first,
  // and leaving it in the hidden card would put it out of reach.
  const skip = $('#skip-media').closest('.skip');
  const spacer = (library ? $('#media-library-card') : $('#media-upload-card'))
    .querySelector('.card-head .grow');
  if (skip && spacer) spacer.parentNode.insertBefore(skip, spacer);
  if (library) loadMedia();
}

$('#media-library').addEventListener('click', () => showMediaView('library'));
$('#media-upload-back').addEventListener('click', () => showMediaView('upload'));

async function loadMedia() {
  const grid = $('#media-grid');
  try {
    const { media } = await api('/api/campaign/media');
    $('#media-count').textContent = media.length
      ? `${camp.media.size} of ${media.length} selected` : '';
    if (!media.length) {
      grid.innerHTML = '<span class="muted">The media bucket is empty — '
                     + 'drop an image above and it is stored in MinIO.</span>';
      return;
    }
    grid.innerHTML = media.map(m => `
      <button class="media-tile ${camp.media.has(m.name) ? 'on' : ''}" data-media="${escapeHtml(m.name)}">
        <img src="/api/minio/media/${encodeURIComponent(m.name)}" alt="" loading="lazy">
        <span class="pick">✓</span>
        <span class="name">${escapeHtml(m.name)}</span>
      </button>`).join('');
  } catch (err) {
    grid.innerHTML = `<span class="muted">${escapeHtml(err.message)}</span>`;
  }
}

$('#media-reload').addEventListener('click', () => loadMedia());

$('#media-grid').addEventListener('click', e => {
  const tile = e.target.closest('[data-media]');
  if (!tile) return;
  const name = tile.dataset.media;
  // Multi-select: the sender picks one at random per recipient from whatever is
  // chosen, so every image sent being identical is itself avoidable.
  if (camp.media.has(name)) camp.media.delete(name); else camp.media.add(name);
  tile.classList.toggle('on', camp.media.has(name));
  const total = $$('.media-tile').length;
  $('#media-count').textContent = `${camp.media.size} of ${total} selected`;
  renderChecklist();
});

/* ── Skipping one of Messages / Media ────────────────────────── */

/* A send carries text, a picture, or both — never neither. So either step may
 * be skipped and the other becomes required, and skipping one takes the second
 * switch away rather than letting the operator arrive at an empty message and
 * be told about it by the send button.
 *
 * Skipping does not clear what the step holds: a skipped Media step with three
 * images still ticked keeps them, because unticking it should give them back
 * rather than make the operator pick them again. */
function applySkips() {
  const messages = $('#skip-messages');
  const media = $('#skip-media');
  camp.skip = messages.checked ? 'messages' : (media.checked ? 'media' : null);

  // The other switch, off the table while one is used.
  messages.disabled = camp.skip === 'media';
  media.disabled = camp.skip === 'messages';
  messages.closest('.skip').classList.toggle('off', messages.disabled);
  media.closest('.skip').classList.toggle('off', media.disabled);
  renderChecklist();
}

$('#skip-messages').addEventListener('change', applySkips);
$('#skip-media').addEventListener('change', applySkips);

/* ── Step 5 · Status ─────────────────────────────────────────── */

/* The Login line of the checklist, over every picked account.
 *
 * Names the ones that are NOT ready rather than counting the ones that are: "2
 * of 3 logged in" leaves the operator to work out which, and the answer decides
 * whether they press Log in or go and open a window. */
function loginStep() {
  const picked = [...camp.accounts];
  const rows = picked.map(id => camp.sessions.get(id));
  const out = picked.filter((id, i) => !rows[i]?.logged_in);
  const label = picked.length > 1 ? 'Accounts logged in' : 'Account logged in';

  if (!picked.length) {
    return { step: 'login', ok: false, label, detail: 'pick an account' };
  }
  if (!camp.sessions.size) {
    return { step: 'login', ok: false, label, detail: 'no session read yet' };
  }
  if (out.length) {
    const names = out.map(id => campAccountName(id)).join(', ');
    return { step: 'login', ok: false, label, detail: `logged out: ${names}` };
  }
  return {
    step: 'login', ok: true, label,
    detail: picked.length > 1
      ? `${picked.length} accounts ready`
      : (camp.sessions.get(picked[0])?.login || `#${picked[0]}`),
  };
}

/* One account's name, off whatever the panel already knows about it. */
function campAccountName(id) {
  const tile = $(`#camp-accounts .acct[data-account="${id}"] .acct-name`);
  if (tile) return tile.textContent;
  return camp.sessions.get(id)?.label || camp.sessions.get(id)?.login || `#${id}`;
}

function renderChecklist() {
  const items = [
    // Every picked account has to be logged in, not just the one on screen: a
    // logged-out one takes its whole slice of the recipients down with it, and
    // ticking on the strength of the others would hide that until the run.
    loginStep(),
    { step: 'users', ok: camp.targets.length > 0, label: 'Recipients selected',
      detail: camp.targets.length ? `${camp.targets.length} recipient(s)`
                                  : 'upload a file or pick from Database' },
    // What the OPERATOR settled on, not what the bucket happens to hold: a
    // message written in the box, or a stored draft picked off the strip.
    // And saved — a run sends what is in MinIO, so unsaved text on screen is
    // not the message that would go, and ticking it would say it was.
    { step: 'messages',
      ok: camp.skip === 'messages' || (!!camp.chosen && !msgs.dirty),
      label: 'Message ready',
      detail: camp.skip === 'messages' ? 'skipped — pictures only'
        : !camp.chosen
        ? (msgs.list.length ? 'write one, or pick a stored draft'
                            : 'write a message')
        : (msgs.dirty ? `${camp.chosen.label} — press Save drafts`
           : `${camp.chosen.label}${camp.drafts > 1
               ? ` · ${camp.drafts} active, sent in rotation` : ''}`) },
    // Optional only while the message is doing the work. Skip Messages and the
    // picture IS the message, so a run with none has nothing to send.
    { step: 'media',
      optional: camp.skip !== 'messages',
      ok: camp.skip === 'messages' ? camp.media.size > 0 : true,
      label: camp.skip === 'messages' ? 'Media' : 'Media (optional)',
      detail: camp.skip === 'media' ? 'skipped — text only'
        : camp.media.size ? `${camp.media.size} image(s) selected`
        : (camp.skip === 'messages' ? 'pick at least one image' : 'text only') },
  ];

  $('#camp-checklist').innerHTML = items.map(i => `
    <li class="${i.ok ? 'ok' : 'no'}">
      <span class="mark">${i.ok ? '✓' : '!'}</span>
      <span>${escapeHtml(i.label)}</span>
      <span class="detail">${escapeHtml(i.detail)}</span>
    </li>`).join('');

  // Mirror the state onto the rail, so progress is visible without opening
  // step 5 to find out. An optional step never shows done — a tick there would
  // read as "you have finished this", which is not a thing it can say.
  for (const item of items) {
    const button = $(`#camp-steps button[data-step="${item.step}"]`);
    if (button) button.classList.toggle('done', item.ok && !item.optional);
  }
  // Readiness is over the steps that are actually required, which now depends
  // on which one was skipped: skip Messages and Media stops being optional.
  const mandatory = items.filter(i => !i.optional);
  const missing = mandatory.filter(i => !i.ok);
  const ready = missing.length === 0;
  $('#camp-steps button[data-step="status"]').classList.toggle('done', ready);
  $('#camp-start').disabled = !ready;
  // A disabled button that does not say why is a dead end, so it names what is
  // outstanding — the same words the checklist above uses, so the line and the
  // list are answering with one voice.
  $('#camp-start-note').textContent = ready
    ? `${camp.targets.length} recipient(s) · there is no unsend on Meetup`
    : `Waiting on: ${missing.map(i => i.label.replace(' (optional)', '')).join(', ')}`;
}

$('#camp-refresh').addEventListener('click', () => { loadCampSession(); loadCampaign(); });

/* Back to a blank box. Whatever was being written is kept — stashing commits it
 * to the list if it has text — so this never costs the operator a message. */
$('#draft-add').addEventListener('click', () => {
  stashDraftEdits();
  msgs.active = -1;
  msgs.scratch = newScratch();
  renderDrafts();
  const area = $('#drafts .d-text');
  if (area) area.focus();
});

$('#draft-save').addEventListener('click', async () => {
  stashDraftEdits();
  // An empty draft is dropped rather than stored: the store's own rule is that
  // a draft IS its text, and a blank tab is a tab the operator opened and did
  // not use. Names and the disabled flag ride along.
  const drafts = msgs.list
    .filter(d => d.text.trim())
    .map(d => ({ label: d.label, text: d.text, disabled: !!d.disabled }));
  try {
    const saved = await api('/api/campaign/drafts',
                            { method: 'POST', body: { drafts } });
    // Re-seat on what came BACK, so the strip shows what is actually stored —
    // a blank tab that was dropped should disappear, not linger as saved. The
    // draft being looked at stays on screen: a save is not a reason to be put
    // back at a blank box.
    const stored = saved.drafts || drafts;
    const wasOn = msgs.active >= 0 ? msgs.list[msgs.active] : null;
    setDrafts(stored, {
      select: wasOn ? stored.findIndex(d => d.label === wasOn.label) : -1,
    });
    const off = drafts.filter(d => d.disabled).length;
    toast(`Saved ${drafts.length} draft(s)${off ? ` · ${off} disabled` : ''}`, 'ok');
  } catch (err) { toast(err.message, 'error'); }
});

/* THE send. One button, and it only exists once every mandatory line on the
 * checklist is ticked — so what is left here is the confirmation, which the
 * dry run used to soften and no longer can. */
async function startOutreach() {
  const body = {
    // Every picked account. The run deals the recipients between them and gives
    // each its own browser window and its own tab in the live panel.
    account_ids: [...camp.accounts],
    account_id: camp.account,
    // Always an explicit list now: recipients are the file you uploaded or the
    // members you ticked in the database, and the filter that used to let a run
    // pick its own is gone from the panel.
    usernames: camp.uploaded || [],
    // The per-run cap in Settings, which is the only limit left to honour.
    limit: 0,
    // Send the whole selection: the backend picks one per recipient at random.
    // Skipping Media sends none, whatever is still ticked in the library —
    // skipped means skipped, not "unless you left something selected".
    images: camp.skip === 'media' ? [] : [...camp.media],
    image: camp.skip === 'media' ? null : ([...camp.media][0] || null),
    // Skipping Messages sends the picture alone. Said explicitly rather than
    // by sending no drafts, because "no drafts" is also what an empty bucket
    // looks like, and those two must not send the same thing.
    no_text: camp.skip === 'messages',
    dry_run: false,
  };
  // Spelled out, because this is now the only thing standing between the press
  // and other people's inboxes: who, with what, and that it cannot be undone.
  const carrying = camp.skip === 'messages'
    ? `${body.images.length} image(s), no text`
    : `${camp.chosen ? camp.chosen.label : 'the saved drafts'}`
      + (body.images.length ? ` · ${body.images.length} image(s)` : '');
  if (!confirm(`Start the campaign?\n\n`
      + `${camp.targets.length} recipient(s) · ${carrying}\n\n`
      + 'There is no unsend on Meetup.')) return;
  // Open the board BEFORE the request, on the recipients already known, so the
  // press lands on a table of Pending rows rather than on nothing while the
  // first poll is in flight.
  // One lane per picked account, dealt the same way the run will deal them, so
  // the tabs are already right before the first poll answers.
  const seeded = camp.targets.map(t => ({
    username: t.username || t, name: t.name || '', status: 'pending',
  }));
  const ids = body.account_ids.length ? body.account_ids : [null];
  openCampLive(ids.map((id, i) => ({
    id, label: campAccountName(id),
    members: seeded.filter((_, n) => n % ids.length === i),
  })), 'starting…');
  try {
    const { task } = await api('/api/campaign/outreach', { method: 'POST', body });
    watchTask(task, {
      onTick: status => applyCampLive(status),
      onDone: result => applyCampLive(null, result),
    });
  } catch (err) {
    setCampLiveFoot(escapeHtml(err.message), 'bad');
    toast(err.message, 'error');
  }
}

$('#camp-start').addEventListener('click', startOutreach);

/* ── Campaign live ───────────────────────────────────────────── */

/* The board the run fills in: one row per recipient — who, which draft, which
 * picture, what has happened to it.
 *
 * The rows are the RUN'S, polled off ctl.meta rather than assembled here: the
 * pairing of recipient to draft to image is decided by outreach() before it
 * sends anything, and a second guess at it on this side could only ever
 * disagree with what actually went out. Before the first poll answers, the
 * recipients are shown as Pending so the panel is never blank. */
const live = { note: '', done: false, tabs: 0, state: '',
               // One lane per account: {id, label, members}. A single-account
               // run is one lane, so the table below has one shape to draw
               // whether the campaign is running from one window or four.
               lanes: [], on: 0 };

function openCampLive(lanes, note = '') {
  live.lanes = lanes || [];
  live.on = 0;
  live.note = note;
  live.done = false;
  live.tabs = 0;
  live.state = 'running';
  renderCampLive();
  const box = $('#camp-live-box');
  if (!box.open) box.showModal();
}

/* The rows of the tab being looked at. */
function liveRows() {
  return (live.lanes[live.on] || {}).members || [];
}

/* Fold the run's own rows in. Called on every poll while it works, and once more
 * with the finished result — a run that ends between two polls would otherwise
 * leave its last recipient showing Processing for good. */
function applyCampLive(status, result) {
  const meta = (status && status.run && status.run.meta) || {};
  // The run's own lanes when it has published them. Falling back to the flat
  // member list keeps a single-account run — and the pre-request table drawn
  // before the first poll — on the same one-lane shape.
  const lanes = meta.accounts || (result && result.accounts);
  const flat = meta.members || (result && result.members) || [];
  if (lanes && lanes.length) live.lanes = lanes;
  else if (flat.length) live.lanes = [{ ...(live.lanes[0] || {}), members: flat }];
  if (live.on >= live.lanes.length) live.on = 0;
  if (meta.tabs) live.tabs = meta.tabs;
  if (result) {
    live.done = true;
    live.state = 'done';
    live.note = result.detail || 'finished';
  } else if (status) {
    // The RUN's own status — running | paused | stopping — not the task's. It is
    // what the two controls are drawn from, so it has to be the same word the
    // backend is acting on.
    live.state = (status.run && status.run.status) || status.state || '';
    live.note = (status.run && status.run.detail) || status.state || '';
  }
  renderCampLive();
}

/* Pause / Resume and End, drawn from the run's own status.
 *
 * End is disabled until the run is paused. Ending a campaign cannot be undone,
 * and the press is worth taking against a table that is holding still rather
 * than one with rows landing under the cursor — so the order is: pause, look,
 * then continue or end. */
function paintCampControls() {
  const pause = $('#camp-pause');
  const end = $('#camp-end');
  if (!pause || !end) return;

  const state = live.state;
  const over = live.done || state === 'done' || state === 'error' || !state;
  // Nothing to pause and nothing to end once it is over — the buttons go rather
  // than sitting there greyed, because "finished" is not a state you act on.
  pause.hidden = over;
  end.hidden = over;
  if (over) return;

  const paused = state === 'paused';
  const stopping = state === 'stopping';

  pause.querySelector('.lbl').textContent = paused ? 'Continue' : 'Pause';
  pause.querySelector('use').setAttribute('href', paused ? '#i-play' : '#i-pause');
  pause.classList.toggle('primary', paused);
  pause.disabled = stopping;

  end.disabled = !paused || stopping;
  end.title = stopping ? 'Stopping — finishing the recipients in flight'
    : paused ? 'End the campaign here — the recipients not yet reached are dropped'
    : 'Pause the campaign first';
}

/* Pause and continue. The run keeps its browser and its queue either way: a
 * paused campaign is stopped mid-list, not let go of. */
$('#camp-pause').addEventListener('click', async () => {
  const going = live.state !== 'paused';
  const button = $('#camp-pause');
  button.disabled = true;
  try {
    const run = await api(`/api/automation/runs/outreach/${going ? 'pause' : 'resume'}`,
                          { method: 'POST' });
    live.state = run.status || (going ? 'paused' : 'running');
    toast(going ? 'Campaign paused — continue or end it' : 'Campaign resumed', 'ok');
  } catch (err) {
    toast(err.message, 'error');
  }
  renderCampLive();
});

$('#camp-end').addEventListener('click', async () => {
  // Across EVERY account, not the tab on screen: End stops the whole campaign,
  // so the number it warns about has to be the whole campaign's.
  const left = live.lanes.flatMap(l => l.members || [])
    .filter(r => !['sent', 'typed', 'failed'].includes(r.status));
  // Spelled out, because this is the half of the list that never gets messaged
  // and there is no way back to it except starting another campaign.
  if (!confirm(`End this campaign?\n\n`
      + `${left.length} recipient(s) have not been messaged and will be dropped.\n`
      + 'The ones already sent stay sent — there is no unsend.')) return;
  try {
    const run = await api('/api/automation/runs/outreach/stop', { method: 'POST' });
    live.state = run.status || 'stopping';
    live.note = 'Ending — finishing the recipients already in flight';
    toast('Ending the campaign', 'warn');
  } catch (err) {
    toast(err.message, 'error');
  }
  renderCampLive();
});

/* How many of each status a set of rows is in. */
function tallyOf(rows) {
  const out = {};
  for (const row of rows || []) {
    const key = row.status || 'pending';
    out[key] = (out[key] || 0) + 1;
  }
  return out;
}

/* The tab strip — one per account, each with its own progress on it.
 *
 * The count belongs ON the tab. With three accounts going at once the operator's
 * question is "which one is behind", and a strip of bare names makes them click
 * through all three to find out. */
function paintLiveTabs() {
  const strip = $('#camp-live-tabs');
  if (!strip) return;
  // A strip of one is a heading dressed as a choice.
  strip.hidden = live.lanes.length < 2;
  if (strip.hidden) return;

  strip.innerHTML = live.lanes.map((lane, i) => {
    const rows = lane.members || [];
    const tally = tallyOf(rows);
    const done = (tally.sent || 0) + (tally.typed || 0) + (tally.failed || 0);
    return `
      <button class="live-tab ${i === live.on ? 'on' : ''}" data-lane="${i}"
              aria-pressed="${i === live.on}">
        <span class="dot ${tally.processing ? 'ok' : (done >= rows.length && rows.length ? 'off' : 'warn')}"></span>
        <span class="lt-name">${escapeHtml(lane.label || `Account ${i + 1}`)}</span>
        <span class="lt-count">${done}/${rows.length}</span>
        ${tally.failed ? `<span class="lt-bad">${tally.failed}</span>` : ''}
      </button>`;
  }).join('');
}

$('#camp-live-tabs').addEventListener('click', e => {
  const tab = e.target.closest('[data-lane]');
  if (!tab) return;
  live.on = Number(tab.dataset.lane) || 0;
  renderCampLive();
});

function renderCampLive() {
  const box = $('#camp-live-box');
  if (!box) return;
  paintLiveTabs();
  const rows = liveRows();
  const tally = tallyOf(rows);
  // Over the WHOLE campaign, not the tab being looked at: the header answers
  // "how big is this", and with three accounts the answer is all three.
  const total = live.lanes.reduce((n, l) => n + (l.members || []).length, 0);

  // The width is worth saying: with several tabs in flight the table shows more
  // than one row Processing at once, and that reads as a bug unless the header
  // has already said how many are meant to be.
  $('#camp-live-sub').textContent = total
    ? `${total} recipient(s)`
      + (live.lanes.length > 1 ? ` · ${live.lanes.length} accounts` : '')
      + (live.tabs > 1 ? ` · ${live.tabs} at a time each` : '')
    : 'no recipients';
  // The counts in the run's own vocabulary, and only the ones that exist — a
  // row of zeroes is four words the operator has to read past to find the one
  // number that is moving.
  //
  // Over the WHOLE campaign, like the count beside it. Per-tab totals here
  // would disagree with "13 recipients · 3 accounts" two words to their left —
  // and each account's own progress is already on its tab.
  const whole = tallyOf(live.lanes.flatMap(l => l.members || []));
  $('#camp-live-tally').innerHTML = Object.keys(STATUS_LOOK)
    .filter(key => whole[key])
    .map(key => `<span class="st ${key}">`
      + `<svg class="ic" viewBox="0 0 24 24"><use href="${STATUS_LOOK[key].icon}"/></svg>`
      + `${whole[key]} ${escapeHtml(STATUS_LOOK[key].label)}</span>`).join('');

  // The operator's scroll position has to survive a redraw that happens every
  // 1.2 seconds for the length of the run.
  const wrap = $('#camp-live-wrap');
  const keep = wrap ? wrap.scrollTop : 0;

  $('#camp-live-table tbody').innerHTML = rows.length ? rows.map((row, i) => {
    const handle = row.username || '';
    const status = row.status || 'pending';
    return `
      <tr class="s-${escapeHtml(status)}">
        <td class="c-n">${i + 1}</td>
        <td>
          <div class="c-who">
            <span class="avatar"><svg class="ic" viewBox="0 0 24 24"><use href="#i-user"/></svg></span>
            <span class="who-text">
              <span class="m-name">${escapeHtml(row.name || handle || '(no name)')}</span>
              ${handle
                ? `<a class="m-handle" href="https://www.meetup.com/members/${encodeURIComponent(handle)}/"
                     target="_blank" rel="noopener">#${escapeHtml(handle)}</a>`
                : '<span class="m-handle none">no profile link</span>'}
            </span>
          </div></td>
        <!-- The draft's NAME, with its words on hover: a column of message
             bodies is unreadable, and "sms2" is the campaign at a glance. -->
        <td class="c-sms" title="${escapeHtml(row.text || '')}">${row.sms
          ? `<span class="tag">${escapeHtml(row.sms)}</span>`
          : '<span class="muted">—</span>'}</td>
        <td class="c-media" title="${escapeHtml(row.media || '')}">${row.media
          ? `<span class="tag media"><svg class="ic" viewBox="0 0 24 24"><use href="#i-image"/></svg>`
            + `${escapeHtml(truncate(row.media, 22))}</span>`
          : '<span class="muted">—</span>'}</td>
        <td class="c-status">${statusCell(status, row)}</td>
      </tr>`;
  }).join('') : `<tr><td colspan="5" class="muted pad">${live.lanes.length > 1
    ? 'No recipients on this account.' : 'Nothing running.'}</td></tr>`;

  if (wrap) wrap.scrollTop = keep;
  // A paused run says so in its own words rather than leaving the last
  // "Messaging @someone" on screen, which reads as still going.
  const note = live.state === 'paused'
    ? `Paused — ${(whole.pending || 0)} recipient(s) not yet reached. `
      + 'Continue, or end the campaign.'
    : live.state === 'stopping'
      ? 'Ending — the recipients already in flight are finishing.'
      : live.note;
  setCampLiveFoot(escapeHtml(note),
                  live.done ? 'ok' : (live.state === 'stopping' ? 'bad' : ''));
  paintCampControls();
}

function setCampLiveFoot(html, tone = '') {
  const foot = $('#camp-live-foot');
  if (!foot) return;
  foot.innerHTML = html || '';
  foot.className = `live-foot ${tone || 'muted'}`;
}

/* Re-openable: closing the board must not end the watch, and a run started
 * before the panel was closed is still the run this shows. */
$('#camp-live').addEventListener('click', () => {
  renderCampLive();
  const box = $('#camp-live-box');
  if (!box.open) box.showModal();
});

$('#camp-live-close').addEventListener('click', () => $('#camp-live-box').close());

/* ── Settings ────────────────────────────────────────────────── */

/* The one setting with a control of its own — see #set-clicksend. Left out of
 * the generated list so there is one switch for it rather than two that have to
 * be kept saying the same thing. */
const OWN_CONTROL = new Set(['click_send']);

async function loadSettings() {
  const { settings, schema } = await api('/api/settings');
  paintClickSend(settings.click_send);
  $('#settings-form').innerHTML = schema
    .filter(row => !OWN_CONTROL.has(row.key))
    .map(row => row.type === 'list' ? listSetting(row, settings[row.key])
              : row.type === 'text' ? textSetting(row, settings[row.key]) : `
    <label class="setting">
      <span class="k">${escapeHtml(row.key)}</span>
      ${row.type === 'bool'
        ? `<input type="checkbox" data-key="${row.key}" ${settings[row.key] ? 'checked' : ''}>`
        : `<input type="number" step="any" data-key="${row.key}"
                  min="${row.min ?? ''}" max="${row.max ?? ''}" value="${settings[row.key]}">`}
      <span class="help">${escapeHtml(row.help)}</span>
    </label>`).join('');
  $$('#settings-form .setting.list').forEach(box => paintList(box, listValues(box)));
  paintLinkPreview();
}

/* A URL needs the width a 120px number box does not have, and its placeholders
 * need to be readable at the moment they are being typed rather than in a help
 * line somewhere else — so a text setting gets its own row shape. */
function textSetting(row, value) {
  const tokens = (row.tokens || [])
    .map(t => `<button type="button" class="tok" data-tok="${escapeHtml(t)}"
                       data-for="${escapeHtml(row.key)}">${escapeHtml(t)}</button>`)
    .join('');
  return `
    <div class="setting wide">
      <span class="k">${escapeHtml(row.key)}</span>
      <input type="text" spellcheck="false" data-key="${escapeHtml(row.key)}"
             value="${escapeHtml(String(value ?? ''))}"
             placeholder="${escapeHtml(String(row.default ?? ''))}">
      <span class="help">${escapeHtml(row.help)}</span>
      ${tokens ? `<div class="toks">${tokens}<span class="ex" id="link-preview"></span></div>` : ''}
    </div>`;
}

/* ── A list setting ──────────────────────────────────────────────
 *
 * One row per entry, each editable in place, each with an X — rather than the
 * ten cities as one 60-character comma-separated line, where changing the fourth
 * one means counting commas and a stray one splits a city in half.
 *
 * STORED as that same comma-separated string: a hidden input carries the real
 * value, the rows are kept in sync with it, and Save collects `input[data-key]`
 * and finds exactly the shape it finds for every other setting. Nothing on the
 * read side — settings.cities, the scrape that picks one — knows this screen
 * exists. */
const LIST_NOUNS = {
  search_cities: { one: 'city', many: 'cities',
                   add: 'Add a city — or paste several, comma-separated',
                   empty: 'No cities — a scrape searches from wherever the '
                        + 'session already is.' },
};

function listSetting(row, value) {
  const noun = LIST_NOUNS[row.key] || {};
  return `
    <div class="setting wide list" data-list="${escapeHtml(row.key)}">
      <span class="k">${escapeHtml(row.key)}</span>
      <input type="hidden" data-key="${escapeHtml(row.key)}"
             value="${escapeHtml(String(value ?? ''))}">
      <div class="rows"></div>
      <div class="add">
        <input type="text" class="add-in" spellcheck="false"
               placeholder="${escapeHtml(noun.add || 'Add an entry')}">
        <button type="button" class="add-go">Add</button>
        <span class="list-n"></span>
      </div>
      <span class="help">${escapeHtml(row.help)}</span>
    </div>`;
}

/* The stored value, split. The hidden input is the source of truth — every edit
 * writes back to it, so this is current even mid-typing. */
function listValues(box) {
  return (box.querySelector('input[data-key]').value || '')
    .split(',').map(s => s.trim()).filter(Boolean);
}

function paintList(box, values) {
  const noun = LIST_NOUNS[box.dataset.list] || {};
  box.querySelector('.rows').innerHTML = values.length
    ? values.map(value => `
        <div class="list-row">
          <input type="text" class="list-in" spellcheck="false"
                 value="${escapeHtml(value)}">
          <button type="button" class="list-x" title="Remove"
                  aria-label="Remove ${escapeHtml(value)}">×</button>
        </div>`).join('')
    : `<p class="list-none">${escapeHtml(noun.empty || 'Empty.')}</p>`;
  syncList(box);
}

/* Rows → the hidden value. Blank rows simply stop counting rather than being
 * deleted from under the cursor: an operator clearing a box to retype it has an
 * empty box for a moment, and a row that vanished when they did would be this
 * control's worst habit. They disappear on Save, when the panel reloads. */
function syncList(box) {
  const values = [...box.querySelectorAll('.list-in')]
    .map(input => input.value.trim()).filter(Boolean);
  box.querySelector('input[data-key]').value = values.join(', ');
  const noun = LIST_NOUNS[box.dataset.list] || {};
  const count = box.querySelector('.list-n');
  if (count) {
    count.textContent = values.length
      ? `${values.length} ${values.length === 1 ? (noun.one || 'entry')
                                               : (noun.many || 'entries')}`
      : '';
  }
}

/* Adding. A pasted "Tokyo, Delhi, Osaka" becomes three rows rather than one row
 * with commas in it — commas are the separator, so a single entry can never
 * contain one and splitting is the only reading that keeps the value honest. */
function addToList(box) {
  const input = box.querySelector('.add-in');
  const parts = (input.value || '').split(',').map(s => s.trim()).filter(Boolean);
  const values = listValues(box);
  const seen = new Set(values.map(v => v.toLowerCase()));
  let dupe = '';
  parts.forEach(part => {
    // Saving would drop it anyway — settings.cities dedupes on read — so a
    // second Tokyo is refused HERE, where the operator can see why.
    if (seen.has(part.toLowerCase())) { dupe = dupe || part; return; }
    seen.add(part.toLowerCase());
    values.push(part);
  });
  paintList(box, values);
  input.value = '';
  input.focus();
  if (dupe) flashDupe(box, dupe);
}

function flashDupe(box, value) {
  const row = [...box.querySelectorAll('.list-row')]
    .find(r => r.querySelector('.list-in').value.trim().toLowerCase()
               === value.toLowerCase());
  if (!row) return;
  row.classList.add('dupe');
  setTimeout(() => row.classList.remove('dupe'), 900);
  toast(`${value} is already in the list`, 'warn');
}

/* What the saved line becomes for one real recipient. The template is only ever
 * seen filled in during a run, which is exactly when a typo in it is expensive,
 * so it is shown filled in here instead. Mirrors meetup.chat_link — including
 * the append-when-no-placeholder rule, since a pasted base URL is the shape most
 * likely to be saved. */
function paintLinkPreview() {
  const box = $('#link-preview');
  const input = $('#settings-form input[data-key="chat_link"]');
  if (!box || !input) return;
  const line = (input.value || input.placeholder || '').trim();
  const id = '212235799', name = 'Nick w';
  // "Nick w" → "Nick+w". Form encoding, not encodeURIComponent's %20 — see
  // meetup.chat_link, and the example is a two-word name for that reason.
  const asName = encodeURIComponent(name).replace(/%20/g, '+');
  let out = line
    .replace(/\{id\}/g, id).replace(/\{username\}/g, id)
    .replace(/\{handle\}/g, id).replace(/\{name\}/g, asName);
  if (line && out === line) {
    out = line + (/[=\/?&]$/.test(line) ? '' : '/') + id;
  }
  box.textContent = out ? `→ ${out}` : '';
}

/* Typing in the link, and clicking a placeholder chip to insert one. Delegated,
 * because the form is rewritten on every load. */
$('#settings-form').addEventListener('input', event => {
  if (event.target.dataset.key === 'chat_link') paintLinkPreview();
  if (event.target.classList.contains('list-in')) {
    syncList(event.target.closest('.setting.list'));
  }
});

/* Enter adds, from the add box. Pressing it and watching nothing happen is the
 * first thing anyone does with a control shaped like this one. */
$('#settings-form').addEventListener('keydown', event => {
  if (event.key !== 'Enter' || !event.target.classList.contains('add-in')) return;
  event.preventDefault();
  addToList(event.target.closest('.setting.list'));
});

$('#settings-form').addEventListener('click', event => {
  const box = event.target.closest('.setting.list');
  if (box) {
    if (event.target.closest('.add-go')) return addToList(box);
    const gone = event.target.closest('.list-x');
    if (gone) {
      gone.closest('.list-row').remove();
      syncList(box);
      // The last row leaving has to leave the empty note behind, and the note is
      // painted rather than hidden — so repaint from what is left.
      if (!box.querySelector('.list-row')) paintList(box, []);
      return;
    }
  }

  const chip = event.target.closest('.tok');
  if (!chip) return;
  const input = $(`#settings-form input[data-key="${chip.dataset.for}"]`);
  if (!input) return;
  // At the caret, not appended — the id belongs wherever the operator has their
  // cursor in the URL, which is rarely the end of it.
  const at = input.selectionStart ?? input.value.length;
  const to = input.selectionEnd ?? at;
  input.value = input.value.slice(0, at) + chip.dataset.tok + input.value.slice(to);
  input.focus();
  input.setSelectionRange(at + chip.dataset.tok.length, at + chip.dataset.tok.length);
  paintLinkPreview();
});

function paintClickSend(on) {
  const button = $('#set-clicksend');
  if (!button) return;
  button.classList.toggle('on', !!on);
  button.setAttribute('aria-pressed', String(!!on));
  button.querySelector('.lbl').textContent = on ? 'On' : 'Off';
}

/* Saved on the press, not on Save. The rest of this panel is numbers nobody
 * changes mid-run; this one decides whether the next campaign puts words in
 * other people's inboxes, and it has to be true the moment it looks true. */
$('#set-clicksend').addEventListener('click', async () => {
  const button = $('#set-clicksend');
  const next = button.getAttribute('aria-pressed') !== 'true';
  paintClickSend(next);                    // answer the press at once
  try {
    const { settings } = await api('/api/settings',
                                   { method: 'POST', body: { click_send: next } });
    // Repaint from what was STORED: a value the backend refused must not leave
    // a button claiming it took.
    paintClickSend(settings.click_send);
    toast(settings.click_send
      ? 'Click Send is on — runs will send'
      : 'Click Send is off — messages are typed and left for you', 'ok');
  } catch (err) {
    paintClickSend(!next);
    toast(err.message, 'error');
  }
});

$('#set-save').addEventListener('click', async () => {
  const changes = {};
  $$('#settings-form input[data-key]').forEach(input => {
    changes[input.dataset.key] = input.type === 'checkbox' ? input.checked : input.value;
  });
  try {
    await api('/api/settings', { method: 'POST', body: changes });
    toast('Settings saved', 'ok');
    loadSettings();
  } catch (err) { toast(err.message, 'error'); }
});

$('#set-reset').addEventListener('click', async () => {
  if (!confirm('Reset every setting to its default?')) return;
  await api('/api/settings/reset', { method: 'POST' });
  loadSettings();
});

/* ── Status pills + activity feed ────────────────────────────── */

// A pill holds a status dot as well as its label, so only the label span is
// written — setting textContent on the pill itself would delete the dot.
function setPill(id, label, state) {
  const pill = $(id);
  pill.querySelector('span').textContent = label;
  pill.className = `pill ${state}`;
}

async function pollStatus() {
  try {
    const [status, minio] = await Promise.all([
      api('/api/automation/status'), api('/api/minio/health'),
    ]);
    const browser = status.browser;
    // With accounts open side by side the pill counts WINDOWS — one open window
    // showing three tabs and three accounts open are different situations, and
    // a page count alone reads the same for both.
    const windows = browser.count ?? (browser.open ? 1 : 0);
    setPill('#pill-browser',
            browser.open
              ? (windows > 1 ? `browser: ${windows} windows`
                             : `browser: ${browser.pages} page(s)`)
              : 'browser: closed',
            browser.open ? 'ok' : '');
    setPill('#pill-minio', `minio: ${minio.up ? 'up' : 'down'}`,
            minio.up ? 'ok' : 'bad');
    // The browser can be closed from outside the app — by the operator, or by a
    // crash — and the Scraping panel must not keep offering buttons that would
    // then run against nothing. Only while that panel is on screen: nobody
    // needs a sessions read every five seconds to look at Settings.
    if (panelIsOn('scraping')) refreshScrapeGate();
  } catch { /* the server is restarting — the next tick will pick it up */ }
  setTimeout(pollStatus, 5000);
}

function openFeed() {
  // SSE rather than a websocket: the stream is one-directional and EventSource
  // reconnects on its own when the server restarts, which during development is
  // constantly.
  const source = new EventSource('/api/data/activity/stream');
  source.onmessage = event => {
    const list = $('#feed');
    list.prepend(feedRow(JSON.parse(event.data)));
    while (list.children.length > 300) list.lastChild.remove();
  };
}

$('#feed-clear').addEventListener('click', async () => {
  await api('/api/data/activity', { method: 'DELETE' });
  $('#feed').innerHTML = '';
});

/* ── Showing and hiding the feed ─────────────────────────────── */

const FEED_KEY = 'meetup-bot.feed-open';

function setFeed(open) {
  document.body.classList.toggle('no-feed', !open);
  const toggle = $('#feed-toggle');
  toggle.classList.toggle('on', open);
  toggle.setAttribute('aria-pressed', String(open));
  // Remembered, because an operator who closed it wants it closed next time
  // too. Guarded: localStorage throws outright in some privacy modes, and a
  // layout preference is not worth breaking boot over.
  try { localStorage.setItem(FEED_KEY, open ? '1' : '0'); } catch { /* not fatal */ }
}

$('#feed-toggle').addEventListener('click',
  () => setFeed(document.body.classList.contains('no-feed')));
$('#feed-close').addEventListener('click', () => setFeed(false));

function restoreFeed() {
  let stored = null;
  try { stored = localStorage.getItem(FEED_KEY); } catch { /* not fatal */ }
  setFeed(stored !== '0');          // open unless it was explicitly closed
}

$('#pill-minio').addEventListener('click', async () => {
  const { logs } = await api('/api/minio/logs');
  $('#minio-log-body').textContent = logs.map(l => `${l.at.slice(11, 19)}  ${l.level.padEnd(7)} ${l.message}`).join('\n');
  $('#minio-log').showModal();
});
$('#minio-close').addEventListener('click', () => $('#minio-log').close());

/* ── Boot ────────────────────────────────────────────────────── */

restoreFeed();
// Accounts first even though Campaign is the panel on screen: it is what fills
// the account dropdowns, including the wizard's own.
loadAccounts().then(loadCampaign);
pollStatus();
openFeed();          // the SSE stream runs whether or not the panel is visible

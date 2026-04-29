// Music Agent v3 — frontend
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  playlists: [],
  currentPlaylist: null,
  lastRecs: [],
  selected: new Set(),
  rejectedIds: new Set(),
  lastParams: null,
};

// ---------------- API helpers ----------------
async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" } };
  if (opts.method) init.method = opts.method;
  if (opts.body) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg, ms = 2200) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}

// ---------------- Init ----------------
async function init() {
  await loadStatus();
  await loadPlaylists();
  await loadChat();
  bindUI();
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    if (!s.ready) {
      if (s.init_error) toast("Init error: " + s.init_error, 3500);
      else toast("Add your Spotify keys in Settings");
    }
    if (s.taste_summary) renderTasteDNA(s.taste_summary);
    else $("#taste-dna").textContent = "Not built yet. Click Rebuild.";
    // First-launch prompt: if connected but no TASTE_PROFILE.md yet, ask
    // the user to run the LLM-powered analyzer.
    const banner = $("#taste-banner");
    if (banner) {
      if (s.ready && s.has_ai && !s.taste_profile_exists
          && !sessionStorage.getItem("taste_banner_dismissed")) {
        banner.style.display = "flex";
      } else {
        banner.style.display = "none";
      }
    }
  } catch (e) {
    toast("Server not reachable");
  }
}

async function runTasteAnalyze() {
  const btn = $("#run-taste-analyze");
  if (!btn) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.innerHTML = `<span class="spinner"></span> Analyzing (1-2 min)`;
  try {
    const r = await api("/api/taste/analyze", { method: "POST" });
    if (r.ok) {
      toast(`Taste profile written — ${r.liked_songs_analyzed} songs analyzed`);
      $("#taste-banner").style.display = "none";
      await loadStatus();
    } else {
      toast("Analyzer error: " + (r.error || "unknown"), 4000);
    }
  } catch (e) {
    toast("Analyzer failed: " + e.message, 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function renderTasteDNA(s) {
  if (!s || !s.center) return;
  const c = s.center;
  const artists = (s.top_artists || []).slice(0, 10);
  const artistList = artists.map(([n, k], i) => `
    <div class="dna-artist">
      <span class="rank">${String(i + 1).padStart(2, "0")}</span>
      <span class="name">${escapeHtml(n)}</span>
      <span class="n">${k}</span>
    </div>`).join("");

  $("#taste-dna").classList.remove("muted");
  $("#taste-dna").innerHTML = `
    <div class="dna-metrics">
      <span class="k">energy</span><span class="v">${(c.energy || 0).toFixed(2)}</span>
      <span class="k">valence</span><span class="v">${(c.valence || 0).toFixed(2)}</span>
      <span class="k">dance</span><span class="v">${(c.danceability || 0).toFixed(2)}</span>
      <span class="k">tempo</span><span class="v">${Math.round(c.tempo || 0)}</span>
      <span class="k">acoustic</span><span class="v">${(c.acousticness || 0).toFixed(2)}</span>
    </div>
    <div class="dna-artist-list">${artistList}</div>
    <div class="dna-total">${s.total || 0} tracks indexed</div>
  `;
}

async function loadPlaylists() {
  try {
    const { playlists } = await api("/api/playlists");
    state.playlists = playlists;
    renderPlaylists();
  } catch (e) {
    $("#playlist-list").innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`;
  }
}

function renderPlaylists() {
  const el = $("#playlist-list");
  if (!state.playlists.length) {
    el.innerHTML = `<div class="muted">No playlists.</div>`;
    return;
  }
  const managed = state.playlists.filter(p => p.is_managed);
  const others = state.playlists.filter(p => !p.is_managed);

  const row = (p) => `
    <div class="playlist-item ${state.currentPlaylist === p.name ? 'active' : ''} ${p.not_yet_created ? 'not-created' : ''}"
         data-name="${escapeAttr(p.name)}" data-id="${escapeAttr(p.id || '')}">
      <span class="pl-name">${escapeHtml(p.name)}</span>
      <span class="pl-count">${p.total || 0}</span>
    </div>`;

  el.innerHTML = [
    managed.length ? `<div class="playlist-label">Managed</div>` : '',
    ...managed.map(row),
    others.length ? `<div class="playlist-label">Other</div>` : '',
    ...others.map(row),
  ].join("");

  $$(".playlist-item").forEach(el => {
    el.addEventListener("click", () => selectPlaylist(el.dataset.name, el.dataset.id));
  });
}

async function selectPlaylist(name, id) {
  state.currentPlaylist = name;
  $("#context-chip").textContent = name;
  $("#context-chip").classList.add("active");
  renderPlaylists();

  // contextual quick actions
  $("#quick-actions").innerHTML = `
    <button class="chip primary" data-action="recommend-for">Recommend for ${escapeHtml(name)}</button>
    <button class="chip" data-action="health">Health check</button>
    <button class="chip" data-action="clean">Clean duplicates</button>
    <button class="chip" data-action="reorder">Reorder for flow</button>
    <button class="chip" data-action="show-tracks">Show tracks</button>
    <button class="chip" data-action="clear-context">Clear</button>
  `;
  bindQuickActions();

  // auto-run health in sidebar
  if (id) {
    showLoading("#health-content");
    $("#health-widget").style.display = "block";
    try {
      const h = await api("/api/playlist/health", { method: "POST", body: { playlist: name } });
      renderHealth(h);
    } catch (e) {
      $("#health-content").innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`;
    }
  } else {
    $("#health-widget").style.display = "none";
  }
}

function renderHealth(h) {
  if (h.error) {
    $("#health-content").innerHTML = `<div class="muted">${escapeHtml(h.error)}</div>`;
    return;
  }
  const pct = Math.round((h.score || 0) * 100);
  $("#health-content").innerHTML = `
    <div class="health-row"><span class="k">Score</span><span class="v">${pct}%</span></div>
    <div class="health-bar"><div class="health-bar-fill" style="width:${pct}%"></div></div>
    <div class="health-row"><span class="k">Tracks</span><span class="v">${h.total || 0}</span></div>
    <div class="health-row"><span class="k">Duplicates</span><span class="v">${h.duplicates || 0}</span></div>
    <div class="health-row"><span class="k">Outliers</span><span class="v">${h.outlier_count || 0}</span></div>
    <div class="health-row"><span class="k">Flow</span><span class="v">${Math.round((h.flow_score || 0) * 100)}%</span></div>
  `;
}

// ---------------- Quick actions ----------------
function bindQuickActions() {
  $$("#quick-actions .chip").forEach(c => {
    c.addEventListener("click", () => handleQuickAction(c.dataset.action));
  });
}

async function handleQuickAction(action) {
  const pl = state.currentPlaylist;
  switch (action) {
    case "build":
      promptChat("Build me a new playlist — ");
      break;
    case "fix":
      promptChat("Fix this playlist — ");
      break;
    case "discover":
      promptChat("Recommend me something new right now");
      break;
    case "reference":
      promptChat("Make me something that feels like this playlist: https://open.spotify.com/playlist/");
      break;
    case "recommend-for":
      if (pl) sendChat(`Recommend songs for ${pl}`);
      break;
    case "health":
      if (pl) sendChat(`Run a health check on ${pl}`);
      break;
    case "clean":
      if (pl) {
        const h = await api("/api/playlist/clean", { method: "POST", body: { playlist: pl } });
        toast(`${h.duplicates_removed || 0} duplicates removed`);
        await loadPlaylists();
        if (pl) await selectPlaylist(pl, state.playlists.find(p => p.name === pl)?.id);
      }
      break;
    case "reorder":
      if (pl) {
        const r = await api("/api/playlist/reorder", { method: "POST",
          body: { playlist: pl, flow_style: "smooth" } });
        if (r.reordered) toast(`${r.reordered} tracks reordered for flow`);
        else toast(r.reason || "Reordered");
      }
      break;
    case "show-tracks":
      if (pl) showPlaylistContents(pl);
      break;
    case "clear-context":
      state.currentPlaylist = null;
      $("#context-chip").textContent = "No playlist selected";
      $("#context-chip").classList.remove("active");
      $("#health-widget").style.display = "none";
      resetQuickActions();
      renderPlaylists();
      break;
  }
}

function resetQuickActions() {
  $("#quick-actions").innerHTML = `
    <button class="chip" data-action="build">Build a playlist</button>
    <button class="chip" data-action="fix">Fix a playlist</button>
    <button class="chip" data-action="discover">Discover</button>
    <button class="chip" data-action="reference">Analyze a URL</button>
  `;
  bindQuickActions();
}

async function showPlaylistContents(pl) {
  const meta = state.playlists.find(p => p.name === pl);
  if (!meta || !meta.id) return toast("Playlist not yet on Spotify");
  showLoading("#content");
  try {
    const { tracks } = await api(`/api/playlist/${meta.id}/tracks`);
    renderPlaylistContents(pl, tracks);
  } catch (e) {
    $("#content").innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`;
  }
}

function renderPlaylistContents(pl, tracks) {
  $("#content").innerHTML = `
    <div class="section-title">
      <span>${escapeHtml(pl)}</span>
      <span class="count">${tracks.length} tracks</span>
    </div>
    <div class="track-list">
      ${tracks.map((t, i) => trackRowHTML(t, i, { removeFrom: pl })).join("")}
    </div>
  `;
  bindTrackActions();
}

// ---------------- Chat ----------------
async function loadChat() {
  try {
    const { messages } = await api("/api/chat/history");
    if (!messages.length) return;
    const box = $("#chat-history");
    const empty = box.querySelector(".chat-empty");
    if (empty) empty.remove();
    messages.forEach(m => appendChat(m.role, m.content, false));
    box.scrollTop = box.scrollHeight;
  } catch (e) { /* silent */ }
}

function appendChat(role, content, scroll = true) {
  const box = $("#chat-history");
  const empty = box.querySelector(".chat-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.textContent = content;
  box.appendChild(div);
  if (scroll) box.scrollTop = box.scrollHeight;
  return div;
}

function promptChat(prefix) {
  $("#chat-input").value = prefix;
  $("#chat-input").focus();
}

async function sendChat(msg) {
  msg = msg || $("#chat-input").value.trim();
  if (!msg) return;
  $("#chat-input").value = "";
  autoResizeInput();

  appendChat("user", msg);
  const thinking = appendChat("assistant", "");
  thinking.innerHTML = '<span class="thinking"><span class="dot"></span><span class="dot"></span><span class="dot"></span></span>';

  try {
    const res = await api("/api/chat", {
      method: "POST",
      body: { message: msg, playlist: state.currentPlaylist },
    });
    thinking.textContent = res.message || "(no response)";

    if (res.questions && res.questions.length) {
      renderQuestions(res.questions);
    }
    if (res.action_result) {
      await handleActionResult(res.action_result, res);
    } else if (res.action && !res.auto_execute) {
      await handleAIAction(res.action, res.message);
    }
  } catch (e) {
    thinking.textContent = "Error: " + e.message;
  }
}

async function handleActionResult(r, fullResult) {
  if (!r || !r.ok) {
    const err = r && r.error ? r.error : "Action failed";
    appendChat("assistant", "Error: " + err);
    return;
  }
  if (r.kind === "recommend") {
    state.lastRecs = r.tracks || [];
    state.lastParams = { ...(fullResult.audio_target || {}), count: fullResult.count,
                        search_queries: fullResult.search_queries || [],
                        flow_style: fullResult.flow_style || "smooth" };
    state.selected = new Set((r.tracks || []).map(t => t.id));
    state.rejectedIds = new Set();
    renderVerifiedResult(r, fullResult);
    if (r.added > 0) {
      toast(`Added ${r.added} to ${r.playlist}`);
      await loadPlaylists();
    }
    return;
  }
  if (r.kind === "clean") {
    toast(`${r.duplicates_removed || 0} duplicates removed`);
    await loadPlaylists();
    if (r.playlist) await selectPlaylist(r.playlist, state.playlists.find(p => p.name === r.playlist)?.id);
    return;
  }
  if (r.kind === "reorder") {
    toast(r.reordered ? `${r.reordered} tracks reordered` : (r.reason || "Reordered"));
    return;
  }
  if (r.kind === "health") {
    renderHealthMain(r);
    return;
  }
  if (r.kind === "analyze_ref" && r.params) {
    await runRecommend(r.params, null);
    return;
  }
}

function renderVerifiedResult(r, fullResult) {
  const tracks = r.tracks || [];
  const playlist = r.playlist;
  const intent = r.intent;
  const headline = (() => {
    if (intent === "add_to_playlist") return `Added to ${playlist}`;
    if (intent === "new_playlist") return `New — ${playlist}`;
    if (intent === "refine") return `Refined${playlist ? ` — ${playlist}` : ""}`;
    return playlist ? `For ${playlist}` : "Suggestions";
  })();
  const rejected = r.rejected || [];
  const rejectedBlock = rejected.length ? `
    <details class="rejected-box">
      <summary>${rejected.length} rejected</summary>
      <div class="rejected-list">
        ${rejected.slice(0, 20).map(rj => `
          <div class="rejected-item">
            <span class="what">${escapeHtml(rj.artist || "")} — ${escapeHtml(rj.title || "")}</span>
            <span class="why">${escapeHtml(rj.reason || "")}</span>
          </div>`).join("")}
      </div>
    </details>` : "";

  const metaBits = [
    `${r.candidate_count || tracks.length} proposed`,
    `${tracks.length} verified`,
    r.padded_from_library ? `${r.padded_from_library} from library` : "",
    r.added ? `${r.added} added` : "",
  ].filter(Boolean).join("  ·  ");

  if (!tracks.length) {
    $("#content").innerHTML = `
      <div class="section-title"><span>${escapeHtml(headline)}</span></div>
      <div class="muted" style="padding:14px 0;">No tracks passed verification. Try rephrasing, or name a few artists you're in the mood for.</div>
      ${rejectedBlock}
    `;
    return;
  }

  const canAdd = intent !== "add_to_playlist" || r.added === 0;
  $("#content").innerHTML = `
    <div class="section-title">
      <span>${escapeHtml(headline)}</span>
      <span class="count">${tracks.length} tracks</span>
    </div>
    <div class="result-meta">${metaBits}</div>
    <div class="refine-bar">
      <span class="label">Refine</span>
      <button class="chip" data-action="more">More like this</button>
      <button class="chip" data-action="refine-chill">Calm it down</button>
      <button class="chip" data-action="refine-energy">Push the energy</button>
      <button class="chip" data-action="refine-variety">Different artists</button>
      ${canAdd ? `<button class="chip primary-cta" id="add-selected">Add ${tracks.length} to ${escapeHtml(playlist || "new playlist")}</button>` : ''}
    </div>
    <div class="track-list">
      ${tracks.map((t, i) => trackRowHTML(t, i, { showFit: true })).join("")}
    </div>
    ${rejectedBlock}
  `;
  bindTrackActions();
  bindRefineActions(playlist);
}

function trackRowHTML(t, i, opts = {}) {
  const f = t.features || {};
  const fit = t.playlist_score != null ? t.playlist_score
            : t.score != null ? t.score : null;
  const fitStr = fit != null ? `metadata_fit ${fit.toFixed(2)}` : "";
  const tempo = f.tempo ? `${Math.round(f.tempo)} bpm` : "";
  const meta = [fitStr, tempo].filter(Boolean).join("  ·  ");
  const url = t.external_url || (t.id ? `https://open.spotify.com/track/${t.id}` : "#");
  return `
    <div class="track-row" data-id="${escapeAttr(t.id || "")}" data-idx="${i}" data-url="${escapeAttr(url)}">
      <span class="idx">${String(i + 1).padStart(2, "0")}</span>
      <span class="title-line">
        <span>${escapeHtml(t.name || "Untitled")}</span>
        <span class="sep">·</span>
        <span class="artist">${escapeHtml(t.artist || "")}</span>
      </span>
      <span class="meta"><span class="fit">${escapeHtml(meta)}</span></span>
    </div>
  `;
}

function renderQuestions(questions) {
  const container = document.createElement("div");
  container.className = "questions";
  container.innerHTML = questions.map((q, qi) => `
    <div class="question-block" data-qi="${qi}">
      <div class="question-text">${escapeHtml(q.text)}</div>
      <div class="question-options">
        ${(q.options || []).map(o => `
          <button class="question-option" data-option="${escapeAttr(o)}">${escapeHtml(o)}</button>
        `).join("")}
      </div>
    </div>
  `).join("");
  $("#content").insertBefore(container, $("#content").firstChild);
  container.querySelectorAll(".question-option").forEach(btn => {
    btn.addEventListener("click", () => {
      const answer = btn.dataset.option;
      btn.closest(".question-block").querySelectorAll(".question-option").forEach(x => x.classList.remove("selected"));
      btn.classList.add("selected");
      sendChat(answer);
    });
  });
}

async function handleAIAction(action, message) {
  if (action.type === "recommend") {
    await runRecommend(action.params || {}, action.playlist);
  } else if (action.type === "clean") {
    const res = await api("/api/playlist/clean", { method: "POST", body: { playlist: action.playlist } });
    toast(`${res.duplicates_removed || 0} duplicates removed`);
    await loadPlaylists();
  } else if (action.type === "health") {
    const h = await api("/api/playlist/health", { method: "POST", body: { playlist: action.playlist } });
    renderHealthMain(h);
  } else if (action.type === "reorder") {
    const r = await api("/api/playlist/reorder", { method: "POST",
      body: { playlist: action.playlist, flow_style: action.flow_style || "smooth" } });
    toast(r.reordered ? `${r.reordered} tracks reordered` : (r.reason || "Reordered"));
  } else if (action.type === "learn_reference") {
    try {
      const { params } = await api("/api/reference", { method: "POST", body: { url: action.url } });
      if (params && Object.keys(params).length) {
        await runRecommend(params, null);
      } else {
        toast("Couldn't analyze that URL");
      }
    } catch (e) { toast(e.message); }
  }
}

function renderHealthMain(h) {
  if (h.error) { toast(h.error); return; }
  const pct = Math.round((h.score || 0) * 100);
  const outliers = (h.outliers || []).map((o, i) => `
    <div class="track-row" data-id="${escapeAttr(o.id)}" data-url="${escapeAttr(o.external_url || `https://open.spotify.com/track/${o.id}`)}">
      <span class="idx">${String(i + 1).padStart(2, "0")}</span>
      <span class="title-line">
        <span>${escapeHtml(o.name)}</span>
        <span class="sep">·</span>
        <span class="artist">${escapeHtml(o.artist)}</span>
      </span>
      <span class="meta"><span class="fit">distance ${o.distance}</span></span>
    </div>
  `).join("");
  $("#content").innerHTML = `
    <div class="section-title">
      <span>${escapeHtml(h.playlist)} — health</span>
      <span class="count">${pct}%</span>
    </div>
    <div class="result-meta">
      ${h.total} tracks  ·  ${h.duplicates} duplicates  ·  ${h.outlier_count} outliers  ·  flow ${Math.round((h.flow_score || 0) * 100)}%
    </div>
    ${outliers ? `
      <div class="section-title" style="font-size:18px; margin-top:22px;">
        <span>Outliers</span>
        <span class="count">${h.outlier_count}</span>
      </div>
      <div class="track-list">${outliers}</div>` : ''}
  `;
  bindTrackActions();
}

// ---------------- Recommendations ----------------
async function runRecommend(params, playlist) {
  showLoading("#content");
  try {
    const res = await api("/api/recommend", {
      method: "POST",
      body: { params, playlist: playlist || state.currentPlaylist },
    });
    state.lastRecs = res.tracks || [];
    state.lastParams = res.params;
    state.selected = new Set(state.lastRecs.map(t => t.id));
    state.rejectedIds = new Set();
    // reuse verified-result renderer for consistency
    renderVerifiedResult({
      tracks: state.lastRecs,
      playlist: res.playlist,
      intent: "suggest",
      candidate_count: state.lastRecs.length,
      rejected: [],
      added: 0,
    }, { audio_target: params });
  } catch (e) {
    $("#content").innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`;
  }
}

function bindTrackActions() {
  $$(".track-row").forEach(row => {
    row.addEventListener("click", (e) => {
      const id = row.dataset.id;
      const url = row.dataset.url;
      if (id) loadEmbed(id);
      else if (url) window.open(url, "_blank");
    });
  });
}

function bindRefineActions(playlist) {
  $$(".refine-bar .chip").forEach(c => {
    if (c.id === "add-selected") return;
    c.addEventListener("click", () => refineRecs(c.dataset.action, playlist));
  });
  $("#add-selected")?.addEventListener("click", () => addSelected(playlist));
}

async function refineRecs(action, playlist) {
  // Route refinement through /api/chat so it hits the verified pipeline.
  const msg = {
    "more":           "more like this, same vibe",
    "refine-chill":   "too intense, calm it down a bit",
    "refine-energy":  "too chill, push the energy",
    "refine-variety": "more variety, different artists",
  }[action] || "refine";
  await sendChat(msg);
}

async function addSelected(playlist) {
  const ids = state.lastRecs.map(t => t.id);
  if (!ids.length) return toast("Nothing to add");
  let pl = playlist || state.currentPlaylist;
  if (!pl) {
    pl = prompt("Playlist name?");
    if (!pl) return;
  }
  const btn = $("#add-selected");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> Adding`;
  }
  try {
    const res = await api("/api/playlist/add", { method: "POST",
      body: { playlist: pl, track_ids: ids, reorder: true } });
    toast(res.already_present ? "All already in playlist"
                              : `Added ${res.added} to ${pl}`);
    await loadPlaylists();
  } catch (e) {
    toast(e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = `Add ${ids.length} to ${pl}`; }
  }
}

// ---------------- Spotify embed ----------------
function loadEmbed(trackId) {
  $("#spotify-embed").innerHTML =
    `<iframe src="https://open.spotify.com/embed/track/${trackId}?utm_source=generator&theme=0"
             allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"
             loading="lazy"></iframe>`;
}

// ---------------- Modals ----------------
function openSettings() {
  const m = $("#settings-modal");
  m.classList.add("open");
  loadConfigIntoForm();
}
function closeModal(m) { m.classList.remove("open"); }

async function loadConfigIntoForm() {
  try {
    const cfg = await api("/api/config");
    $("#cfg-spotify-id").value = cfg.spotify_client_id || "";
    $("#cfg-spotify-secret").value = cfg.spotify_client_secret || "";
    $("#cfg-spotify-redirect").value = cfg.spotify_redirect_uri || "http://127.0.0.1:8888/callback";
    $("#cfg-anthropic").value = cfg.anthropic_api_key || "";
    $("#cfg-openai").value = cfg.openai_api_key || "";
  } catch (e) {}
}

async function saveConfig() {
  const body = {
    spotify_client_id: $("#cfg-spotify-id").value,
    spotify_client_secret: $("#cfg-spotify-secret").value.startsWith("...") ? undefined : $("#cfg-spotify-secret").value,
    spotify_redirect_uri: $("#cfg-spotify-redirect").value,
    anthropic_api_key: $("#cfg-anthropic").value.startsWith("...") ? undefined : $("#cfg-anthropic").value,
    openai_api_key: $("#cfg-openai").value.startsWith("...") ? undefined : $("#cfg-openai").value,
  };
  Object.keys(body).forEach(k => body[k] === undefined && delete body[k]);
  await api("/api/config", { method: "POST", body });
  await api("/api/connect", { method: "POST" });
  toast("Config saved");
  closeModal($("#settings-modal"));
  await loadStatus();
  await loadPlaylists();
}

async function openRules() {
  $("#rules-modal").classList.add("open");
  const { rules } = await api("/api/rules");
  $("#rules-list").innerHTML = rules.length ? rules.map(r => `
    <div class="rule-item">
      <div class="rule-text">${escapeHtml(r.rule)}</div>
      <div class="rule-conf">${Math.round((r.confidence || 0) * 100)}%</div>
      <button class="icon-btn" data-delete-rule="${r.id}">Remove</button>
    </div>
  `).join("") : `<div class="muted">No rules learned yet. Give feedback on recommendations and rules will emerge.</div>`;
  $$("[data-delete-rule]").forEach(b => b.addEventListener("click", async () => {
    await api(`/api/rules/${b.dataset.deleteRule}`, { method: "DELETE" });
    openRules();
  }));
}

// ---------------- UI bindings ----------------
function autoResizeInput() {
  const ta = $("#chat-input");
  if (!ta) return;
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
}

function bindUI() {
  $("#chat-send").addEventListener("click", () => sendChat());
  const ta = $("#chat-input");
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  ta.addEventListener("input", autoResizeInput);

  $("#refresh-playlists").addEventListener("click", async () => {
    const r = await fetch("/api/playlists?force=1");
    state.playlists = (await r.json()).playlists;
    renderPlaylists();
  });
  $("#build-taste").addEventListener("click", async () => {
    const btn = $("#build-taste");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>`;
    try {
      const r = await api("/api/taste/build", { method: "POST" });
      if (r.summary) renderTasteDNA(r.summary);
      toast("Taste DNA rebuilt");
    } catch (e) { toast("Build failed: " + e.message); }
    finally { btn.disabled = false; btn.textContent = "Rebuild"; }
  });
  $("#open-settings").addEventListener("click", openSettings);
  $("#open-rules").addEventListener("click", openRules);
  $("#run-taste-analyze")?.addEventListener("click", runTasteAnalyze);
  $("#dismiss-taste-banner")?.addEventListener("click", () => {
    sessionStorage.setItem("taste_banner_dismissed", "1");
    $("#taste-banner").style.display = "none";
  });
  $$(".modal [data-close]").forEach(b => b.addEventListener("click", () => closeModal(b.closest(".modal"))));
  $$(".modal").forEach(m => m.addEventListener("click", (e) => {
    if (e.target === m) closeModal(m);
  }));
  $("#save-config").addEventListener("click", saveConfig);
  bindQuickActions();
}

function showLoading(sel) {
  const el = document.querySelector(sel);
  if (el) el.innerHTML = `<div class="loading-row"><span class="spinner"></span> loading</div>`;
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s).replace(/"/g, "&quot;"); }

init();

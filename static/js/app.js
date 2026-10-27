/* FF Level Filter — Panel UI */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let STATE = {
  regions: {},
  activeRegion: null,
  activeFilter: "22-30+",
  filters: [],
  filterLabels: {},
  polling: null,
  lastLogLen: 0,
};

// ─── Utils ───
function toast(msg, type = "info") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("#toastWrap").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function fmtNum(n) {
  return new Intl.NumberFormat().format(n || 0);
}

async function api(path, opts = {}) {
  try {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    
    if (!res.ok) {
      // যদি সার্ভার 400 বা 500 এরর দেয়
      return { ok: false, error: `Server Error: ${res.status}` };
    }
    return await res.json();
  } catch (error) {
    console.error("API Error:", error);
    // যদি সার্ভার বন্ধ থাকে বা কানেকশন না পায়
    return { ok: false, error: "Server connection failed. Please restart api server" };
  }
}

// ─── Load initial data ───
async function loadFilters() {
  const data = await api("/api/filters");
  if (data.ok) {
    STATE.filters = data.filters;
    STATE.filterLabels = data.labels;
    renderFilterTabs();
  }
}

async function loadRegions() {
  const data = await api("/api/data");
  if (!data || !data.ok) { 
    $("#regionsList").innerHTML = `<div class="empty-state">Failed to load regions</div>`;
    toast(data?.error || "Failed to load regions", "error"); 
    return; 
  }
  
  STATE.regions = data.regions || {};
  renderRegions();
  renderStats();
  populateRegionSelect();
  
  if (!STATE.activeRegion && Object.keys(STATE.regions).length > 0) {
    STATE.activeRegion = Object.keys(STATE.regions)[0];
    renderRegions();
    loadResults();
  }
}

function renderStats() {
  const regions = Object.values(STATE.regions);
  $("#statRegions").textContent = fmtNum(regions.length);
  $("#statAccounts").textContent = fmtNum(regions.reduce((a, r) => a + r.input_count, 0));
  $("#statResults").textContent = fmtNum(regions.reduce((a, r) => a + r.total_results, 0));
  const failedEl = $("#statFailed");
  failedEl.textContent = fmtNum(STATE.failedTotal || 0);

  // Clear বাটন কন্ট্রোল (ডাটা থাকলেই বাটন দেখাবে)
  const totalResults = regions.reduce((a, r) => a + r.total_results, 0);
  const clearBtn = $("#clearBtn");
  if (totalResults > 0 || STATE.failedTotal > 0) {
    clearBtn.style.display = "inline-block";
  } else {
    clearBtn.style.display = "none";
  }
}

function renderRegions() {
  const list = $("#regionsList");
  const regions = Object.keys(STATE.regions || {});
  if (!regions.length) {
    list.innerHTML = `<div class="empty-state">No regions uploaded yet</div>`;
    return;
  }
  
  list.innerHTML = regions.map((r) => {
    const d = STATE.regions[r] || { input_count: 0, total_results: 0 };
    const active = r === STATE.activeRegion ? "active" : "";
    return `
      <div class="region-card ${active}" data-region="${r}">
        <div>
          <div class="region-name">${r}</div>
          <div class="region-meta">${d.input_count} input • ${d.total_results} results</div>
        </div>
        <div class="region-actions">
          <button class="region-scan-btn" data-scan="${r}">Scan</button>
          <button class="region-del-btn" data-del="${r}" title="Delete Region" style="background: rgba(255,92,124,0.15); color: var(--danger); border: 1px solid rgba(255,92,124,0.35); padding: 5px 10px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; margin-left: 4px;">🗑</button>
        </div>
      </div>`;
  }).join("");

  // কার্ড সিলেক্ট করার লজিক
  $$(".region-card").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.target.closest('[data-scan]') || e.target.closest('[data-del]')) return;
      STATE.activeRegion = card.dataset.region;
      renderRegions();
      $("#regionSelect").value = STATE.activeRegion;
      loadResults();
    });
  });
  
  // Scan বাটনের লজিক
  $$("[data-scan]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      startScan(btn.dataset.scan);
    });
  });

  // Delete (🗑) বাটনের লজিক
  $$("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const region = btn.dataset.del;
      if (!confirm(`Are you sure you want to delete region ${region} and its data file?`)) return;
      
      const res = await api(`/api/region/${region}`, { method: "DELETE" });
      if (res.ok) {
        toast(`Region ${region} deleted`, "success");
        if (STATE.activeRegion === region) STATE.activeRegion = null;
        await loadRegions();
      } else {
        toast(res.error || "Delete failed", "error");
      }
    });
  });
}

function populateRegionSelect() {
  const sel = $("#regionSelect");
  const regions = Object.keys(STATE.regions);
  sel.innerHTML = regions.map((r) => `<option value="${r}">${r}</option>`).join("");
  if (STATE.activeRegion) sel.value = STATE.activeRegion;
  sel.onchange = () => {
    STATE.activeRegion = sel.value;
    renderRegions();
    loadResults();
  };
}

// ─── Filters ───
function renderFilterTabs() {
  const wrap = $("#filterTabs");
  wrap.innerHTML = STATE.filters.map((f) => `
    <button class="filter-tab ${f === STATE.activeFilter ? "active" : ""}" data-filter="${f}">
      ${STATE.filterLabels[f] || f}
    </button>
  `).join("");
  $$(".filter-tab").forEach((t) => {
    t.addEventListener("click", () => {
      STATE.activeFilter = t.dataset.filter;
      renderFilterTabs();
      loadResults();
    });
  });
}

// ─── Results ───
async function loadResults() {
  if (!STATE.activeRegion) return;
  
  // ✅ এখানে encodeURIComponent যোগ করা হয়েছে
  const url = `/api/results/${encodeURIComponent(STATE.activeRegion)}/${encodeURIComponent(STATE.activeFilter)}`;
  const data = await api(url);
  
  const body = $("#resultsBody");
  if (!data.ok) { 
    body.innerHTML = `<tr><td colspan="3" class="table-empty">${data.error}</td></tr>`; 
    return; 
  }
  if (!data.accounts || !data.accounts.length) {
    body.innerHTML = `<tr><td colspan="3" class="table-empty">No accounts in this filter yet</td></tr>`;
    return;
  }
  
  body.innerHTML = data.accounts.map((acc, i) => {
    const displayStr = acc.password ? `${acc.uid}:${acc.password}` : acc.uid;
    return `
    <tr>
      <td class="mono">${i + 1}</td>
      <td class="mono">${escapeHtml(displayStr)}</td>
      <td class="th-right">
        <button class="copy-btn" data-copy="${escapeAttr(displayStr)}">Copy</button>
      </td>
    </tr>
  `}).join("");
  
  $$(".copy-btn").forEach((b) => {
    b.onclick = () => {
      navigator.clipboard.writeText(b.dataset.copy);
      toast("Copied!", "success");
    };
  });
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function escapeAttr(s) { return String(s).replace(/"/g, "&quot;"); }

// ─── Scan ───
async function startScan(region = "ALL") {
  const res = await api("/api/start", { method: "POST", body: JSON.stringify({ region }) });
  if (!res.ok) { toast(res.error || "Start failed", "error"); return; }
  toast(`Scan started for ${region}`, "success");
  startPolling();
}

function startPolling() {
  if (STATE.polling) return;
  STATE.lastLogLen = 0;
  $("#logBody").innerHTML = "";
  STATE.polling = setInterval(pollStatus, 450);
  pollStatus();
}

async function pollStatus() {
  const s = await api("/api/status");
  updateStatusUI(s);
  if (s.done && !s.running) {
    clearInterval(STATE.polling);
    STATE.polling = null;
    toast("Scan finished", "success");
    await loadRegions();
  }
}

function updateStatusUI(s) {
  const pill = $("#statusPill");
  if (s.running) {
    pill.className = "pill pill-running"; pill.textContent = "RUNNING";
  } else if (s.done && s.progress > 0) {
    pill.className = "pill pill-done"; pill.textContent = "DONE";
  } else {
    pill.className = "pill pill-idle"; pill.textContent = "IDLE";
  }

  const pct = s.total ? Math.round((s.progress / s.total) * 100) : 0;
  $("#progressBar").style.width = pct + "%";
  $("#percentText").textContent = pct + "%";
  $("#progressMeta").textContent = `${s.progress} / ${s.total}`;
  $("#currentJob").textContent = s.running
    ? `${s.current_region || "-"} → ${(s.current_account || "").slice(0, 24)}`
    : "Idle";

  // counters
  const grid = $("#countersGrid");
  const entries = Object.entries(s.counters || {});
  grid.innerHTML = entries.map(([k, v]) => `
    <div class="counter-box">
      <span class="counter-label">${STATE.filterLabels[k] || k}</span>
      <span class="counter-value">${v}</span>
    </div>`).join("") + `
    <div class="counter-box">
      <span class="counter-label">Failed</span>
      <span class="counter-value" style="color:var(--danger)">${s.failed || 0}</span>
    </div>`;

  STATE.failedTotal = s.failed;
  renderStats();

  // log
  if (s.log && s.log.length !== STATE.lastLogLen) {
    const body = $("#logBody");
    body.innerHTML = s.log.map((l) =>
      `<div class="log-line"><span class="ts">${l.ts}</span>${escapeHtml(l.msg)}</div>`
    ).join("");
    body.scrollTop = body.scrollHeight;
    STATE.lastLogLen = s.log.length;
  }
}

// ─── Upload ───
let selectedFile = null;

const uploadModal = $("#uploadModal");
const dropZone = $("#dropZone");
const fileInput = $("#jsonFile");
const dropText = $("#dropZoneText");

$("#uploadBtn").onclick = () => {
  $("#regionCode").value = "";
  selectedFile = null;
  fileInput.value = "";
  dropText.innerHTML = '<span style="font-size: 24px; display: block; margin-bottom: 8px;">📁</span>Drag & drop JSON here or <span style="color: var(--cyan);">Click to browse</span>';
  $("#uploadError").textContent = "";
  uploadModal.classList.add("open");
};

$("#cancelUpload").onclick = () => uploadModal.classList.remove("open");

// Drag & Drop Events
dropZone.onclick = () => fileInput.click();

dropZone.ondragover = (e) => {
  e.preventDefault();
  dropZone.classList.add("dragover");
};

dropZone.ondragleave = (e) => {
  e.preventDefault();
  dropZone.classList.remove("dragover");
};

dropZone.ondrop = (e) => {
  e.preventDefault();
  dropZone.classList.remove("dragover");
  
  if (e.dataTransfer.files.length > 0) {
    const file = e.dataTransfer.files[0];
    if (!file.name.endsWith('.json')) {
      toast("Please drop a valid JSON file", "error");
      return;
    }
    selectedFile = file;
    dropText.innerHTML = `<span style="font-size: 24px; display: block; margin-bottom: 8px;">✅</span>Selected: <b>${file.name}</b>`;
  }
};

fileInput.onchange = () => {
  if (fileInput.files.length > 0) {
    const file = fileInput.files[0];
    if (!file.name.endsWith('.json')) {
      toast("Please select a valid JSON file", "error");
      return;
    }
    selectedFile = file;
    dropText.innerHTML = `<span style="font-size: 24px; display: block; margin-bottom: 8px;">✅</span>Selected: <b>${file.name}</b>`;
  }
};

$("#confirmUpload").onclick = async () => {
  const region = ($("#regionCode").value || "").trim().toUpperCase();
  
  if (!region) { $("#uploadError").textContent = "Region code din (e.g. BD, IND)"; return; }
  if (!selectedFile) { $("#uploadError").textContent = "JSON file select ba drop korun"; return; }

  try {
    const text = await selectedFile.text();
    const json = JSON.parse(text);
    
    let accounts = Array.isArray(json) ? json : [];
    if (!accounts.length && json && typeof json === "object") {
      accounts = json[region] || json[Object.keys(json)[0]] || [];
    }
    if (!Array.isArray(accounts)) {
      $("#uploadError").textContent = "JSON must be an array of {uid, password}";
      return;
    }

    const res = await api(`/api/region/${region}`, {
      method: "POST",
      body: JSON.stringify({ accounts }),
    });
    
    if (res.ok) {
      toast(`Uploaded ${res.count} account(s) → ${region}`, "success");
      uploadModal.classList.remove("open");
      await loadRegions();
      populateRegionSelect();
    } else {
      $("#uploadError").textContent = res.error || "Upload failed";
    }
  } catch (e) {
    $("#uploadError").textContent = "Invalid JSON: " + e.message;
  }
};

$("#startAllBtn").onclick = () => startScan("ALL");

// Clear বাটনের সেফটি চেক
if ($("#clearBtn")) {
  $("#clearBtn").onclick = async () => {
    if (!confirm("Are you sure? All results will be deleted permanently.")) return;
    const res = await api("/api/clear", { method: "POST" });
    if (res.ok) {
      toast("Data cleared successfully", "success");
      setTimeout(() => window.location.reload(), 1000); 
    } else {
      toast(res.error || "Failed to clear", "error");
    }
  };
}

// Download বাটনের সেফটি চেক
if ($("#downloadBtn")) {
  $("#downloadBtn").onclick = async () => {
    if (!STATE.activeRegion) {
      toast("No region selected", "error");
      return;
    }
    
    const res = await api(`/api/results/${STATE.activeRegion}/${STATE.activeFilter}`);
    if (!res.ok) {
      toast(res.error || "Failed to fetch data", "error");
      return;
    }
    
    if (!res.accounts || res.accounts.length === 0) {
      toast("No data available to download for this filter", "error");
      return;
    }
    
    const dataStr = JSON.stringify(res.accounts, null, 2);
    const blob = new Blob([dataStr], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${STATE.activeRegion}_${STATE.activeFilter}_Accounts.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    toast("Download started", "success");
  };
}

// ─── Init ───
(async function init() {
  await loadFilters();
  await loadRegions();
  // Poll status once for state
  pollStatus().catch(() => {});
})();
(() => {
  "use strict";

  const TOKEN = document.querySelector('meta[name="app-token"]')?.content || "";
  const API_TOKEN_HEADER = "X-WeightWise-Token";
  const RECENT_KEY = "weightwise.recentRoots.v1";
  const ROOT_KEY = "weightwise.lastRoot.v1";

  const state = {
    config: null,
    result: null,
    selections: new Map(),
    scanning: false,
    currentJobId: null,
    pollTimer: null,
    pollFailures: 0,
    displayLimit: 20,
    filter: "",
    recent: [],
    activeModal: null,
    lastFocused: null,
    deleteBusy: false,
    deleteContext: null,
    browserPath: null,
    browserRequestId: 0,
  };

  let el = {};

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    cacheElements();
    bindEvents();
    loadRecentRoots();
    updateScopeNote();

    try {
      const config = await api("/api/config");
      state.config = config;
      applyConfig(config);
    } catch (error) {
      setEngine("Engine unavailable");
      showAlert(
        "The local engine did not respond.",
        ["Reload this page from the WeightWise window. If the window was closed, restart the app."],
        "error"
      );
      return;
    }

    const savedRoot = safeGet(ROOT_KEY);
    if (savedRoot) {
      el.directoryInput.value = savedRoot;
    } else {
      el.directoryInput.value = state.config.default_root || "";
    }

    await loadInitialResult();
  }

  function cacheElements() {
    const ids = [
      "appVersion", "engineStatus", "scanForm", "directoryInput", "browseButton",
      "scanButton", "recursiveOption", "weightsOnlyOption", "hiddenOption",
      "scopeNote", "scopeNoteText", "formatCount", "recentRow", "recentPaths",
      "progressPanel", "progressTitle", "progressMessage", "cancelScanButton",
      "progressTrack", "progressFill", "progressPercent", "progressCounts",
      "progressSpeed", "progressCurrent", "alertBanner", "resultsSection",
      "resultsTitle", "groupCount", "groupLabel", "resultRoot", "scanMeta",
      "revealRootButton", "rescanButton", "statFiles", "statFilesFoot",
      "statGroups", "statGroupsFoot", "statCopies", "statCopiesFoot",
      "statReclaim", "scanNotes", "selectionTitle", "selectionSummary",
      "resetSelection", "reviewDeletion", "reviewDeletionLabel",
      "groupCountChip", "groupSearch", "groupList", "filterEmpty",
      "showMoreGroups", "showMoreCount", "emptyResults", "emptyTitle",
      "emptyMessage", "emptyMeta", "emptyRescan", "emptyChooseFolder",
      "formatChips", "browserModal", "browserQuick", "browserPathInput",
      "browserUp", "browserGo", "browserList", "browserError", "browserCurrent",
      "browserChoose", "deleteModal", "deleteModalTitle", "deleteModalSubtitle",
      "reviewCopies", "reviewSize", "reviewGroups", "deleteError", "reviewList",
      "recycleModeCard", "recycleMode", "permanentModeCard", "permanentMode",
      "reviewConfirm", "permanentConfirmWrap", "permanentConfirmInput",
      "cancelDelete", "confirmDelete", "confirmDeleteLabel", "toastStack", "method",
    ];
    for (const id of ids) {
      el[id] = document.getElementById(id);
    }
    el.scanButtonLabel = el.scanButton.querySelector(".scan-button-label");
    el.scanButtonSpinner = el.scanButton.querySelector(".button-spinner");
    el.deleteSpinner = el.confirmDelete.querySelector(".button-spinner");
  }

  function bindEvents() {
    el.scanForm.addEventListener("submit", (event) => {
      event.preventDefault();
      startScan();
    });
    el.browseButton.addEventListener("click", openBrowser);
    el.emptyChooseFolder.addEventListener("click", openBrowser);
    el.rescanButton.addEventListener("click", startScan);
    el.emptyRescan.addEventListener("click", startScan);
    el.weightsOnlyOption.addEventListener("change", updateScopeNote);
    el.cancelScanButton.addEventListener("click", cancelScan);
    el.revealRootButton.addEventListener("click", () => {
      if (state.result) revealPath(state.result.root);
    });

    el.resetSelection.addEventListener("click", () => {
      initializeSelections();
      renderGroupList();
      updateSelectionSummary();
      toast("Smart keep re-applied. The cleanest copy in each group is retained.", "info");
    });
    el.reviewDeletion.addEventListener("click", () => openDeleteReview(null));
    el.groupSearch.addEventListener("input", () => {
      state.filter = el.groupSearch.value.trim();
      state.displayLimit = 20;
      renderGroupList();
    });
    el.showMoreGroups.addEventListener("click", () => {
      state.displayLimit += 20;
      renderGroupList();
    });
    el.groupList.addEventListener("change", handleGroupCheckbox);
    el.groupList.addEventListener("click", handleGroupAction);

    document.querySelectorAll("[data-scroll]").forEach((button) => {
      button.addEventListener("click", () => {
        const target = document.querySelector(button.dataset.scroll);
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
        document.querySelectorAll(".nav-item").forEach((item) => {
          item.classList.toggle("is-active", item === button);
        });
      });
    });

    el.recentPaths.addEventListener("click", (event) => {
      const button = event.target.closest("[data-recent-path]");
      if (!button) return;
      el.directoryInput.value = button.dataset.recentPath || "";
      el.directoryInput.focus();
      el.directoryInput.scrollIntoView({ behavior: "smooth", block: "center" });
    });

    document.querySelectorAll("[data-close-modal]").forEach((node) => {
      node.addEventListener("click", () => closeModal(node.dataset.closeModal));
    });
    document.addEventListener("keydown", handleGlobalKeydown);

    el.recycleMode.addEventListener("change", updateDeleteMode);
    el.permanentMode.addEventListener("change", updateDeleteMode);
    el.reviewConfirm.addEventListener("change", updateDeleteButton);
    el.permanentConfirmInput.addEventListener("input", updateDeleteButton);
    el.cancelDelete.addEventListener("click", () => closeModal("deleteModal"));
    el.confirmDelete.addEventListener("click", submitDelete);

    el.browserQuick.addEventListener("click", (event) => {
      const button = event.target.closest("[data-browser-path]");
      if (button) loadBrowser(button.dataset.browserPath);
    });
    el.browserList.addEventListener("click", (event) => {
      const row = event.target.closest("[data-folder-path]");
      if (row) loadBrowser(row.dataset.folderPath);
    });
    el.browserUp.addEventListener("click", () => {
      if (el.browserUp.dataset.parent) loadBrowser(el.browserUp.dataset.parent);
    });
    el.browserGo.addEventListener("click", () => loadBrowser(el.browserPathInput.value));
    el.browserPathInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadBrowser(el.browserPathInput.value);
      }
    });
    el.browserChoose.addEventListener("click", chooseBrowserFolder);

    window.addEventListener("beforeunload", () => {
      if (state.pollTimer) clearTimeout(state.pollTimer);
    });
  }

  function applyConfig(config) {
    el.appVersion.textContent = `v${config.version || "1.0.0"}`;
    const extensions = Array.isArray(config.weight_extensions) ? config.weight_extensions : [];
    const ordered = [];
    if (extensions.includes(".safetensors")) ordered.push(".safetensors");
    for (const extension of extensions) {
      if (!ordered.includes(extension)) ordered.push(extension);
    }
    if (ordered.length) {
      el.formatCount.textContent = `${ordered[0]} + ${Math.max(0, ordered.length - 1)} more formats`;
    }    el.formatChips.innerHTML = ordered
      .map((extension) => `<span class="format-chip">${esc(extension)}</span>`)
      .join("");
    if (config.can_recycle === false) {
      el.recycleMode.disabled = true;
      el.recycleModeCard.classList.add("is-disabled");
      el.permanentMode.checked = true;
      el.permanentModeCard.classList.add("is-selected");
      el.recycleModeCard.classList.remove("is-selected");
    }
  }

  async function loadInitialResult() {
    try {
      const payload = await api("/api/result");
      if (!payload.result) {
        setEngine("Awaiting scan");
        return;
      }
      state.result = payload.result;
      el.directoryInput.value = payload.result.root;
      safeSet(ROOT_KEY, payload.result.root);
      initializeSelections();
      renderStats();
      renderOutcome({ scroll: false });
      updateSelectionSummary();
      setEngine("Last scan loaded");
    } catch (error) {
      setEngine("Ready");
      showAlert("Could not restore the last scan.", [error.message], "error");
    }
  }

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { [API_TOKEN_HEADER]: TOKEN };
    let body;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }
    const response = await fetch(path, {
      method,
      headers,
      body,
      cache: "no-store",
      credentials: "same-origin",
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = null;
    }
    if (!response.ok) {
      const error = new Error(payload?.error || `The local engine returned ${response.status}.`);
      error.status = response.status;
      error.details = Array.isArray(payload?.details) ? payload.details : [];
      throw error;
    }
    return payload || {};
  }

  async function startScan() {
    if (state.scanning) {
      toast("A scan is already running.", "info");
      return;
    }
    const path = el.directoryInput.value.trim();
    if (!path) {
      el.directoryInput.focus();
      toast("Choose a folder to scan first.", "error");
      return;
    }

    clearAlert();
    setScanBusy(true);
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.pollFailures = 0;
    state.currentJobId = null;
    resetProgress();
    el.resultsSection.classList.add("is-hidden");
    el.emptyResults.classList.add("is-hidden");
    el.progressPanel.classList.remove("is-hidden");
    el.cancelScanButton.disabled = false;
    el.cancelScanButton.textContent = "Cancel scan";
    setEngine("Scanning");
    el.progressPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });

    try {
      const payload = await api("/api/scan", {
        method: "POST",
        body: {
          path,
          recursive: el.recursiveOption.checked,
          weights_only: el.weightsOnlyOption.checked,
          include_hidden: el.hiddenOption.checked,
        },
      });
      const job = payload.job;
      if (!job || !job.id) throw new Error("The engine did not return a scan job.");
      state.currentJobId = job.id;
      state.pollFailures = 0;
      updateProgress(job);
      schedulePoll(300);
    } catch (error) {
      setScanBusy(false);
      hideProgress();
      restoreOutcome();
      setEngine("Scan error");
      showAlert("The scan could not start.", [error.message], "error");
      toast(error.message, "error");
    }
  }

  function schedulePoll(delay) {
    if (!state.scanning || !state.currentJobId) return;
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(pollScan, delay);
  }

  async function pollScan() {
    if (!state.scanning || !state.currentJobId) return;
    const jobId = state.currentJobId;
    try {
      const payload = await api(`/api/scan/status?job_id=${encodeURIComponent(jobId)}`);
      if (!state.scanning || jobId !== state.currentJobId) return;
      state.pollFailures = 0;
      const job = payload.job;
      if (!job) throw new Error("The scan job disappeared.");
      if (job.status === "running") {
        updateProgress(job);
        schedulePoll(420);
      } else {
        await finishJob(job);
      }
    } catch (error) {
      state.pollFailures += 1;
      if (state.pollFailures <= 10) {
        setEngine("Reconnecting…");
        schedulePoll(900);
      } else {
        setScanBusy(false);
        hideProgress();
        restoreOutcome();
        setEngine("Connection lost");
        showAlert(
          "Lost contact with the local engine.",
          ["The scan may still be running. Reload the page to reconnect, or restart WeightWise."],
          "error"
        );
      }
    }
  }

  async function finishJob(job) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.currentJobId = null;

    if (job.status === "complete") {
      setEngine("Loading results");
      let result = null;
      let lastError = null;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          const payload = await api("/api/result");
          result = payload.result;
          break;
        } catch (error) {
          lastError = error;
          await delay(300);
        }
      }
      if (!result) {
        setScanBusy(false);
        hideProgress();
        restoreOutcome();
        setEngine("Scan error");
        showAlert(
          "The scan finished, but its results could not be loaded.",
          [lastError?.message || "Reload the page to fetch the latest result."],
          "error"
        );
        return;
      }

      setScanBusy(false);
      hideProgress();
      state.result = result;
      state.filter = "";
      el.groupSearch.value = "";
      state.displayLimit = 20;
      el.directoryInput.value = result.root;
      rememberRoot(result.root);
      initializeSelections();
      renderStats();
      renderOutcome({ scroll: true });
      updateSelectionSummary();
      setEngine(result.groups.length ? "Duplicates found" : "Library clean");
      if (!result.groups.length) {
        toast("No byte-identical copies were found.", "info");
      }
      return;
    }

    setScanBusy(false);
    hideProgress();
    restoreOutcome();

    if (job.status === "cancelled") {
      setEngine("Scan cancelled");
      toast("Scan cancelled. Previous results were left untouched.", "info");
      return;
    }

    setEngine("Scan error");
    showAlert(
      "The scan could not finish.",
      [job.error || "An unexpected error occurred while reading the library."],
      "error"
    );
    toast("The scan failed. See the details above.", "error");
  }

  async function cancelScan() {
    if (!state.scanning) return;
    el.cancelScanButton.disabled = true;
    el.cancelScanButton.textContent = "Cancelling…";
    try {
      await api("/api/scan/cancel", { method: "POST", body: {} });
      el.progressMessage.textContent = "Stopping after the current read. This can take a second…";
    } catch (error) {
      el.cancelScanButton.disabled = false;
      el.cancelScanButton.textContent = "Cancel scan";
      if (error.status !== 409) toast(error.message, "error");
    }
  }

  function setScanBusy(busy) {
    state.scanning = busy;
    el.scanButton.disabled = busy;
    el.scanButton.classList.toggle("is-busy", busy);
    el.scanButtonSpinner.classList.toggle("is-hidden", !busy);
    el.scanButtonLabel.textContent = busy ? "Scanning library…" : "Scan for duplicates";
    if (!busy) {
      el.cancelScanButton.disabled = false;
      el.cancelScanButton.textContent = "Cancel scan";
    }
  }

  function resetProgress() {
    el.progressTitle.textContent = "Reading the library";
    el.progressMessage.textContent = "Mapping your files and looking for same-size candidates.";
    el.progressFill.style.width = "0%";
    el.progressTrack.classList.add("is-indeterminate");
    el.progressPercent.textContent = "Starting";
    el.progressCounts.textContent = "0 files found";
    el.progressSpeed.textContent = "";
    el.progressCurrent.textContent = "Preparing the scanner…";
  }

  function updateProgress(job) {
    const phase = job.phase || "starting";
    const hasProgress = typeof job.progress === "number";
    const percentage = hasProgress ? Math.max(0, Math.min(100, job.progress)) : null;

    if (phase === "discovering" || phase === "starting") {
      el.progressTitle.textContent = "Reading the directory";
      el.progressMessage.textContent = "Walking the library and narrowing the field to files with matching sizes.";
      el.progressTrack.classList.add("is-indeterminate");
      el.progressPercent.textContent = "Indexing";
    } else if (phase === "hashing") {
      el.progressTitle.textContent = "Fingerprinting candidates";
      el.progressMessage.textContent = "Streaming full file content through SHA-256. Only exact matches become groups.";
      el.progressTrack.classList.remove("is-indeterminate");
      el.progressPercent.textContent = percentage === null ? "Hashing" : `${Math.round(percentage)}%`;
    } else if (phase === "finalizing") {
      el.progressTitle.textContent = "Comparing fingerprints";
      el.progressMessage.textContent = "Sorting matches and choosing the cleanest copy in each group.";
      el.progressTrack.classList.remove("is-indeterminate");
      el.progressPercent.textContent = percentage === null ? "Finalizing" : `${Math.round(percentage)}%`;
    } else {
      el.progressTitle.textContent = job.message || "Scanning";
      el.progressTrack.classList.remove("is-indeterminate");
      el.progressPercent.textContent = percentage === null ? "Working" : `${Math.round(percentage)}%`;
    }

    if (hasProgress) {
      el.progressFill.style.width = `${percentage}%`;
    }

    const found = Number(job.files_found || 0);
    const hashed = Number(job.files_hashed || 0);
    const toHash = Number(job.files_to_hash || 0);
    if (phase === "hashing" && toHash > 0) {
      el.progressCounts.textContent = `${formatNumber(hashed)} of ${formatNumber(toHash)} candidates fingerprinted · ${formatNumber(found)} files found`;
    } else {
      el.progressCounts.textContent = `${formatNumber(found)} files found`;
    }

    const bytes = Number(job.bytes_processed || 0);
    const elapsed = Number(job.elapsed_ms || 0);
    if (phase === "hashing" && bytes > 0 && elapsed > 300) {
      el.progressSpeed.textContent = `${formatBytes(bytes / (elapsed / 1000))}/s average`;
    } else {
      el.progressSpeed.textContent = "";
    }

    const current = job.current_file || (phase === "finalizing" ? "Building duplicate groups…" : "Discovering weight files…");
    el.progressCurrent.textContent = current;
    el.progressCurrent.title = current;
  }

  function hideProgress() {
    el.progressPanel.classList.add("is-hidden");
    el.progressFill.style.width = "0%";
    el.progressTrack.classList.remove("is-indeterminate");
  }

  function renderStats() {
    if (!state.result) return;
    const result = state.result;
    const stats = result.stats || {};
    el.statFiles.textContent = formatNumber(stats.matched_files || 0);
    el.statFilesFoot.textContent = stats.candidate_files
      ? `${formatNumber(stats.candidate_files)} same-size candidates checked`
      : "unique sizes across the library";
    el.statGroups.textContent = formatNumber(stats.duplicate_groups || 0);
    el.statGroupsFoot.textContent = `${formatNumber(stats.duplicate_files || 0)} files inside matching sets`;
    el.statCopies.textContent = formatNumber(stats.redundant_copies || 0);
    el.statCopiesFoot.textContent = stats.duplicate_groups
      ? `across ${formatNumber(stats.duplicate_groups)} duplicate groups`
      : "no redundant copies found";
    el.statReclaim.textContent = formatBytes(stats.reclaimable_bytes || 0);
    el.groupCount.textContent = formatNumber(stats.duplicate_groups || 0);
    el.groupLabel.textContent = stats.duplicate_groups === 1 ? "duplicate group found" : "duplicate groups found";
    el.groupCountChip.textContent = formatNumber(stats.duplicate_groups || 0);
    el.resultRoot.textContent = result.root;
    el.resultRoot.title = result.root;
    const scanBits = [];
    scanBits.push(formatDuration(stats.duration_ms || 0));
    scanBits.push(`${formatNumber(stats.files_hashed || 0)} SHA-256 fingerprints`);
    if (stats.cache_hits) scanBits.push(`${formatNumber(stats.cache_hits)} cached`);
    el.scanMeta.textContent = scanBits.join(" · ");
  }

  function renderOutcome({ scroll = false } = {}) {
    if (!state.result) {
      el.resultsSection.classList.add("is-hidden");
      el.emptyResults.classList.add("is-hidden");
      return;
    }
    const result = state.result;
    const groups = result.groups || [];
    renderStats();
    renderScanNotes();

    if (!groups.length) {
      el.resultsSection.classList.add("is-hidden");
      el.emptyResults.classList.remove("is-hidden");
      const removed = Number(result.stats.removed_files || 0);
      if (removed > 0) {
        el.emptyTitle.textContent = "Duplicates cleared.";
        el.emptyMessage.textContent = `WeightWise removed ${formatNumber(removed)} duplicate ${removed === 1 ? "copy" : "copies"}. The remaining weight files are distinct, and one copy from every original group was preserved.`;
      } else {
        el.emptyTitle.textContent = "No byte-identical copies.";
        el.emptyMessage.textContent = result.stats.candidate_files
          ? `${formatNumber(result.stats.candidate_files)} same-size candidates were fingerprinted, but every full-file digest was different. Your weights are already lean.`
          : "No two weight files shared a byte size, so there was nothing to fingerprint. Your library is already lean.";
      }
      const emptyBits = [
        `${formatNumber(result.stats.matched_files || 0)} weight files remaining`,
        `${formatDuration(result.stats.duration_ms || 0)} scan time`,
      ];
      if (removed > 0) emptyBits.unshift(`${formatNumber(removed)} copies removed this session`);
      if (result.stats.errors_count) emptyBits.push(`${formatNumber(result.stats.errors_count)} paths skipped`);
      el.emptyMeta.textContent = emptyBits.join(" · ");
      if (scroll) {
        window.setTimeout(() => el.emptyResults.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
      }
      return;
    }

    el.emptyResults.classList.add("is-hidden");
    el.resultsSection.classList.remove("is-hidden");
    renderGroupList();
    updateSelectionSummary();
    if (scroll) {
      window.setTimeout(() => el.resultsSection.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
    }
  }

  function restoreOutcome() {
    if (state.result) {
      renderStats();
      renderOutcome({ scroll: false });
      updateSelectionSummary();
    } else {
      el.resultsSection.classList.add("is-hidden");
      el.emptyResults.classList.add("is-hidden");
    }
  }

  function renderScanNotes() {
    if (!state.result) {
      el.scanNotes.classList.add("is-hidden");
      return;
    }
    const stats = state.result.stats || {};
    const notes = [];
    if (stats.errors_count) {
      const detail = (stats.errors || [])
        .slice(0, 6)
        .map((error) => `${error.path}: ${error.message}`)
        .join("\n");
      notes.push({
        text: `${formatNumber(stats.errors_count)} path${stats.errors_count === 1 ? "" : "s"} skipped`,
        title: detail || "Some files could not be read.",
        warn: true,
      });
    }
    if (stats.cache_hits) {
      notes.push({
        text: `${formatNumber(stats.cache_hits)} cached fingerprints reused`,
        title: "Unchanged files were fingerprinted in an earlier scan using the same size, timestamp and SHA-256 digest.",
        warn: false,
      });
    }
    const hardlinkGroups = (state.result.groups || []).filter((group) =>
      group.files.some((file) => file.hardlink)
    ).length;
    if (hardlinkGroups) {
      notes.push({
        text: `${formatNumber(hardlinkGroups)} group${hardlinkGroups === 1 ? "" : "s"} with hard links`,
        title: "Hard-linked paths share the same file on disk. Removing a path may not free additional space.",
        warn: true,
      });
    }
    if (stats.skipped_empty) {
      notes.push({
        text: `${formatNumber(stats.skipped_empty)} empty files skipped`,
        title: "Zero-byte files are not meaningful weight duplicates.",
        warn: false,
      });
    }
    if (!notes.length) {
      el.scanNotes.classList.add("is-hidden");
      el.scanNotes.innerHTML = "";
      return;
    }
    el.scanNotes.classList.remove("is-hidden");
    el.scanNotes.innerHTML = notes
      .map(
        (note) =>
          `<span class="scan-note${note.warn ? " warn" : ""}" title="${esc(note.title)}">${esc(note.text)}</span>`
      )
      .join("");
  }

  function initializeSelections() {
    state.selections = new Map();
    if (!state.result) return;
    for (const group of state.result.groups || []) {
      state.selections.set(group.id, defaultsForGroup(group));
    }
  }

  function defaultsForGroup(group) {
    const validIds = new Set(group.files.map((file) => file.id));
    const provided = Array.isArray(group.default_delete_ids)
      ? group.default_delete_ids.filter((id) => validIds.has(id))
      : [];
    let selected = new Set(provided);
    if (!selected.size || selected.size >= group.files.length) {
      selected = new Set(
        group.files
          .filter((file) => file.id !== group.recommended_keep_id)
          .map((file) => file.id)
      );
    }
    if (selected.size >= group.files.length && group.files.length) {
      selected.delete(group.files[group.files.length - 1].id);
    }
    return selected;
  }

  function getSelection(groupId) {
    return state.selections.get(groupId) || new Set();
  }

  function selectedCountForGroup(group) {
    return getSelection(group.id).size;
  }

  function remainingForGroup(group) {
    const selected = getSelection(group.id);
    return group.files.filter((file) => !selected.has(file.id));
  }

  function groupHasHardlinks(group) {
    return group.files.some((file) => file.hardlink);
  }

  function keeperForGroup(group) {
    const remaining = remainingForGroup(group);
    return (
      remaining.find((file) => file.id === group.recommended_keep_id) ||
      remaining[0] ||
      null
    );
  }

  function renderGroupList() {
    if (!state.result) return;
    const groups = state.result.groups || [];
    const query = state.filter.toLowerCase();
    const filtered = query
      ? groups.filter((group) => {
          if (group.digest.toLowerCase().includes(query)) return true;
          return group.files.some(
            (file) =>
              file.name.toLowerCase().includes(query) ||
              file.relative_path.toLowerCase().includes(query)
          );
        })
      : groups;

    if (!filtered.length) {
      el.groupList.innerHTML = "";
      el.filterEmpty.classList.toggle("is-hidden", !query);
      el.showMoreGroups.classList.add("is-hidden");
      return;
    }

    el.filterEmpty.classList.add("is-hidden");
    const shown = filtered.slice(0, state.displayLimit);
    el.groupList.innerHTML = shown
      .map((group) => renderGroupCard(group, groups.indexOf(group) + 1))
      .join("");

    const remaining = filtered.length - shown.length;
    if (remaining > 0) {
      el.showMoreGroups.classList.remove("is-hidden");
      el.showMoreCount.textContent = `(${formatNumber(remaining)} more)`;
    } else {
      el.showMoreGroups.classList.add("is-hidden");
      el.showMoreCount.textContent = "";
    }
  }

  function renderGroupCard(group, groupNumber) {
    const selected = getSelection(group.id);
    const selectedCount = selected.size;
    const eachSize = formatBytes(group.size);
    const count = group.files.length;
    const reclaim = formatBytes(group.reclaimable_bytes || 0);
    const hashShort = `${group.digest.slice(0, 16)}…`;
    const remaining = remainingForGroup(group);
    const keeper = keeperForGroup(group);
    const keeperFileId = remaining.length === 1 ? keeper?.id : null;
    const revealPath = keeper?.path || group.files[0]?.path || "";

    const rows = group.files
      .map((file) => {
        const checked = selected.has(file.id);
        const badges = [];
        if (file.badges?.includes("recommended")) {
          badges.push('<span class="badge badge-keep">Recommended</span>');
        }
        if (file.badges?.includes("oldest")) {
          badges.push('<span class="badge badge-oldest">Oldest</span>');
        }
        if (file.hardlink) {
          badges.push('<span class="badge badge-hardlink">Hard link</span>');
        }
        if (file.copyish) {
          badges.push('<span class="badge badge-copyish">Copy name</span>');
        }
        const location = file.directory === "." ? "library root" : file.directory;
        const modified = formatDate(file.mtime_ms);
        return `
          <tr class="${checked ? "is-selected" : ""}${file.id === keeperFileId ? " is-keeper" : ""}" data-row-file-id="${esc(file.id)}">
            <td>
              <label class="check-label" title="Select this copy for deletion">
                <input type="checkbox" data-file-id="${esc(file.id)}" ${checked ? "checked" : ""} aria-label="Select ${esc(file.name)} for deletion">
                <span class="check-box" aria-hidden="true"><svg viewBox="0 0 14 14"><path d="m2.5 7.2 3 3 6-6"/></svg></span>
              </label>
            </td>
            <td>
              <div class="file-identity">
                <div class="file-name-line">
                  <span class="file-name" title="${esc(file.path)}">${esc(file.name)}</span>
                  ${badges.join("")}
                </div>
                <span class="file-path" title="${esc(file.path)}">${esc(location)}</span>
                <span class="file-mobile-meta">${esc(eachSize)} · ${esc(modified)}</span>
              </div>
            </td>
            <td>${esc(eachSize)}</td>
            <td title="${esc(formatDateTime(file.mtime_ms))}">${esc(modified)}</td>
          </tr>`;
      })
      .join("");

    return `
      <article class="group-card" data-group-id="${esc(group.id)}" aria-label="Duplicate group ${groupNumber}: ${count} identical files">
        <header class="group-head">
          <span class="group-index">${String(groupNumber).padStart(2, "0")}</span>
          <div class="group-title-wrap">
            <div class="group-title">
              <h4>${formatNumber(count)} identical ${count === 1 ? "copy" : "copies"}</h4>
              <span class="group-size-badge">${esc(eachSize)} each</span>
            </div>
            <div class="group-subline">
              <span class="hash-label" title="${esc(group.digest)}">SHA-256 ${esc(hashShort)}</span>
              <span class="separator"></span>
              <span class="reclaim-label">${esc(reclaim)} recoverable</span>
              <span class="separator"></span>
              <span>${esc(group.recommended_reason)}</span>
            </div>
          </div>
          <div class="group-head-actions">
            <button class="smart-keep" type="button" data-action="smart-keep" title="Select every copy except the recommended one">
              <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8 13 4v3.6c0 3.2-2.1 5.4-5 6.6-2.9-1.2-5-3.4-5-6.6V4l5-2.2Z"/><path d="m5.9 7.8 1.4 1.4 2.8-2.8"/></svg>
              <span>Smart keep</span>
            </button>
          </div>
        </header>

        <div class="keep-note${selectedCount === group.files.length ? " is-warning" : ""}" data-keep-note>
          ${buildKeepNote(group)}
        </div>

        <table class="file-table">
          <thead>
            <tr>
              <th scope="col">Del</th>
              <th scope="col">File</th>
              <th scope="col">Size</th>
              <th scope="col">Modified</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>

        <footer class="group-foot">
          <span class="safety-lock">
            <svg viewBox="0 0 14 14" aria-hidden="true"><path d="M7 1.5 12 3.4v3.2c0 2.8-1.9 4.7-5 5.9-3.1-1.2-5-3.1-5-5.9V3.4L7 1.5Z"/><path d="m4.9 6.8 1.4 1.4 2.8-2.8"/></svg>
            ${groupHasHardlinks(group) ? "Hard-linked paths share storage" : "At least one copy always stays"}
          </span>
          <div class="group-actions">
            <button class="reveal-button" type="button" data-action="reveal" data-path="${esc(revealPath)}">Reveal in Explorer</button>
            <button class="group-delete" type="button" data-action="delete-group" ${selectedCount === 0 ? "disabled" : ""}>
              <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M6.2 2.3h3.6M4.4 4.5l.6 9.2h6l.6-9.2M6.8 7.3v3.8M9.2 7.3v3.8"/></svg>
              <span>${selectedCount === 0 ? "Select copies" : `Delete ${selectedCount}`}</span>
            </button>
          </div>
        </footer>
      </article>`;
  }

  function buildKeepNote(group) {
    const selected = getSelection(group.id);
    const remaining = remainingForGroup(group);
    const selectedCount = selected.size;
    const recommended = group.files.find((file) => file.id === group.recommended_keep_id);
    const keeper = keeperForGroup(group);
    const icon = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8 13 4v3.6c0 3.2-2.1 5.4-5 6.6-2.9-1.2-5-3.4-5-6.6V4l5-2.2Z"/><path d="m5.9 7.8 1.4 1.4 2.8-2.8"/></svg>`;

    if (!selectedCount) {
      return `${icon}<span>No copies selected yet. Tick a copy to remove; at least one copy will always stay.</span>`;
    }
    if (!remaining.length) {
      return `${icon}<span>Every copy is selected. Clear one to keep it. WeightWise will never remove an entire group.</span>`;
    }

    const keepIsRecommended = keeper && keeper.id === group.recommended_keep_id;
    const recLabel = recommended ? esc(recommended.name) : "";
    const keepName = keeper ? esc(keeper.name) : "";
    if (remaining.length === 1) {
      const why = keepIsRecommended
        ? `smart keep${group.recommended_reason ? ` — ${esc(group.recommended_reason)}` : ""}`
        : "your chosen keeper";
      return `${icon}<span><strong>Keeping <span class="keep-name" title="${esc(keeper.path)}">${keepName}</span></strong> (${why}). ${formatNumber(selectedCount)} ${selectedCount === 1 ? "copy" : "copies"} selected for deletion.</span>`;
    }

    const recommendedNote = recLabel
      ? `Recommended: <span class="keep-name">${recLabel}</span>.`
      : "";
    return `${icon}<span>${formatNumber(selectedCount)} selected for deletion · ${formatNumber(remaining.length)} copies will remain. ${recommendedNote}</span>`;
  }

  function handleGroupCheckbox(event) {
    const checkbox = event.target.closest("input[data-file-id]");
    if (!checkbox) return;
    const card = checkbox.closest("[data-group-id]");
    if (!card) return;
    const group = findGroup(card.dataset.groupId);
    if (!group) return;
    const selection = state.selections.get(group.id) || new Set();
    const fileId = checkbox.dataset.fileId;

    if (checkbox.checked) {
      if (selection.size >= group.files.length - 1 && !selection.has(fileId)) {
        checkbox.checked = false;
        toast("Keep at least one copy in every group.", "error");
        return;
      }
      selection.add(fileId);
    } else {
      selection.delete(fileId);
    }
    state.selections.set(group.id, selection);

    const row = checkbox.closest("tr");
    if (row) row.classList.toggle("is-selected", checkbox.checked);
    const remaining = remainingForGroup(group);
    card.querySelectorAll("tr[data-row-file-id]").forEach((rowNode) => {
      rowNode.classList.toggle(
        "is-keeper",
        remaining.length === 1 && rowNode.dataset.rowFileId === remaining[0].id
      );
    });

    const note = card.querySelector("[data-keep-note]");
    if (note) {
      note.innerHTML = buildKeepNote(group);
      note.classList.toggle("is-warning", selection.size === group.files.length);
    }
    const groupDelete = card.querySelector('[data-action="delete-group"]');
    if (groupDelete) {
      const count = selection.size;
      groupDelete.disabled = count === 0;
      const label = groupDelete.querySelector("span");
      if (label) label.textContent = count === 0 ? "Select copies" : `Delete ${count}`;
    }
    updateSelectionSummary();
  }

  function handleGroupAction(event) {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    const card = button.closest("[data-group-id]");
    if (!card) return;
    const group = findGroup(card.dataset.groupId);
    if (!group) return;

    if (action === "smart-keep") {
      state.selections.set(group.id, defaultsForGroup(group));
      renderGroupList();
      updateSelectionSummary();
      toast("Smart keep applied to this group.", "info");
    } else if (action === "delete-group") {
      if (!selectedCountForGroup(group)) {
        toast("Select at least one copy to remove.", "info");
        return;
      }
      openDeleteReview(group.id);
    } else if (action === "reveal") {
      revealPath(button.dataset.path || "");
    }
  }

  function findGroup(groupId) {
    return (state.result?.groups || []).find((group) => group.id === groupId) || null;
  }

  function updateSelectionSummary() {
    if (!state.result) return;
    const groups = state.result.groups || [];
    const selectedGroups = [];
    let copies = 0;
    let bytes = 0;
    let allDefault = true;

    for (const group of groups) {
      const selection = getSelection(group.id);
      if (selection.size) selectedGroups.push(group);
      copies += selection.size;
      for (const file of group.files) {
        if (selection.has(file.id)) bytes += file.size;
      }
      if (!setsEqual(selection, defaultsForGroup(group))) allDefault = false;
    }

    if (!copies) {
      el.selectionTitle.textContent = "Nothing selected";
      el.selectionSummary.textContent = "Select copies to remove. At least one copy from every group is always protected.";
    } else if (allDefault) {
      el.selectionTitle.textContent = "Smart keep is on";
      el.selectionSummary.textContent = `${formatNumber(copies)} ${copies === 1 ? "copy" : "copies"} selected across ${formatNumber(selectedGroups.length)} ${selectedGroups.length === 1 ? "group" : "groups"} · ${formatBytes(bytes)} of duplicate data.`;
    } else {
      el.selectionTitle.textContent = "Custom selection";
      el.selectionSummary.textContent = `${formatNumber(copies)} ${copies === 1 ? "copy" : "copies"} selected across ${formatNumber(selectedGroups.length)} ${selectedGroups.length === 1 ? "group" : "groups"} · ${formatBytes(bytes)} of duplicate data.`;
    }

    el.reviewDeletion.disabled = copies === 0;
    el.reviewDeletionLabel.textContent = copies
      ? `Review ${formatNumber(copies)} deletion${copies === 1 ? "" : "s"}`
      : "Review deletion";
  }

  function setsEqual(a, b) {
    if (a.size !== b.size) return false;
    for (const value of a) if (!b.has(value)) return false;
    return true;
  }

  function openDeleteReview(groupId) {
    if (!state.result) return;
    const groups = state.result.groups || [];
    const contextGroups = [];

    for (const group of groups) {
      if (groupId && group.id !== groupId) continue;
      const selection = getSelection(group.id);
      if (!selection.size) continue;
      if (selection.size >= group.files.length) {
        toast("Keep at least one copy in every group before reviewing.", "error");
        return;
      }
      contextGroups.push({ group, fileIds: new Set(selection) });
    }

    if (!contextGroups.length) {
      toast("Select at least one copy to remove.", "info");
      return;
    }

    state.deleteContext = contextGroups;
    state.deleteBusy = false;
    el.deleteError.classList.add("is-hidden");
    el.deleteError.innerHTML = "";
    renderReviewList(contextGroups);

    const totalCopies = contextGroups.reduce((sum, entry) => sum + entry.fileIds.size, 0);
    const totalBytes = contextGroups.reduce((sum, entry) => {
      return sum + entry.group.files.reduce(
        (subtotal, file) => subtotal + (entry.fileIds.has(file.id) ? file.size : 0),
        0
      );
    }, 0);
    el.reviewCopies.textContent = formatNumber(totalCopies);
    el.reviewSize.textContent = formatBytes(totalBytes);
    el.reviewGroups.textContent = formatNumber(contextGroups.length);
    el.deleteModalTitle.textContent = `Delete ${formatNumber(totalCopies)} duplicate ${totalCopies === 1 ? "copy" : "copies"}?`;
    el.deleteModalSubtitle.textContent = `${formatNumber(contextGroups.length)} ${contextGroups.length === 1 ? "group is" : "groups are"} protected. One verified copy from each will stay exactly where it is.`;

    el.reviewConfirm.checked = false;
    el.permanentConfirmInput.value = "";
    el.permanentConfirmWrap.classList.add("is-hidden");
    el.recycleMode.disabled = state.config?.can_recycle === false;
    if (state.config?.can_recycle === false) {
      el.permanentMode.checked = true;
    } else {
      el.recycleMode.checked = true;
    }
    updateDeleteMode();
    openModal("deleteModal");
  }

  function renderReviewList(contextGroups) {
    const items = contextGroups.map(({ group, fileIds }) => {
      const remaining = group.files.filter((file) => !fileIds.has(file.id));
      const keeper = remaining.find((file) => file.id === group.recommended_keep_id) || remaining[0];
      const deleted = group.files.filter((file) => fileIds.has(file.id));
      const extraKeepers = remaining.filter((file) => file.id !== keeper?.id);
      const keepNames = keeper
        ? `${esc(keeper.name)}${extraKeepers.length ? ` <span class="review-extra">+${formatNumber(extraKeepers.length)} more staying</span>` : ""}`
        : "—";
      return `
        <div class="review-group">
          <div class="review-group-head">
            <strong>${formatNumber(group.files.length)} identical · ${esc(formatBytes(group.size))} each</strong>
            <span>${formatNumber(deleted.length)} removed</span>
          </div>
          <div class="review-keep"><b>Keep</b><span class="review-name-list" title="${esc(keeper?.path || "")}">${keepNames}</span></div>
          <div class="review-delete"><b>Remove</b><span class="review-name-list">${deleted.map((file) => esc(file.name)).join(" · ")}</span></div>
        </div>`;
    });
    el.reviewList.innerHTML = items.join("");
  }

  function updateDeleteMode() {
    const permanent = el.permanentMode.checked;
    el.recycleModeCard.classList.toggle("is-selected", !permanent);
    el.permanentModeCard.classList.toggle("is-selected", permanent);
    el.permanentModeCard.classList.toggle("danger-mode", permanent);
    el.permanentConfirmWrap.classList.toggle("is-hidden", !permanent);
    el.deleteError.classList.add("is-hidden");
    if (!state.deleteBusy) {
      const count = state.deleteContext
        ? state.deleteContext.reduce((sum, entry) => sum + entry.fileIds.size, 0)
        : 0;
      el.confirmDeleteLabel.textContent = permanent
        ? `Permanently delete ${formatNumber(count)}`
        : `Move ${formatNumber(count)} to Recycle Bin`;
    }
    updateDeleteButton();
  }

  function updateDeleteButton() {
    if (!state.deleteContext) {
      el.confirmDelete.disabled = true;
      return;
    }
    const permanent = el.permanentMode.checked;
    const typed = el.permanentConfirmInput.value.trim() === "DELETE";
    const allowed = el.reviewConfirm.checked && (!permanent || typed) && !state.deleteBusy;
    el.confirmDelete.disabled = !allowed;
    if (permanent) {
      el.deleteModalTitle.textContent = "Permanently delete duplicate copies?";
    } else {
      const count = state.deleteContext.reduce((sum, entry) => sum + entry.fileIds.size, 0);
      el.deleteModalTitle.textContent = `Delete ${formatNumber(count)} duplicate ${count === 1 ? "copy" : "copies"}?`;
    }
  }

  async function submitDelete() {
    if (!state.deleteContext || state.deleteBusy || el.confirmDelete.disabled) return;
    const permanent = el.permanentMode.checked;
    const mode = permanent ? "permanent" : "recycle";
    const payloadGroups = state.deleteContext.map((entry) => ({
      group_id: entry.group.id,
      file_ids: Array.from(entry.fileIds),
    }));
    const context = state.deleteContext;
    state.deleteBusy = true;
    setDeleteBusy(true);

    try {
      const payload = await api("/api/delete", {
        method: "POST",
        body: {
          confirm: true,
          mode,
          confirm_text: permanent ? el.permanentConfirmInput.value.trim() : "",
          groups: payloadGroups,
        },
      });
      state.deleteBusy = false;
      setDeleteBusy(false);
      state.deleteContext = null;
      closeModal("deleteModal", true);
      clearAlert();

      if (payload.result) {
        state.result = payload.result;
        initializeSelections();
        renderStats();
        renderOutcome({ scroll: false });
        updateSelectionSummary();
      }

      const deleted = Number(payload.deleted_count || 0);
      const failed = Number(payload.failed_count || 0);
      const resultGroups = state.result?.groups?.length || 0;
      if (payload.mode === "permanent") {
        const freed = formatBytes(payload.freed_estimate_bytes || 0);
        toast(
          failed
            ? `Permanently deleted ${deleted} copies · ${failed} could not be removed.`
            : `Permanently deleted ${deleted} ${deleted === 1 ? "copy" : "copies"} · ${freed} freed.`,
          failed ? "error" : "success"
        );
      } else {
        toast(
          failed
            ? `Moved ${deleted} copies to the Recycle Bin · ${failed} could not be removed.`
            : `Moved ${deleted} ${deleted === 1 ? "copy" : "copies"} to the Recycle Bin. Empty the bin to free the space.`,
          failed ? "error" : "success"
        );
      }

      if (failed) {
        setEngine("Deletion partially failed");
        showAlert(
          "Some copies could not be removed.",
          (payload.errors || []).map((error) => `${error.path}: ${error.error}`),
          "error"
        );
      } else {
        setEngine(resultGroups ? "Duplicates reduced" : "Library clean");
      }
      if (!resultGroups) {
        toast("No duplicate groups remain in this library.", "info");
      }
    } catch (error) {
      state.deleteBusy = false;
      setDeleteBusy(false);
      const details = Array.isArray(error.details)
        ? error.details.map((detail) => `${detail.path || "File"}: ${detail.error || ""}`)
        : [];
      showDeleteError(
        error.status === 409 ? "Nothing was deleted." : "Deletion could not start.",
        error.message,
        details
      );
      toast(error.message, "error");
    }
  }

  function setDeleteBusy(busy) {
    el.cancelDelete.disabled = busy;
    el.reviewConfirm.disabled = busy;
    el.permanentConfirmInput.disabled = busy;
    el.recycleMode.disabled = busy || state.config?.can_recycle === false;
    el.permanentMode.disabled = busy;
    el.deleteModal.querySelectorAll(".modal-close").forEach((button) => {
      button.disabled = busy;
    });
    el.deleteSpinner.classList.toggle("is-hidden", !busy);
    if (busy) {
      el.confirmDelete.disabled = true;
      el.confirmDeleteLabel.textContent = "Verifying & deleting…";
    } else {
      updateDeleteButton();
    }
  }

  function showDeleteError(title, message, details) {
    el.deleteError.classList.remove("is-hidden");
    const detailHtml = details.length
      ? `<ul>${details
          .slice(0, 5)
          .map((detail) => `<li>${esc(detail)}</li>`)
          .join("")}</ul>`
      : "";
    el.deleteError.innerHTML = `<strong>${esc(title)}</strong>${esc(message)}${detailHtml}`;
  }

  function openBrowser() {
    openModal("browserModal");
    el.browserError.classList.add("is-hidden");
    el.browserError.textContent = "";
    el.browserPathInput.value = "";
    el.browserCurrent.textContent = "—";
    el.browserChoose.disabled = true;
    el.browserUp.disabled = true;
    el.browserUp.dataset.parent = "";
    el.browserList.innerHTML = '<div class="browser-empty">Reading folders…</div>';
    const start = (el.directoryInput.value.trim() || state.result?.root || state.config?.default_root || "").replace(/^"(.*)"$/, "$1");
    const initialLoad = loadBrowser(start);
    const initialRequestId = state.browserRequestId;
    initialLoad.then((ok) => {
      if (!ok && state.config?.default_root && start !== state.config.default_root && state.browserRequestId === initialRequestId) {
        loadBrowser(state.config.default_root);
      }
    });
  }

  async function loadBrowser(path) {
    const requestId = ++state.browserRequestId;
    el.browserChoose.disabled = true;
    el.browserError.classList.add("is-hidden");
    el.browserList.innerHTML = '<div class="browser-empty">Reading folders…</div>';
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : "";
      const payload = await api(`/api/browse${query}`);
      if (requestId !== state.browserRequestId) return false;
      state.browserPath = payload.path;
      el.browserPathInput.value = payload.path;
      el.browserCurrent.textContent = payload.path;
      el.browserCurrent.title = payload.path;
      el.browserPathInput.title = payload.path;
      el.browserUp.disabled = !payload.parent;
      el.browserUp.dataset.parent = payload.parent || "";
      el.browserChoose.disabled = false;
      renderBrowserQuick(payload);
      renderBrowserList(payload);
      return true;
    } catch (error) {
      if (requestId !== state.browserRequestId) return false;
      state.browserPath = null;
      showBrowserError(error.message);
      el.browserList.innerHTML = '<div class="browser-empty">Folder could not be opened.</div>';
      return false;
    }
  }

  function renderBrowserQuick(payload) {
    const entries = [];
    const seen = new Set();
    for (const entry of payload.quick_access || []) {
      const key = entry.path.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      entries.push(entry);
    }
    for (const drive of payload.drives || []) {
      const key = drive.path.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      entries.push({ label: drive.label, path: drive.path });
    }
    el.browserQuick.innerHTML = entries
      .map(
        (entry) =>
          `<button class="quick-chip" type="button" data-browser-path="${esc(entry.path)}" title="${esc(entry.path)}">${esc(entry.label)}</button>`
      )
      .join("");
  }

  function renderBrowserList(payload) {
    const directories = payload.directories || [];
    if (!directories.length) {
      el.browserList.innerHTML = '<div class="browser-empty">This folder has no subfolders.</div>';
      return;
    }
    el.browserList.innerHTML = directories
      .map(
        (directory) => `
          <button class="browser-row" type="button" data-folder-path="${esc(directory.path)}" title="${esc(directory.path)}">
            <svg viewBox="0 0 18 18" aria-hidden="true"><path d="M2.4 5.1a1.3 1.3 0 0 1 1.3-1.3h3.2l1.3 1.6h5.1a1.3 1.3 0 0 1 1.3 1.3v5.7a1.3 1.3 0 0 1-1.3 1.3H3.7a1.3 1.3 0 0 1-1.3-1.3V5.1Z"/></svg>
            <span>${esc(directory.name)}</span>
            <small>open →</small>
          </button>`
      )
      .join("");
    if (payload.truncated) {
      el.browserList.insertAdjacentHTML(
        "beforeend",
        '<div class="browser-empty" style="height:auto;padding:10px;font-size:9px">Showing the first 400 folders. Type a path to go deeper.</div>'
      );
    }
  }

  function showBrowserError(message) {
    el.browserError.textContent = message;
    el.browserError.classList.remove("is-hidden");
    el.browserChoose.disabled = true;
    el.browserCurrent.textContent = "—";
  }

  function chooseBrowserFolder() {
    if (!state.browserPath) return;
    el.directoryInput.value = state.browserPath;
    safeSet(ROOT_KEY, state.browserPath);
    closeModal("browserModal");
    el.directoryInput.focus();
  }

  function openModal(id) {
    const layer = document.getElementById(id);
    if (!layer) return;
    if (state.activeModal && state.activeModal !== id) {
      closeModal(state.activeModal, true);
    }
    state.lastFocused = document.activeElement;
    state.activeModal = id;
    layer.classList.remove("is-hidden");
    layer.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    window.setTimeout(() => {
      const modal = layer.querySelector(".modal");
      if (modal) modal.focus({ preventScroll: true });
    }, 20);
  }

  function closeModal(id, force = false) {
    if (!id) return;
    if (id === "deleteModal" && state.deleteBusy && !force) return;
    const layer = document.getElementById(id);
    if (!layer || layer.classList.contains("is-hidden")) return;
    layer.classList.add("is-hidden");
    layer.setAttribute("aria-hidden", "true");
    if (state.activeModal === id) {
      state.activeModal = null;
      document.body.classList.remove("modal-open");
      const target = state.lastFocused;
      state.lastFocused = null;
      if (target && typeof target.focus === "function" && document.contains(target)) {
        target.focus({ preventScroll: true });
      }
    }
  }

  function handleGlobalKeydown(event) {
    if (event.key === "Escape" && state.activeModal) {
      closeModal(state.activeModal);
      return;
    }
    if (event.key !== "Tab" || !state.activeModal) return;
    const layer = document.getElementById(state.activeModal);
    if (!layer) return;
    const focusable = Array.from(
      layer.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((node) => node.offsetParent !== null);
    if (!focusable.length) {
      event.preventDefault();
      const modal = layer.querySelector(".modal");
      if (modal) modal.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey) {
      if (active === first || !layer.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !layer.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  }

  function updateScopeNote() {
    const weightsOnly = el.weightsOnlyOption.checked;
    el.scopeNote.classList.toggle("is-warning", !weightsOnly);
    el.scopeNoteText.textContent = weightsOnly
      ? "Comparing model weight formats only. Switch this off to compare every file."
      : "Every file type will be compared — including configs, cache files and anything else in the folder.";
  }

  async function revealPath(path) {
    if (!path) return;
    try {
      await api("/api/reveal", { method: "POST", body: { path } });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function setEngine(text) {
    el.engineStatus.textContent = text;
  }

  function showAlert(title, details = [], tone = "error") {
    el.alertBanner.classList.remove("is-hidden", "info");
    if (tone === "info") el.alertBanner.classList.add("info");
    el.alertBanner.innerHTML = "";

    const heading = document.createElement("strong");
    heading.textContent = title;
    el.alertBanner.appendChild(heading);

    const cleanDetails = details.filter(Boolean).map(String);
    if (cleanDetails.length === 1) {
      const text = document.createElement("span");
      text.textContent = cleanDetails[0];
      el.alertBanner.appendChild(text);
    } else if (cleanDetails.length > 1) {
      const list = document.createElement("ul");
      for (const detail of cleanDetails.slice(0, 6)) {
        const item = document.createElement("li");
        item.textContent = detail;
        list.appendChild(item);
      }
      el.alertBanner.appendChild(list);
    }

    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "alert-close";
    closeButton.setAttribute("aria-label", "Dismiss alert");
    closeButton.innerHTML = '<svg viewBox="0 0 18 18" aria-hidden="true"><path d="m4 4 10 10M14 4 4 14"/></svg>';
    closeButton.addEventListener("click", clearAlert);
    el.alertBanner.appendChild(closeButton);
  }

  function clearAlert() {
    el.alertBanner.classList.add("is-hidden");
    el.alertBanner.classList.remove("info");
    el.alertBanner.innerHTML = "";
  }

  function toast(message, type = "success", duration = 4200) {
    const node = document.createElement("div");
    node.className = `toast${type === "error" ? " is-error" : type === "info" ? " is-info" : ""}`;
    node.textContent = message;
    el.toastStack.appendChild(node);
    while (el.toastStack.children.length > 4) {
      el.toastStack.firstElementChild?.remove();
    }
    window.setTimeout(() => {
      node.classList.add("is-leaving");
      window.setTimeout(() => node.remove(), 220);
    }, duration);
  }

  function loadRecentRoots() {
    try {
      const parsed = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
      if (Array.isArray(parsed)) {
        state.recent = parsed.filter((item) => typeof item === "string").slice(0, 5);
      }
    } catch (_) {
      state.recent = [];
    }
    renderRecentRoots();
  }

  function rememberRoot(root) {
    if (!root) return;
    safeSet(ROOT_KEY, root);
    state.recent = [root, ...state.recent.filter((item) => item.toLowerCase() !== root.toLowerCase())].slice(0, 5);
    safeSet(RECENT_KEY, JSON.stringify(state.recent));
    renderRecentRoots();
  }

  function renderRecentRoots() {
    if (!state.recent.length) {
      el.recentRow.classList.add("is-hidden");
      return;
    }
    el.recentRow.classList.remove("is-hidden");
    el.recentPaths.innerHTML = state.recent
      .map(
        (root) =>
          `<button class="recent-path" type="button" data-recent-path="${esc(root)}" title="${esc(root)}">${esc(root)}</button>`
      )
      .join("");
  }

  function safeGet(key) {
    try {
      return localStorage.getItem(key) || "";
    } catch (_) {
      return "";
    }
  }

  function safeSet(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_) {
      /* Storage is optional. */
    }
  }

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const amount = bytes / 1024 ** exponent;
    let digits = 0;
    if (amount < 10) digits = 2;
    else if (amount < 100) digits = 1;
    return `${amount.toFixed(digits).replace(/\.0+$/, "").replace(/(\.\d)0$/, "$1")} ${units[exponent]}`;
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString();
  }

  function formatDuration(milliseconds) {
    const ms = Math.max(0, Number(milliseconds) || 0);
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const seconds = ms / 1000;
    if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return `${minutes}m ${rest}s`;
  }

  const dateFormatter = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

  function formatDate(milliseconds) {
    try {
      return dateFormatter.format(new Date(milliseconds));
    } catch (_) {
      return "—";
    }
  }

  function formatDateTime(milliseconds) {
    try {
      return dateTimeFormatter.format(new Date(milliseconds));
    } catch (_) {
      return "—";
    }
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  const escapeMap = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => escapeMap[character]);
  }
})();

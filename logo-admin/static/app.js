(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const csrfToken = $("meta[name='csrf-token']")?.content || "";
  const state = {
    stores: [],
    store: "",
    styles: [],
    style: "",
    styleRecord: null,
    detail: null,
    editing: null,
    selectedDesign: null,
    designDetail: null,
    requestSequence: { styles: 0, designs: 0, designDetail: 0 },
    vocab: null,
    audit: { beforeId: null, filters: {} },
  };

  const els = {
    storeSearch: $("#store-search"),
    storeOptions: $("#store-options"),
    styleSearch: $("#style-search"),
    styleActiveOnly: $("#style-active-only"),
    styleAssignedOnly: $("#style-assigned-only"),
    styleOptions: $("#style-options"),
    selectionStatus: $("#selection-status"),
    empty: $("#empty-state"),
    workspace: $("#style-workspace"),
    grid: $("#assignment-grid"),
    styleTitle: $("#style-title"),
    styleSummary: $("#style-summary"),
    styleBadge: $("#style-state-badge"),
    ownershipWarning: $("#ownership-warning"),
    settingsForm: $("#settings-form"),
    settingEnabled: $("#setting-enabled"),
    settingAllowsNone: $("#setting-allows-none"),
    styleActive: $("#style-active"),
    exportLink: $("#export-link"),
    exportScope: $("#export-scope"),
    storeSettingsDialog: $("#store-settings-dialog"),
    storeSettingsButton: $("#store-settings-button"),
    storeSettingsContext: $("#store-settings-context"),
    assignmentDialog: $("#assignment-dialog"),
    assignmentForm: $("#assignment-form"),
    assignmentTitle: $("#assignment-dialog-title"),
    assignmentContext: $("#assignment-context"),
    designSearch: $("#design-search"),
    designOptions: $("#design-options"),
    designId: $("#assignment-design-id"),
    selectedDesign: $("#selected-design"),
    selectedDesignName: $("#selected-design-name"),
    selectedDesignMeta: $("#selected-design-meta"),
    designPreview: $("#design-preview"),
    scheme: $("#assignment-scheme"),
    logoCode: $("#assignment-logo-code"),
    location: $("#assignment-location"),
    placementVocabOptions: $("#placement-vocab-options"),
    background: $("#assignment-background"),
    nameOverride: $("#assignment-name-override"),
    cost: $("#assignment-cost"),
    sort: $("#assignment-sort"),
    optional: $("#assignment-optional"),
    active: $("#assignment-active"),
    imageUrl: $("#assignment-image-url"),
    upload: $("#assignment-upload"),
    uploadStatus: $("#upload-status"),
    applyColors: $("#apply-colors-button"),
    softRemove: $("#soft-remove-button"),
    hardRemove: $("#hard-remove-button"),
    copyDialog: $("#copy-dialog"),
    copyForm: $("#copy-form"),
    copyStore: $("#copy-store"),
    copySearch: $("#copy-style-search"),
    copySource: $("#copy-source-style"),
    copyOptions: $("#copy-style-options"),
    copyOverwrite: $("#copy-overwrite"),
    importFile: $("#import-file"),
    importDialog: $("#import-dialog"),
    importResults: $("#import-results"),
    legacyDialog: $("#legacy-dialog"),
    legacyForm: $("#legacy-form"),
    legacyFile: $("#legacy-file"),
    legacyOverwrite: $("#legacy-overwrite"),
    legacyResults: $("#legacy-results"),
    reportsDialog: $("#reports-dialog"),
    reportFilters: $("#report-filters"),
    reportStore: $("#report-store"),
    reportReason: $("#report-reason"),
    reportResults: $("#report-results"),
    reportCount: $("#report-count"),
    auditDialog: $("#audit-dialog"),
    auditFilters: $("#audit-filters"),
    auditStore: $("#audit-store"),
    auditStyle: $("#audit-style"),
    auditActor: $("#audit-actor"),
    auditAction: $("#audit-action"),
    auditResults: $("#audit-results"),
    auditMore: $("#audit-more"),
    auditCount: $("#audit-count"),
    auditExport: $("#audit-export"),
    syncDialog: $("#sync-dialog"),
    syncResults: $("#sync-results"),
    confirmDialog: $("#confirm-dialog"),
    confirmTitle: $("#confirm-title"),
    confirmMessage: $("#confirm-message"),
    confirmAction: $("#confirm-action"),
    confirmCancel: $("#confirm-cancel"),
    toastRegion: $("#toast-region"),
  };

  function text(value, fallback = "") {
    return value === null || value === undefined ? fallback : String(value);
  }

  function bool(value, fallback = false) {
    if (value === null || value === undefined) return fallback;
    if (typeof value === "string") return ["1", "true", "t", "yes", "on"].includes(value.toLowerCase());
    return Boolean(value);
  }

  function escapeHtml(value) {
    return text(value).replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[char]);
  }

  function envelope(payload, key) {
    if (Array.isArray(payload)) return payload;
    return Array.isArray(payload?.[key]) ? payload[key] : [];
  }

  function errorMessage(payload, fallback) {
    const detail = payload?.detail ?? payload?.error ?? payload?.message;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : "";
          const msg = item.msg || JSON.stringify(item);
          return field && field !== "body" ? `${field}: ${msg}` : msg;
        })
        .join("; ");
    }
    if (typeof detail === "object" && detail) return detail.message || JSON.stringify(detail);
    return text(detail, fallback);
  }

  async function api(path, options = {}) {
    const init = { credentials: "same-origin", ...options };
    const headers = new Headers(init.headers || {});
    if (csrfToken && !["GET", "HEAD"].includes((init.method || "GET").toUpperCase())) {
      headers.set("X-CSRF-Token", csrfToken);
    }
    if (init.body && !(init.body instanceof FormData) && typeof init.body !== "string") {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(init.body);
    }
    headers.set("Accept", "application/json");
    init.headers = headers;

    const response = await fetch(path, init);
    if (response.status === 401) {
      window.location.assign("/login?expired=1");
      throw new Error("Your session expired. Please sign in again.");
    }
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      const fallback = response.status >= 500
        ? "Something went wrong on the server. Please try again."
        : `The request could not be completed (error ${response.status}).`;
      const error = new Error(errorMessage(payload, fallback));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function debounce(fn, delay = 220) {
    let timeout;
    return (...args) => {
      window.clearTimeout(timeout);
      timeout = window.setTimeout(() => fn(...args), delay);
    };
  }

  function setBusy(button, busy, label = "Working...") {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.innerHTML;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.innerHTML = `<span class="spinner" aria-hidden="true"></span>${escapeHtml(label)}`;
    } else {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      if (button.dataset.originalLabel) button.innerHTML = button.dataset.originalLabel;
      delete button.dataset.originalLabel;
    }
  }

  function toast(message, type = "success") {
    const node = document.createElement("div");
    node.className = `toast${type === "error" ? " toast--error" : ""}`;
    node.setAttribute("role", type === "error" ? "alert" : "status");
    const content = document.createElement("span");
    content.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss notification");
    close.textContent = "×";
    close.addEventListener("click", () => node.remove());
    node.append(content, close);
    els.toastRegion.append(node);
    // Errors stay until dismissed so staff can read what went wrong;
    // successes auto-hide.
    if (type !== "error") window.setTimeout(() => node.remove(), 6500);
  }

  // Standard list error state: friendly message + a working Retry button.
  function renderErrorState(container, message, retryFn) {
    if (!container) return;
    container.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "grid-empty";
    const line = document.createElement("div");
    line.textContent = message;
    wrap.append(line);
    if (retryFn) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "button button--ghost button--small";
      retry.style.marginTop = "8px";
      retry.textContent = "Try again";
      retry.addEventListener("click", () => retryFn());
      wrap.append(retry);
    }
    container.append(wrap);
  }

  function friendlyLoadError(what, error) {
    const raw = text(error?.message, "");
    return `Couldn't load ${what}. ${raw ? raw + " " : ""}Check your connection and try again.`;
  }

  function openDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  }

  function confirmAction({ title = "Confirm action", message, actionLabel = "Continue", danger = true }) {
    els.confirmTitle.textContent = title;
    els.confirmMessage.textContent = message;
    els.confirmAction.textContent = actionLabel;
    els.confirmAction.className = danger ? "button button--danger" : "button button--primary";
    openDialog(els.confirmDialog);
    return new Promise((resolve) => {
      const finish = (answer) => {
        els.confirmAction.removeEventListener("click", yes);
        els.confirmCancel.removeEventListener("click", no);
        els.confirmDialog.removeEventListener("cancel", no);
        closeDialog(els.confirmDialog);
        resolve(answer);
      };
      const yes = () => finish(true);
      const no = (event) => { event?.preventDefault(); finish(false); };
      els.confirmAction.addEventListener("click", yes);
      els.confirmCancel.addEventListener("click", no);
      els.confirmDialog.addEventListener("cancel", no);
    });
  }

  function setOptionsOpen(input, list, open) {
    list.hidden = !open;
    input.setAttribute("aria-expanded", String(open));
    const activeId = input.getAttribute("aria-activedescendant");
    if (open && activeId) {
      const active = document.getElementById(activeId);
      if (!active || !list.contains(active)) input.removeAttribute("aria-activedescendant");
    }
    if (!open) {
      input.removeAttribute("aria-activedescendant");
      $$('[role="option"]', list).forEach((option) => {
        option.setAttribute("aria-selected", "false");
      });
    }
  }

  function bindListKeyboard(input, list) {
    if (!list.id) list.id = `combobox-list-${Math.random().toString(36).slice(2)}`;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("aria-controls", list.id);
    input.setAttribute("aria-expanded", String(!list.hidden));
    list.setAttribute("role", "listbox");
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOptionsOpen(input, list, false);
        return;
      }
      const options = $$("[role='option']:not(.option--empty)", list);
      if (!options.length) return;
      let index = options.findIndex((option) => option.getAttribute("aria-selected") === "true");
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        if (event.key === "Home") index = 0;
        else if (event.key === "End") index = options.length - 1;
        else if (event.key === "ArrowDown") index = Math.min(index + 1, options.length - 1);
        else index = index < 0 ? options.length - 1 : Math.max(index - 1, 0);
        options.forEach((option, i) => option.setAttribute("aria-selected", String(i === index)));
        input.setAttribute("aria-activedescendant", options[index].id);
        options[index].scrollIntoView({ block: "nearest" });
        setOptionsOpen(input, list, true);
      } else if (event.key === "Enter" && index >= 0 && !list.hidden) {
        event.preventDefault();
        options[index].click();
      }
    });
  }

  function appendOption(list, { title, subtitle = "", meta = "", onSelect }) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "option";
    option.id = `${list.id || "combobox-option"}-option-${list.childElementCount}`;
    option.tabIndex = -1;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    const label = document.createElement("span");
    const strong = document.createElement("strong");
    strong.textContent = title;
    label.append(strong);
    if (subtitle) {
      const small = document.createElement("small");
      small.textContent = subtitle;
      label.append(small);
    }
    option.append(label);
    if (meta) {
      const metadata = document.createElement("span");
      metadata.className = "option__meta";
      metadata.textContent = meta;
      option.append(metadata);
    }
    option.addEventListener("click", onSelect);
    list.append(option);
  }

  function showEmptyOption(list, message) {
    list.replaceChildren();
    const option = document.createElement("div");
    option.className = "option option--empty";
    option.id = `${list.id || "combobox-option"}-empty`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-disabled", "true");
    option.textContent = message;
    list.append(option);
  }

  function storeCode(record) {
    return text(typeof record === "string" ? record : record.fdm4_store ?? record.store ?? record.code).trim();
  }

  function styleCode(record) {
    return text(typeof record === "string" ? record : record.product_style ?? record.style ?? record.code).trim();
  }

  function styleName(record) {
    return text(record?.name ?? record?.description ?? record?.product_name).trim();
  }

  function storeDisplay(store) {
    return text(store?.display_name) || storeCode(store);
  }

  function storeByCode(code) {
    return state.stores.find((item) => storeCode(item) === code) || null;
  }

  function storeDisplayFor(code) {
    const record = storeByCode(code);
    return record ? storeDisplay(record) : code;
  }

  function storeBlogTag(store) {
    const ids = text(store?.blog_ids);
    if (!ids) return "";
    const path = text(store?.blog_path);
    return `blog ${ids}${path && path !== "/" ? ` · ${path}` : ""}`;
  }

  function storeMeta(store) {
    const tag = storeBlogTag(store);
    return tag ? `${storeCode(store)} · ${tag}` : storeCode(store);
  }

  function storeInputLabel(store) {
    return `${storeDisplay(store)} - ${storeMeta(store)}`;
  }

  function renderStoreOptions(query = "") {
    const q = query.trim().toLowerCase();
    const matches = state.stores.filter((store) => {
      if (!q) return true;
      return storeDisplay(store).toLowerCase().includes(q) || storeCode(store).toLowerCase().includes(q);
    });
    els.storeOptions.replaceChildren();
    if (!matches.length) {
      showEmptyOption(els.storeOptions, "No matching stores");
    } else {
      matches.forEach((store) => {
        const styles = Number(store?.assigned_styles ?? store?.style_count ?? 0);
        const assignments = Number(store?.assignment_count ?? 0);
        appendOption(els.storeOptions, {
          title: storeDisplay(store),
          subtitle: storeMeta(store),
          meta: styles ? `${styles} style${styles === 1 ? "" : "s"} · ${assignments} assignment${assignments === 1 ? "" : "s"}` : "no logos configured yet",
          onSelect: async () => {
            els.storeSearch.value = storeInputLabel(store);
            setOptionsOpen(els.storeSearch, els.storeOptions, false);
            await selectStore(storeCode(store));
          },
        });
      });
    }
    setOptionsOpen(els.storeSearch, els.storeOptions, true);
  }

  async function loadStores() {
    try {
      const payload = await api("/api/stores");
      state.stores = envelope(payload, "stores");
      els.storeSearch.disabled = false;
      els.storeSearch.placeholder = "Search store name or code";
      populateReportStores();
      els.selectionStatus.textContent = `${state.stores.length} storefront${state.stores.length === 1 ? "" : "s"} available.`;

      const params = new URLSearchParams(window.location.search);
      const requestedStore = params.get("store") || window.localStorage.getItem("logoAdminStore") || "";
      const requestedRecord = state.stores.find((item) => storeCode(item) === requestedStore);
      if (requestedRecord) {
        els.storeSearch.value = storeInputLabel(requestedRecord);
        await selectStore(requestedStore);
        const requestedStyle = params.get("style");
        if (requestedStyle) await selectStyle({ product_style: requestedStyle });
      }
    } catch (error) {
      els.storeSearch.placeholder = "Unable to load stores";
      els.selectionStatus.textContent = error.message;
      toast(error.message, "error");
    }
  }

  // Reusable searchable store dropdown (same combobox pattern as the main
  // store picker). `hidden` keeps the selected code, so existing reads of
  // els.<x>.value keep working unchanged.
  function attachStoreCombobox({ search, hidden, options, allLabel = "", onPick = null }) {
    const searchEl = $(search), hiddenEl = $(hidden), optionsEl = $(options);
    if (!searchEl || !hiddenEl || !optionsEl || searchEl.dataset.comboAttached) return;
    searchEl.dataset.comboAttached = "1";
    let pickedLabel = "";
    const pick = (code, label) => {
      pickedLabel = code ? label : "";
      hiddenEl.value = code;
      searchEl.value = pickedLabel;
      setOptionsOpen(searchEl, optionsEl, false);
      if (onPick) onPick(code);
    };
    const render = (query = "") => {
      const q = query.trim().toLowerCase();
      optionsEl.replaceChildren();
      if (allLabel && (!q || allLabel.toLowerCase().includes(q))) {
        appendOption(optionsEl, { title: allLabel, subtitle: "", meta: "", onSelect: () => pick("", "") });
      }
      const matches = (state.stores || []).filter((s) => !q || storeDisplay(s).toLowerCase().includes(q) || storeMeta(s).toLowerCase().includes(q));
      if (!matches.length && !allLabel) { showEmptyOption(optionsEl, "No matching stores"); }
      matches.forEach((s) => appendOption(optionsEl, {
        title: storeDisplay(s), subtitle: storeMeta(s), meta: "",
        onSelect: () => pick(storeCode(s), `${storeDisplay(s)} (${storeMeta(s)})`),
      }));
      setOptionsOpen(searchEl, optionsEl, true);
    };
    searchEl.addEventListener("input", () => {
      // Any edit away from the picked label voids the hidden selection -
      // otherwise "pick Lewis, then retype Mariani" silently submits Lewis.
      if (searchEl.value !== pickedLabel) { hiddenEl.value = ""; pickedLabel = ""; }
      render(searchEl.value);
    });
    searchEl.addEventListener("focus", () => { searchEl.select(); render(""); });
    searchEl.addEventListener("blur", () => setTimeout(() => setOptionsOpen(searchEl, optionsEl, false), 200));
    bindListKeyboard(searchEl, optionsEl);
  }

  function populateReportStores() {
    attachStoreCombobox({ search: "#report-store-search", hidden: "#report-store", options: "#report-store-options", allLabel: "All stores" });
    attachStoreCombobox({ search: "#audit-store-search", hidden: "#audit-store", options: "#audit-store-options", allLabel: "All stores" });
    attachStoreCombobox({
      search: "#header-store-search", hidden: "#header-store-value", options: "#header-store-options",
      onPick: (code) => { if (code && code !== state.store) selectStore(code); },
    });
    syncHeaderStore();
  }

  function syncHeaderStore() {
    const el = $("#header-store-search");
    if (el && document.activeElement !== el) {
      el.value = state.store ? `${storeDisplayFor(state.store)} (${state.store})` : "";
    }
    const hv = $("#header-store-value");
    if (hv) hv.value = state.store || "";
  }

  // Mirror the global active store into every store-scoped selector's display
  // (header, Logo Configuration, Logo Names, Product Mix). Focused inputs are
  // left alone so we never fight the user's typing.
  function syncStorePickers() {
    syncHeaderStore();
    const label = state.store ? `${storeDisplayFor(state.store)} (${state.store})` : "";
    [["#store-search", null], ["#names-store-search", "#names-store"], ["#mix-store-search", "#mix-store"]].forEach(([searchSel, hiddenSel]) => {
      const searchEl = $(searchSel);
      if (searchEl && document.activeElement !== searchEl) searchEl.value = label;
      if (hiddenSel) { const hiddenEl = $(hiddenSel); if (hiddenEl) hiddenEl.value = state.store || ""; }
    });
  }

  async function selectStore(store) {
    state.store = store;
    syncStorePickers();
    state.style = "";
    state.styleRecord = null;
    state.detail = null;
    state.styles = [];
    els.styleSearch.value = "";
    els.styleSearch.disabled = !store;
    els.styleSearch.placeholder = store ? "Search style code or name" : "Choose a store first";
    els.storeSettingsButton.disabled = !store;
    els.exportLink.href = store ? `/api/export?${new URLSearchParams({ store })}` : "/api/export";
    els.exportScope.textContent = store ? `CSV for ${storeDisplayFor(store)}` : "CSV for all stores";
    els.empty.hidden = false;
    els.workspace.hidden = true;
    els.ownershipWarning.hidden = true;
    // A Bulk Apply preview belongs to the store it was built for - clear it so
    // a stale preview can't be applied against the newly selected store.
    const bulkPanel = $("#bulk-apply-panel");
    if (bulkPanel && !bulkPanel.hidden) {
      bulkPanel.hidden = true;
      const tbody = document.querySelector("#bulk-preview-table tbody");
      if (tbody) tbody.replaceChildren();
      const wrap = $("#bulk-preview-table-wrap");
      if (wrap) wrap.hidden = true;
      const summary = $("#bulk-preview-summary");
      if (summary) summary.textContent = "";
      const result = $("#bulk-result");
      if (result) result.textContent = "";
      const bulkApplyBtn = $("#bulk-apply-btn");
      if (bulkApplyBtn) bulkApplyBtn.disabled = true;
    }
    setOptionsOpen(els.styleSearch, els.styleOptions, false);
    updateUrl();
    if (document.body.dataset.view === "names") { namesState.offset = 0; loadNames(); }
    if (store && (mixState.store || "") !== store) {
      if (document.body.dataset.view === "mix") {
        mixSelectStore(store);
      } else {
        // Quiet adoption: the mix view re-renders itself on next open.
        mixState.store = store;
        mixState.offset = 0;
        mixState.q = "";
        mixState.selected = new Set();
      }
    }
    if (!store) return;
    window.localStorage.setItem("logoAdminStore", store);
    els.selectionStatus.textContent = `Loading styles for ${storeDisplayFor(store)}...`;
    await searchStyles("");
    els.selectionStatus.textContent = state.styles.length === 0 && els.styleAssignedOnly?.checked
      ? `No styles with logos yet for ${storeDisplayFor(store)} - untick "Assigned only" to browse the full catalog.`
      : `${state.styles.length} style${state.styles.length === 1 ? "" : "s"} found for ${storeDisplayFor(store)}.`;
  }

  async function searchStyles(query, target = "main") {
    if (!state.store) return [];
    const sequence = ++state.requestSequence.styles;
    const params = new URLSearchParams({ store: state.store });
    if (query.trim()) params.set("q", query.trim());
    params.set("active_only", els.styleActiveOnly?.checked === false ? "false" : "true");
    params.set("assigned_only", els.styleAssignedOnly?.checked === false ? "false" : "true");
    try {
      const payload = await api(`/api/styles?${params}`);
      if (sequence !== state.requestSequence.styles) return [];
      const styles = envelope(payload, "styles");
      if (target === "main") {
        state.styles = styles;
        renderStyleOptions(styles, els.styleOptions, els.styleSearch, selectStyle);
      } else {
        renderStyleOptions(styles.filter((item) => styleCode(item) !== state.style), els.copyOptions, els.copySearch, (record) => {
          els.copySource.value = styleCode(record);
          els.copySearch.value = `${styleCode(record)}${styleName(record) ? ` - ${styleName(record)}` : ""}`;
          setOptionsOpen(els.copySearch, els.copyOptions, false);
        });
      }
      return styles;
    } catch (error) {
      const list = target === "main" ? els.styleOptions : els.copyOptions;
      showEmptyOption(list, error.message);
      setOptionsOpen(target === "main" ? els.styleSearch : els.copySearch, list, true);
      return [];
    }
  }

  function renderStyleOptions(styles, list, input, select) {
    list.replaceChildren();
    if (!styles.length) {
      showEmptyOption(list, "No matching styles");
    } else {
      styles.forEach((record) => {
        const assignments = Number(record?.assignment_count || 0);
        const colors = Number(record?.color_count || 0);
        appendOption(list, {
          title: styleCode(record),
          subtitle: styleName(record),
          meta: assignments ? `${assignments} assignment${assignments === 1 ? "" : "s"}` : colors ? `${colors} colors` : "",
          onSelect: () => { select(record); setOptionsOpen(input, list, false); },
        });
      });
    }
    setOptionsOpen(input, list, true);
  }

  async function selectStyle(record) {
    const code = styleCode(record);
    if (!code || !state.store) return;
    setOptionsOpen(els.styleSearch, els.styleOptions, false);
    state.style = code;
    state.styleRecord = record;
    els.styleSearch.value = `${code}${styleName(record) ? ` - ${styleName(record)}` : ""}`;
    updateUrl();
    els.empty.hidden = true;
    els.workspace.hidden = false;
    els.styleTitle.textContent = code;
    els.styleSummary.textContent = styleName(record) || storeDisplayFor(state.store);
    els.grid.innerHTML = '<div class="grid-loading"><span class="spinner" aria-hidden="true"></span> Loading assignments...</div>';
    loadProductLink(state.store, code);
    await refreshStyle();
  }

  // Turn the style title into a link to the live product page on the sync
  // target (dev now, prod after cutover). Lazy and soft-failing: the title
  // renders immediately as text; the link attaches when WordPress answers.
  async function loadProductLink(store, style) {
    try {
      const result = await api(`/api/product-link?${new URLSearchParams({ store, style })}`);
      const url = text(result.view_url).trim();
      if (!url || state.store !== store || state.style !== style) return;
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.target = "_blank";
      anchor.rel = "noopener";
      anchor.className = "style-title-link";
      anchor.textContent = style;
      anchor.title = `Open this product on ${wpTargetHost()} in a new tab`;
      const hint = document.createElement("span");
      hint.className = "style-title-link__hint";
      hint.setAttribute("aria-hidden", "true");
      hint.textContent = "↗";
      anchor.append(hint);
      els.styleTitle.replaceChildren(anchor);
    } catch {
      // No link - WordPress unreachable or no published product for the style.
    }
  }

  function updateUrl() {
    const url = new URL(window.location.href);
    if (state.store) url.searchParams.set("store", state.store); else url.searchParams.delete("store");
    if (state.style) url.searchParams.set("style", state.style); else url.searchParams.delete("style");
    window.history.replaceState({}, "", url);
  }

  async function refreshStyle() {
    if (!state.store || !state.style) return;
    const params = new URLSearchParams({ store: state.store, style: state.style });
    try {
      const detail = await api(`/api/style?${params}`);
      if (detail.store && text(detail.store) !== state.store) return;
      if (detail.style && text(detail.style) !== state.style) return;
      state.detail = detail;
      renderStyle();
    } catch (error) {
      els.grid.innerHTML = `<div class="grid-empty">${escapeHtml(error.message)}</div>`;
      toast(error.message, "error");
    }
  }

  function renderStyle() {
    const detail = state.detail || {};
    const assignments = envelope(detail, "assignments");
    const activeCount = assignments.filter((item) => bool(item.active, true)).length;
    const styleMixed = activeCount > 0 && activeCount < assignments.length;
    const styleActive = assignments.length === 0 || activeCount === assignments.length;
    els.styleActive.checked = styleActive;
    els.styleActive.indeterminate = styleMixed;
    els.styleActive.disabled = assignments.length === 0;
    els.styleBadge.textContent = styleMixed ? "Mixed" : styleActive ? "Active" : "Inactive";
    els.styleBadge.className = `badge badge--${styleMixed ? "mixed" : styleActive ? "active" : "inactive"}`;
    const colors = envelope(detail, "colors");
    els.styleSummary.textContent = `${styleName(state.styleRecord) || storeDisplayFor(state.store)} · ${colors.length} color${colors.length === 1 ? "" : "s"} · ${activeCount} active assignment${activeCount === 1 ? "" : "s"}`;
    renderGrid(colors, assignments);
  }

  function colorCode(color) {
    return text(typeof color === "string" ? color : color.code ?? color.color_code ?? color.garment_color_code).trim();
  }

  function colorName(color) {
    return text(typeof color === "string" ? color : color.name ?? color.color ?? color.description, colorCode(color)).trim();
  }

  function renderGrid(colors, assignments) {
    els.grid.replaceChildren();
    if (!colors.length) {
      const empty = document.createElement("div");
      empty.className = "grid-empty";
      empty.textContent = "No active garment colors were found for this store and style.";
      els.grid.append(empty);
      return;
    }
    if (colors.length && colors.every((color) => color.warehouse_active === false)) {
      const notice = document.createElement("div");
      notice.className = "grid-empty";
      notice.textContent = "This style is not in the store's live FDM4 catalog, so no garment colors are editable. Remaining cards are legacy assignments kept for reference; they will apply automatically if the style returns to the catalog.";
      els.grid.append(notice);
    }
    const byKey = new Map(assignments.map((assignment) => [
      `${text(assignment.garment_color_code)}:${Math.max(1, Number(assignment.option_row) || 1)}:${Number(assignment.position)}`,
      assignment,
    ]));
    const header = document.createElement("div");
    header.className = "grid-row grid-row--header";
    ["Garment color", "Position 1", "Position 2", "Position 3"].forEach((label) => {
      const cell = document.createElement("div");
      cell.className = "grid-cell";
      cell.textContent = label;
      header.append(cell);
    });
    els.grid.append(header);

    colors.forEach((color, colorIndex) => {
      const code = colorCode(color);
      const editable = color.warehouse_active !== false;
      const colorAssignments = assignments.filter((assignment) => text(assignment.garment_color_code) === code);
      const rowNumbers = [...new Set(
        colorAssignments.map((assignment) => Math.max(1, Number(assignment.option_row) || 1))
      )].sort((a, b) => a - b);
      if (!rowNumbers.length) rowNumbers.push(1);

      const hex = text(color.hex ?? color.hex_code);
      const accent = /^#[0-9a-f]{3,8}$/i.test(hex) ? hex : "";
      // Every row of one color group shares subtle banding + a swatch-colored
      // accent stripe, so multi-row colors read as one block at a glance.
      const decorate = (row, isFirst = false) => {
        row.classList.add("grid-row--group");
        if (colorIndex % 2 === 1) row.classList.add("grid-row--band");
        if (!editable) row.classList.add("grid-row--dead");
        if (isFirst) row.classList.add("grid-row--group-start");
        if (accent) row.style.setProperty("--band-accent", accent);
      };

      rowNumbers.forEach((optionRow, subIndex) => {
        const row = document.createElement("div");
        row.className = `grid-row${subIndex > 0 ? " grid-row--suboption" : ""}`;
        decorate(row, subIndex === 0);
        const colorCell = document.createElement("div");
        colorCell.className = "grid-cell grid-cell--color";
        if (subIndex === 0) {
          const swatch = document.createElement("span");
          swatch.className = "color-swatch";
          if (accent) swatch.style.backgroundColor = accent;
          else swatch.textContent = code.slice(0, 3).toUpperCase();
          const info = document.createElement("span");
          info.className = "color-info";
          const strong = document.createElement("strong");
          strong.textContent = colorName(color);
          const small = document.createElement("small");
          small.textContent = rowNumbers.length > 1 ? `${code} · row ${optionRow} of ${rowNumbers.length}` : code;
          info.append(strong, small);
          if (color.warehouse_active === false) info.append(miniTag("Retired color"));
          colorCell.append(swatch, info);
        } else {
          const marker = document.createElement("span");
          marker.className = "option-row-marker";
          marker.textContent = `↳ ${colorName(color)} · row ${optionRow}`;
          colorCell.append(marker);
        }
        row.append(colorCell);

        for (let position = 1; position <= 3; position += 1) {
          const cell = document.createElement("div");
          cell.className = "grid-cell";
          const assignment = byKey.get(`${code}:${optionRow}:${position}`);
          if (assignment) cell.append(assignmentCard(color, position, assignment, optionRow));
          else if (!editable) cell.append(inactiveColorPlaceholder());
          else cell.append(addButton(color, position, optionRow));
          row.append(cell);
        }
        els.grid.append(row);
      });

      // Group footer: add-row for live colors, clear-color whenever data
      // exists (retired colors included - that is the cleanup case).
      if (editable || colorAssignments.length) {
        const addRow = document.createElement("div");
        addRow.className = "grid-row grid-row--add-option";
        decorate(addRow);
        const label = document.createElement("div");
        label.className = "grid-cell grid-cell--color";
        if (editable) {
          const nextRow = rowNumbers[rowNumbers.length - 1] + 1;
          const button = document.createElement("button");
          button.type = "button";
          button.className = "add-option-row";
          button.innerHTML = '<span aria-hidden="true">+</span> Add row';
          button.title = `Add another selectable logo row for ${colorName(color)} - customers choose one row at checkout`;
          button.addEventListener("click", () => openAssignment(color, 1, null, nextRow));
          label.append(button);
        }
        if (colorAssignments.length) {
          const clear = document.createElement("button");
          clear.type = "button";
          clear.className = "clear-color";
          clear.textContent = `Clear color (${colorAssignments.length})`;
          clear.title = `Permanently delete every logo assignment on ${colorName(color)}`;
          clear.addEventListener("click", (e) => clearColor(color, colorAssignments.length, e.currentTarget));
          label.append(clear);
        }
        addRow.append(label);
        for (let i = 0; i < 3; i += 1) {
          const filler = document.createElement("div");
          filler.className = "grid-cell grid-cell--filler";
          addRow.append(filler);
        }
        els.grid.append(addRow);
      }
    });
  }

  async function clearColor(color, count, btn = null) {
    const accepted = await confirmAction({
      title: `Clear all rows for ${colorName(color)}?`,
      message: `This permanently deletes ${count} logo assignment${count === 1 ? "" : "s"} on ${colorName(color)} (${colorCode(color)}) from the warehouse. The next sync removes them from the website. Every deletion is recorded in the Activity log.`,
      actionLabel: "Delete all rows",
      danger: true,
    });
    if (!accepted) return;
    if (btn) setBusy(btn, true, "Deleting...");
    try {
      const params = new URLSearchParams({
        fdm4_store: state.store,
        product_style: state.style,
        garment_color_code: colorCode(color),
        hard: "true",
      });
      const result = await api(`/api/assignments-by-color?${params}`, { method: "DELETE" });
      const removed = Number(result.removed ?? 0);
      toast(`Removed ${removed} assignment${removed === 1 ? "" : "s"} from ${colorName(color)}.`);
      await refreshStyle();
    } catch (error) {
      toast(error.message, "error");
    } finally { if (btn) setBusy(btn, false); }
  }

  // Legacy sheet semantics: lb-black = the logo renders on a dark garment
  // (light/white thread), lb-white = dark thread on light garments. Preview
  // chips use the matching shade so light logos are actually visible; when
  // the background class is unset, a mid gray keeps both extremes legible.
  function imageShadeClass(background) {
    const value = text(background).trim().toLowerCase();
    if (value === "lb-black") return "img-shade--dark";
    if (value === "lb-white") return "img-shade--light";
    return "img-shade--neutral";
  }

  function assignmentCard(color, position, assignment, optionRow) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `assignment-card${bool(assignment.active, true) ? "" : " assignment-card--inactive"}`;
    button.setAttribute("aria-label", `Edit ${text(assignment.logo_code, "logo")} on ${colorName(color)}, row ${optionRow}, position ${position}`);
    const image = document.createElement("span");
    image.className = `assignment-image ${imageShadeClass(assignment.background)}`;
    const imageUrl = text(assignment.image_url).trim();
    if (imageUrl) {
      const img = document.createElement("img");
      img.src = imageUrl;
      img.alt = "";
      img.loading = "lazy";
      img.addEventListener("error", () => { image.replaceChildren(document.createTextNode("No image")); });
      image.append(img);
    } else image.textContent = "No image";
    const info = document.createElement("span");
    info.className = "assignment-info";
    const name = document.createElement("strong");
    const assignmentCode = text(assignment.logo_code, text(assignment.design_id, "Logo"));
    const assignmentDesc = text(assignment.display_name, "").trim();
    name.textContent = assignmentDesc && assignmentDesc !== assignmentCode
      ? `${assignmentCode} - ${assignmentDesc}`
      : assignmentCode;
    const scheme = document.createElement("small");
    scheme.textContent = `Scheme ${text(assignment.color_scheme_id, "-")} · ${text(assignment.location, "No placement")}`;
    const flags = document.createElement("span");
    flags.className = "assignment-flags";
    if (bool(assignment.optional)) flags.append(miniTag("Optional"));
    if (!bool(assignment.active, true)) flags.append(miniTag("Inactive"));
    if (assignment.cost_override !== null && assignment.cost_override !== "" && assignment.cost_override !== undefined) flags.append(miniTag(`$${Number(assignment.cost_override).toFixed(2)}`));
    info.append(name, scheme, flags);
    button.append(image, info);
    button.addEventListener("click", () => openAssignment(color, position, assignment, optionRow));
    return button;
  }

  function miniTag(label) {
    const tag = document.createElement("span");
    tag.className = "mini-tag";
    tag.textContent = label;
    return tag;
  }

  function addButton(color, position, optionRow) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "add-assignment";
    button.innerHTML = '<span aria-hidden="true">+</span> Add logo';
    button.setAttribute("aria-label", `Add a logo to ${colorName(color)}, row ${optionRow}, position ${position}`);
    button.addEventListener("click", () => openAssignment(color, position, null, optionRow));
    return button;
  }

  function inactiveColorPlaceholder() {
    const placeholder = document.createElement("span");
    placeholder.className = "add-assignment add-assignment--disabled";
    placeholder.textContent = "Unavailable";
    return placeholder;
  }

  function resetAssignmentForm() {
    state.requestSequence.designDetail += 1;
    els.assignmentForm.reset();
    els.designId.value = "";
    els.selectedDesign.hidden = true;
    els.designPreview.replaceChildren(document.createTextNode("No preview"));
    els.scheme.replaceChildren(new Option("Choose a design first", ""));
    setOptionsOpen(els.location, els.placementVocabOptions, false);
    els.sort.value = "0";
    els.active.checked = true;
    els.upload.value = "";
    els.uploadStatus.textContent = "PNG, JPEG, WebP, or GIF";
    state.selectedDesign = null;
    state.designDetail = null;
  }

  async function openAssignment(color, position, assignment, optionRow = 1) {
    resetAssignmentForm();
    // Open the dialog immediately; the vocabulary fetch fills in behind it so
    // the first click doesn't appear to do nothing while /api/vocab loads.
    const vocabReady = ensureVocab();
    state.editing = { color, position, assignment, optionRow };
    const editing = Boolean(assignment);
    els.assignmentTitle.textContent = editing ? "Edit logo" : "Add logo";
    els.assignmentContext.textContent = `${colorName(color)} (${colorCode(color)}) · Row ${optionRow} · Position ${position}`;
    els.applyColors.hidden = !editing;
    els.softRemove.hidden = !editing;
    els.hardRemove.hidden = !editing;
    if (editing) {
      els.designSearch.value = `${text(assignment.logo_code)} · Design ${text(assignment.design_id)}`;
      els.designId.value = text(assignment.design_id);
      els.logoCode.value = text(assignment.logo_code);
      els.location.value = text(assignment.location);
      els.cost.value = assignment.cost_override ?? "";
      els.sort.value = assignment.sort_order ?? 0;
      els.optional.checked = bool(assignment.optional);
      els.active.checked = bool(assignment.active, true);
      els.imageUrl.value = text(assignment.image_url);
      els.nameOverride.value = text(assignment.name_override);
      els.softRemove.textContent = bool(assignment.active, true) ? "Deactivate" : "Keep inactive";
      els.softRemove.hidden = !bool(assignment.active, true);
    }
    if (!editing) els.nameOverride.value = "";
    openDialog(els.assignmentDialog);
    await vocabReady;
    populateBackgroundOptions(editing ? text(assignment.background) : "");
    if (editing && assignment.design_id) {
      await loadDesignDetail(text(assignment.design_id), {
        design_id: assignment.design_id,
        logo_code: assignment.logo_code,
        description: assignment.description,
      }, assignment);
    } else {
      window.setTimeout(() => els.designSearch.focus(), 30);
    }
  }

  async function ensureVocab() {
    if (state.vocab) return state.vocab;
    try {
      const payload = await api("/api/vocab");
      state.vocab = {
        placements: Array.isArray(payload.placements) ? payload.placements : [],
        backgrounds: Array.isArray(payload.backgrounds) ? payload.backgrounds : [],
      };
    } catch (error) {
      state.vocab = { placements: [], backgrounds: [] };
      if (!ensureVocab.warned) {
        ensureVocab.warned = true;
        toast("Couldn't load the placement list - placements may appear empty. Reload the page to retry.", "error");
      }
    }
    return state.vocab;
  }

  function populateBackgroundOptions(selected) {
    const backgrounds = (state.vocab?.backgrounds || []).map((item) => text(item.background)).filter(Boolean);
    const value = text(selected).trim();
    // Preserve a legacy value that isn't in the vocabulary (odd casing, retired
    // tag) so editing an old assignment never silently clears it.
    if (value && !backgrounds.includes(value)) backgrounds.push(value);
    els.background.replaceChildren(new Option("None", ""));
    backgrounds.forEach((bg) => els.background.add(new Option(bg, bg)));
    els.background.value = value;
  }

  function renderPlacementOptions(query = "") {
    const vocab = state.vocab || { placements: [] };
    const q = query.trim().toLowerCase();
    const matches = vocab.placements.filter((item) => !q || text(item.location).toLowerCase().includes(q));
    els.placementVocabOptions.replaceChildren();
    if (!matches.length) {
      showEmptyOption(els.placementVocabOptions, q ? "No matching placements - you can type a new one" : "No placements yet");
    } else {
      matches.slice(0, 60).forEach((item) => {
        const uses = Number(item.uses || 0);
        const canonical = item.canonical !== false;
        const meta = [canonical ? "FDM4" : "", uses ? `×${uses}` : ""].filter(Boolean).join(" · ");
        appendOption(els.placementVocabOptions, {
          title: text(item.location),
          meta,
          onSelect: () => {
            els.location.value = text(item.location);
            setOptionsOpen(els.location, els.placementVocabOptions, false);
          },
        });
      });
    }
    setOptionsOpen(els.location, els.placementVocabOptions, true);
  }

  async function searchDesigns(query) {
    const sequence = ++state.requestSequence.designs;
    showEmptyOption(els.designOptions, query.trim() ? "Searching..." : "Loading this store's designs...");
    setOptionsOpen(els.designSearch, els.designOptions, true);
    try {
      const params = new URLSearchParams({ q: query.trim() });
      if (state.store) params.set("store", state.store);
      const payload = await api(`/api/designs?${params}`);
      if (sequence !== state.requestSequence.designs) return;
      const designs = envelope(payload, "designs");
      els.designOptions.replaceChildren();
      if (!designs.length) {
        showEmptyOption(els.designOptions, "No matching FDM4 designs");
        return;
      }
      designs.forEach((design) => {
        const id = text(design.design_id ?? design.id);
        const code = designCode(design);
        appendOption(els.designOptions, {
          title: code ? `${code} - ${text(design.description ?? design.web_description, "Unnamed design")}` : text(design.description ?? design.web_description, `Design ${id}`),
          subtitle: text(design.web_description),
          meta: `Design ${id}`,
          onSelect: () => {
            els.designSearch.value = `${code ? `${code} · ` : ""}${text(design.description ?? design.web_description, `Design ${id}`)}`;
            setOptionsOpen(els.designSearch, els.designOptions, false);
            loadDesignDetail(id, design, null);
          },
        });
      });
    } catch (error) {
      showEmptyOption(els.designOptions, error.message);
    }
  }

  function designCode(design) {
    const codes = design?.logo_codes ?? design?.short_codes ?? design?.codes;
    return text(design?.logo_code ?? design?.code ?? (Array.isArray(codes) ? codes[0] : codes)).trim();
  }

  async function loadDesignDetail(id, selected, assignment) {
    const sequence = ++state.requestSequence.designDetail;
    state.selectedDesign = selected;
    els.designId.value = id;
    els.selectedDesign.hidden = false;
    els.selectedDesignName.textContent = text(selected.description ?? selected.web_description, `Design ${id}`);
    els.selectedDesignMeta.textContent = `Design ${id}${designCode(selected) ? ` · ${designCode(selected)}` : ""}`;
    els.designPreview.replaceChildren(document.createTextNode("Loading preview..."));
    try {
      const payload = await api(`/api/designs/${encodeURIComponent(id)}`);
      if (sequence !== state.requestSequence.designDetail || els.designId.value !== id) return;
      state.designDetail = payload;
      const design = payload.design || selected || {};
      state.selectedDesign = design;
      els.selectedDesignName.textContent = text(design.description ?? design.web_description, `Design ${id}`);
      els.selectedDesignMeta.textContent = `Design ${id}${designCode(design) || designCode(selected) ? ` · ${designCode(design) || designCode(selected)}` : ""}`;
      if (!assignment || !els.logoCode.value) els.logoCode.value = designCode(design) || designCode(selected);
      populateSchemes(envelope(payload, "schemes"), assignment?.color_scheme_id);
      populatePlacements(envelope(payload, "placements"), assignment?.location);
      updateDesignPreview(assignment?.image_url);
    } catch (error) {
      if (sequence !== state.requestSequence.designDetail || els.designId.value !== id) return;
      els.designPreview.replaceChildren(document.createTextNode("Preview unavailable"));
      toast(error.message, "error");
      if (assignment?.color_scheme_id) populateSchemes([{ color_scheme_id: assignment.color_scheme_id }], assignment.color_scheme_id);
    }
  }

  function populateSchemes(schemes, selectedValue) {
    els.scheme.replaceChildren(new Option("Choose a scheme...", ""));
    schemes.forEach((scheme) => {
      const id = text(scheme.color_scheme_id ?? scheme.scheme_id ?? scheme.id);
      const label = text(scheme.name ?? scheme.description, id ? `Scheme ${id}` : "No colorway on file - production art only");
      const option = new Option(label, id);
      if (scheme.is_colorway === false || !id) option.disabled = true;
      els.scheme.add(option);
    });
    if (!schemes.length) {
      const empty = new Option("No FDM4 art files found for this design", "");
      empty.disabled = true;
      els.scheme.add(empty);
    }
    if (selectedValue !== undefined && selectedValue !== null) {
      const value = text(selectedValue);
      if (!Array.from(els.scheme.options).some((option) => option.value === value)) els.scheme.add(new Option(`Scheme ${value}`, value));
      els.scheme.value = value;
    }
    // Every real option disabled means this design has no usable colorway -
    // say so next to the field instead of letting Save fail with the
    // browser's generic "please select an item" message.
    const usable = Array.from(els.scheme.options).some((o) => o.value && !o.disabled);
    let note = document.getElementById("scheme-empty-note");
    if (!usable) {
      if (!note) {
        note = document.createElement("p");
        note.id = "scheme-empty-note";
        note.className = "muted text-small";
        els.scheme.insertAdjacentElement("afterend", note);
      }
      note.textContent = "This design has no color scheme with art on file in FDM4, so it can't be assigned yet. Pick a different design, or ask FDM4 to add the art.";
    } else if (note) {
      note.remove();
    }
  }

  function populatePlacements(placements, selectedValue) {
    // Placement suggestions now come from the global vocabulary combobox;
    // design_pool locations are FDM4 internal codes (rcap/lcap) and were noise.
    if (selectedValue) els.location.value = text(selectedValue);
  }

  function schemeAssetUrl(scheme) {
    const direct = text(scheme?.preview_url ?? scheme?.image_url ?? scheme?.url ?? scheme?.public_url).trim();
    if (direct) return direct;
    const assets = Array.isArray(scheme?.assets) ? scheme.assets : [];
    const asset = assets.find((item) => /preview|thumb/i.test(text(item.resource_type ?? item.type))) || assets[0] || scheme;
    return text(asset?.preview_url ?? asset?.image_url ?? asset?.url ?? asset?.public_url).trim();
  }

  function updateDesignPreview(preferredUrl = "") {
    const schemes = envelope(state.designDetail || {}, "schemes");
    const selectedScheme = schemes.find((item) => text(item.color_scheme_id ?? item.scheme_id ?? item.id) === els.scheme.value);
    const existingWarehouseUrl = text(selectedScheme?.warehouse_image_url).trim();
    if (!els.imageUrl.value.trim() && existingWarehouseUrl) els.imageUrl.value = existingWarehouseUrl;
    const url = text(preferredUrl).trim() || schemeAssetUrl(selectedScheme) || text(state.designDetail?.design?.preview_url).trim();
    els.designPreview.className = `preview-frame ${imageShadeClass(els.background.value)}`;
    els.designPreview.replaceChildren();
    if (!url) {
      els.designPreview.textContent = "No preview";
      return;
    }
    const image = document.createElement("img");
    image.src = url;
    image.alt = "Selected logo preview";
    image.addEventListener("error", () => { els.designPreview.replaceChildren(document.createTextNode("Preview unavailable")); });
    els.designPreview.append(image);
  }

  function assignmentPayload() {
    const { color, position, optionRow, assignment } = state.editing;
    return {
      fdm4_store: state.store,
      product_style: state.style,
      garment_color_code: colorCode(color),
      position,
      option_row: optionRow ?? 1,
      design_id: els.designId.value.trim(),
      logo_code: els.logoCode.value.trim().toUpperCase(),
      color_scheme_id: els.scheme.value,
      location: els.location.value.trim(),
      optional: els.optional.checked,
      background: els.background.value.trim(),
      cost_override: els.cost.value === "" ? null : Number(els.cost.value),
      sort_order: Number.parseInt(els.sort.value || "0", 10),
      active: els.active.checked,
      image_url: els.imageUrl.value.trim(),
      name_override: els.nameOverride.value.trim(),
      expected_updated_at: assignment?.updated_at || null,
    };
  }

  async function saveAssignment(event) {
    event.preventDefault();
    if (!els.designId.value.trim()) {
      toast("Choose an FDM4 design before saving.", "error");
      els.designSearch.focus();
      return;
    }
    if (!els.assignmentForm.reportValidity()) return;
    const button = $("#save-assignment-button");
    setBusy(button, true, "Saving...");
    try {
      await api("/api/assignments", { method: "PUT", body: assignmentPayload() });
      closeDialog(els.assignmentDialog);
      toast("Logo assignment saved.");
      await refreshStyle();
    } catch (error) {
      if (error.status === 409) {
        const reload = await confirmAction({
          title: "Assignment changed",
          message: "Another update was saved after this editor opened. Reload the latest assignment before making further changes.",
          actionLabel: "Reload",
          danger: false,
        });
        if (reload) {
          closeDialog(els.assignmentDialog);
          await refreshStyle();
        }
        return;
      }
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function removeAssignment(hard) {
    const assignment = state.editing?.assignment;
    if (!assignment) return;
    if (!hard && !bool(assignment.active, true)) {
      toast("This assignment is already inactive.", "error");
      return;
    }
    const accepted = await confirmAction({
      title: hard ? "Delete assignment permanently?" : "Deactivate assignment?",
      message: hard
        ? (state.editing.position === 1
          ? "This removes the entire selectable logo row, including positions 2 and 3, and cannot be undone."
          : "This removes the warehouse assignment and cannot be undone. The next sync will remove it from WordPress.")
        : (state.editing.position === 1
          ? "This turns off the entire selectable logo row, including positions 2 and 3."
          : "The logo is kept here but will be removed from the website the next time this store is synced."),
      actionLabel: hard ? "Delete permanently" : "Deactivate",
      danger: true,
    });
    if (!accepted) return;
    const params = new URLSearchParams({
      fdm4_store: state.store,
      product_style: state.style,
      garment_color_code: colorCode(state.editing.color),
      position: String(state.editing.position),
      option_row: String(state.editing.optionRow ?? 1),
      hard: String(hard),
    });
    const busyBtn = hard ? els.hardRemove : els.softRemove;
    if (busyBtn) setBusy(busyBtn, true, hard ? "Deleting..." : "Deactivating...");
    try {
      await api(`/api/assignments?${params}`, { method: "DELETE" });
      closeDialog(els.assignmentDialog);
      toast(hard ? "Assignment deleted." : "Assignment deactivated.");
      await refreshStyle();
    } catch (error) {
      toast(error.message, "error");
    } finally { if (busyBtn) setBusy(busyBtn, false); }
  }

  async function applyAllColors() {
    const assignment = state.editing?.assignment;
    if (!assignment) return;
    const totalColors = (state.detail?.colors || []).length;
    const accepted = await confirmAction({
      title: "Apply to every garment color?",
      message: `Copy ${text(assignment.logo_code, "this logo")} (row ${state.editing.optionRow ?? 1}, position ${state.editing.position}) to ${totalColors ? `all ${totalColors} available colors` : "every available color"}. Occupied slots are preserved.`,
      actionLabel: "Apply to all colors",
      danger: false,
    });
    if (!accepted) return;
    if (els.applyColors) setBusy(els.applyColors, true, "Applying...");
    try {
      const result = await api("/api/apply-all-colors", {
        method: "POST",
        body: {
          store: state.store,
          style: state.style,
          garment_color_code: colorCode(state.editing.color),
          position: state.editing.position,
          option_row: state.editing.optionRow ?? 1,
          overwrite: false,
        },
      });
      closeDialog(els.assignmentDialog);
      const copied = Number(result.copied ?? result.created ?? result.applied ?? 0);
      const skipped = Number(result.skipped_without_primary ?? 0);
      toast(`Applied to ${copied} color${copied === 1 ? "" : "s"}${skipped ? `; skipped ${skipped} without a primary logo` : ""}.`);
      await refreshStyle();
    } catch (error) {
      toast(error.message, "error");
    } finally { if (els.applyColors) setBusy(els.applyColors, false); }
  }

  async function uploadImage() {
    const file = els.upload.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    els.uploadStatus.innerHTML = '<span class="spinner" aria-hidden="true"></span> Uploading...';
    try {
      const result = await api("/api/upload", { method: "POST", body: form });
      const url = text(result.image_url ?? result.url).trim();
      if (!url) throw new Error("The upload succeeded but returned no image URL.");
      els.imageUrl.value = url;
      els.uploadStatus.textContent = file.name;
      updateDesignPreview(url);
      toast("Image uploaded. Save the assignment to keep the URL.");
    } catch (error) {
      els.uploadStatus.textContent = "Upload failed";
      toast(error.message, "error");
    }
  }

  async function openStoreSettings() {
    if (!state.store) return;
    els.storeSettingsContext.textContent = storeDisplayFor(state.store);
    els.settingEnabled.checked = true;
    els.settingAllowsNone.checked = false;
    openDialog(els.storeSettingsDialog);
    const button = $("button[type='submit']", els.settingsForm);
    setBusy(button, true, "Loading...");
    try {
      const payload = await api(`/api/settings/${encodeURIComponent(state.store)}`);
      const settings = payload.settings || {};
      els.settingEnabled.checked = bool(settings.enabled, true);
      els.settingAllowsNone.checked = bool(settings.allows_none, false);
    } catch (error) {
      closeDialog(els.storeSettingsDialog);
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (!state.store) return;
    const button = $("button[type='submit']", els.settingsForm);
    setBusy(button, true, "Saving...");
    try {
      await api(`/api/settings/${encodeURIComponent(state.store)}`, {
        method: "PUT",
        body: { enabled: els.settingEnabled.checked, allows_none: els.settingAllowsNone.checked },
      });
      toast(`Store settings saved for ${storeDisplayFor(state.store)}.`);
      closeDialog(els.storeSettingsDialog);
      if (state.style) await refreshStyle();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  }

  function openCopyDialog() {
    els.copyForm.reset();
    els.copySource.value = "";
    els.copyStore.textContent = storeDisplayFor(state.store);
    openDialog(els.copyDialog);
    searchStyles("", "copy");
    window.setTimeout(() => els.copySearch.focus(), 30);
  }

  async function copyStyle(event) {
    event.preventDefault();
    const source = els.copySource.value.trim();
    if (!source) {
      toast("Choose a source style from the search results.", "error");
      return;
    }
    if (els.copyOverwrite.checked) {
      const ok = await confirmAction({
        title: "Overwrite existing logos?",
        message: `Copying ${source} onto ${state.style} with "Overwrite occupied positions" checked replaces any logo already set on matching positions. This cannot be undone.`,
        actionLabel: "Copy and overwrite",
      });
      if (!ok) return;
    }
    const submit = $("button[type='submit']", els.copyForm);
    setBusy(submit, true, "Copying...");
    try {
      const result = await api("/api/copy-style", {
        method: "POST",
        body: { store: state.store, source_style: source, target_style: state.style, overwrite: els.copyOverwrite.checked },
      });
      closeDialog(els.copyDialog);
      const copied = Number(result.created ?? result.copied ?? 0);
      const skipped = Number(result.skipped_without_primary ?? 0);
      toast(`Copied ${copied} assignment${copied === 1 ? "" : "s"}${skipped ? `; skipped ${skipped} companion row${skipped === 1 ? "" : "s"} without a position-1 logo` : ""}.`);
      await refreshStyle();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(submit, false);
    }
  }

  async function importCsv() {
    const file = els.importFile.files?.[0];
    if (!file) return;
    const accepted = await confirmAction({
      title: state.store ? `Import CSV into ${state.store}?` : "Import CSV assignments?",
      message: state.store
        ? `“${file.name}” overwrites ${state.store}'s logo assignments wherever the CSV names them. Every change is recorded in the Activity log.`
        : `No store is selected, so “${file.name}” applies to whichever stores the CSV itself names - possibly many at once. Every change is recorded in the Activity log.`,
      actionLabel: "Import CSV",
      danger: !state.store,
    });
    if (!accepted) {
      els.importFile.value = "";
      return;
    }
    const form = new FormData();
    form.append("file", file);
    if (state.store) form.append("store", state.store);
    els.importResults.innerHTML = '<p><span class="spinner" aria-hidden="true"></span> Validating and importing CSV...</p>';
    openDialog(els.importDialog);
    try {
      const result = await api("/api/import", { method: "POST", body: form });
      renderResult(els.importResults, result, "Import completed");
      toast("CSV import completed.");
      if (state.style) await refreshStyle();
    } catch (error) {
      els.importResults.innerHTML = `<div class="notice notice--error" role="alert"><span class="notice__icon">!</span><div><strong>Import failed</strong><br>${escapeHtml(error.message)}</div></div>`;
    } finally {
      els.importFile.value = "";
    }
  }

  async function importLegacySheets(event) {
    event.preventDefault();
    const file = els.legacyFile.files?.[0];
    if (!file) {
      toast("Choose an NDJSON export first.");
      return;
    }
    const overwrite = els.legacyOverwrite.checked;
    const accepted = await confirmAction({
      title: "Import legacy sheets?",
      message: `“${file.name}” updates warehouse logo data for EVERY store present in the file${overwrite ? " and OVERWRITES manual edits (overwrite is checked)" : " (manual edits are preserved)"}. Every change is recorded in the Activity log.`,
      actionLabel: overwrite ? "Import + overwrite" : "Import sheets",
      danger: overwrite,
    });
    if (!accepted) return;
    const form = new FormData();
    form.append("file", file);
    form.append("preserve_manual", els.legacyOverwrite.checked ? "false" : "true");
    const submit = els.legacyForm.querySelector('button[type="submit"]');
    setBusy(submit, true, "Importing...");
    els.legacyResults.innerHTML = '<p><span class="spinner" aria-hidden="true"></span> Resolving and importing legacy sheets...</p>';
    try {
      const result = await api("/api/legacy-import", { method: "POST", body: form });
      renderResult(els.legacyResults, result, "Legacy import completed");
      toast("Legacy sheet import completed.");
      if (state.style) await refreshStyle();
    } catch (error) {
      els.legacyResults.innerHTML = `<div class="notice notice--error" role="alert"><span class="notice__icon">!</span><div><strong>Import failed</strong><br>${escapeHtml(error.message)}</div></div>`;
    } finally {
      setBusy(submit, false);
      els.legacyFile.value = "";
    }
  }

  let mirrorRunning = false;

  async function mirrorLegacyImages() {
    if (mirrorRunning) {
      toast("Image mirroring is already running - wait for it to finish.", "error");
      openDialog(els.importDialog);
      return;
    }
    const accepted = await confirmAction({
      title: "Mirror legacy images for ALL stores?",
      message: "This is a global migration, not a page. It copies every legacy sheet image into the warehouse and rewrites the image link on every store's logo assignments - tens of thousands of rows across all stores at once, no matter which store is selected. It is safe to re-run (already-mirrored images are skipped), and storefronts only change when a store is next synced.",
      actionLabel: "Mirror all stores",
      danger: true,
    });
    if (!accepted) return;
    mirrorRunning = true;
    els.importResults.innerHTML = '<p><span class="spinner" aria-hidden="true"></span> Mirroring legacy images into the warehouse...</p>';
    openDialog(els.importDialog);
    const totals = { processed: 0, downloaded: 0, reused: 0, repointed_assignments: 0, failed: 0 };
    let remaining = -1;
    try {
      for (let batch = 0; batch < 100; batch += 1) {
        const form = new FormData();
        form.append("limit", "50");
        const result = await api("/api/legacy-import-images", { method: "POST", body: form });
        ["processed", "downloaded", "reused", "repointed_assignments", "failed"].forEach((key) => {
          totals[key] += Number(result[key] || 0);
        });
        const nowRemaining = Number(result.remaining || 0);
        els.importResults.innerHTML = `<p><span class="spinner" aria-hidden="true"></span> Mirrored ${totals.downloaded + totals.reused} URL(s)... ${nowRemaining} remaining</p>`;
        if (!nowRemaining) { remaining = 0; break; }
        // No forward progress (every remaining URL failed) - stop and report.
        if (remaining !== -1 && nowRemaining >= remaining) { remaining = nowRemaining; break; }
        remaining = nowRemaining;
      }
      const leftover = Math.max(remaining, 0);
      renderResult(els.importResults, { ...totals, remaining: leftover, misses: totals.failed },
        leftover ? `Legacy image mirror stopped early - ${leftover} image(s) remaining, run it again to continue` : "Legacy image mirror finished");
      toast(leftover ? `Image mirroring stopped early - ${leftover} remaining. Run it again to continue.` : "Legacy image mirroring finished.");
      if (state.style) await refreshStyle();
    } catch (error) {
      els.importResults.innerHTML = `<div class="notice notice--error" role="alert"><span class="notice__icon">!</span><div><strong>Image mirroring failed</strong><br>${escapeHtml(error.message)}</div></div>`;
    } finally { mirrorRunning = false; }
  }

  const RESULT_STAT_LABELS = {
    processed: "rows processed",
    downloaded: "images downloaded",
    reused: "images already mirrored",
    repointed_assignments: "image links updated",
    failed: "failed",
    remaining: "remaining",
    misses: "rows needing attention",
    created: "created",
    updated: "updated",
    skipped: "skipped",
    upserted: "rows written",
    stores: "stores",
    assignments: "assignments",
  };

  function renderResult(container, result, title) {
    const stats = result.stats && typeof result.stats === "object" ? result.stats : result;
    const entries = Object.entries(stats).filter(([, value]) => typeof value === "number" || typeof value === "boolean");
    const errorRows = result.errors ?? result.unresolved;
    const errors = Array.isArray(errorRows) ? errorRows : [];
    const missCount = Number(result.misses ?? errors.length ?? 0);
    container.innerHTML = `
      <div class="notice ${missCount ? "notice--warning" : "notice--success"}">
        <span class="notice__icon" aria-hidden="true">${missCount ? "!" : "✓"}</span>
        <div><strong>${escapeHtml(title)}</strong><br>${missCount ? `${missCount} row${missCount === 1 ? " needs" : "s need"} attention and ${missCount === 1 ? "was" : "were"} added to the import punch list.` : "No unresolved rows were reported."}</div>
      </div>
      <div class="result-summary">${entries.map(([key, value]) => `<div class="stat"><strong>${escapeHtml(value)}</strong><small>${escapeHtml(RESULT_STAT_LABELS[key] || key.replaceAll("_", " "))}</small></div>`).join("")}</div>
      ${errors.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Row</th><th>Reason</th><th>Detail</th></tr></thead><tbody>${errors.map((item, index) => `<tr><td>${escapeHtml(item.row ?? item.line ?? index + 1)}</td><td>${escapeHtml(item.reason ?? item.error ?? "Unresolved")}</td><td>${escapeHtml(item.detail ?? item.message ?? "")}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
  }

  async function loadReports(event) {
    event?.preventDefault();
    const params = new URLSearchParams({ limit: "500" });
    if (els.reportStore.value) params.set("store", els.reportStore.value);
    if (els.reportReason.value) params.set("reason", els.reportReason.value);
    els.reportResults.innerHTML = '<div class="grid-loading"><span class="spinner" aria-hidden="true"></span> Loading punch list...</div>';
    try {
      const payload = await api(`/api/import-report${params.size ? `?${params}` : ""}`);
      const reports = envelope(payload, "reports").length ? envelope(payload, "reports") : envelope(payload, "rows");
      const total = Number(payload.total ?? reports.length);
      els.reportCount.textContent = `${total} unresolved row${total === 1 ? "" : "s"}${total > reports.length ? ` · showing ${reports.length}` : ""}`;
      const quickFilter = $("#report-quick-filter");
      if (!reports.length) {
        els.reportResults.innerHTML = '<div class="grid-empty">No unresolved imports match these filters.</div>';
        if (quickFilter) { quickFilter.hidden = true; quickFilter.value = ""; }
        return;
      }
      if (quickFilter) { quickFilter.hidden = false; quickFilter.value = ""; }
      els.reportResults.innerHTML = `<table class="data-table"><thead><tr><th>Imported</th><th>Store / style</th><th>Product color</th><th>Logo</th><th>Reason</th><th>Detail</th></tr></thead><tbody>${reports.map((row) => `<tr><td>${escapeHtml(formatDate(row.imported_at))}</td><td><strong>${escapeHtml(row.fdm4_store)}</strong><br><code>${escapeHtml(row.product_style)}</code></td><td>${escapeHtml(row.product_color)}</td><td><code>${escapeHtml(row.logo_code)}</code></td><td>${escapeHtml(row.reason)}</td><td>${escapeHtml(row.detail)}</td></tr>`).join("")}</tbody></table>`;
    } catch (error) {
      els.reportResults.innerHTML = `<div class="grid-empty">${escapeHtml(error.message)}</div>`;
      els.reportCount.textContent = "";
    }
  }

  function formatDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? text(value) : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  const AUDIT_ACTION_LABELS = {
    assignment_created: "Logo added",
    assignment_updated: "Logo updated",
    assignment_deleted: "Logo deleted",
    store_settings_created: "Store settings created",
    store_settings_updated: "Store settings updated",
    sync_requested: "Sync to WordPress",
  };

  function auditValue(value) {
    if (value === null || value === undefined || value === "") return "-";
    if (value === true) return "yes";
    if (value === false) return "no";
    return String(value);
  }

  function auditDetailText(entry) {
    const detail = entry.detail || {};
    if (entry.action === "sync_requested") {
      const styles = Array.isArray(detail.styles) && detail.styles.length
        ? `styles ${detail.styles.join(", ")}`
        : "entire store";
      const stats = detail.stats || {};
      const outcome = "applied" in stats || "errors" in stats
        ? `applied ${stats.applied ?? 0}, errors ${stats.errors ?? 0}`
        : "";
      return [styles, outcome].filter(Boolean).join(" · ");
    }
    if (detail.changes && typeof detail.changes === "object") {
      return Object.entries(detail.changes)
        .map(([key, change]) => `${key}: ${auditValue(change?.from)} → ${auditValue(change?.to)}`)
        .join("; ");
    }
    const row = detail.new || detail.old;
    if (row && typeof row === "object") {
      return [
        row.logo_code ? `logo ${row.logo_code}${row.color_scheme_id ? `/${row.color_scheme_id}` : ""}` : "",
        row.design_id ? `design ${row.design_id}` : "",
        row.location ? `at ${row.location}` : "",
      ].filter(Boolean).join(" · ");
    }
    return "";
  }

  function auditTarget(entry) {
    const parts = [];
    if (entry.garment_color_code) parts.push(entry.garment_color_code);
    if (entry.option_row) parts.push(`row ${entry.option_row}`);
    if (entry.position) parts.push(`pos ${entry.position}`);
    return parts.join(" · ") || "-";
  }

  function populateAuditStores() {
    // Combobox attach happens once in populateReportStores(); the options list
    // reads state.stores live, so no repopulation is needed here.
  }

  function updateAuditExport() {
    const params = new URLSearchParams();
    const filters = state.audit.filters;
    if (filters.store) params.set("store", filters.store);
    if (filters.style) params.set("style", filters.style);
    if (filters.actor) params.set("actor", filters.actor);
    if (filters.action) params.set("action", filters.action);
    els.auditExport.href = `/api/audit-log/export${params.size ? `?${params}` : ""}`;
  }

  async function loadAudit(reset = false) {
    if (reset) {
      state.audit.beforeId = null;
      updateAuditExport();
      els.auditResults.innerHTML = '<div class="grid-loading"><span class="spinner" aria-hidden="true"></span> Loading history...</div>';
    }
    const params = new URLSearchParams({ limit: "50" });
    const filters = state.audit.filters;
    if (filters.store) params.set("store", filters.store);
    if (filters.style) params.set("style", filters.style);
    if (filters.actor) params.set("actor", filters.actor);
    if (filters.action) params.set("action", filters.action);
    if (state.audit.beforeId) params.set("before_id", String(state.audit.beforeId));
    try {
      const payload = await api(`/api/audit-log?${params}`);
      const entries = envelope(payload, "entries");
      state.audit.beforeId = payload.next_before_id ?? null;
      els.auditMore.hidden = !state.audit.beforeId;
      const rowsHtml = entries.map((entry) => `<tr>
        <td>${escapeHtml(formatDate(entry.at))}</td>
        <td>${escapeHtml(entry.actor)}</td>
        <td>${escapeHtml(AUDIT_ACTION_LABELS[entry.action] || entry.action)}</td>
        <td><strong>${escapeHtml(entry.fdm4_store || "-")}</strong>${entry.product_style ? `<br><code>${escapeHtml(entry.product_style)}</code>` : ""}</td>
        <td>${escapeHtml(auditTarget(entry))}</td>
        <td>${escapeHtml(auditDetailText(entry))}</td>
      </tr>`).join("");
      if (reset) {
        els.auditResults.innerHTML = entries.length
          ? `<table class="data-table"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Store / style</th><th>Color / row / position</th><th>Details</th></tr></thead><tbody>${rowsHtml}</tbody></table>`
          : '<div class="grid-empty">No recorded changes match these filters.</div>';
      } else if (entries.length) {
        $("tbody", els.auditResults)?.insertAdjacentHTML("beforeend", rowsHtml);
      }
      const shown = $$("tbody tr", els.auditResults).length;
      els.auditCount.textContent = shown ? `Showing ${shown} entr${shown === 1 ? "y" : "ies"}${state.audit.beforeId ? " · more available" : ""}` : "";
    } catch (error) {
      if (reset) els.auditResults.innerHTML = `<div class="grid-empty">${escapeHtml(error.message)}</div>`;
      toast(error.message, "error");
    }
  }

  function wpTargetHost() {
    return document.body.dataset.wpHost || "WordPress";
  }

  async function sync(scope) {
    if (!state.store) return;
    const styleScope = scope === "style" ? [state.style] : [];
    const button = scope === "style" ? $("#sync-style-button") : $("#sync-store-button");
    const sibling = scope === "style" ? $("#sync-store-button") : $("#sync-style-button");
    if (scope === "store") {
      const accepted = await confirmAction({
        title: `Sync all of ${storeDisplayFor(state.store)}?`,
        message: `This pushes every configured style's logo setup for this store to ${wpTargetHost()}. It can take a few minutes.`,
        actionLabel: "Sync entire store",
        danger: false,
      });
      if (!accepted) return;
    }
    setBusy(button, true, "Syncing...");
    if (sibling) sibling.disabled = true;
    try {
      const result = await api("/api/sync", { method: "POST", body: { store: state.store, styles: styleScope } });
      renderSyncResult(result, scope);
      openDialog(els.syncDialog);
      els.ownershipWarning.hidden = result.owned !== false;
      const errors = Number(result.stats?.errors ?? result.reconcile?.stats?.errors ?? 0);
      toast(errors ? `Sync finished, but ${errors} product${errors === 1 ? "" : "s"} could not be updated on the website.` : "Sync to the website finished.", errors ? "error" : "success");
    } catch (error) {
      els.syncResults.innerHTML = `<div class="notice notice--error"><span class="notice__icon">!</span><div><strong>Sync failed</strong><br>${escapeHtml(error.message)}</div></div>`;
      openDialog(els.syncDialog);
    } finally {
      setBusy(button, false);
      if (sibling) sibling.disabled = false;
    }
  }

  function renderSyncResult(result, scope) {
    const stats = result.stats && typeof result.stats === "object" ? { ...result.stats } : { ...result };
    // Single-style sync: show what THIS product now carries instead of the
    // environment-wide design-map size (which confused more than it informed).
    if (scope === "style" && Array.isArray(result.style_logos) && result.style_logos.length) {
      delete stats.design_map_rows;
      stats.logo_rows_on_product = Number(result.style_logos[0].logo_rows ?? 0);
    }
    const numeric = Object.entries(stats).filter(([, value]) => typeof value === "number");
    const errorCount = Number(stats.errors ?? 0);
    const ownership = errorCount > 0
      ? `<div class="notice notice--warning"><span class="notice__icon">!</span><div><strong>Sync finished with errors</strong><br>${escapeHtml(errorCount)} product${errorCount === 1 ? "" : "s"} could not be updated on the website. Try syncing again, or contact an administrator with this store and style.</div></div>`
      : result.owned === false
      ? '<div class="notice notice--warning"><span class="notice__icon">!</span><div><strong>This store is not on the new logo system yet</strong><br>The website accepted the sync anyway, so the result cannot be trusted. Ask an administrator to switch this store over before relying on it.</div></div>'
      : `<div class="notice notice--success"><span class="notice__icon">✓</span><div><strong>Sync finished</strong><br>${escapeHtml(wpTargetHost())} accepted the update.</div></div>`;
    // Friendly labels for the stat keys the sync returns; anything unknown
    // falls back to the de-underscored key.
    const STAT_LABELS = {
      applied: "products updated",
      skipped_manual: "skipped (hand-managed)",
      errors: "errors",
      design_map_rows: "design lookup rows",
      logo_rows_on_product: "logo rows on this product",
      repointed_assignments: "image links updated",
      styles: "styles",
      would_change: "would change",
    };
    els.syncResults.innerHTML = `${ownership}<p>Scope: <strong>${escapeHtml(scope === "style" ? `${storeDisplayFor(state.store)} / ${state.style}` : `${storeDisplayFor(state.store)} / all configured styles`)}</strong></p><div class="result-summary">${numeric.map(([key, value]) => `<div class="stat"><strong>${escapeHtml(value)}</strong><small>${escapeHtml(STAT_LABELS[key] || key.replaceAll("_", " "))}</small></div>`).join("") || '<p class="muted">No numeric stats were returned.</p>'}</div>`;
  }

  // ===== Allowlisted in-app assistant =====
  // The server omits the entire assistant DOM for operators who are not on the
  // per-user allowlist. This client initializer therefore always fails closed
  // when the root element is absent.
  const assistantAsyncGuard = window.ArbAgentAsyncGuard;
  const assistantRequestGuard = assistantAsyncGuard?.create?.() ?? null;
  const assistantState = {
    sessionId: null,
    changeSet: null,
    mappingJob: null,
    controller: null,
    streaming: false,
    sessionLoading: false,
    messageHistoryLoading: false,
    historyLoaded: false,
    writesEnabled: false,
    generation: 0,
    operationControllers: new Set(),
    messages: [],
    messagesTruncated: false,
    messagesOldestCursor: null,
    sessions: [],
    sessionsTruncated: false,
    sessionsOldestCursor: null,
    reviewQueue: [],
    reviewTruncated: false,
    reviewOldestCursor: null,
    mappingQueue: [],
    mappingTruncated: false,
    mappingOldestCursor: null,
    localTurnSequence: 0,
  };

  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const HASH_PATTERN = /^[0-9a-f]{64}$/i;

  function strictUuid(value) {
    const candidate = text(value).trim();
    return UUID_PATTERN.test(candidate) ? candidate : "";
  }

  function resetAssistantInteraction(elements) {
    if (!elements) return;
    const composerBlocked = assistantRequestGuard === null
      || assistantAsyncGuard.composerBlocked(assistantState);
    elements.send.disabled = composerBlocked;
    elements.stop.hidden = !assistantState.streaming;
    elements.input.disabled = composerBlocked;
    if (elements.attach) elements.attach.disabled = composerBlocked;
  }

  function advanceAssistantGeneration(elements) {
    assistantState.generation += 1;
    assistantState.controller?.abort();
    assistantState.operationControllers.forEach((controller) => controller.abort());
    assistantState.operationControllers.clear();
    assistantState.controller = null;
    assistantState.streaming = false;
    assistantState.sessionLoading = false;
    assistantState.messageHistoryLoading = false;
    if (assistantRequestGuard !== null) {
      assistantAsyncGuard.invalidateAll(assistantRequestGuard);
    }
    // A superseded stream deliberately skips its own finally block. Restore
    // the controls here so switching sessions can never strand the composer.
    resetAssistantInteraction(elements);
    return assistantState.generation;
  }

  function assistantContextMatches(generation, sessionId = assistantState.sessionId) {
    return generation === assistantState.generation
      && sessionId === assistantState.sessionId;
  }

  function assistantGenerationMatches(generation) {
    return generation === assistantState.generation;
  }

  function mergeAssistantRecords(existing, incoming) {
    const merged = [...existing];
    const known = new Set(existing.map((item) => (
      strictUuid(item?.id ?? item?.session_id ?? item?.change_set_id ?? item?.job_id)
    )).filter(Boolean));
    incoming.forEach((item) => {
      const id = strictUuid(item?.id ?? item?.session_id ?? item?.change_set_id ?? item?.job_id);
      if (id && !known.has(id)) {
        known.add(id);
        merged.push(item);
      }
    });
    return merged;
  }

  function assistantOperationController() {
    const controller = new AbortController();
    assistantState.operationControllers.add(controller);
    return controller;
  }

  function finishAssistantOperation(controller) {
    assistantState.operationControllers.delete(controller);
  }

  function agentNode(tag, className = "", value = null) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== null && value !== undefined) node.textContent = String(value);
    return node;
  }

  function appendAgentText(container, value) {
    const node = document.createElement("span");
    node.textContent = String(value ?? "");
    container.append(node);
    return node;
  }

  function appendToolChip(container, toolName) {
    const chip = document.createElement("span");
    chip.className = "assistant-tool-chip";
    chip.textContent = "Used " + String(toolName ?? "tool");
    container.append(chip);
    return chip;
  }

  function agentValue(value) {
    if (value === null || value === undefined || value === "") return "-";
    if (typeof value === "string") return value;
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  function agentMessage(container, role, value = "") {
    const message = agentNode("div", `assistant-message assistant-message--${role === "user" ? "user" : "assistant"}`);
    const label = agentNode("strong", "assistant-message__label", role === "user" ? "You" : "Assistant");
    const content = agentNode("div", "assistant-message__content");
    if (value) appendAgentText(content, value);
    message.append(label, content);
    container.append(message);
    container.scrollTop = container.scrollHeight;
    return content;
  }

  function setAssistantStatus(elements, message, kind = "") {
    elements.status.textContent = String(message ?? "");
    elements.status.className = `assistant-status${kind ? ` assistant-status--${kind}` : ""}`;
  }

  function emptyAssistantMessages(elements) {
    elements.messages.replaceChildren();
    const empty = agentNode("div", "assistant-empty");
    empty.append(
      agentNode("strong", "", "Start with a question"),
      agentNode(
        "span",
        "",
        assistantState.writesEnabled
          ? "I can inspect bounded warehouse data. Any proposed change will be staged for your review."
          : "I can inspect bounded warehouse data. Changes are disabled during this read-only pilot.",
      ),
    );
    elements.messages.append(empty);
  }

  function appendReviewField(container, label, value) {
    const row = agentNode("div", "assistant-review-field");
    row.append(
      agentNode("dt", "", label),
      agentNode("dd", "", agentValue(value)),
    );
    container.append(row);
  }

  async function agentResponsePayload(response) {
    const contentType = response.headers.get("content-type") || "";
    try {
      return contentType.includes("json") ? await response.json() : await response.text();
    } catch {
      return null;
    }
  }

  async function consumeAssistantSse(response, onEvent) {
    return assistantAsyncGuard.consumeSse(response, onEvent);
  }

  function normalizeChangeSet(payload) {
    const envelopeValue = payload?.change_set && typeof payload.change_set === "object"
      ? payload.change_set
      : payload;
    if (!envelopeValue || typeof envelopeValue !== "object" || Array.isArray(envelopeValue)) return null;
    const id = strictUuid(envelopeValue.id ?? envelopeValue.change_set_id ?? payload?.change_set_id);
    const revision = Number(envelopeValue.revision ?? payload?.revision);
    const previewHash = text(envelopeValue.preview_hash ?? payload?.preview_hash).trim();
    if (!id || !Number.isInteger(revision) || revision < 0) return null;
    return {
      id,
      revision,
      previewHash: HASH_PATTERN.test(previewHash) ? previewHash : "",
      status: text(envelopeValue.status, "pending").trim().toLowerCase(),
      reviewBlocked: bool(envelopeValue.review_blocked ?? payload?.review_blocked),
      containsHardDelete: bool(envelopeValue.contains_hard_delete ?? payload?.contains_hard_delete),
      items: Array.isArray(payload?.items) ? payload.items : Array.isArray(envelopeValue.items) ? envelopeValue.items : [],
      scopes: envelopeValue.affected_scopes ?? payload?.affected_scopes ?? [],
      diff: envelopeValue.preview_diff ?? payload?.preview_diff ?? {},
    };
  }

  async function loadChangeSet(
    elements,
    changeSetId,
    warning = "",
    generation = assistantState.generation,
    expectedSessionId = assistantState.sessionId,
  ) {
    const id = strictUuid(changeSetId);
    if (!id) return;
    const requestRevision = assistantAsyncGuard.begin(
      assistantRequestGuard,
      "changeSet",
    );
    const controller = assistantOperationController();
    try {
      const payload = await api(`/api/agent/change-sets/${encodeURIComponent(id)}`, {
        signal: controller.signal,
      });
      if (
        !assistantContextMatches(generation, expectedSessionId)
        || !assistantAsyncGuard.current(
          assistantRequestGuard,
          "changeSet",
          requestRevision,
        )
      ) return;
      renderChangeSet(elements, payload, warning);
    } catch (error) {
      if (
        error?.name !== "AbortError"
        && assistantContextMatches(generation, expectedSessionId)
        && assistantAsyncGuard.current(
          assistantRequestGuard,
          "changeSet",
          requestRevision,
        )
      ) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
    }
  }

  async function loadMappingJob(
    elements,
    jobId,
    generation = assistantState.generation,
    expectedSessionId = assistantState.sessionId,
  ) {
    const id = strictUuid(jobId);
    if (!id) return;
    const requestRevision = assistantAsyncGuard.begin(
      assistantRequestGuard,
      "mapping",
    );
    const controller = assistantOperationController();
    try {
      const payload = await api(`/api/agent/spreadsheets/${encodeURIComponent(id)}`, {
        signal: controller.signal,
      });
      if (
        !assistantContextMatches(generation, expectedSessionId)
        || !assistantAsyncGuard.current(
          assistantRequestGuard,
          "mapping",
          requestRevision,
        )
      ) return;
      renderMappingJob(elements, payload);
    } catch (error) {
      if (
        error?.name !== "AbortError"
        && assistantContextMatches(generation, expectedSessionId)
        && assistantAsyncGuard.current(
          assistantRequestGuard,
          "mapping",
          requestRevision,
        )
      ) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
    }
  }

  function appendAssistantWorkflowNavigation(
    container,
    label,
    items,
    activeId,
    onSelect,
    truncated = false,
    onLoadMore = null,
  ) {
    if (items.length < 2 && !truncated) return;
    const navigation = agentNode("div", "assistant-review__actions");
    navigation.setAttribute("aria-label", label);
    items.forEach((item, index) => {
      const id = strictUuid(item?.id ?? item?.change_set_id ?? item?.job_id);
      if (!id) return;
      const status = text(item?.status, "item").toLowerCase();
      const button = agentNode(
        "button",
        "button button--ghost",
        `${index + 1}. ${status.replaceAll("_", " ")}`,
      );
      button.type = "button";
      button.disabled = id === activeId;
      button.addEventListener("click", () => onSelect(id));
      navigation.append(button);
    });
    if (truncated && onLoadMore) {
      const more = agentNode("button", "button button--ghost", "Load more");
      more.type = "button";
      more.addEventListener("click", () => onLoadMore(more));
      navigation.append(more);
    }
    container.append(navigation);
  }

  function renderChangeSet(elements, payload, warning = "") {
    const changeSet = normalizeChangeSet(payload);
    if (!changeSet) {
      elements.review.hidden = true;
      elements.review.replaceChildren();
      return;
    }
    assistantState.changeSet = changeSet;
    assistantState.reviewQueue = assistantAsyncGuard.upsertRecord(
      assistantState.reviewQueue,
      changeSet,
    );
    elements.review.replaceChildren();
    elements.review.hidden = false;

    const heading = agentNode("div", "assistant-review__heading");
    const titleBlock = agentNode("div");
    titleBlock.append(
      agentNode("p", "eyebrow", "Human review"),
      agentNode("h3", "", changeSet.status === "pending" ? "Staged changes" : "Change set"),
    );
    heading.append(titleBlock, agentNode("span", "badge", changeSet.status));
    elements.review.append(heading);
    appendAssistantWorkflowNavigation(
      elements.review,
      "Change sets in this chat",
      assistantState.reviewQueue,
      changeSet.id,
      (id) => loadChangeSet(elements, id),
      assistantState.reviewTruncated,
      (button) => loadMoreAssistantChangeSets(elements, button),
    );

    if (warning) {
      const alert = agentNode("div", "notice notice--warning");
      alert.setAttribute("role", "alert");
      alert.append(agentNode("span", "notice__icon", "!"), agentNode("span", "", warning));
      elements.review.append(alert);
    }

    const metadata = agentNode("dl", "assistant-review-fields");
    appendReviewField(metadata, "Revision", changeSet.revision);
    appendReviewField(metadata, "Preview hash", changeSet.previewHash || "Unavailable");
    appendReviewField(metadata, "Affected scopes", changeSet.scopes);
    elements.review.append(metadata);

    const commands = agentNode("div", "assistant-review__section");
    commands.append(agentNode("h4", "", "Ordered commands"));
    if (changeSet.items.length) {
      const list = agentNode("ol", "assistant-command-list");
      changeSet.items.forEach((item) => {
        const entry = agentNode("li");
        entry.append(
          agentNode("strong", "", item?.tool_name ?? item?.name ?? "Command"),
          agentNode("pre", "assistant-code", agentValue(item?.arguments ?? item?.args ?? {})),
        );
        list.append(entry);
      });
      commands.append(list);
    } else {
      commands.append(agentNode("p", "muted", "No command details were returned."));
    }
    elements.review.append(commands);

    const changes = agentNode("div", "assistant-review__section");
    changes.append(
      agentNode("h4", "", "Net before / after"),
      agentNode("pre", "assistant-code assistant-code--diff", agentValue(changeSet.diff)),
    );
    elements.review.append(changes);

    const actions = agentNode("div", "assistant-review__actions");
    if (changeSet.reviewBlocked) {
      actions.append(agentNode("p", "muted", "Spreadsheet rows are still being staged. Review controls will appear when the batch is complete."));
    } else if (changeSet.status === "pending" && assistantState.writesEnabled) {
      let acknowledgement = null;
      if (changeSet.containsHardDelete) {
        const label = agentNode("label", "assistant-destructive-ack");
        acknowledgement = document.createElement("input");
        acknowledgement.type = "checkbox";
        label.append(
          acknowledgement,
          agentNode("span", "", "I understand this permanently deletes assignment data and may remove companion rows."),
        );
        elements.review.append(label);
      }

      const discard = agentNode("button", "button button--ghost", "Discard");
      discard.type = "button";
      const confirm = agentNode("button", "button button--primary", "Confirm changes");
      confirm.type = "button";
      confirm.disabled = Boolean(acknowledgement);
      acknowledgement?.addEventListener("change", () => { confirm.disabled = !acknowledgement.checked; });
      discard.addEventListener("click", () => discardChangeSet(elements, changeSet, discard));
      confirm.addEventListener("click", () => applyChangeSet(elements, changeSet, Boolean(acknowledgement?.checked), confirm));
      actions.append(discard, confirm);
    } else if (changeSet.status === "applied" && assistantState.writesEnabled) {
      const undo = agentNode("button", "button button--secondary", "Undo applied changes");
      undo.type = "button";
      undo.addEventListener("click", () => undoChangeSet(elements, changeSet, undo));
      actions.append(undo);
    }
    if (!assistantState.writesEnabled && ["pending", "applied"].includes(changeSet.status)) {
      actions.append(agentNode("p", "muted", "Agent writes are disabled in this environment."));
    }
    elements.review.append(actions);
  }

  async function applyChangeSet(elements, displayedChangeSet, acknowledged, button) {
    if (!displayedChangeSet.previewHash) {
      setAssistantStatus(elements, "This preview has no valid confirmation hash. Refresh it before applying.", "error");
      return;
    }
    const generation = assistantState.generation;
    const expectedSessionId = assistantState.sessionId;
    const controller = assistantOperationController();
    setBusy(button, true, "Applying...");
    try {
      await api(`/api/agent/change-sets/${encodeURIComponent(displayedChangeSet.id)}/apply`, {
        method: "POST",
        signal: controller.signal,
        body: {
          revision: displayedChangeSet.revision,
          preview_hash: displayedChangeSet.previewHash,
          acknowledge_hard_delete: acknowledged,
        },
      });
      if (!assistantContextMatches(generation, expectedSessionId)) return;
      toast("The staged changes were applied.");
      await loadChangeSet(elements, displayedChangeSet.id, "", generation, expectedSessionId);
    } catch (error) {
      if (error?.name === "AbortError" || !assistantContextMatches(generation, expectedSessionId)) return;
      if (error.status === 409) {
        await loadChangeSet(elements, displayedChangeSet.id, "The warehouse changed after this preview. Review the refreshed values before confirming again.", generation, expectedSessionId);
      } else {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      setBusy(button, false);
    }
  }

  async function discardChangeSet(elements, displayedChangeSet, button) {
    const generation = assistantState.generation;
    const expectedSessionId = assistantState.sessionId;
    const controller = assistantOperationController();
    setBusy(button, true, "Discarding...");
    try {
      await api(`/api/agent/change-sets/${encodeURIComponent(displayedChangeSet.id)}/discard`, {
        method: "POST", body: {}, signal: controller.signal,
      });
      if (!assistantContextMatches(generation, expectedSessionId)) return;
      toast("The staged changes were discarded.");
      await loadChangeSet(elements, displayedChangeSet.id, "", generation, expectedSessionId);
    } catch (error) {
      if (error?.name !== "AbortError" && assistantContextMatches(generation, expectedSessionId)) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      setBusy(button, false);
    }
  }

  async function undoChangeSet(elements, displayedChangeSet, button) {
    const generation = assistantState.generation;
    const expectedSessionId = assistantState.sessionId;
    const controller = assistantOperationController();
    setBusy(button, true, "Undoing...");
    try {
      await api(`/api/agent/change-sets/${encodeURIComponent(displayedChangeSet.id)}/undo`, {
        method: "POST", body: {}, signal: controller.signal,
      });
      if (!assistantContextMatches(generation, expectedSessionId)) return;
      toast("The applied changes were undone.");
      await loadChangeSet(elements, displayedChangeSet.id, "", generation, expectedSessionId);
    } catch (error) {
      if (error?.name === "AbortError" || !assistantContextMatches(generation, expectedSessionId)) return;
      const message = error.status === 409
        ? "Undo was blocked because the affected warehouse rows changed after apply. Nothing was overwritten."
        : error.message;
      setAssistantStatus(elements, message, "error");
    } finally {
      finishAssistantOperation(controller);
      setBusy(button, false);
    }
  }

  function normalizeMappingJob(payload) {
    const job = payload?.job && typeof payload.job === "object" ? payload.job : payload;
    if (!job || typeof job !== "object" || Array.isArray(job)) return null;
    const id = strictUuid(job.id ?? job.job_id ?? payload?.job_id);
    const revision = Number(job.mapping_revision ?? payload?.mapping_revision);
    const mappingHash = text(job.mapping_hash ?? payload?.mapping_hash).trim();
    if (!id || !Number.isInteger(revision) || revision < 1 || !HASH_PATTERN.test(mappingHash)) return null;
    return {
      id,
      revision,
      mappingHash,
      status: text(job.status, "mapping_pending"),
      originalName: text(job.original_name ?? payload?.original_name, "Spreadsheet"),
      mapping: job.mapping ?? payload?.mapping ?? {},
      rejectedRows: Array.isArray(job.rejected_rows) ? job.rejected_rows : Array.isArray(payload?.rejected_rows) ? payload.rejected_rows : [],
    };
  }

  function renderMappingJob(elements, payload) {
    const job = normalizeMappingJob(payload);
    if (!job) {
      elements.mapping.hidden = true;
      elements.mapping.replaceChildren();
      return;
    }
    assistantState.mappingJob = job;
    assistantState.mappingQueue = assistantAsyncGuard.upsertRecord(
      assistantState.mappingQueue,
      job,
    );
    elements.mapping.replaceChildren();
    elements.mapping.hidden = false;

    const heading = agentNode("div", "assistant-review__heading");
    const title = agentNode("div");
    title.append(agentNode("p", "eyebrow", "Spreadsheet mapping"), agentNode("h3", "", job.originalName));
    heading.append(title, agentNode("span", "badge", job.status));
    elements.mapping.append(heading);
    appendAssistantWorkflowNavigation(
      elements.mapping,
      "Spreadsheet jobs in this chat",
      assistantState.mappingQueue,
      job.id,
      (id) => loadMappingJob(elements, id),
      assistantState.mappingTruncated,
      (button) => loadMoreAssistantSpreadsheetJobs(elements, button),
    );

    const fields = agentNode("dl", "assistant-review-fields");
    appendReviewField(fields, "Mapping revision", job.revision);
    appendReviewField(fields, "Mapping hash", job.mappingHash);
    elements.mapping.append(fields);
    elements.mapping.append(
      agentNode("h4", "", "Proposed column mapping"),
      agentNode("pre", "assistant-code", agentValue(job.mapping)),
    );

    if (job.rejectedRows.length) {
      elements.mapping.append(
        agentNode("h4", "", "Rows requiring attention"),
        agentNode("pre", "assistant-code", agentValue(job.rejectedRows)),
      );
    }

    if (["mapping_pending", "mapping_confirmed"].includes(job.status) && assistantState.writesEnabled) {
      const actions = agentNode("div", "assistant-review__actions");
      const confirm = agentNode(
        "button",
        "button button--primary",
        job.status === "mapping_confirmed" ? "Resume staging rows" : "Confirm mapping and stage rows",
      );
      confirm.type = "button";
      confirm.addEventListener("click", () => confirmMapping(elements, job, confirm));
      actions.append(confirm);
      elements.mapping.append(actions);
    }
  }

  async function confirmMapping(elements, displayedJob, button) {
    const generation = assistantState.generation;
    const expectedSessionId = assistantState.sessionId;
    const controller = assistantOperationController();
    setBusy(button, true, "Staging rows...");
    try {
      const result = await api(`/api/agent/spreadsheets/${encodeURIComponent(displayedJob.id)}/confirm-mapping`, {
        method: "POST",
        signal: controller.signal,
        body: {
          mapping_revision: displayedJob.revision,
          mapping_hash: displayedJob.mappingHash,
        },
      });
      if (!assistantContextMatches(generation, expectedSessionId)) return;
      renderMappingJob(elements, result);
      const changeSetId = strictUuid(result?.change_set_id ?? result?.change_set?.id);
      if (changeSetId) await loadChangeSet(elements, changeSetId, "", generation, expectedSessionId);
      if (text(result?.status).toLowerCase() === "rejected") {
        toast("No spreadsheet rows could be staged. Review the row errors.", "error");
      } else {
        toast("The spreadsheet rows were staged. Review the change set before confirming.");
      }
    } catch (error) {
      if (error?.name !== "AbortError" && assistantContextMatches(generation, expectedSessionId)) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      setBusy(button, false);
    }
  }

  async function uploadAssistantSpreadsheet(elements, file) {
    if (
      !assistantState.sessionId
      || assistantState.sessionLoading
      || assistantState.messageHistoryLoading
      || assistantState.streaming
    ) {
      setAssistantStatus(elements, "Start a chat before attaching a spreadsheet.", "error");
      elements.file.value = "";
      return;
    }
    const generation = assistantState.generation;
    const expectedSessionId = assistantState.sessionId;
    const controller = assistantOperationController();
    const formData = new FormData();
    formData.append("file", file);
    formData.append("session_id", assistantState.sessionId);
    const instruction = elements.spreadsheetInstruction.value.trim();
    if (instruction) formData.append("instruction", instruction);
    elements.attach.disabled = true;
    setAssistantStatus(elements, "Inspecting spreadsheet...");
    try {
      const result = await api("/api/agent/spreadsheets", {
        method: "POST", body: formData, signal: controller.signal,
      });
      if (!assistantContextMatches(generation, expectedSessionId)) return;
      renderMappingJob(elements, result);
      setAssistantStatus(elements, "Review and confirm the proposed mapping. No warehouse rows have changed.");
    } catch (error) {
      if (error?.name !== "AbortError" && assistantContextMatches(generation, expectedSessionId)) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      resetAssistantInteraction(elements);
      elements.file.value = "";
    }
  }

  function updateAssistantSession(event, generation) {
    if (generation !== assistantState.generation) return;
    const id = strictUuid(event?.session_id ?? event?.session?.id);
    if (id) assistantState.sessionId = id;
  }

  async function streamAssistantTurn(elements, message) {
    if (
      assistantState.streaming
      || assistantState.sessionLoading
      || assistantState.messageHistoryLoading
    ) return;
    const generation = assistantState.generation;
    const messageViewRevision = assistantAsyncGuard.begin(
      assistantRequestGuard,
      "message",
    );
    const localTurnKey = `local-${generation}-${++assistantState.localTurnSequence}`;
    assistantState.streaming = true;
    const controller = new AbortController();
    assistantState.controller = controller;
    resetAssistantInteraction(elements);
    setAssistantStatus(elements, "Assistant is working...");

    const empty = $(".assistant-empty", elements.messages);
    empty?.remove();
    agentMessage(elements.messages, "user", message);
    const assistantContent = agentMessage(elements.messages, "assistant");
    let sawText = false;
    let assistantText = "";
    let turnRemembered = false;

    const rememberVisibleTurn = () => {
      if (turnRemembered) return;
      assistantAsyncGuard.rememberTurn(
        assistantState,
        message,
        assistantText || assistantContent.textContent || "",
        localTurnKey,
      );
      turnRemembered = true;
    };

    try {
      const body = { message };
      if (assistantState.sessionId) body.session_id = assistantState.sessionId;
      const response = await fetch("/api/agent/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Accept": "text/event-stream",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (response.status === 401) {
        window.location.assign("/login?expired=1");
        throw new Error("Your session expired. Please sign in again.");
      }
      if (!response.ok) {
        const payload = await agentResponsePayload(response);
        throw new Error(errorMessage(payload, `Request failed (${response.status})`));
      }

      await consumeAssistantSse(response, (event) => {
        if (
          generation !== assistantState.generation
          || !assistantAsyncGuard.current(
            assistantRequestGuard,
            "message",
            messageViewRevision,
          )
        ) return;
        updateAssistantSession(event, generation);
        const eventType = text(event.type).toLowerCase();
        if (eventType === "token" || eventType === "text" || eventType === "delta") {
          const delta = text(event.text ?? event.delta ?? "");
          appendAgentText(assistantContent, delta);
          assistantText += delta;
          sawText = true;
        } else if (eventType === "tool") {
          appendToolChip(assistantContent, event.name ?? event.tool_name);
        } else if (eventType === "error") {
          throw new Error(text(event.message, "The assistant could not complete the request."));
        }

        const changeSetId = strictUuid(event.change_set_id ?? event.change_set?.id);
        if (event.change_set && typeof event.change_set === "object") renderChangeSet(elements, event);
        else if (changeSetId) loadChangeSet(
          elements,
          changeSetId,
          "",
          generation,
          assistantState.sessionId,
        );
      });

      if (
        generation !== assistantState.generation
        || !assistantAsyncGuard.current(
          assistantRequestGuard,
          "message",
          messageViewRevision,
        )
      ) return;
      if (!sawText && !assistantContent.childNodes.length) {
        assistantText = "The request completed without a text response.";
        appendAgentText(assistantContent, assistantText);
      }
      rememberVisibleTurn();
      setAssistantStatus(elements, "Ready");
      assistantState.historyLoaded = false;
    } catch (error) {
      if (
        generation !== assistantState.generation
        || !assistantAsyncGuard.current(
          assistantRequestGuard,
          "message",
          messageViewRevision,
        )
      ) return;
      const cancelled = error?.name === "AbortError";
      if (!assistantContent.childNodes.length) {
        assistantText = cancelled
          ? "Response stopped."
          : "The assistant could not complete this request.";
        appendAgentText(assistantContent, assistantText);
      }
      rememberVisibleTurn();
      setAssistantStatus(elements, cancelled ? "Response stopped." : error.message, cancelled ? "" : "error");
    } finally {
      if (
        generation !== assistantState.generation
        || !assistantAsyncGuard.current(
          assistantRequestGuard,
          "message",
          messageViewRevision,
        )
      ) return;
      assistantState.streaming = false;
      if (assistantState.controller === controller) assistantState.controller = null;
      resetAssistantInteraction(elements);
      elements.input.focus();
      elements.messages.scrollTop = elements.messages.scrollHeight;
    }
  }

  function renderAssistantMessageHistory(elements) {
    elements.messages.replaceChildren();
    if (assistantState.messagesTruncated && assistantState.messagesOldestCursor) {
      const older = agentNode("button", "button button--ghost", "Load older messages");
      older.type = "button";
      older.addEventListener("click", () => loadOlderAssistantMessages(elements, older));
      elements.messages.append(older);
    }
    assistantState.messages.forEach((message) => {
      const content = agentMessage(
        elements.messages,
        message?.role === "user" ? "user" : "assistant",
        message?.content ?? "",
      );
      if (bool(message?.content_truncated)) {
        content.append(agentNode(
          "span",
          "assistant-message__truncation muted",
          "Older message content was truncated by the history safety limit.",
        ));
      }
    });
    if (!assistantState.messages.length) emptyAssistantMessages(elements);
  }

  async function loadOlderAssistantMessages(elements, button) {
    const cursor = assistantState.messagesOldestCursor;
    const sessionId = assistantState.sessionId;
    const generation = assistantState.generation;
    const beforeId = strictUuid(cursor?.id);
    const beforeCreatedAt = text(cursor?.created_at).trim();
    if (
      !sessionId
      || !beforeId
      || !beforeCreatedAt
      || assistantState.streaming
      || assistantState.sessionLoading
      || assistantState.messageHistoryLoading
    ) return;
    const messageViewRevision = assistantAsyncGuard.begin(
      assistantRequestGuard,
      "message",
    );
    assistantState.messageHistoryLoading = true;
    resetAssistantInteraction(elements);
    const controller = assistantOperationController();
    setBusy(button, true, "Loading...");
    try {
      const query = new URLSearchParams({
        before_created_at: beforeCreatedAt,
        before_id: beforeId,
      });
      const payload = await api(
        `/api/agent/sessions/${encodeURIComponent(sessionId)}?${query}`,
        { signal: controller.signal },
      );
      if (
        !assistantContextMatches(generation, sessionId)
        || !assistantAsyncGuard.current(
          assistantRequestGuard,
          "message",
          messageViewRevision,
        )
      ) return;
      assistantAsyncGuard.mergeOlderMessages(assistantState, payload);
      renderAssistantMessageHistory(elements);
    } catch (error) {
      if (
        error?.name !== "AbortError"
        && assistantContextMatches(generation, sessionId)
        && assistantAsyncGuard.current(
          assistantRequestGuard,
          "message",
          messageViewRevision,
        )
      ) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      setBusy(button, false);
      if (
        assistantContextMatches(generation, sessionId)
        && assistantAsyncGuard.current(
          assistantRequestGuard,
          "message",
          messageViewRevision,
        )
      ) {
        assistantState.messageHistoryLoading = false;
        resetAssistantInteraction(elements);
      }
    }
  }

  async function loadMoreAssistantChangeSets(elements, button) {
    const cursor = assistantState.reviewOldestCursor;
    const sessionId = assistantState.sessionId;
    const generation = assistantState.generation;
    const beforePriority = Number(cursor?.priority);
    const beforeId = strictUuid(cursor?.id);
    const beforeUpdatedAt = text(cursor?.updated_at).trim();
    if (
      !sessionId
      || !Number.isInteger(beforePriority)
      || ![0, 1].includes(beforePriority)
      || !beforeId
      || !beforeUpdatedAt
    ) return;
    const requestRevision = assistantAsyncGuard.begin(
      assistantRequestGuard,
      "reviewPage",
    );
    const controller = assistantOperationController();
    setBusy(button, true, "Loading...");
    try {
      const query = new URLSearchParams({
        change_set_before_priority: String(beforePriority),
        change_set_before_updated_at: beforeUpdatedAt,
        change_set_before_id: beforeId,
      });
      const payload = await api(
        `/api/agent/sessions/${encodeURIComponent(sessionId)}?${query}`,
        { signal: controller.signal },
      );
      if (
        !assistantContextMatches(generation, sessionId)
        || !assistantAsyncGuard.current(
          assistantRequestGuard,
          "reviewPage",
          requestRevision,
        )
      ) return;
      const page = Array.isArray(payload?.change_sets) ? payload.change_sets : [];
      assistantState.reviewQueue = mergeAssistantRecords(
        assistantState.reviewQueue,
        page,
      );
      assistantState.reviewTruncated = bool(payload?.change_sets_truncated);
      assistantState.reviewOldestCursor = payload?.change_sets_oldest_cursor ?? null;
      const activeId = assistantState.changeSet?.id
        || strictUuid(assistantState.reviewQueue[0]?.id);
      if (activeId) {
        await loadChangeSet(elements, activeId, "", generation, sessionId);
      }
    } catch (error) {
      if (
        error?.name !== "AbortError"
        && assistantContextMatches(generation, sessionId)
        && assistantAsyncGuard.current(
          assistantRequestGuard,
          "reviewPage",
          requestRevision,
        )
      ) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      setBusy(button, false);
    }
  }

  async function loadMoreAssistantSpreadsheetJobs(elements, button) {
    const cursor = assistantState.mappingOldestCursor;
    const sessionId = assistantState.sessionId;
    const generation = assistantState.generation;
    const beforePriority = Number(cursor?.priority);
    const beforeId = strictUuid(cursor?.id);
    const beforeCreatedAt = text(cursor?.created_at).trim();
    if (
      !sessionId
      || !Number.isInteger(beforePriority)
      || ![0, 1].includes(beforePriority)
      || !beforeId
      || !beforeCreatedAt
    ) return;
    const requestRevision = assistantAsyncGuard.begin(
      assistantRequestGuard,
      "mappingPage",
    );
    const controller = assistantOperationController();
    setBusy(button, true, "Loading...");
    try {
      const query = new URLSearchParams({
        spreadsheet_before_priority: String(beforePriority),
        spreadsheet_before_created_at: beforeCreatedAt,
        spreadsheet_before_id: beforeId,
      });
      const payload = await api(
        `/api/agent/sessions/${encodeURIComponent(sessionId)}?${query}`,
        { signal: controller.signal },
      );
      if (
        !assistantContextMatches(generation, sessionId)
        || !assistantAsyncGuard.current(
          assistantRequestGuard,
          "mappingPage",
          requestRevision,
        )
      ) return;
      const page = Array.isArray(payload?.spreadsheet_jobs)
        ? payload.spreadsheet_jobs
        : [];
      assistantState.mappingQueue = mergeAssistantRecords(
        assistantState.mappingQueue,
        page,
      );
      assistantState.mappingTruncated = bool(payload?.spreadsheet_jobs_truncated);
      assistantState.mappingOldestCursor = payload?.spreadsheet_jobs_oldest_cursor ?? null;
      const activeId = assistantState.mappingJob?.id
        || strictUuid(assistantState.mappingQueue[0]?.id);
      if (activeId) {
        await loadMappingJob(elements, activeId, generation, sessionId);
      }
    } catch (error) {
      if (
        error?.name !== "AbortError"
        && assistantContextMatches(generation, sessionId)
        && assistantAsyncGuard.current(
          assistantRequestGuard,
          "mappingPage",
          requestRevision,
        )
      ) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      setBusy(button, false);
    }
  }

  async function renderAssistantHistory(elements, payload, generation, sessionId) {
    assistantState.messages = Array.isArray(payload?.messages)
      ? payload.messages
      : [];
    assistantState.messagesTruncated = bool(payload?.messages_truncated);
    assistantState.messagesOldestCursor = payload?.messages_oldest_cursor ?? null;
    renderAssistantMessageHistory(elements);

    const changeSets = Array.isArray(payload?.change_sets) ? payload.change_sets : [];
    assistantState.reviewQueue = changeSets.filter((item) => (
      strictUuid(item?.id ?? item?.change_set_id)
    ));
    assistantState.reviewTruncated = bool(payload?.change_sets_truncated);
    assistantState.reviewOldestCursor = payload?.change_sets_oldest_cursor ?? null;
    const latestChangeSet = changeSets.find((item) => (
      text(item?.status).toLowerCase() === "pending"
      && Number.isFinite(Date.parse(item?.expires_at))
      && Date.parse(item.expires_at) > Date.now()
    ))
      || changeSets.find((item) => text(item?.status).toLowerCase() === "applied")
      || changeSets[0]
      || null;
    const latestChangeSetId = strictUuid(latestChangeSet?.id ?? latestChangeSet?.change_set_id);
    if (latestChangeSetId) {
      // The history projection is intentionally bounded. Always fetch the
      // canonical detail before exposing a confirmation control.
      elements.review.replaceChildren();
      elements.review.hidden = true;
      await loadChangeSet(elements, latestChangeSetId, "", generation, sessionId);
      if (!assistantContextMatches(generation, sessionId)) return;
    } else {
      assistantState.changeSet = null;
      elements.review.replaceChildren();
      elements.review.hidden = true;
    }

    const spreadsheetJobs = Array.isArray(payload?.spreadsheet_jobs) ? payload.spreadsheet_jobs : [];
    assistantState.mappingQueue = spreadsheetJobs.filter((item) => (
      strictUuid(item?.id ?? item?.job_id)
    ));
    assistantState.mappingTruncated = bool(payload?.spreadsheet_jobs_truncated);
    assistantState.mappingOldestCursor = payload?.spreadsheet_jobs_oldest_cursor ?? null;
    const latestMappingJob = spreadsheetJobs.find((item) => ["mapping_pending", "mapping_confirmed"].includes(text(item?.status).toLowerCase()))
      || spreadsheetJobs.find((item) => text(item?.status).toLowerCase() === "mapping_processing")
      || spreadsheetJobs[0]
      || null;
    const latestMappingJobId = strictUuid(latestMappingJob?.id ?? latestMappingJob?.job_id);
    if (latestMappingJobId) {
      await loadMappingJob(elements, latestMappingJobId, generation, sessionId);
    }
    else {
      assistantState.mappingJob = null;
      elements.mapping.replaceChildren();
      elements.mapping.hidden = true;
    }
  }

  async function selectAssistantSession(elements, sessionId) {
    const id = strictUuid(sessionId);
    if (!id) return;
    const generation = advanceAssistantGeneration(elements);
    assistantState.sessionId = id;
    assistantState.changeSet = null;
    assistantState.mappingJob = null;
    assistantState.reviewQueue = [];
    assistantState.reviewTruncated = false;
    assistantState.reviewOldestCursor = null;
    assistantState.mappingQueue = [];
    assistantState.mappingTruncated = false;
    assistantState.mappingOldestCursor = null;
    elements.review.replaceChildren();
    elements.review.hidden = true;
    elements.mapping.replaceChildren();
    elements.mapping.hidden = true;
    assistantAsyncGuard.beginSessionLoad(
      assistantState,
      elements.messages,
      agentNode("div", "assistant-empty", "Loading previous chat..."),
    );
    resetAssistantInteraction(elements);
    setAssistantStatus(elements, "Loading previous chat...");
    let loaded = false;
    const controller = assistantOperationController();
    try {
      const payload = await api(`/api/agent/sessions/${encodeURIComponent(id)}`, {
        signal: controller.signal,
      });
      if (!assistantContextMatches(generation, id)) return;
      await renderAssistantHistory(elements, payload, generation, id);
      if (!assistantContextMatches(generation, id)) return;
      loaded = true;
      elements.sessionList.hidden = true;
      elements.historyToggle.textContent = "Show";
      elements.historyToggle.setAttribute("aria-expanded", "false");
      setAssistantStatus(elements, "Loaded previous chat.");
    } catch (error) {
      if (error?.name !== "AbortError" && assistantContextMatches(generation, id)) {
        elements.messages.replaceChildren(
          agentNode(
            "div",
            "assistant-empty",
            "This chat could not be loaded. Choose another chat or start a new one.",
          ),
        );
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      if (assistantContextMatches(generation, id) && loaded) {
        assistantState.sessionLoading = false;
        resetAssistantInteraction(elements);
        elements.input.focus();
      }
    }
  }

  function renderAssistantSessionList(elements) {
    elements.sessionList.replaceChildren();
    if (!assistantState.sessions.length) {
      elements.sessionList.append(agentNode("p", "muted", "No previous chats."));
      return;
    }
    assistantState.sessions.forEach((session) => {
      const id = strictUuid(session?.id ?? session?.session_id);
      if (!id) return;
      const button = agentNode("button", "assistant-session", session?.title || "Untitled chat");
      button.type = "button";
      button.addEventListener("click", () => selectAssistantSession(elements, id));
      elements.sessionList.append(button);
    });
    if (assistantState.sessionsTruncated && assistantState.sessionsOldestCursor) {
      const more = agentNode("button", "assistant-session", "Load older chats");
      more.type = "button";
      more.addEventListener("click", () => loadAssistantSessions(elements, true, more));
      elements.sessionList.append(more);
    }
  }

  async function loadAssistantSessions(elements, append = false, button = null) {
    if (assistantState.historyLoaded && !append) return;
    const generation = assistantState.generation;
    const query = new URLSearchParams();
    if (append) {
      const cursor = assistantState.sessionsOldestCursor;
      const beforeId = strictUuid(cursor?.id);
      const beforeUpdatedAt = text(cursor?.updated_at).trim();
      if (!beforeId || !beforeUpdatedAt) return;
      query.set("before_updated_at", beforeUpdatedAt);
      query.set("before_id", beforeId);
    }
    const controller = assistantOperationController();
    if (button) setBusy(button, true, "Loading...");
    try {
      const queryString = query.toString();
      const payload = await api(
        `/api/agent/sessions${queryString ? `?${queryString}` : ""}`,
        { signal: controller.signal },
      );
      if (!assistantGenerationMatches(generation)) return;
      const sessions = Array.isArray(payload) ? payload : Array.isArray(payload?.sessions) ? payload.sessions : [];
      assistantState.sessions = append
        ? mergeAssistantRecords(assistantState.sessions, sessions)
        : sessions.filter((session) => strictUuid(session?.id ?? session?.session_id));
      assistantState.sessionsTruncated = bool(payload?.sessions_truncated);
      assistantState.sessionsOldestCursor = payload?.sessions_oldest_cursor ?? null;
      renderAssistantSessionList(elements);
      assistantState.historyLoaded = true;
    } catch (error) {
      if (error?.name !== "AbortError" && assistantGenerationMatches(generation)) {
        setAssistantStatus(elements, error.message, "error");
      }
    } finally {
      finishAssistantOperation(controller);
      if (button) setBusy(button, false);
    }
  }

  function resetAssistant(elements) {
    advanceAssistantGeneration(elements);
    assistantState.sessionId = null;
    assistantState.changeSet = null;
    assistantState.mappingJob = null;
    assistantState.messages = [];
    assistantState.messagesTruncated = false;
    assistantState.messagesOldestCursor = null;
    assistantState.reviewQueue = [];
    assistantState.reviewTruncated = false;
    assistantState.reviewOldestCursor = null;
    assistantState.mappingQueue = [];
    assistantState.mappingTruncated = false;
    assistantState.mappingOldestCursor = null;
    elements.review.replaceChildren();
    elements.review.hidden = true;
    elements.mapping.replaceChildren();
    elements.mapping.hidden = true;
    emptyAssistantMessages(elements);
    setAssistantStatus(elements, "New chat ready.");
    resetAssistantInteraction(elements);
    elements.input.focus();
  }

  function setAssistantOpen(elements, open) {
    elements.panel.hidden = !open;
    elements.backdrop.hidden = !open;
    elements.panel.setAttribute("aria-hidden", String(!open));
    elements.toggle.setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("assistant-open", open);
    if (open) {
      loadAssistantSessions(elements);
      window.requestAnimationFrame(() => elements.input.focus());
    } else {
      elements.toggle.focus();
    }
  }

  function initAssistant() {
    const panel = $("#assistant-panel");
    const toggle = $("#assistant-toggle");
    if (!panel || !toggle) return;
    if (!assistantAsyncGuard || !assistantRequestGuard) {
      $("#assistant-backdrop")?.remove();
      panel.remove();
      toggle.remove();
      return;
    }
    assistantState.writesEnabled = panel.dataset.writesEnabled === "true";
    const elements = {
      panel,
      toggle,
      backdrop: $("#assistant-backdrop"),
      close: $("#assistant-close"),
      newChat: $("#assistant-new"),
      historyToggle: $("#assistant-history-toggle"),
      sessionList: $("#assistant-session-list"),
      status: $("#assistant-status"),
      messages: $("#assistant-messages"),
      review: $("#assistant-review"),
      mapping: $("#assistant-mapping"),
      form: $("#assistant-form"),
      input: $("#assistant-input"),
      send: $("#assistant-send"),
      stop: $("#assistant-stop"),
      attach: $("#assistant-attach"),
      file: $("#assistant-file"),
      spreadsheetInstruction: $("#assistant-spreadsheet-instruction"),
    };

    toggle.addEventListener("click", () => setAssistantOpen(elements, panel.hidden));
    elements.close.addEventListener("click", () => setAssistantOpen(elements, false));
    elements.backdrop.addEventListener("click", () => setAssistantOpen(elements, false));
    elements.newChat.addEventListener("click", () => resetAssistant(elements));
    elements.historyToggle.addEventListener("click", async () => {
      const show = elements.sessionList.hidden;
      elements.sessionList.hidden = !show;
      elements.historyToggle.textContent = show ? "Hide" : "Show";
      elements.historyToggle.setAttribute("aria-expanded", String(show));
      if (show) await loadAssistantSessions(elements);
    });
    elements.form.addEventListener("submit", (event) => {
      event.preventDefault();
      const message = elements.input.value.trim();
      if (
        !message
        || assistantState.streaming
        || assistantState.sessionLoading
        || assistantState.messageHistoryLoading
      ) return;
      elements.input.value = "";
      streamAssistantTurn(elements, message);
    });
    elements.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        elements.form.requestSubmit();
      }
    });
    elements.stop.addEventListener("click", () => assistantState.controller?.abort());
    if (elements.attach && elements.file) {
      elements.attach.addEventListener("click", () => elements.file.click());
      elements.file.addEventListener("change", () => {
        const file = elements.file.files?.[0];
        if (file) uploadAssistantSpreadsheet(elements, file);
      });
    }
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !panel.hidden) setAssistantOpen(elements, false);
    });
    return elements;
  }

  // ===== Bulk-apply panel =====

  const bulkState = {
    requestSequence: 0,
  };

  async function bulkSearchDesigns(query) {
    const sequence = ++bulkState.requestSequence;
    const list = $("#bulk-logo-options");
    const input = $("#bulk-logo-search");
    showEmptyOption(list, query.trim() ? "Searching..." : "Loading designs...");
    setOptionsOpen(input, list, true);
    try {
      const params = new URLSearchParams({ q: query.trim() });
      if (state.store) params.set("store", state.store);
      const payload = await api(`/api/designs?${params}`);
      if (sequence !== bulkState.requestSequence) return;
      const designs = envelope(payload, "designs");
      list.replaceChildren();
      if (!designs.length) { showEmptyOption(list, "No matching designs"); return; }
      designs.forEach((design) => {
        const id = text(design.design_id ?? design.id);
        const code = designCode(design);
        const schemes = Array.isArray(design.schemes) ? design.schemes : [];
        // Surface each (logo_code, color_scheme_id) pair as a distinct option.
        // If the design has no embedded schemes we still offer it as one entry and let the
        // operator pick the scheme text manually after - but normally designs carry schemes.
        if (schemes.length) {
          schemes.forEach((scheme) => {
            const schemeId = text(scheme.color_scheme_id ?? scheme.scheme_id ?? scheme.id);
            if (!schemeId || scheme.is_colorway === false) return;
            const schemeName = text(scheme.name ?? scheme.description, schemeId);
            const variantCode = code || "?";
            appendOption(list, {
              title: `${variantCode} · ${schemeName}`,
              subtitle: text(design.description ?? design.web_description, `Design ${id}`),
              meta: `Design ${id} · Scheme ${schemeId}`,
              onSelect: () => {
                $("#bulk-logo-search").value = `${variantCode} · ${schemeName}`;
                setOptionsOpen($("#bulk-logo-search"), list, false);
                $("#bulk-logo-code").value = variantCode;
                $("#bulk-logo-scheme").value = schemeId;
                const selectedLogo = $("#bulk-selected-logo");
                selectedLogo.textContent = `Logo code: ${variantCode} · Color scheme: ${schemeId}`;
                selectedLogo.hidden = false;
                // Auto-set light/dark class default from variant code suffix,
                // but never overwrite a choice the user already made.
                const cls = /BK/i.test(schemeId) ? "light" : /WH/i.test(schemeId) ? "dark" : null;
                if (cls && !bulkClassTouched) {
                  $("#bulk-class").value = cls;
                  selectedLogo.textContent += ` · Target set to ${cls} garments (change it below if needed)`;
                }
              },
            });
          });
        } else {
          appendOption(list, {
            title: code ? `${code} - ${text(design.description ?? design.web_description, `Design ${id}`)}` : text(design.description ?? design.web_description, `Design ${id}`),
            meta: `Design ${id}`,
            onSelect: () => {
              $("#bulk-logo-search").value = code || text(design.description, `Design ${id}`);
              setOptionsOpen($("#bulk-logo-search"), list, false);
              $("#bulk-logo-code").value = code;
              $("#bulk-logo-scheme").value = "";
              const selectedLogo = $("#bulk-selected-logo");
              selectedLogo.textContent = `Logo code: ${code} - no color scheme selected yet`;
              selectedLogo.hidden = false;
            },
          });
        }
      });
    } catch (error) {
      showEmptyOption(list, error.message);
    }
  }

  let bulkClassTouched = false;
  let bulkPlacementWired = false;

  function renderBulkPlacementOptions(query = "") {
    const input = $("#bulk-placement");
    const list = $("#bulk-placement-options");
    if (!input || !list) return;
    const vocab = state.vocab || { placements: [] };
    const q = query.trim().toLowerCase();
    const matches = (vocab.placements || []).filter((item) => !q || text(item.location).toLowerCase().includes(q));
    list.replaceChildren();
    if (!matches.length) {
      showEmptyOption(list, q ? "No matching placements - you can type a new one" : "No placements yet");
    } else {
      matches.slice(0, 60).forEach((item) => {
        appendOption(list, {
          title: text(item.location),
          onSelect: () => {
            input.value = text(item.location);
            setOptionsOpen(input, list, false);
          },
        });
      });
    }
    setOptionsOpen(input, list, true);
  }

  function wireBulkPlacement() {
    if (bulkPlacementWired) return;
    const input = $("#bulk-placement");
    const list = $("#bulk-placement-options");
    if (!input || !list) return;
    bulkPlacementWired = true;
    input.addEventListener("input", () => renderBulkPlacementOptions(input.value));
    input.addEventListener("focus", () => renderBulkPlacementOptions(input.value));
    input.addEventListener("blur", () => setTimeout(() => setOptionsOpen(input, list, false), 200));
    bindListKeyboard(input, list);
  }

  async function bulkPreview() {
    const logoCode = $("#bulk-logo-code").value.trim();
    const logoScheme = $("#bulk-logo-scheme").value.trim();
    if (!logoCode) { toast("Choose a logo variant first.", "error"); return; }
    if (!state.store) { toast("No store selected.", "error"); return; }
    const targetMode = document.querySelector('input[name="bulk-target"]:checked').value;
    const body = {
      fdm4_store: state.store,
      logo_code: logoCode,
      color_scheme: logoScheme,
      target: targetMode === "light_dark"
        ? { mode: "light_dark", class: $("#bulk-class").value }
        : { mode: "colors", color_codes: bulkSelectedColors() },
    };
    const previewBtn = $("#bulk-preview-btn");
    setBusy(previewBtn, true, "Previewing...");
    try {
      const res = await api("/api/bulk-apply/preview", { method: "POST", body });
      const tb = document.querySelector("#bulk-preview-table tbody");
      tb.replaceChildren();
      const summary = $("#bulk-preview-summary");
      const tableWrap = $("#bulk-preview-table-wrap");
      if (res.unresolved_reason) {
        summary.textContent = res.unresolved_reason;
        tableWrap.hidden = true;
        $("#bulk-apply-btn").disabled = true;
        return;
      }
      res.rows.forEach((r) => {
        const tr = document.createElement("tr");
        const was = r.was ? `${escapeHtml(r.was.logo_code)}-${escapeHtml(r.was.color_scheme)}` : "-";
        const checkboxLabel = `Include ${r.style_code} - ${r.color} (${r.color_code})`;
        tr.innerHTML = `<td><input type="checkbox" checked aria-label="${escapeHtml(checkboxLabel)}"></td><td>${escapeHtml(r.style_code)}</td><td>${escapeHtml(r.color)}</td><td>${escapeHtml(r.new.logo_code)}-${escapeHtml(r.new.color_scheme)} <span class="muted">(was ${was})</span></td>`;
        tr.dataset.style = r.style_code;
        tr.dataset.color = r.color_code;
        tb.append(tr);
      });
      summary.textContent = `${res.counts.total} product${res.counts.total === 1 ? "" : "s"}` +
        (res.counts.unclassified ? ` - ${res.counts.unclassified} color${res.counts.unclassified === 1 ? "" : "s"} unclassified (add in Colors tab)` : "");
      tableWrap.hidden = res.rows.length === 0;
      $("#bulk-apply-btn").disabled = res.rows.length === 0;
      $("#bulk-preview-filter").value = "";
      $("#bulk-preview-shown").textContent = "";
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(previewBtn, false);
    }
  }

  async function bulkApply() {
    const rows = Array.from(document.querySelectorAll("#bulk-preview-table tbody tr"))
      .filter((tr) => tr.querySelector("input[type='checkbox']").checked)
      .map((tr) => ({ style_code: tr.dataset.style, color_code: tr.dataset.color }));
    if (!rows.length) { toast("No rows selected.", "error"); return; }
    const placement = $("#bulk-placement").value;
    if (!placement) { toast("Choose a placement first.", "error"); return; }
    const ok = await confirmAction({
      title: "Apply this logo?",
      message: `Apply ${$("#bulk-logo-code").value}/${$("#bulk-logo-scheme").value} (${placement}) to ${rows.length} checked row${rows.length === 1 ? "" : "s"} on ${storeDisplayFor(state.store)}? You can undo this batch afterwards. Nothing reaches the website until you sync the store.`,
      actionLabel: `Apply to ${rows.length} row${rows.length === 1 ? "" : "s"}`,
      danger: false,
    });
    if (!ok) return;
    const applyBtn = $("#bulk-apply-btn");
    setBusy(applyBtn, true, "Applying...");
    try {
      const res = await api("/api/bulk-apply/execute", { method: "POST", body: {
        fdm4_store: state.store,
        logo_code: $("#bulk-logo-code").value,
        color_scheme: $("#bulk-logo-scheme").value,
        placement,
        rows,
      }});
      const warn = res.image_url_missing
        ? ` (${res.image_url_missing} row${res.image_url_missing === 1 ? "" : "s"} had no image - import the logo to this store first)`
        : "";
      const resultEl = $("#bulk-result");
      resultEl.innerHTML = `Applied ${escapeHtml(String(res.applied))}.${escapeHtml(warn)} Sync this store to push live. <button type="button" id="bulk-undo-btn" class="button button--ghost button--small">Undo</button>`;
      $("#bulk-undo-btn").addEventListener("click", (e) => bulkUndo(res.batch_id, e.currentTarget));
      await loadBulkHistory();
      toast(`Bulk apply complete - ${res.applied} assignment${res.applied === 1 ? "" : "s"} written.`);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(applyBtn, false);
      // setBusy re-enables unconditionally; keep Apply disabled after a
      // success so the same checked rows can't be double-applied.
      if ($("#bulk-undo-btn")) applyBtn.disabled = true;
    }
  }

  async function bulkUndo(batchId, button = null) {
    const undoBtn = button || $("#bulk-undo-btn");
    if (undoBtn) setBusy(undoBtn, true, "Undoing...");
    try {
      const res = await api("/api/bulk-apply/undo", { method: "POST", body: { batch_id: batchId } });
      const restored = Number(res.restored ?? 0);
      const skipped = Number(res.skipped ?? 0);
      $("#bulk-result").textContent = `Undone - ${restored} row${restored === 1 ? "" : "s"} restored` +
        (skipped ? `; ${skipped} newer edit${skipped === 1 ? "" : "s"} preserved.` : ".");
      await loadBulkHistory();
      toast(skipped ? "Bulk undo preserved newer edits." : "Bulk apply undone.");
    } catch (error) {
      toast(error.message, "error");
      if (undoBtn) setBusy(undoBtn, false);
    }
  }

  async function loadBulkHistory() {
    const box = $("#bulk-history");
    if (!box || !state.store) return;
    try {
      const payload = await api(`/api/bulk-apply/batches?${new URLSearchParams({ store: state.store, limit: "10" })}`);
      const batches = envelope(payload, "batches");
      if (!batches.length) {
        box.textContent = "No bulk batches for this store yet.";
        return;
      }
      box.innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>When</th><th>Logo</th><th>Rows</th><th>Actor</th><th>Status</th></tr></thead><tbody>${batches.map((batch) => `<tr><td>${escapeHtml(formatDate(batch.created_at))}</td><td><code>${escapeHtml(batch.logo_code)}-${escapeHtml(batch.color_scheme)}</code><br><small>${escapeHtml(batch.placement)}</small></td><td>${escapeHtml(batch.applied)}</td><td>${escapeHtml(batch.created_by)}</td><td>${batch.undone_at ? `Undone ${escapeHtml(formatDate(batch.undone_at))}` : `<button type="button" class="button button--ghost button--small bulk-history-undo" data-batch="${escapeHtml(batch.batch_id)}">Undo</button>`}</td></tr>`).join("")}</tbody></table></div>`;
      $$(".bulk-history-undo", box).forEach((button) => {
        button.addEventListener("click", () => bulkUndo(Number(button.dataset.batch), button));
      });
    } catch (error) {
      box.innerHTML = `<div class="grid-empty">${escapeHtml(friendlyLoadError("the bulk history", error))}</div>`;
    }
  }

  async function openBulkApplyPanel() {
    const panel = $("#bulk-apply-panel");
    if (!state.store) { toast("Choose a store first.", "error"); return; }
    // Fill store name
    $("#bulk-store-name").textContent = storeDisplayFor(state.store);
    // Reset state
    $("#bulk-logo-search").value = "";
    $("#bulk-logo-code").value = "";
    $("#bulk-logo-scheme").value = "";
    $("#bulk-selected-logo").hidden = true;
    $("#bulk-selected-logo").textContent = "";
    document.querySelector('input[name="bulk-target"][value="light_dark"]').checked = true;
    $("#bulk-class").value = "dark";
    bulkClassTouched = false;
    document.querySelector("#bulk-preview-table tbody").replaceChildren();
    $("#bulk-preview-table-wrap").hidden = true;
    $("#bulk-preview-summary").textContent = "";
    $("#bulk-result").textContent = "";
    $("#bulk-apply-btn").disabled = true;
    panel.hidden = false;
    await loadBulkHistory();
    // Placement is the same searchable combobox as the assignment dialog,
    // reading the shared vocabulary; the field keeps its last value.
    await ensureVocab();
    wireBulkPlacement();
    // Populate the color checkbox list from /api/colors (store-specific).
    // A filterable tick-list replaces the old Ctrl/Cmd-click multi-select,
    // which quietly lost selections for anyone unfamiliar with the shortcut.
    const colorList = $("#bulk-colors-list");
    try {
      const selectedBefore = new Set(bulkSelectedColors());
      const params = new URLSearchParams({ limit: "500" });
      if (state.store) params.set("store", state.store);
      const { colors } = await api(`/api/colors?${params}`);
      colorList.replaceChildren();
      colors.forEach((c) => {
        const label = document.createElement("label");
        label.className = "field--checkbox bulk-color-option";
        label.dataset.haystack = `${c.color_name} ${c.color_code} ${c.light_dark}`.toLowerCase();
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = c.color_code;
        cb.checked = selectedBefore.has(c.color_code);
        cb.addEventListener("change", updateBulkColorsCount);
        label.append(cb, document.createTextNode(` ${c.color_name} (${c.color_code}) - ${c.light_dark}`));
        colorList.append(label);
      });
      if (!colors.length) colorList.innerHTML = '<span class="muted">No colors found for this store.</span>';
      updateBulkColorsCount();
    } catch {
      colorList.innerHTML = '<span class="muted">Couldn\'t load the color list - you can still target by light/dark.</span>';
    }
  }

  function bulkSelectedColors() {
    return $$("#bulk-colors-list input[type='checkbox']").filter((cb) => cb.checked).map((cb) => cb.value);
  }

  function updateBulkColorsCount() {
    const el = $("#bulk-colors-count");
    if (el) el.textContent = String(bulkSelectedColors().length);
  }


  // ----- Logo sync ownership (which stores this app may sync) -----
  const ownershipState = { rows: [] };

  async function openOwnershipDialog() {
    openDialog($("#ownership-dialog"));
    await loadOwnership();
  }

  async function loadOwnership() {
    const box = $("#ownership-list");
    box.innerHTML = '<div class="grid-empty">Loading...</div>';
    $("#ownership-count").textContent = "";
    try {
      await ensureStores();
      const resp = await api("/api/logo-ownership");
      ownershipState.rows = Array.isArray(resp.stores) ? resp.stores.slice() : [];
      renderOwnership();
    } catch (e) { renderErrorState(box, friendlyLoadError("the store list", e), loadOwnership); }
  }

  function renderOwnership() {
    const box = $("#ownership-list");
    const q = ($("#ownership-filter")?.value || "").trim().toLowerCase();
    const rows = ownershipState.rows
      .map((r) => ({ ...r, label: storeDisplayFor(r.fdm4_store) }))
      .filter((r) => !q || `${r.fdm4_store} ${r.label}`.toLowerCase().includes(q))
      .sort((a, b) => (Number(b.owned) - Number(a.owned)) || a.label.localeCompare(b.label));
    const total = ownershipState.rows.length;
    const on = ownershipState.rows.filter((r) => r.owned).length;
    $("#ownership-count").textContent = total ? `${on} of ${total} stores sync their logos from this app` : "";
    if (!rows.length) {
      box.innerHTML = total ? '<div class="grid-empty">No stores match your filter.</div>' : '<div class="grid-empty">No mapped stores found.</div>';
      return;
    }
    box.innerHTML = `<table class="data-table"><thead><tr><th>Store</th><th>Logo sync</th><th></th></tr></thead><tbody>${rows.map((r) => `
      <tr data-store="${escapeHtml(r.fdm4_store)}">
        <td><strong>${escapeHtml(r.label)}</strong><br><code>${escapeHtml(r.fdm4_store)}</code></td>
        <td>${r.owned ? '<span class="chip dark">On - synced from this app</span>' : '<span class="chip">Off - legacy sheets</span>'}</td>
        <td class="name-actions">${r.owned
          ? '<button class="button button--small button--ghost own-off" type="button">Turn off</button>'
          : '<button class="button button--small button--primary own-on" type="button">Turn on...</button>'}</td>
      </tr>`).join("")}</tbody></table>`;
    $$(".own-on", box).forEach((b) => b.addEventListener("click", () => enableOwnership(b.closest("tr").dataset.store, b)));
    $$(".own-off", box).forEach((b) => b.addEventListener("click", () => disableOwnership(b.closest("tr").dataset.store, b)));
  }

  async function enableOwnership(store, btn) {
    const name = storeDisplayFor(store);
    setBusy(btn, true, "Checking safety...");
    let preview;
    try {
      preview = await api(`/api/logo-ownership/preview?${new URLSearchParams({ store })}`);
    } catch (e) {
      setBusy(btn, false);
      toast(`Safety check failed - sync was NOT turned on. ${e.message}`, "error");
      return;
    }
    setBusy(btn, false);
    let ok;
    let ack = 0;
    if (preview.safe) {
      ok = await confirmAction({
        title: `Turn on logo sync for ${name}?`,
        message: preview.wp_logo_styles
          ? `Safety check passed: all ${preview.wp_logo_styles} styles that currently have logos on the website are covered by this app's data - nothing will be lost. From now on this app controls the store's logos; press Sync to push changes live.`
          : `This store has no logos on the website yet, so nothing can be lost. From now on this app controls the store's logos; press Sync to push changes live.`,
        actionLabel: "Turn on sync",
        danger: false,
      });
    } else {
      const missing = preview.missing.map((m) => text(m.style));
      const shown = missing.slice(0, 10).join(", ") + (missing.length > 10 ? ` and ${missing.length - 10} more` : "");
      ok = await confirmAction({
        title: `${missing.length} style${missing.length === 1 ? "" : "s"} would lose logos`,
        message: `${name} has ${preview.wp_logo_styles} styles with logos on the website, but ${missing.length} (${shown}) have no data in this app. Turning sync on removes their logos from the website on the next sync. Import this store's legacy sheets first (Logos menu) unless you really mean to remove them.`,
        actionLabel: `Turn on anyway - remove ${missing.length}`,
      });
      ack = missing.length;
    }
    if (!ok) return;
    setBusy(btn, true, "Turning on...");
    try {
      await api("/api/logo-ownership", { method: "POST", body: { fdm4_store: store, owned: true, acknowledge_missing: ack } });
      toast(`Logo sync is ON for ${name}. Open the store and press Sync to push its logos live.`);
      if (state.store === store && els.ownershipWarning) els.ownershipWarning.hidden = true;
      await loadOwnership();
    } catch (e) { setBusy(btn, false); toast(e.message, "error"); }
  }

  async function disableOwnership(store, btn) {
    const name = storeDisplayFor(store);
    const ok = await confirmAction({
      title: `Turn off logo sync for ${name}?`,
      message: `The website keeps the logos it has today, but this app can no longer sync changes to ${name} until sync is turned back on. Edits made here stay saved in the meantime.`,
      actionLabel: "Turn off sync",
    });
    if (!ok) return;
    setBusy(btn, true, "Turning off...");
    try {
      await api("/api/logo-ownership", { method: "POST", body: { fdm4_store: store, owned: false, acknowledge_missing: 0 } });
      toast(`Logo sync is OFF for ${name}.`);
      await loadOwnership();
    } catch (e) { setBusy(btn, false); toast(e.message, "error"); }
  }

  function wireEvents() {
    els.storeSearch.addEventListener("input", () => renderStoreOptions(els.storeSearch.value));
    els.storeSearch.addEventListener("focus", () => renderStoreOptions(els.storeSearch.value));
    els.storeSearch.addEventListener("blur", () => setTimeout(() => {
      // Typed-over text without a new pick: restore the active store's label
      // so the field never disagrees with the workspace below it.
      setOptionsOpen(els.storeSearch, els.storeOptions, false);
      if (!state.store) return;
      const rec = storeByCode(state.store);
      const label = rec ? storeInputLabel(rec) : state.store;
      if (els.storeSearch.value !== label) els.storeSearch.value = label;
    }, 220));
    bindListKeyboard(els.storeSearch, els.storeOptions);
    els.styleSearch.addEventListener("input", debounce(() => searchStyles(els.styleSearch.value)));
    els.styleActiveOnly.addEventListener("change", () => {
      if (state.store) searchStyles(els.styleSearch.value);
    });
    els.styleAssignedOnly.addEventListener("change", () => {
      if (state.store) searchStyles(els.styleSearch.value);
    });
    els.styleSearch.addEventListener("focus", () => {
      if (state.styles.length) renderStyleOptions(state.styles, els.styleOptions, els.styleSearch, selectStyle);
      else searchStyles(els.styleSearch.value);
    });
    bindListKeyboard(els.styleSearch, els.styleOptions);

    $("#refresh-button").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      setBusy(btn, true, "");
      try {
        if (state.style) await refreshStyle(); else if (state.store) await searchStyles(els.styleSearch.value); else await loadStores();
        toast("Warehouse data refreshed.");
      } catch (error) {
        toast(friendlyLoadError("fresh warehouse data", error), "error");
      } finally { setBusy(btn, false); }
    });
    els.settingsForm.addEventListener("submit", saveSettings);
    els.storeSettingsButton.addEventListener("click", openStoreSettings);
    els.styleActive.addEventListener("change", async () => {
      els.styleActive.indeterminate = false;
      if (!state.store || !state.style) return;
      const active = els.styleActive.checked;
      const count = (state.detail?.assignments || []).length;
      const ok = await confirmAction(active
        ? { title: "Turn on every logo on this style?", message: `All ${count || "its"} logo assignment${count === 1 ? "" : "s"} on style ${state.style} will show on the website after the next sync of this store.`, actionLabel: "Turn all on", danger: false }
        : { title: "Turn off every logo on this style?", message: `All ${count || "its"} logo assignment${count === 1 ? "" : "s"} on style ${state.style} will disappear from the website after the next sync of this store. Nothing is deleted - you can turn them back on.`, actionLabel: "Turn all off" });
      if (!ok) {
        els.styleActive.checked = !active;
        return;
      }
      els.styleActive.disabled = true;
      try {
        await api("/api/style-active", {
          method: "POST",
          body: { store: state.store, style: state.style, active },
        });
        toast(`All logo assignments on this style are now ${active ? "on" : "off"}. Sync the store to update the website.`);
        await refreshStyle();
      } catch (error) {
        toast(error.message, "error");
        els.styleActive.checked = !active;
        els.styleActive.disabled = false;
      }
    });

    els.assignmentForm.addEventListener("submit", saveAssignment);
    els.designSearch.addEventListener("input", debounce(() => searchDesigns(els.designSearch.value)));
    els.designSearch.addEventListener("focus", () => searchDesigns(els.designSearch.value));
    els.location.addEventListener("input", async () => { await ensureVocab(); renderPlacementOptions(els.location.value); });
    els.location.addEventListener("focus", async () => { await ensureVocab(); renderPlacementOptions(els.location.value); });
    bindListKeyboard(els.location, els.placementVocabOptions);
    bindListKeyboard(els.designSearch, els.designOptions);
    els.scheme.addEventListener("change", () => updateDesignPreview());
    els.background.addEventListener("change", () => updateDesignPreview());
    els.upload.addEventListener("change", uploadImage);
    els.softRemove.addEventListener("click", () => removeAssignment(false));
    els.hardRemove.addEventListener("click", () => removeAssignment(true));
    els.applyColors.addEventListener("click", applyAllColors);
    $("#copy-style-button").addEventListener("click", openCopyDialog);
    els.copySearch.addEventListener("input", debounce(() => { els.copySource.value = ""; searchStyles(els.copySearch.value, "copy"); }));
    els.copySearch.addEventListener("focus", () => searchStyles(els.copySearch.value, "copy"));
    bindListKeyboard(els.copySearch, els.copyOptions);
    els.copyForm.addEventListener("submit", copyStyle);
    $("#import-button").addEventListener("click", () => els.importFile.click());
    els.importFile.addEventListener("change", importCsv);
    $("#legacy-import-button").addEventListener("click", () => {
      els.legacyResults.innerHTML = "";
      els.legacyForm.reset();
      openDialog(els.legacyDialog);
    });
    els.legacyForm.addEventListener("submit", importLegacySheets);
    $("#legacy-images-button").addEventListener("click", mirrorLegacyImages);
    $("#reports-button").addEventListener("click", () => { openDialog(els.reportsDialog); loadReports(); });
    els.reportFilters.addEventListener("submit", loadReports);
    $("#ownership-button")?.addEventListener("click", openOwnershipDialog);
    $("#dash-ownership")?.addEventListener("click", openOwnershipDialog);
    $("#ownership-warning-open")?.addEventListener("click", openOwnershipDialog);
    $("#ownership-filter")?.addEventListener("input", () => renderOwnership());
    $("#audit-button").addEventListener("click", () => {
      populateAuditStores();
      openDialog(els.auditDialog);
      loadAudit(true);
    });
    els.auditFilters.addEventListener("submit", (event) => {
      event.preventDefault();
      state.audit.filters = {
        store: els.auditStore.value,
        style: els.auditStyle.value.trim(),
        actor: els.auditActor.value.trim(),
        action: els.auditAction.value,
      };
      loadAudit(true);
    });
    els.auditMore.addEventListener("click", async () => {
      setBusy(els.auditMore, true, "Loading...");
      try { await loadAudit(false); } finally { setBusy(els.auditMore, false); }
    });
    $("#sync-style-button").addEventListener("click", () => sync("style"));
    $("#sync-store-button").addEventListener("click", () => sync("store"));

    $$(".dialog-close").forEach((button) => button.addEventListener("click", () => closeDialog(button.closest("dialog"))));
    $$('dialog').forEach((dialog) => dialog.addEventListener("click", (event) => {
      if (event.target === dialog && dialog !== els.confirmDialog) closeDialog(dialog);
    }));
    document.addEventListener("click", (event) => {
      [[els.storeSearch, els.storeOptions], [els.styleSearch, els.styleOptions], [els.designSearch, els.designOptions], [els.location, els.placementVocabOptions], [els.copySearch, els.copyOptions]].forEach(([input, list]) => {
        if (!input.contains(event.target) && !list.contains(event.target)) setOptionsOpen(input, list, false);
      });
      // Close bulk-logo combobox on outside click.
      const bulkInput = $("#bulk-logo-search");
      const bulkList = $("#bulk-logo-options");
      if (bulkInput && bulkList && !bulkInput.contains(event.target) && !bulkList.contains(event.target)) {
        setOptionsOpen(bulkInput, bulkList, false);
      }
    });

    // Bulk-apply panel wiring
    $("#bulk-apply-open").addEventListener("click", openBulkApplyPanel);
    $("#bulk-apply-close").addEventListener("click", () => { $("#bulk-apply-panel").hidden = true; });
    $("#bulk-preview-btn").addEventListener("click", bulkPreview);
    $("#bulk-class")?.addEventListener("change", () => { bulkClassTouched = true; });
    $("#bulk-colors-filter")?.addEventListener("input", () => {
      const q = $("#bulk-colors-filter").value.trim().toLowerCase();
      $$("#bulk-colors-list .bulk-color-option").forEach((label) => {
        label.hidden = q !== "" && !(label.dataset.haystack || "").includes(q);
      });
    });
    $("#bulk-apply-btn").addEventListener("click", bulkApply);
    $("#bulk-all").addEventListener("change", (event) => {
      // Only toggle rows the current filter leaves visible.
      document.querySelectorAll("#bulk-preview-table tbody tr:not([hidden]) input[type='checkbox']").forEach((cb) => { cb.checked = event.target.checked; });
    });
    $("#bulk-preview-filter").addEventListener("input", () => {
      const q = $("#bulk-preview-filter").value.trim().toLowerCase();
      let shown = 0, total = 0;
      document.querySelectorAll("#bulk-preview-table tbody tr").forEach((tr) => {
        total++;
        const hit = !q || tr.textContent.toLowerCase().includes(q);
        tr.hidden = !hit;
        if (hit) shown++;
      });
      $("#bulk-preview-shown").textContent = q ? `${shown} of ${total} shown` : "";
    });
    $("#report-quick-filter").addEventListener("input", () => {
      const q = $("#report-quick-filter").value.trim().toLowerCase();
      document.querySelectorAll("#report-results tbody tr").forEach((tr) => {
        tr.hidden = Boolean(q) && !tr.textContent.toLowerCase().includes(q);
      });
    });
    $("#tier-filter").addEventListener("input", () => renderTierAssignments());
    const bulkLogoInput = $("#bulk-logo-search");
    const bulkLogoList = $("#bulk-logo-options");
    bulkLogoInput.addEventListener("input", debounce(() => bulkSearchDesigns(bulkLogoInput.value)));
    bulkLogoInput.addEventListener("focus", () => bulkSearchDesigns(bulkLogoInput.value));
    bindListKeyboard(bulkLogoInput, bulkLogoList);
  }

  // ===== Warehouse Operations: view switching + pricing management =====
  const VIEWS = ["dashboard", "logo", "pricing", "names", "colors", "prices", "blocks", "mix", "stock", "health", "help"];
  function switchView(name) {
    if (!VIEWS.includes(name)) name = "dashboard";
    VIEWS.forEach((v) => { const el = $(`#view-${v}`); if (el) el.hidden = v !== name; });
    $$(".main-nav__link, .main-nav__item").forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
    $$(".main-nav__group").forEach((g) => {
      const trigger = g.querySelector(".main-nav__trigger");
      if (trigger) trigger.classList.toggle("is-active", (g.dataset.group || "").split(",").includes(name));
    });
    document.body.dataset.view = name;
    const url = new URL(window.location.href);
    url.searchParams.set("view", name);
    window.history.replaceState({}, "", url);
    if (name === "pricing") loadPricing();
    if (name === "names") loadNames();
    if (name === "colors") loadColors();
    if (name === "prices") loadPriceRules();
    if (name === "blocks") loadSyncBlocks();
    if (name === "mix") loadProductMix();
    if (name === "stock") loadStockOverrides();
    if (name === "health") loadHealth();
    healthTimerSync(name);
  }

  async function ensureStores() {
    if (!state.stores.length) {
      try { await loadStores(); }
      catch {
        if (!ensureStores.warned) {
          ensureStores.warned = true;
          toast("Couldn't load the store list - store pickers may look empty. Reload the page to retry.", "error");
        }
      }
    }
  }

  async function loadPricing() {
    await ensureStores();
    prefillStoreField("#tier-store-search", "#tier-store-code");
    loadTiers();
    loadAssignments();
  }

  async function loadTiers() {
    $("#tier-list").innerHTML = '<div class="grid-empty">Loading...</div>';
    try {
      const tiers = envelope(await api("/api/pricing/tiers"), "tiers");
      state.tiers = tiers;
      const sel = $("#tier-select");
      sel.replaceChildren(new Option("Choose a pricing level...", ""));
      tiers.forEach((t) => sel.add(new Option(t.tier_name + (t.is_msrp ? " (same as no level - full retail)" : ""), t.tier_name)));
      if (!tiers.length) { $("#tier-list").innerHTML = '<div class="grid-empty">No pricing levels are defined yet.</div>'; return; }
      $("#tier-list").innerHTML = `<table class="data-table"><thead><tr><th>Pricing level</th><th>Used when FDM4 sends no price?</th></tr></thead><tbody>${tiers.map((t) => `<tr><td>${escapeHtml(t.tier_name)}</td><td>${t.is_msrp ? "No - full retail price (MSRP)" : "Yes"}</td></tr>`).join("")}</tbody></table>`;
    } catch (e) { renderErrorState($("#tier-list"), friendlyLoadError("the pricing levels", e), loadTiers); }
  }

  let tierRows = [];
  let tierListTruncated = false;
  const tierSort = { key: "store", dir: "asc" };

  function renderTierAssignments() {
    const box = $("#tier-assignments");
    if (!tierRows.length) { box.innerHTML = '<div class="grid-empty">No stores are on a backup pricing level. Stores that already price correctly do not need one.</div>'; return; }
    const q = ($("#tier-filter")?.value || "").trim().toLowerCase();
    const rows = tierRows.filter((r) => !q || `${text(r.display_name, r.fdm4_store)} ${r.fdm4_store} ${r.tier_name} ${text(r.note)}`.toLowerCase().includes(q));
    if (!rows.length) { box.innerHTML = '<div class="grid-empty">No tier assignments match that filter.</div>'; return; }
    const keyFns = {
      store: (r) => text(r.display_name, r.fdm4_store).toLowerCase(),
      tier: (r) => text(r.tier_name).toLowerCase(),
      updated: (r) => text(r.updated_at),
    };
    const keyFn = keyFns[tierSort.key] || keyFns.store;
    rows.sort((a, b) => { const ka = keyFn(a), kb = keyFn(b); return (ka < kb ? -1 : ka > kb ? 1 : 0) * (tierSort.dir === "desc" ? -1 : 1); });
    const arrow = (k) => (tierSort.key === k ? (tierSort.dir === "desc" ? " ▼" : " ▲") : " ↕");
    const ariaSort = (k) => (tierSort.key === k ? (tierSort.dir === "desc" ? "descending" : "ascending") : "none");
    const sortTh = (k, label) => `<th data-tsort="${k}" role="button" tabindex="0" aria-sort="${ariaSort(k)}" title="Sort by ${label.toLowerCase()}">${label}${arrow(k)}</th>`;
    box.innerHTML = `<table class="data-table"><thead><tr>${sortTh("store", "Store")}${sortTh("tier", "Level")}<th>Note</th>${sortTh("updated", "Updated")}<th></th></tr></thead><tbody>${rows.map((r) => `<tr>
        <td><strong>${escapeHtml(text(r.display_name, r.fdm4_store))}</strong><br><code>${escapeHtml(r.fdm4_store)}</code></td>
        <td>${escapeHtml(r.tier_name)}</td>
        <td>${escapeHtml(text(r.note))}</td>
        <td>${escapeHtml(formatDate(r.updated_at))}</td>
        <td><button class="button button--ghost button--small tier-remove" type="button" data-store="${escapeHtml(r.fdm4_store)}">Remove</button></td>
      </tr>`).join("")}</tbody></table>${tierListTruncated ? '<p class="muted" style="padding:.4rem .2rem 0">Showing the first 500 stores - use the filter to narrow the list.</p>' : ""}`;
    $$(".tier-remove", box).forEach((b) => b.addEventListener("click", () => removeTier(b.dataset.store, b)));
    $$("th[data-tsort]", box).forEach((th) => {
      const toggle = () => {
        const k = th.dataset.tsort;
        if (tierSort.key === k) { tierSort.dir = tierSort.dir === "asc" ? "desc" : "asc"; }
        else { tierSort.key = k; tierSort.dir = "asc"; }
        renderTierAssignments();
      };
      th.addEventListener("click", toggle);
      th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
  }

  async function loadAssignments() {
    const box = $("#tier-assignments");
    box.innerHTML = '<div class="grid-empty">Loading...</div>';
    try {
      const resp = await api("/api/pricing/store-tiers");
      tierRows = envelope(resp, "assignments");
      tierListTruncated = resp?.truncated === true;
      renderTierAssignments();
    } catch (e) { renderErrorState(box, friendlyLoadError("the store pricing levels", e), loadAssignments); }
  }

  async function removeTier(store, btn = null) {
    const accepted = await confirmAction({ title: "Remove this pricing level?", message: `Remove the backup pricing level for ${storeDisplayFor(store)}? Items with no price from FDM4 will go back to full retail price (MSRP) within the hour.`, actionLabel: "Remove", danger: true });
    if (!accepted) return;
    if (btn) btn.disabled = true;
    try { await api(`/api/pricing/store-tier?${new URLSearchParams({ fdm4_store: store })}`, { method: "DELETE" }); toast("Pricing level removed."); loadAssignments(); }
    catch (e) { if (btn) btn.disabled = false; toast(e.message, "error"); }
  }

  // Prefill a single-store FORM field (tier assignment, sync-block creation)
  // from the global active store when the field is still empty. Form fields
  // never write back to the active store - only context selectors do.
  function prefillStoreField(searchSel, hiddenSel) {
    const hiddenEl = $(hiddenSel), searchEl = $(searchSel);
    if (!hiddenEl || !searchEl || hiddenEl.value || searchEl.value) return;
    if (!state.store) return;
    hiddenEl.value = state.store;
    const rec = storeByCode(state.store);
    // Same label the combobox writes on pick, so the field never shows the
    // same store two different ways.
    searchEl.value = rec ? `${storeDisplay(rec)} (${storeMeta(rec)})` : `${storeDisplayFor(state.store)} (${state.store})`;
  }

  async function saveTier(event) {
    event.preventDefault();
    const store = $("#tier-store-code").value.trim();
    const tier = $("#tier-select").value;
    if (!store) { toast("Choose a store.", "error"); return; }
    if (!tier) { toast("Choose a pricing level.", "error"); return; }
    const existing = tierRows.find((r) => r.fdm4_store === store);
    const changeText = existing && existing.tier_name !== tier
      ? `Change ${storeDisplayFor(store)} from ${existing.tier_name} to ${tier}?`
      : `Put ${storeDisplayFor(store)} on ${tier}?`;
    const ok = await confirmAction({
      title: "Change this store's backup pricing?",
      message: `${changeText} Any product FDM4 hasn't priced will use ${tier} prices across the whole store, starting within the hour.`,
      actionLabel: "Save",
      danger: false,
    });
    if (!ok) return;
    const button = $("#tier-form button[type='submit']");
    setBusy(button, true, "Saving...");
    try {
      await api("/api/pricing/store-tier", { method: "PUT", body: { fdm4_store: store, tier_name: tier, note: $("#tier-note").value.trim() } });
      toast(`Saved: ${storeDisplayFor(store)} is on ${tier}. Prices update within the hour.`);
      $("#tier-form").reset(); $("#tier-store-code").value = "";
      loadAssignments();
    } catch (e) { toast(e.message, "error"); } finally { setBusy(button, false); }
  }

  // ----- Logo names -----
  const namesState = { q: "", filter: "", limit: 50, offset: 0, total: 0 };
  let namesSearchTimer = null;

  function syncNamesStoreSelect() {
    attachStoreCombobox({
      search: "#names-store-search", hidden: "#names-store", options: "#names-store-options",
      allLabel: "All stores - global names",
      onPick: (code) => selectStore(code),
    });
    const hidden = $("#names-store"), searchEl = $("#names-store-search");
    if (hidden) hidden.value = state.store || "";
    if (searchEl && document.activeElement !== searchEl) {
      searchEl.value = state.store ? `${storeDisplayFor(state.store)} (${state.store})` : "";
    }
  }

  async function loadNames() {
    const box = $("#names-list");
    box.innerHTML = '<div class="grid-empty">Loading...</div>';
    syncNamesStoreSelect();
    const context = $("#names-context");
    if (context) {
      context.textContent = state.store
        ? `Showing only the logos used by ${storeDisplayFor(state.store)}. Name edits apply to this store only; rows marked "This store only" have a name set just for this store.`
        : "No store selected - showing every named logo across all stores. Edits to shared rows change the name on every store that doesn't have its own custom name.";
    }
    // "Unnamed only" needs a store: unnamed logos only exist relative to a
    // store's assignments, so the all-stores list can never show them.
    if (!state.store && namesState.filter === "unnamed") {
      box.innerHTML = '<div class="grid-empty">Pick a store first - "Unnamed only" lists the logos that store uses which have no name yet.</div>';
      $("#names-pager").hidden = true;
      return;
    }
    $("#names-prev").disabled = true;
    $("#names-next").disabled = true;
    try {
      const params = new URLSearchParams({ q: namesState.q, store: state.store || "", filter: namesState.filter, limit: namesState.limit, offset: namesState.offset });
      const resp = await api(`/api/logo-names?${params}`);
      namesState.total = resp.total || 0;
      renderNames(envelope(resp, "names"));
    } catch (e) { renderErrorState(box, friendlyLoadError("the logo names", e), loadNames); $("#names-pager").hidden = true; }
  }

  function renderNames(rows) {
    const box = $("#names-list");
    if (!rows.length) {
      const msg = namesState.q
        ? "No logos match that search."
        : (namesState.filter ? "No logos match this filter." : (state.store ? "This store has no logos yet." : "No logo names yet."));
      box.innerHTML = `<div class="grid-empty">${msg}</div>`;
      $("#names-pager").hidden = true;
      return;
    }
    box.innerHTML = `<table class="data-table"><thead><tr><th>Logo</th><th>Color</th><th>Name (shown to customers)</th><th>Source</th><th></th></tr></thead><tbody>${rows.map((r) => `<tr data-design="${escapeHtml(r.design_id)}" data-scheme="${escapeHtml(r.color_scheme_id)}" data-rowstore="${escapeHtml(state.store ? state.store : text(r.fdm4_store))}" data-fdm4desc="${escapeHtml(text(r.fdm4_description))}" data-override="${r.store_specific && state.store ? "1" : ""}">
        <td><strong>${escapeHtml(text(r.logo_code, "-"))}</strong><br><code title="FDM4 design number">D${escapeHtml(r.design_id)}</code>${r.art_id ? `<br><small class="muted" title="FDM4 artwork number">art ${escapeHtml(r.art_id)}</small>` : ""}</td>
        <td><code>${escapeHtml(r.color_scheme_id)}</code></td>
        <td><input class="name-input" type="text" value="${escapeHtml(r.name)}" data-original="${escapeHtml(r.name)}" maxlength="200" aria-label="Logo name"></td>
        <td><span class="name-source${r.locked ? " name-source--edited" : ""}">${r.locked ? "edited" : escapeHtml(text(r.source, "unnamed"))}</span>${r.store_specific ? `<br><span class="badge-override">${state.store ? "This store only" : `Only for ${escapeHtml(storeDisplayFor(text(r.fdm4_store)))}`}</span>` : (state.store ? '<br><small class="muted">shared name (all stores)</small>' : "")}</td>
        <td class="name-actions">
          <button class="button button--primary button--small name-save" type="button">Save</button>
          <button class="button button--ghost button--small name-repull" type="button" title="Refresh this design's name from FDM4's current description">Refresh from FDM4</button>
        </td>
      </tr>`).join("")}</tbody></table>`;
    $$(".name-save", box).forEach((b) => b.addEventListener("click", () => { const tr = b.closest("tr"); saveName(tr, b); }));
    $$(".name-input", box).forEach((inp) => inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); const tr = inp.closest("tr"); saveName(tr, $(".name-save", tr)); }
    }));
    $$(".name-repull", box).forEach((b) => b.addEventListener("click", () => { const tr = b.closest("tr"); repullName(tr.dataset.design, b, tr.dataset.fdm4desc || "", tr.dataset.override === "1"); }));
    const start = namesState.offset + 1, end = namesState.offset + rows.length;
    $("#names-range").textContent = `${start}-${end} of ${namesState.total}`;
    $("#names-prev").disabled = namesState.offset === 0;
    $("#names-next").disabled = end >= namesState.total;
    $("#names-pager").hidden = false;
  }

  async function saveName(tr, button) {
    const design = tr.dataset.design;
    const scheme = tr.dataset.scheme;
    const input = $(".name-input", tr);
    const rowStore = tr.dataset.rowstore || "";
    const name = (input.value || "").trim();
    if (!name) { toast("Name can't be empty.", "error"); return; }
    if (name === (input.dataset.original || "")) {
      toast("No change to save - the name is the same.");
      return;
    }
    if (!rowStore) {
      const ok = await confirmAction({
        title: "Change this name on all stores?",
        message: `"${name}" becomes the shopper-facing name on every store that doesn't have its own custom name for this logo. It also stops future FDM4 refreshes from changing it.`,
        actionLabel: "Save for all stores",
        danger: false,
      });
      if (!ok) return;
    }
    setBusy(button, true, "Saving...");
    try {
      await api("/api/logo-names", { method: "PUT", body: { design_id: design, color_scheme_id: scheme, name, fdm4_store: rowStore } });
      toast(rowStore ? `Name saved for ${storeDisplayFor(rowStore)} only. It appears on the store website after the next sync.` : "Shared name saved for all stores. It appears on the store websites after the next sync.");
      loadNames();
    } catch (e) { toast(e.message, "error"); } finally { setBusy(button, false); }
  }

  async function repullName(design, button, fdm4Desc = "", hasOverride = false) {
    const descLine = fdm4Desc ? ` FDM4 currently calls it "${fdm4Desc}".` : "";
    const overrideLine = hasOverride ? " This refreshes the shared name; this store's own custom name still wins here." : "";
    const accepted = await confirmAction({ title: "Refresh from FDM4?", message: `Refresh design D${design}'s name(s) from FDM4's current description.${descLine} Names you've edited by hand are kept.${overrideLine}`, actionLabel: "Refresh", danger: false });
    if (!accepted) return;
    setBusy(button, true, "Refreshing...");
    try {
      const resp = await api("/api/logo-names/repull", { method: "POST", body: { design_id: design, force: false } });
      toast(resp.changed ? `Updated ${resp.changed} name(s) from FDM4.` : "Nothing changed - the name either already matches FDM4, was hand-edited (kept), or FDM4 has no description for it.");
      loadNames();
    } catch (e) { toast(e.message, "error"); } finally { setBusy(button, false); }
  }

  // ----- Colors review -----

  const colorsState = { sort: "", dir: "asc", limit: 50, offset: 0, total: 0 };

  async function loadColors() {
    const q = $("#color-search").value;
    const rev = $("#color-review-only").checked;
    const cls = $("#color-class-filter").value;
    const params = new URLSearchParams({ q, needs_review: rev, cls, sort: colorsState.sort, direction: colorsState.dir, limit: String(colorsState.limit), offset: String(colorsState.offset) });
    const tb = document.querySelector("#color-table tbody");
    tb.replaceChildren();
    document.getElementById("color-summary").textContent = "Loading...";
    try {
      const resp = await api(`/api/colors?${params}`);
      const colors = resp.colors || [];
      colorsState.total = resp.total ?? colors.length;
      const s = resp.summary || {};
      tb.replaceChildren();
      // Sort indicators on headers (aria-sort for screen readers, glyph for eyes).
      $$("#color-table thead th[data-sort]").forEach((th) => {
        const base = th.textContent.replace(/ [▲▼↕]$/, "");
        const isActive = th.dataset.sort === colorsState.sort;
        th.textContent = isActive ? `${base} ${colorsState.dir === "desc" ? "▼" : "▲"}` : `${base} ↕`;
        th.setAttribute("aria-sort", isActive ? (colorsState.dir === "desc" ? "descending" : "ascending") : "none");
      });
      const sourceLabel = (v) => (v === "ai" ? "AI guess" : v === "manual" ? "Set by staff" : text(v));
      if (!colors.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = '<td colspan="6" class="grid-empty">No colors match these filters.</td>';
        tb.append(tr);
      }
      colors.forEach((c) => {
        const tr = document.createElement("tr");
        const confPct = c.confidence === null || c.confidence === undefined || c.source === "manual"
          ? "" : `${Math.round(Number(c.confidence) * 100)}%`;
        tr.innerHTML = `<td>${escapeHtml(c.color_name)}</td><td>${escapeHtml(c.color_code)}</td>
          <td>${c.style_count}</td>
          <td><button class="chip ${escapeHtml(c.light_dark)}" type="button" title="Click to cycle light → dark → both">${escapeHtml(c.light_dark)}</button></td>
          <td class="color-source-cell">${escapeHtml(sourceLabel(c.source))}</td><td class="color-conf-cell">${confPct}</td>`;
        const chip = tr.querySelector("button");
        chip.addEventListener("click", async () => {
          const next = c.light_dark === "light" ? "dark" : (c.light_dark === "dark" ? "both" : "light");
          chip.disabled = true;
          try {
            await api("/api/colors", { method: "PUT", body: { color_code: c.color_code, light_dark: next } });
            // Update the row in place - a full reload re-sorts the table and
            // shifts rows under the reviewer's cursor mid-workflow.
            c.light_dark = next;
            c.source = "manual";
            c.confidence = null;
            chip.className = `chip ${next}`;
            chip.textContent = next;
            tr.querySelector(".color-source-cell").textContent = "Set by staff";
            tr.querySelector(".color-conf-cell").textContent = "";
          } catch (e) { toast(e.message, "error"); }
          finally { chip.disabled = false; }
        });
        tb.append(tr);
      });
      document.getElementById("color-summary").textContent =
        `${colorsState.total} colors - ${s.light ?? 0} light / ${s.dark ?? 0} dark / ${s.both ?? 0} both - ${s.review ?? 0} need review`
        + (resp.truncated ? " - too many results to count exactly, narrow your search" : "");
      const pager = $("#color-pager");
      if (colorsState.total > colorsState.limit) {
        pager.hidden = false;
        const start = colorsState.offset + 1;
        const end = Math.min(colorsState.offset + colors.length, colorsState.total);
        $("#color-range").textContent = `${start}-${end} of ${colorsState.total}`;
        $("#color-prev").disabled = colorsState.offset <= 0;
        $("#color-next").disabled = end >= colorsState.total;
      } else {
        pager.hidden = true;
      }
    } catch (e) {
      tb.replaceChildren();
      document.getElementById("color-summary").textContent = friendlyLoadError("the colors", e);
      $("#color-pager").hidden = true;
      toast(e.message, "error");
    }
  }

  // ----- Sync blocks -----
  const sbState = { blocks: [] };

  async function loadSyncBlocks() {
    const box = $("#sb-list");
    box.innerHTML = '<div class="grid-empty">Loading...</div>';
    try {
      await ensureStores();
      attachStoreCombobox({ search: "#sb-store-search", hidden: "#sb-store", options: "#sb-store-options" });
      prefillStoreField("#sb-store-search", "#sb-store");
      const resp = await api("/api/sync-blocks");
      sbState.blocks = resp.blocks || [];
      renderSyncBlocks();
    } catch (e) { renderErrorState(box, friendlyLoadError("the sync freezes", e), loadSyncBlocks); }
  }

  // Plain-language description of what turning a block OFF means, per scope.
  function sbOffConsequence(b) {
    if (b.style_code) return `Style ${b.style_code} starts updating from FDM4 again on ${storeDisplayFor(b.fdm4_store)} within the hour.`;
    if (b.scope === "pricing") return `The hourly sync will start changing ${storeDisplayFor(b.fdm4_store)}'s prices again within the hour. Any hand-set prices will be overwritten by FDM4 prices.`;
    return `${storeDisplayFor(b.fdm4_store)} starts syncing normally again within the hour (prices, stock, and product updates resume).`;
  }

  function renderSyncBlocks() {
    const box = $("#sb-list");
    const q = ($("#sb-search")?.value || "").trim().toLowerCase();
    const rows = sbState.blocks.filter((b) => !q || `${b.fdm4_store} ${storeDisplayFor(b.fdm4_store)} ${b.style_code} ${text(b.note)}`.toLowerCase().includes(q));
    if (!rows.length) {
      box.innerHTML = sbState.blocks.length
        ? '<div class="grid-empty">No freezes match your search.</div>'
        : '<div class="grid-empty">No freezes - every store and product updates normally.</div>';
      return;
    }
    box.innerHTML = `<table class="data-table"><thead><tr><th>Store</th><th>What's frozen</th><th>Reason</th><th>On/Off</th><th>Updated</th><th></th></tr></thead><tbody>${rows.map((b) => `<tr data-store="${escapeHtml(b.fdm4_store)}" data-style="${escapeHtml(b.style_code)}">
      <td><strong>${escapeHtml(storeDisplayFor(b.fdm4_store))}</strong><br><code>${escapeHtml(b.fdm4_store)}</code></td>
      <td>${b.style_code ? `style <code>${escapeHtml(b.style_code)}</code>` : (b.scope === "pricing" ? '<span class="chip">PRICES ONLY</span>' : '<span class="chip dark">ENTIRE STORE</span>')}</td>
      <td class="note-cell">${escapeHtml(text(b.note))}</td>
      <td><button class="chip ${b.active ? "dark" : ""} sb-toggle" type="button" aria-pressed="${b.active ? "true" : "false"}" title="Click to turn this freeze on or off">${b.active ? (b.scope === "pricing" && !b.style_code ? "Prices frozen" : "On") : "Off"}</button></td>
      <td>${escapeHtml(formatDate(b.updated_at))}<br><small class="muted">${escapeHtml(text(b.updated_by))}</small></td>
      <td><button class="button button--small button--ghost sb-delete" type="button">Remove</button></td>
    </tr>`).join("")}</tbody></table>`;
    $$(".sb-toggle", box).forEach((btn) => btn.addEventListener("click", async () => {
      const tr = btn.closest("tr");
      const b = sbState.blocks.find((x) => x.fdm4_store === tr.dataset.store && x.style_code === tr.dataset.style);
      if (!b) { toast("That row changed - reloading the list.", "error"); return loadSyncBlocks(); }
      let ok;
      if (b.active) {
        ok = await confirmAction({ title: "Turn this freeze off?", message: sbOffConsequence(b), actionLabel: "Turn it off" });
      } else if (!b.style_code) {
        ok = await confirmAction(b.scope === "pricing"
          ? { title: "Freeze this store's prices again?", message: `${storeDisplayFor(b.fdm4_store)} keeps updating normally (new products, stock), but the sync will stop changing its existing prices.`, actionLabel: "Freeze prices", danger: false }
          : { title: "Freeze the entire store again?", message: `${storeDisplayFor(b.fdm4_store)} will be completely skipped by the hourly update (no price, stock, or product changes) until you turn this off.`, actionLabel: "Freeze store" });
      } else {
        ok = await confirmAction({ title: "Freeze this style again?", message: `Style ${b.style_code} on ${storeDisplayFor(b.fdm4_store)} will stop receiving updates from FDM4 until you turn this off.`, actionLabel: "Freeze style", danger: false });
      }
      if (!ok) return;
      btn.disabled = true;
      try {
        await api("/api/sync-blocks/toggle", { method: "PUT", body: { fdm4_store: b.fdm4_store, style_code: b.style_code, active: !b.active } });
        toast(!b.active ? "Freeze turned back on - takes effect within the hour." : "Freeze turned off - normal updates resume within the hour.");
        loadSyncBlocks();
      } catch (e) { btn.disabled = false; toast(e.message, "error"); }
    }));
    $$(".sb-delete", box).forEach((btn) => btn.addEventListener("click", async () => {
      const tr = btn.closest("tr");
      const b = sbState.blocks.find((x) => x.fdm4_store === tr.dataset.store && x.style_code === tr.dataset.style);
      const label = tr.dataset.style ? `the freeze on style ${tr.dataset.style}` : "this store freeze";
      const consequence = b ? sbOffConsequence(b) : "Normal updates resume within the hour.";
      const ok = await confirmAction({ title: "Remove this freeze?", message: `Remove ${label} for ${storeDisplayFor(tr.dataset.store)}? ${consequence}`, actionLabel: "Remove", danger: true });
      if (!ok) return;
      btn.disabled = true;
      try { await api(`/api/sync-blocks?${new URLSearchParams({ store: tr.dataset.store, style: tr.dataset.style })}`, { method: "DELETE" }); toast("Freeze removed."); loadSyncBlocks(); }
      catch (e) { btn.disabled = false; toast(e.message, "error"); }
    }));
  }

  const soState = { overrides: [] };

  async function loadStockOverrides() {
    const box = $("#so-list");
    box.innerHTML = '<div class="grid-empty">Loading...</div>';
    try {
      const resp = await api("/api/stock-overrides");
      soState.overrides = resp.overrides || [];
      renderStockOverrides();
    } catch (e) { renderErrorState(box, friendlyLoadError("the stock exceptions", e), loadStockOverrides); }
  }

  function renderStockOverrides() {
    const box = $("#so-list");
    const q = ($("#so-search")?.value || "").trim().toLowerCase();
    const rows = soState.overrides.filter((o) => !q || `${o.style_code} ${text(o.product_name)} ${text(o.brand)} ${text(o.note)}`.toLowerCase().includes(q));
    if (!rows.length) {
      box.innerHTML = soState.overrides.length
        ? '<div class="grid-empty">No exceptions match your search.</div>'
        : '<div class="grid-empty">No exceptions yet - the automatic rule decides everything: third-party brands show as always in stock, Arborwear styles show real warehouse stock.</div>';
      return;
    }
    box.innerHTML = `<table class="data-table"><thead><tr><th>Style</th><th>Shows as</th><th>Reason</th><th>On/Off</th><th>Updated</th><th></th></tr></thead><tbody>${rows.map((o) => `<tr data-style="${escapeHtml(o.style_code)}">
      <td><strong>${escapeHtml(o.style_code)}</strong><br><small class="muted">${escapeHtml(text(o.product_name || ""))}${o.brand ? " · " + escapeHtml(o.brand) : ""}</small></td>
      <td>${o.mode === "fake" ? '<span class="chip dark">Always in stock</span>' : '<span class="chip">Real stock</span>'}</td>
      <td class="note-cell">${escapeHtml(text(o.note))}</td>
      <td><button class="chip ${o.active ? "dark" : ""} so-toggle" type="button" aria-pressed="${o.active ? "true" : "false"}" title="Click to turn this exception on or off">${o.active ? "On" : "Off (paused)"}</button></td>
      <td>${escapeHtml(formatDate(o.updated_at))}<br><small class="muted">${escapeHtml(text(o.updated_by))}</small></td>
      <td><button class="button button--small button--ghost so-delete" type="button">Remove</button></td>
    </tr>`).join("")}</tbody></table>`;
    $$(".so-toggle", box).forEach((btn) => btn.addEventListener("click", async () => {
      const tr = btn.closest("tr");
      const o = soState.overrides.find((x) => x.style_code === tr.dataset.style);
      if (!o) { toast("That row changed - reloading the list.", "error"); return loadStockOverrides(); }
      const ok = await confirmAction(o.active
        ? { title: "Pause this exception?", message: `Style ${o.style_code} goes back to the automatic rule (always-in-stock for third-party brands, real stock for Arborwear) on the store websites within the hour.`, actionLabel: "Pause it" }
        : { title: "Turn this exception back on?", message: `Style ${o.style_code} will show as ${o.mode === "fake" ? "always in stock (customers can always order it)" : "its real warehouse stock count"} on the store websites within the hour.`, actionLabel: "Turn it on", danger: false });
      if (!ok) return;
      btn.disabled = true;
      try {
        await api("/api/stock-overrides/toggle", { method: "PUT", body: { style_code: o.style_code, active: !o.active } });
        toast(!o.active ? "Exception turned back on - the stores update within the hour." : "Exception paused - the automatic rule decides again within the hour.");
        loadStockOverrides();
      } catch (e) { btn.disabled = false; toast(e.message, "error"); }
    }));
    $$(".so-delete", box).forEach((btn) => btn.addEventListener("click", async () => {
      const tr = btn.closest("tr");
      const ok = await confirmAction({ title: "Remove this exception?", message: `Style ${tr.dataset.style} goes back to the automatic rule (always-in-stock for third-party brands, real stock for Arborwear) on the store websites within the hour.`, actionLabel: "Remove", danger: true });
      if (!ok) return;
      btn.disabled = true;
      try { await api(`/api/stock-overrides?${new URLSearchParams({ style: tr.dataset.style })}`, { method: "DELETE" }); toast("Exception removed."); loadStockOverrides(); }
      catch (e) { btn.disabled = false; toast(e.message, "error"); }
    }));
  }

  async function addStockOverride() {
    const style = $("#so-style").value.trim();
    const mode = $("#so-mode").value;
    if (!style) { toast("Enter a style number first.", "error"); return; }
    const existing = soState.overrides.find((o) => o.style_code.toUpperCase() === style.toUpperCase());
    if (existing) {
      const ok = await confirmAction({
        title: "Replace the existing exception?",
        message: `Style ${existing.style_code} already has an exception (${existing.mode === "fake" ? "always in stock" : "real stock"}, ${existing.active ? "on" : "paused"}). Adding again replaces it with "${mode === "fake" ? "always in stock" : "real stock"}" and turns it on.`,
        actionLabel: "Replace it",
      });
      if (!ok) return;
    } else {
      const ok = await confirmAction(mode === "fake"
        ? { title: "Always show this style as in stock?", message: `Customers will always be able to order style ${style} on the store websites (it will show 99,999 in stock), even if the warehouse runs out. Takes effect within the hour.`, actionLabel: "Add exception", danger: false }
        : { title: "Show real stock for this style?", message: `Style ${style} will show its true warehouse stock count on the store websites, even if it's a third-party brand. Takes effect within the hour.`, actionLabel: "Add exception", danger: false });
      if (!ok) return;
    }
    const btn = $("#so-add");
    setBusy(btn, true, "Adding...");
    try {
      const resp = await api("/api/stock-overrides", { method: "PUT", body: { style_code: style, mode, note: $("#so-note").value.trim() } });
      toast(`${resp.style_code} (${resp.product_name || "unnamed"}${resp.brand ? ", " + resp.brand : ""}) now shows ${mode === "fake" ? "as always in stock" : "its real stock"} across ${resp.variants} size/color option(s) - the stores update within the hour.`);
      $("#so-style").value = ""; $("#so-note").value = "";
      loadStockOverrides();
    } catch (e) { toast(e.message, "error"); }
    finally { setBusy(btn, false); }
  }

  function syncSbWhole() {
    const whole = $("#sb-whole").checked;
    const ta = $("#sb-styles");
    ta.disabled = whole;
    ta.closest(".field")?.classList.toggle("is-disabled", whole);
    const pricing = $("#sb-pricing-only");
    if (pricing) {
      pricing.disabled = !whole;
      if (!whole) pricing.checked = false;
    }
  }

  async function addSyncBlock() {
    const store = $("#sb-store").value.trim();
    const whole = $("#sb-whole").checked;
    // Whole-store submissions exclude the (disabled, dimmed) style list.
    const styles = whole ? [] : $("#sb-styles").value.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
    if (!store) { toast("Pick a store first.", "error"); return; }
    if (!whole && !styles.length) { toast("Tick “entire store” or paste at least one style #.", "error"); return; }
    const btn = $("#sb-add");
    const pricingOnly = whole && $("#sb-pricing-only")?.checked;
    if (whole) {
      const ok = await confirmAction(pricingOnly
        ? { title: "Freeze pricing for the entire store?", message: `${storeDisplayFor(store)} keeps syncing normally (new styles, stock, status), but the sync will never change an existing product's price. New products still get their initial FDM4 price.`, actionLabel: "Freeze pricing", danger: false }
        : { title: "Block the entire store?", message: `${storeDisplayFor(store)} will be completely skipped by the product sync (no price, stock, or catalog updates) until unblocked.`, actionLabel: "Block store" });
      if (!ok) return;
    }
    setBusy(btn, true, "Adding...");
    try {
      const resp = await api("/api/sync-blocks", { method: "PUT", body: { fdm4_store: store, whole_store: whole, styles, note: $("#sb-note").value.trim(), scope: pricingOnly ? "pricing" : "full" } });
      if (whole) {
        toast(pricingOnly ? "Prices frozen - the store keeps updating, prices stay put." : "Store frozen - it will be skipped starting with the next hourly update.");
      } else {
        const perStyle = Array.isArray(resp?.per_style) ? resp.per_style : [];
        const hits = perStyle.filter((p) => Number(p.products) > 0);
        const misses = perStyle.filter((p) => !(Number(p.products) > 0));
        const saved = Number(resp?.saved ?? styles.length);
        const detail = hits.length ? ` ${hits.map((p) => `${p.style}: ${p.products} product${Number(p.products) === 1 ? "" : "s"}`).join(", ")}.` : "";
        toast(`${saved} style freeze${saved === 1 ? "" : "s"} saved.${detail}`);
        if (misses.length) toast(`These style numbers matched no products and were saved anyway: ${misses.map((p) => p.style).join(", ")}. If they are typos, remove them from the list below.`, "error");
      }
      $("#sb-styles").value = ""; $("#sb-note").value = ""; $("#sb-whole").checked = false; syncSbWhole();
      loadSyncBlocks();
    } catch (e) { toast(e.payload?.message || e.message, "error"); } finally { setBusy(btn, false); }
  }

  $("#sb-add").addEventListener("click", addSyncBlock);
  $("#sb-search").addEventListener("input", () => renderSyncBlocks());
  $("#sb-whole").addEventListener("change", syncSbWhole);
  $("#so-add").addEventListener("click", addStockOverride);
  $("#so-search").addEventListener("input", () => renderStockOverrides());
  // Enter submits the add rows, matching what fast typists expect.
  ["#so-style", "#so-note"].forEach((sel) => $(sel)?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addStockOverride(); }
  }));
  $("#sb-note")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addSyncBlock(); }
  });

  // ----- Product mix -----
  const mixState = {
    store: "", stores: [], storesLoaded: false, styles: [], total: 0, summary: null,
    limit: 25, offset: 0, q: "", sort: "style", dir: "asc",
    selected: new Set(), editing: null,
  };

  function mixStoreInfo(code) {
    return (mixState.stores || []).find((s) => s.fdm4_store === code) || null;
  }

  async function loadProductMix() {
    await ensureStores();
    attachStoreCombobox({ search: "#mix-store-search", hidden: "#mix-store", options: "#mix-store-options", onPick: (code) => mixSelectStore(code) });
    if (!mixState.store && state.store) mixState.store = state.store;
    const mh = $("#mix-store"), ms = $("#mix-store-search");
    if (mh && !mh.value && mixState.store) mh.value = mixState.store;
    if (ms && !ms.value && mixState.store) ms.value = `${storeDisplayFor(mixState.store)} (${mixState.store})`;
    await mixRefreshStores();
    renderMixBody();
  }

  async function mixRefreshStores() {
    const wrap = $("#mix-enrolled");
    try {
      const resp = await api("/api/product-mix/stores");
      mixState.stores = resp.stores || [];
      mixState.storesLoaded = true;
      renderMixChips();
    } catch (e) {
      // Mark the load as failed so renderMixBody doesn't misread an enrolled
      // store as un-enrolled and offer to enroll it again.
      mixState.storesLoaded = false;
      if (wrap) wrap.innerHTML = `<span class="muted">${escapeHtml(friendlyLoadError("the customized-store list", e))}</span>`;
    }
  }

  function renderMixChips() {
    const wrap = $("#mix-enrolled");
    if (!wrap) return;
    if (!mixState.stores.length) { wrap.innerHTML = '<span class="muted">None yet - every store follows FDM4.</span>'; return; }
    wrap.innerHTML = mixState.stores.map((s) => `<button type="button" class="chip ${s.fdm4_store === mixState.store ? "dark" : ""} mix-enrolled-chip" data-store="${escapeHtml(s.fdm4_store)}">${escapeHtml(storeDisplayFor(s.fdm4_store))} · ${s.mode === "all" ? "ALL PRODUCTS" : `${Number(s.style_count) || 0} styles`}</button>`).join("");
    $$(".mix-enrolled-chip", wrap).forEach((b) => b.addEventListener("click", () => {
      const code = b.dataset.store;
      $("#mix-store").value = code;
      $("#mix-store-search").value = `${storeDisplayFor(code)} (${code})`;
      mixSelectStore(code);
    }));
  }

  function mixSelectStore(code) {
    mixState.store = code || "";
    if (code && code !== state.store) selectStore(code);
    mixState.offset = 0;
    mixState.q = "";
    mixState.selected = new Set();
    renderMixChips();
    renderMixBody();
  }

  function renderMixBody() {
    const box = $("#mix-body");
    if (!box) return;
    if (!mixState.store) { box.innerHTML = '<div class="grid-empty">Pick a store to view or take control of its product mix.</div>'; return; }
    if (!mixState.storesLoaded) {
      renderErrorState(box, "Couldn't load the store list, so this store's status is unknown.", async () => { await mixRefreshStores(); renderMixBody(); });
      return;
    }
    const info = mixStoreInfo(mixState.store);
    if (!info || !info.active) return renderMixEnroll(box);
    if (info.mode === "all") return renderMixAll(box, info);
    renderMixList(box);
  }

  function renderMixEnroll(box) {
    const name = storeDisplayFor(mixState.store);
    box.innerHTML = `<section class="card">
      <div class="card__header card__header--compact"><div><p class="eyebrow">${escapeHtml(name)}</p><h3>This store follows FDM4</h3></div></div>
      <div class="card__body"><p class="muted">Its products are controlled by FDM4 today - nothing to manage here. Take control by choosing how this store's mix should work:</p></div>
      <div class="mix-choice-grid">
        <button type="button" class="mix-choice mix-enable" data-mode="all"><strong>All products - follow FDM4</strong><small>Carries everything FDM4 offers this store, including new products, automatically. Switch to a curated list any time.</small></button>
        <button type="button" class="mix-choice mix-enable" data-mode="list"><strong>Curated list - start from the current FDM4 mix</strong><small>Imports the store's current styles as your starting point. New FDM4 products stay out until you add or import them.</small></button>
      </div>
    </section>`;
    $$(".mix-enable", box).forEach((b) => b.addEventListener("click", () => mixEnable(b.dataset.mode, b)));
  }

  async function mixEnable(mode, btn) {
    const name = storeDisplayFor(mixState.store);
    const ok = await confirmAction({
      title: mode === "all" ? "Follow FDM4 for this store?" : "Start a curated list?",
      message: mode === "all"
        ? `${name} will carry everything FDM4 offers it - including new products - automatically. Nothing changes on the storefront by enabling this.`
        : `${name}'s current FDM4 mix is imported as your editable starting list. Nothing changes on the storefront until you remove something.`,
      actionLabel: mode === "all" ? "Follow FDM4" : "Import and start",
      danger: false,
    });
    if (!ok) return;
    setBusy(btn, true, "Enabling...");
    try {
      const resp = await api("/api/product-mix/stores", { method: "PUT", body: { fdm4_store: mixState.store, mode } });
      toast(mode === "all" ? "Done - this store now follows FDM4 automatically (all products)." : `Curated list started - imported ${Number(resp.imported) || 0} styles from FDM4.`);
      await mixRefreshStores();
      renderMixBody();
    } catch (e) { toast(e.payload?.message || e.message, "error"); } finally { setBusy(btn, false); }
  }

  function renderMixAll(box, info) {
    const name = storeDisplayFor(mixState.store);
    box.innerHTML = `<section class="card">
      <div class="card__header card__header--compact"><div><p class="eyebrow">${escapeHtml(name)}</p><h3>Product mix override</h3></div><span class="chip dark">ALL PRODUCTS</span></div>
      <div class="mix-status-card">
        <p class="muted">This store carries everything FDM4 offers, including new products, automatically.${info.note ? `<br><small>${escapeHtml(info.note)}</small>` : ""}</p>
        <div class="mix-actions">
          <button type="button" class="button button--ghost mix-switch-list">Switch to curated list</button>
          <button type="button" class="button button--danger-ghost mix-disable">Disable override</button>
        </div>
      </div>
    </section>`;
    $(".mix-switch-list", box).addEventListener("click", (e) => mixSwitchMode("list", e.currentTarget));
    $(".mix-disable", box).addEventListener("click", (e) => mixDisable(e.currentTarget));
  }

  async function mixSwitchMode(mode, btn) {
    const name = storeDisplayFor(mixState.store);
    if (mode === "list") {
      const ok = await confirmAction({ title: "Switch to a curated list?", message: `Snapshots ${name}'s current mix as your editable list. New FDM4 products stop flowing in automatically until you add or import them.`, actionLabel: "Switch", danger: false });
      if (!ok) return;
    } else {
      let detail = "";
      setBusy(btn, true, "Checking impact...");
      try {
        const p = await api("/api/product-mix/preview", { method: "POST", body: { store: mixState.store, action: "mode", mode: "all" } });
        if (Number(p.products_restored) > 0) detail = ` About ${Number(p.products_restored)} products you removed come back on the next sync.`;
      } catch { /* preview optional - generic copy below */ }
      setBusy(btn, false);
      const ok = await confirmAction({ title: "Follow FDM4 again?", message: `${name} goes back to carrying everything FDM4 offers, including new products, automatically.${detail}`, actionLabel: "Follow FDM4" });
      if (!ok) return;
    }
    setBusy(btn, true, "Switching...");
    try {
      await api("/api/product-mix/stores/mode", { method: "PUT", body: { fdm4_store: mixState.store, mode } });
      toast(mode === "list" ? "Curated list ready - the current mix is snapshotted." : "Following FDM4 - all products, automatically.");
      await mixRefreshStores();
      renderMixBody();
    } catch (e) { toast(e.payload?.message || e.message, "error"); } finally { setBusy(btn, false); }
  }

  async function mixDisable(btn) {
    const name = storeDisplayFor(mixState.store);
    let detail = "";
    setBusy(btn, true, "Checking impact...");
    try {
      const p = await api("/api/product-mix/preview", { method: "POST", body: { store: mixState.store, action: "disable" } });
      if (Number(p.products_restored) > 0) detail = ` About ${Number(p.products_restored)} removed products come back on the next sync.`;
    } catch { /* fall back to generic copy */ }
    setBusy(btn, false);
    const ok = await confirmAction({ title: "Stop customizing this store?", message: `${name} goes back to carrying exactly what FDM4 offers it, and your custom product list is deleted.${detail}`, actionLabel: "Hand back to FDM4" });
    if (!ok) return;
    setBusy(btn, true, "Working...");
    try {
      // The endpoint takes the store as a query parameter, not a JSON body.
      await api(`/api/product-mix/stores?${new URLSearchParams({ store: mixState.store })}`, { method: "DELETE" });
      toast("Done - FDM4 is back in control of this store.");
      await mixRefreshStores();
      renderMixBody();
    } catch (e) { toast(e.payload?.message || e.message, "error"); } finally { setBusy(btn, false); }
  }

  function renderMixList(box) {
    const name = storeDisplayFor(mixState.store);
    box.innerHTML = `<section class="card">
      <div class="card__header card__header--compact">
        <div><p class="eyebrow">${escapeHtml(name)}</p><h3>Curated product mix</h3></div>
        <div class="mix-actions">
          <button type="button" class="button button--ghost button--small mix-import">Import missing from FDM4</button>
          <button type="button" class="button button--ghost button--small mix-reset">Reset to FDM4</button>
          <button type="button" class="button button--danger-ghost button--small mix-disable">Hand back to FDM4</button>
        </div>
      </div>
      <div class="mix-tiles"></div>
      <div class="table-toolbar">
        <div class="input-with-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15.5 14h-.8l-.3-.3a6.5 6.5 0 1 0-.7.7l.3.3v.8l5 5 1.5-1.5-5-5Zm-6 0A4.5 4.5 0 1 1 14 9.5 4.5 4.5 0 0 1 9.5 14Z"/></svg><input type="search" class="mix-search" placeholder="Filter styles..." aria-label="Filter styles in the mix"></div>
        <button type="button" class="button button--danger-ghost button--small mix-remove-selected" hidden>Remove selected</button>
      </div>
      <div class="mix-list table-wrap" aria-live="polite"><div class="grid-empty">Loading...</div></div>
      <div class="pager mix-pager" hidden>
        <button type="button" class="button button--ghost button--small mix-prev">Previous</button>
        <span class="muted mix-range"></span>
        <button type="button" class="button button--ghost button--small mix-next">Next</button>
      </div>
    </section>
    <section class="card">
      <div class="card__header card__header--compact"><div><p class="eyebrow">Add</p><h3>Add styles to the mix</h3></div></div>
      <div class="card__body">
        <div class="form-row">
          <label class="field"><span class="field__label">Style #s (newline / comma separated)</span><textarea class="mix-add-styles" rows="3" placeholder="e.g.&#10;18500&#10;400340"></textarea></label>
          <button type="button" class="button button--primary mix-add-btn">Add styles</button>
        </div>
        <p class="mix-bulk-note">You can only add products FDM4 already offers this store. Added styles carry all their colors - open a style to trim colors or sizes.</p>
      </div>
    </section>`;
    $(".mix-search", box).addEventListener("input", debounce(() => { mixState.q = $(".mix-search", box).value.trim(); mixState.offset = 0; loadMixStyles(); }));
    $(".mix-prev", box).addEventListener("click", () => { if (mixState.offset > 0) { mixState.offset = Math.max(0, mixState.offset - mixState.limit); loadMixStyles(); } });
    $(".mix-next", box).addEventListener("click", () => { mixState.offset += mixState.limit; loadMixStyles(); });
    $(".mix-import", box).addEventListener("click", (e) => mixImport("merge", e.currentTarget));
    $(".mix-reset", box).addEventListener("click", (e) => mixImport("reset", e.currentTarget));
    $(".mix-disable", box).addEventListener("click", (e) => mixDisable(e.currentTarget));
    $(".mix-add-btn", box).addEventListener("click", (e) => mixAddStyles(e.currentTarget));
    $(".mix-remove-selected", box).addEventListener("click", (e) => mixRemoveStyles([...mixState.selected], e.currentTarget));
    loadMixStyles();
  }

  let mixLoadSeq = 0;

  async function loadMixStyles() {
    const box = $("#mix-body");
    const list = box ? $(".mix-list", box) : null;
    if (!list) return;
    const seq = ++mixLoadSeq;
    list.innerHTML = '<div class="grid-empty">Loading...</div>';
    const params = new URLSearchParams({ store: mixState.store, q: mixState.q, limit: String(mixState.limit), offset: String(mixState.offset) });
    try {
      const resp = await api(`/api/product-mix?${params}`);
      if (seq !== mixLoadSeq) return; // a newer request superseded this one
      mixState.styles = resp.styles || [];
      mixState.total = Number(resp.total) || 0;
      mixState.summary = resp.summary || {};
      renderMixTiles(box);
      renderMixTable(box);
      renderMixPager(box);
    } catch (e) {
      if (seq !== mixLoadSeq) return;
      renderErrorState(list, friendlyLoadError("this store's product list", e), loadMixStyles);
    }
  }

  function renderMixTiles(box) {
    const t = $(".mix-tiles", box);
    if (!t) return;
    const s = mixState.summary || {};
    const drift = Number(s.new_in_fdm4) || 0;
    t.innerHTML = `
      <div class="mix-tile"><strong>${Number(s.in_mix) || 0}</strong><small>Styles in mix</small></div>
      <div class="mix-tile${drift ? " mix-tile--warn" : ""}"><strong>${drift}</strong><small>New in FDM4, not in mix</small></div>
      <div class="mix-tile"><strong>${Number(s.products_live) || 0}</strong><small>Products live</small></div>`;
    const imp = $(".mix-import", box);
    if (imp && !imp.disabled) imp.textContent = drift ? `Import missing from FDM4 (${drift})` : "Import missing from FDM4";
  }

  function mixColorsSummary(row) {
    const colors = row.colors;
    const ex = row.size_excludes || null;
    const exCount = ex ? Object.values(ex).reduce((n, arr) => n + (Array.isArray(arr) ? arr.length : 0), 0) : 0;
    let base = colors === null || colors === undefined ? "All colors" : `${colors.length} color${colors.length === 1 ? "" : "s"}`;
    if (exCount) base += ` · ${exCount} size${exCount === 1 ? "" : "s"} excluded`;
    return base;
  }

  function renderMixTable(box) {
    const list = $(".mix-list", box);
    if (!list) return;
    if (!mixState.styles.length) {
      list.innerHTML = `<div class="grid-empty">${mixState.q ? "No styles match the filter." : "No styles in the list yet. Add styles below or import from FDM4."}</div>`;
      syncMixSelection(box);
      return;
    }
    const rows = [...mixState.styles].sort((a, b) => {
      let va, vb;
      if (mixState.sort === "products") { va = Number(a.products_live) || 0; vb = Number(b.products_live) || 0; }
      else { va = text(a.style_code); vb = text(b.style_code); }
      const cmp = va < vb ? -1 : va > vb ? 1 : 0;
      return mixState.dir === "desc" ? -cmp : cmp;
    });
    const arrow = (key) => (mixState.sort === key ? (mixState.dir === "desc" ? " ▼" : " ▲") : " ↕");
    const ariaSort = (key) => (mixState.sort === key ? (mixState.dir === "desc" ? "descending" : "ascending") : "none");
    const sourceLabel = (s) => (s === "import" ? "Imported from FDM4" : s === "manual" ? "Added by hand" : text(s));
    const multiPage = mixState.total > mixState.limit;
    list.innerHTML = `<table class="data-table"><thead><tr>
      <th><input type="checkbox" class="mix-select-all" aria-label="Select all styles on this page"></th>
      <th data-msort="style" role="button" tabindex="0" aria-sort="${ariaSort("style")}" title="Sort by style">Style${arrow("style")}</th><th>Product</th><th>Colors</th><th>How added</th><th>Added</th><th data-msort="products" role="button" tabindex="0" aria-sort="${ariaSort("products")}" title="${multiPage ? "Sorts this page only" : "Sort"}">Products live${arrow("products")}</th><th></th>
    </tr></thead><tbody>${rows.map((r) => `<tr data-style="${escapeHtml(r.style_code)}">
      <td><input type="checkbox" class="mix-row-check" aria-label="Select style ${escapeHtml(r.style_code)}"${mixState.selected.has(r.style_code) ? " checked" : ""}></td>
      <td><code>${escapeHtml(r.style_code)}</code></td>
      <td>${escapeHtml(text(r.name))}</td>
      <td>${escapeHtml(mixColorsSummary(r))}</td>
      <td>${escapeHtml(sourceLabel(text(r.source)))}</td>
      <td>${escapeHtml(formatDate(r.added_at))}<br><small class="muted">${escapeHtml(text(r.added_by))}</small></td>
      <td>${Number(r.products_live) || 0}</td>
      <td><button type="button" class="button button--ghost button--small mix-edit">Edit</button> <button type="button" class="button button--danger-ghost button--small mix-remove">Remove</button></td>
    </tr>`).join("")}</tbody></table>`;
    $$("th[data-msort]", list).forEach((th) => {
      const toggle = () => {
        const key = th.dataset.msort;
        if (mixState.sort === key) mixState.dir = mixState.dir === "asc" ? "desc" : "asc";
        else { mixState.sort = key; mixState.dir = "asc"; }
        renderMixTable(box);
      };
      th.addEventListener("click", toggle);
      th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
    const selectAll = $(".mix-select-all", list);
    selectAll.checked = rows.every((r) => mixState.selected.has(r.style_code));
    selectAll.addEventListener("change", () => {
      rows.forEach((r) => { if (selectAll.checked) mixState.selected.add(r.style_code); else mixState.selected.delete(r.style_code); });
      renderMixTable(box);
    });
    $$(".mix-row-check", list).forEach((cb) => cb.addEventListener("change", () => {
      const style = cb.closest("tr").dataset.style;
      if (cb.checked) mixState.selected.add(style); else mixState.selected.delete(style);
      syncMixSelection(box);
    }));
    $$(".mix-edit", list).forEach((b) => b.addEventListener("click", () => openMixStyleEditor(b.closest("tr").dataset.style)));
    $$(".mix-remove", list).forEach((b) => b.addEventListener("click", (e) => mixRemoveStyles([b.closest("tr").dataset.style], e.currentTarget)));
    syncMixSelection(box);
  }

  function syncMixSelection(box) {
    const btn = $(".mix-remove-selected", box);
    if (!btn) return;
    const n = mixState.selected.size;
    btn.hidden = n === 0;
    btn.textContent = `Remove selected (${n})`;
  }

  function renderMixPager(box) {
    const pager = $(".mix-pager", box);
    if (!pager) return;
    if (mixState.total > mixState.limit) {
      pager.hidden = false;
      const start = mixState.offset + 1;
      const end = Math.min(mixState.offset + mixState.styles.length, mixState.total);
      $(".mix-range", box).textContent = `${start}-${end} of ${mixState.total}`;
      $(".mix-prev", box).disabled = mixState.offset <= 0;
      $(".mix-next", box).disabled = end >= mixState.total;
    } else pager.hidden = true;
  }

  async function mixRemoveStyles(styles, btn = null) {
    styles = (styles || []).filter(Boolean);
    if (!styles.length) return;
    const name = storeDisplayFor(mixState.store);
    const plural = styles.length === 1 ? "" : "s";
    let impact = `Removing ${styles.length} style${plural} retires their products on ${name} at the next sync. Products are hidden and set out of stock - never deleted - and come back if you re-add the style.`;
    if (btn) setBusy(btn, true, "Checking impact...");
    try {
      const p = await api("/api/product-mix/preview", { method: "POST", body: { store: mixState.store, action: "remove", styles } });
      impact = `Removing ${Number(p.styles_affected) || styles.length} style${plural} retires ${Number(p.products_retired) || 0} products on ${name} at the next sync. Products are hidden and set out of stock - never deleted - and come back if you re-add the style.`;
    } catch { /* fall back to generic copy */ }
    if (btn) setBusy(btn, false);
    const ok = await confirmAction({ title: "Remove from mix?", message: impact, actionLabel: "Remove" });
    if (!ok) return;
    setBusy(btn, true, "Removing...");
    try {
      await api("/api/product-mix", { method: "DELETE", body: { store: mixState.store, styles } });
      toast(`${styles.length} style${plural} removed from the mix.`);
      mixState.selected = new Set();
      await mixRefreshStores();
      loadMixStyles();
    } catch (e) { toast(e.payload?.message || e.message, "error"); } finally { setBusy(btn, false); }
  }

  async function mixAddStyles(btn) {
    const box = $("#mix-body");
    const ta = $(".mix-add-styles", box);
    if (!ta) return;
    const styles = ta.value.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
    if (!styles.length) { toast("Paste at least one style #.", "error"); return; }
    setBusy(btn, true, "Adding...");
    try {
      const resp = await api("/api/product-mix", { method: "PUT", body: { store: mixState.store, styles } });
      const perStyle = Array.isArray(resp?.per_style) ? resp.per_style : [];
      const misses = perStyle.filter((p) => !(Number(p.products) > 0));
      const saved = Number(resp?.saved ?? styles.length);
      toast(`${saved} style${saved === 1 ? "" : "s"} added - products appear on the store within the hour.`);
      if (misses.length) toast(`These style numbers matched no products and were still added to the list: ${misses.map((p) => p.style).join(", ")}. FDM4 doesn't offer them to this store - if they are typos, remove them from the list.`, "error");
      ta.value = "";
      await mixRefreshStores();
      loadMixStyles();
    } catch (e) { toast(e.payload?.message || e.message, "error"); } finally { setBusy(btn, false); }
  }

  async function mixImport(mode, btn) {
    const name = storeDisplayFor(mixState.store);
    if (mode === "reset") {
      let detail = "";
      setBusy(btn, true, "Checking impact...");
      try {
        const p = await api("/api/product-mix/preview", { method: "POST", body: { store: mixState.store, action: "reset" } });
        const back = Number(p.products_restored) || 0;
        const gone = Number(p.products_retired) || 0;
        if (back || gone) detail = ` ${back} removed products come back${gone ? ` and ${gone} are retired` : ""} on the next sync.`;
      } catch { /* fall back to generic copy */ }
      setBusy(btn, false);
      const ok = await confirmAction({ title: "Reset to FDM4?", message: `Wipes your edits for ${name} and matches FDM4's current mix exactly - including bringing back everything you removed.${detail}`, actionLabel: "Reset to FDM4" });
      if (!ok) return;
    } else {
      const drift = Number(mixState.summary?.new_in_fdm4) || 0;
      const ok = await confirmAction({
        title: "Add FDM4's new styles?",
        message: drift
          ? `${drift} style${drift === 1 ? "" : "s"} FDM4 now offers ${name} will be added to the store's list and appear on the live website within the hour.`
          : `Any styles FDM4 now offers ${name} that aren't in your list yet will be added and appear on the live website within the hour.`,
        actionLabel: drift ? `Add ${drift} style${drift === 1 ? "" : "s"}` : "Add new styles",
        danger: false,
      });
      if (!ok) return;
    }
    setBusy(btn, true, mode === "reset" ? "Resetting..." : "Importing...");
    try {
      const resp = await api("/api/product-mix/import", { method: "POST", body: { store: mixState.store, mode } });
      const added = Number(resp.added) || 0;
      toast(mode === "reset"
        ? `Reset complete - the mix now matches FDM4 (${added} style${added === 1 ? "" : "s"} restored).`
        : (added ? `Imported ${added} missing style${added === 1 ? "" : "s"} from FDM4.` : "Nothing new - the mix already covers FDM4's current offering."));
      await mixRefreshStores();
      loadMixStyles();
    } catch (e) { toast(e.payload?.message || e.message, "error"); } finally { setBusy(btn, false); }
  }

  // --- Style editor dialog ---

  function mixEditorChanged() {
    const ed = mixState.editing;
    if (!ed) return;
    ed.confirmed = false;
    const warn = $("#mix-style-warning");
    warn.hidden = true;
    const btn = $("#mix-style-save");
    if (btn.getAttribute("aria-busy") !== "true") btn.textContent = "Save style";
    syncMixEditorState();
  }

  function syncMixEditorState() {
    const ed = mixState.editing;
    if (!ed) return;
    const btn = $("#mix-style-save");
    if (btn.getAttribute("aria-busy") === "true") return;
    const status = $("#mix-style-status");
    if (!ed.work.all && ed.work.colors.size === 0) {
      btn.disabled = true;
      status.textContent = "Select at least one color - to drop the whole product, remove the style from the mix instead.";
    } else {
      btn.disabled = false;
      status.textContent = "";
    }
  }

  async function openMixStyleEditor(styleCode) {
    const dialog = $("#mix-style-dialog");
    const row = mixState.styles.find((r) => r.style_code === styleCode);
    $("#mix-style-title").textContent = row && text(row.name) ? `Style ${styleCode} - ${text(row.name)}` : `Style ${styleCode}`;
    const body = $("#mix-style-colors");
    body.innerHTML = '<div class="grid-empty">Loading...</div>';
    $("#mix-style-warning").hidden = true;
    $("#mix-style-status").textContent = "";
    const save = $("#mix-style-save");
    save.disabled = true;
    save.textContent = "Save style";
    mixState.editing = null;
    openDialog(dialog);
    try {
      const resp = await api(`/api/product-mix/style?${new URLSearchParams({ store: mixState.store, style: styleCode })}`);
      const available = resp.available || [];
      const loadedColors = resp.colors === null || resp.colors === undefined ? null : resp.colors.map((c) => text(c));
      const loadedExcludes = resp.size_excludes || {};
      const work = {
        all: loadedColors === null,
        colors: new Set(loadedColors === null ? available.map((c) => text(c.color)) : loadedColors),
        excludes: {},
      };
      Object.entries(loadedExcludes).forEach(([color, sizes]) => { work.excludes[color] = new Set(Array.isArray(sizes) ? sizes : []); });
      mixState.editing = { style: styleCode, available, loadedColors, loadedExcludes, work, confirmed: false, openColors: new Set(Object.keys(loadedExcludes)) };
      $("#mix-style-all-colors").checked = work.all;
      renderMixStyleColors();
      syncMixEditorState();
    } catch (e) {
      renderErrorState(body, friendlyLoadError("this style's colors", e), () => openMixStyleEditor(styleCode));
    }
  }

  function renderMixStyleColors() {
    const ed = mixState.editing;
    if (!ed) return;
    const body = $("#mix-style-colors");
    if (!ed.available.length) { body.innerHTML = '<div class="grid-empty">No color channels found for this style.</div>'; return; }
    body.innerHTML = ed.available.map((c) => {
      const color = text(c.color);
      const on = ed.work.all || ed.work.colors.has(color);
      const excl = ed.work.excludes[color] || new Set();
      // Sizes arrive as {code, label} objects; the code is the saved value.
      const sizes = (c.sizes || []).map((s) => (typeof s === "object" && s ? { code: text(s.code), label: text(s.label, text(s.code)) } : { code: text(s), label: text(s) }));
      const inCount = sizes.filter((s) => !excl.has(s.code)).length;
      const open = ed.openColors.has(color);
      return `<div class="mix-color${on ? "" : " is-off"}" data-color="${escapeHtml(color)}">
        <label class="field--checkbox mix-color__head"><input type="checkbox" class="mix-color-inc"${on ? " checked" : ""}${ed.work.all ? " disabled" : ""} aria-label="Include color ${escapeHtml(color)}"> <strong>${escapeHtml(color)}</strong>${c.color_name && c.color_name !== color ? `&nbsp;<span class="muted">${escapeHtml(text(c.color_name))}</span>` : ""}&nbsp;<span class="muted">(${Number(c.variations) || 0} variation${Number(c.variations) === 1 ? "" : "s"})</span></label>
        ${sizes.length ? `<details${open ? " open" : ""}><summary class="mix-size-summary">Sizes - ${inCount} of ${sizes.length} included${excl.size ? ` (${excl.size} excluded)` : ""}</summary><div class="mix-size-grid">${sizes.map((s) => `<label class="field--checkbox"><input type="checkbox" class="mix-size-inc" data-size="${escapeHtml(s.code)}"${excl.has(s.code) ? "" : " checked"}${!on || ed.work.all ? " disabled" : ""} aria-label="Include size ${escapeHtml(s.label)} for color ${escapeHtml(color)}"> ${escapeHtml(s.label)}</label>`).join("")}</div></details>` : ""}
      </div>`;
    }).join("");
    $$(".mix-color-inc", body).forEach((cb) => cb.addEventListener("change", () => {
      const color = cb.closest(".mix-color").dataset.color;
      if (cb.checked) ed.work.colors.add(color); else ed.work.colors.delete(color);
      rememberMixOpenDetails();
      mixEditorChanged();
      renderMixStyleColors();
    }));
    $$(".mix-size-inc", body).forEach((cb) => cb.addEventListener("change", () => {
      const wrap = cb.closest(".mix-color");
      const color = wrap.dataset.color;
      const size = cb.dataset.size;
      if (!ed.work.excludes[color]) ed.work.excludes[color] = new Set();
      if (cb.checked) ed.work.excludes[color].delete(size); else ed.work.excludes[color].add(size);
      const excl = ed.work.excludes[color];
      const sizes = $$(".mix-size-inc", wrap);
      const summary = $(".mix-size-summary", wrap);
      if (summary) summary.textContent = `Sizes - ${sizes.length - excl.size} of ${sizes.length} included${excl.size ? ` (${excl.size} excluded)` : ""}`;
      mixEditorChanged();
    }));
    $$("details", body).forEach((d) => d.addEventListener("toggle", () => {
      const color = d.closest(".mix-color").dataset.color;
      if (d.open) ed.openColors.add(color); else ed.openColors.delete(color);
    }));
  }

  function rememberMixOpenDetails() {
    const ed = mixState.editing;
    if (!ed) return;
    ed.openColors = new Set($$("#mix-style-colors details[open]").map((d) => d.closest(".mix-color").dataset.color));
  }

  function mixEditorPayload() {
    const ed = mixState.editing;
    const colors = ed.work.all ? null : [...ed.work.colors];
    const excludes = {};
    if (!ed.work.all) {
      for (const c of ed.work.colors) {
        const ex = [...(ed.work.excludes[c] || [])];
        if (ex.length) excludes[c] = ex;
      }
    }
    return { store: mixState.store, style_code: ed.style, colors, size_excludes: Object.keys(excludes).length ? excludes : null };
  }

  function mixCoverageReduced() {
    const ed = mixState.editing;
    const loadedAll = ed.loadedColors === null;
    const loadedColors = new Set(loadedAll ? ed.available.map((c) => text(c.color)) : ed.loadedColors);
    const nowColors = ed.work.all ? new Set(ed.available.map((c) => text(c.color))) : ed.work.colors;
    for (const c of loadedColors) if (!nowColors.has(c)) return true;
    for (const c of nowColors) {
      const loadedEx = new Set((ed.loadedExcludes || {})[c] || []);
      const nowEx = ed.work.all ? new Set() : (ed.work.excludes[c] || new Set());
      for (const s of nowEx) if (!loadedEx.has(s)) return true;
    }
    return false;
  }

  async function saveMixStyle() {
    const ed = mixState.editing;
    if (!ed) return;
    const btn = $("#mix-style-save");
    const warn = $("#mix-style-warning");
    const payload = mixEditorPayload();
    if (payload.colors && !payload.colors.length) { syncMixEditorState(); return; }
    if (!ed.confirmed && mixCoverageReduced()) {
      setBusy(btn, true, "Checking impact...");
      let retired = null;
      try {
        const p = await api("/api/product-mix/preview", { method: "POST", body: { store: mixState.store, action: "style", style_code: ed.style, colors: payload.colors, size_excludes: payload.size_excludes } });
        retired = Number(p.products_retired) || 0;
      } catch { /* preview unavailable - still require an explicit confirm */ }
      setBusy(btn, false);
      if (retired === null || retired > 0) {
        warn.textContent = retired === null
          ? "This change removes coverage - the affected products are retired on the next sync (hidden and out of stock, never deleted). Press Save again to confirm."
          : `This change retires ${retired} product${retired === 1 ? "" : "s"} on ${storeDisplayFor(mixState.store)} at the next sync. They're hidden and set out of stock - never deleted. Press Save again to confirm.`;
        warn.hidden = false;
        ed.confirmed = true;
        btn.textContent = "Save - confirm change";
        return;
      }
    }
    setBusy(btn, true, "Saving...");
    try {
      await api("/api/product-mix/style", { method: "PUT", body: payload });
      toast(`Style ${ed.style} saved - changes reach the store on the next sync.`);
      closeDialog($("#mix-style-dialog"));
      mixState.editing = null;
      await mixRefreshStores();
      loadMixStyles();
    } catch (e) { toast(e.payload?.message || e.message, "error"); } finally { setBusy(btn, false); }
  }

  $("#mix-style-save").addEventListener("click", saveMixStyle);
  $("#mix-style-all-colors").addEventListener("change", () => {
    const ed = mixState.editing;
    if (!ed) return;
    ed.work.all = $("#mix-style-all-colors").checked;
    if (!ed.work.all && ed.work.colors.size === 0) ed.work.colors = new Set(ed.available.map((c) => text(c.color)));
    rememberMixOpenDetails();
    mixEditorChanged();
    renderMixStyleColors();
  });

  // ----- Price rules -----
  const prState = { rules: [], dims: null, editing: null, chips: { stores: [], brands: [], categories: [] }, previewed: {} };

  // Generic searchable chip multi-select (same combobox pattern as stores).
  function attachChipPicker({ search, options, chips, listName, items, labelFor = null }) {
    const searchEl = $(search), optionsEl = $(options), chipsEl = $(chips);
    if (!searchEl || searchEl.dataset.pickerAttached) return;
    searchEl.dataset.pickerAttached = "1";
    const render = (query = "") => {
      const q = query.trim().toLowerCase();
      const chosen = new Set(prState.chips[listName]);
      const matches = (items() || []).filter((v) => !chosen.has(v) && (!q || v.toLowerCase().includes(q))).slice(0, 40);
      optionsEl.replaceChildren();
      if (!matches.length) { showEmptyOption(optionsEl, q ? "No matches" : "Nothing left to add"); }
      matches.forEach((v) => appendOption(optionsEl, {
        title: labelFor ? labelFor(v) : v, subtitle: "", meta: "",
        onSelect: () => {
          prState.chips[listName].push(v);
          prChip(listName, v, chipsEl);
          searchEl.value = "";
          setOptionsOpen(searchEl, optionsEl, false);
          prUpdateTargetSummary();
        },
      }));
      setOptionsOpen(searchEl, optionsEl, true);
    };
    searchEl.addEventListener("input", () => render(searchEl.value));
    searchEl.addEventListener("focus", () => render(searchEl.value));
    searchEl.addEventListener("blur", () => setTimeout(() => setOptionsOpen(searchEl, optionsEl, false), 200));
    bindListKeyboard(searchEl, optionsEl);
  }

  function prUpdateTargetSummary() {
    const el = $("#pr-target-summary");
    if (!el) return;
    const stores = prState.chips.stores;
    const tiers = $$("#pr-tiers input:checked").map((c) => c.value);
    if (!stores.length && !tiers.length) {
      el.className = "notice notice--warning notice--tight";
      el.innerHTML = "<strong>Targets EVERY store.</strong> Add stores or tick a tier to narrow it.";
    } else {
      el.className = "notice notice--success notice--tight";
      const parts = [];
      if (stores.length) parts.push(`${stores.length} store${stores.length === 1 ? "" : "s"}: ${stores.map((s) => storeDisplayFor(s)).join(", ")}`);
      if (tiers.length) parts.push(`every store on tier ${tiers.join(", ")}`);
      el.innerHTML = `<strong>Affects:</strong> ${escapeHtml(parts.join(" - plus "))}`;
    }
  }

  function prEffectSummary(r) {
    const v = r.effect_value !== null && r.effect_value !== undefined ? Number(r.effect_value) : null;
    switch (r.effect_type) {
      case "percent": return `${v > 0 ? "+" : ""}${v}%`;
      case "flat": return `${v > 0 ? "+" : "−"}$${Math.abs(v).toFixed(4).replace(/\.?0+$/, "")}`;
      case "set_price": return `= $${v}`;
      case "price_level": return `level: ${text(r.price_level_key).toUpperCase()}`;
      case "margin_over_cost": return `cost × ${v}`;
      default: return r.effect_type;
    }
  }

  function prTargetSummary(r) {
    const bits = [];
    const stores = (r.stores || []).length, tiers = (r.store_tiers || []).length;
    bits.push(!stores && !tiers ? "ALL stores" : [stores ? `${stores} store${stores === 1 ? "" : "s"}` : "", tiers ? `tier ${r.store_tiers.join(", ")}` : ""].filter(Boolean).join(" + "));
    if ((r.brands || []).length) bits.push(`${r.brands.length} brand${r.brands.length === 1 ? "" : "s"}`);
    if ((r.categories || []).length) bits.push(`${r.categories.length} categor${r.categories.length === 1 ? "y" : "ies"}`);
    if ((r.styles || []).length) bits.push(`${r.styles.length} style${r.styles.length === 1 ? "" : "s"}`);
    return bits.join(" · ");
  }

  async function loadPriceRules() {
    const box = $("#pr-list");
    box.innerHTML = '<div class="grid-empty">Loading...</div>';
    try {
      await ensureStores();
      // Dimensions failing should not hide the rules themselves.
      try { prState.dims = await api("/api/price-rules/dimensions"); }
      catch { prState.dims = prState.dims || null; toast("Couldn't load the brand/category lists - the New rule dialog may be limited until you reload.", "error"); }
      const resp = await api("/api/price-rules");
      prState.rules = resp.rules || [];
      renderPRList();
    } catch (e) { renderErrorState(box, friendlyLoadError("the price rules", e), loadPriceRules); }
  }

  function renderPRList() {
    const box = $("#pr-list");
    const q = ($("#pr-search")?.value || "").trim().toLowerCase();
    const st = $("#pr-status-filter")?.value || "";
    let rules = prState.rules.filter((r) => {
      if (st === "active" && !r.active) return false;
      if (st === "inactive" && r.active) return false;
      if (!q) return true;
      const storeNames = (r.stores || []).map((s) => storeDisplayFor(s)).join(" ");
      return `${r.name} ${text(r.note)} ${(r.stores || []).join(" ")} ${storeNames} ${(r.brands || []).join(" ")} ${(r.styles || []).join(" ")} ${(r.categories || []).join(" ")}`.toLowerCase().includes(q);
    });
    if (!rules.length) {
      box.innerHTML = prState.rules.length
        ? '<div class="grid-empty">No rules match your filter.</div>'
        : '<div class="grid-empty">No price rules yet - create one with “New rule”. Nothing changes any price until a rule is activated (after preview).</div>';
      return;
    }
    box.innerHTML = `<table class="data-table"><thead><tr><th>Rule</th><th>Status</th><th>Priority</th><th>Targets</th><th>Effect</th><th>Schedule</th><th></th></tr></thead><tbody>${rules.map((r) => `<tr data-id="${r.rule_id}">
      <td><strong>${escapeHtml(r.name)}</strong>${r.note ? `<br><small class="muted">${escapeHtml(r.note)}</small>` : ""}</td>
      <td><span class="chip ${r.active ? "dark" : ""}">${r.active ? "On" : "Off"}</span>${r.stackable ? '<br><small class="muted">combinable</small>' : ""}</td>
      <td>${r.priority}</td>
      <td>${escapeHtml(prTargetSummary(r))}</td>
      <td>${escapeHtml(prEffectSummary(r))}${r.floor_price ? `<br><small class="muted">never below $${Number(r.floor_price).toFixed(2)}</small>` : ""}</td>
      <td>${r.effective_from || r.effective_until ? `${escapeHtml(text(r.effective_from, "..."))} → ${escapeHtml(text(r.effective_until, "..."))}` : '<span class="muted">always</span>'}</td>
      <td class="name-actions">
        <button class="button button--small button--secondary pr-preview" type="button">Preview</button>
        <button class="button button--small ${r.active ? "button--ghost" : "button--primary"} pr-toggle" type="button" title="${!r.active && !r.last_previewed_at ? "Preview required before this rule can be turned on" : ""}">${r.active ? "Turn off" : "Turn on"}</button>
        <button class="button button--small button--ghost pr-edit" type="button">Edit</button>
        <button class="button button--small button--ghost pr-delete" type="button">Delete</button>
      </td></tr>`).join("")}</tbody></table>`;
    $$(".pr-edit", box).forEach((b) => b.addEventListener("click", () => openPREditor(prState.rules.find((r) => r.rule_id === Number(b.closest("tr").dataset.id)))));
    $$(".pr-preview", box).forEach((b) => b.addEventListener("click", () => previewPR(Number(b.closest("tr").dataset.id), b)));
    $$(".pr-delete", box).forEach((b) => b.addEventListener("click", () => deletePR(Number(b.closest("tr").dataset.id), b)));
    $$(".pr-toggle", box).forEach((b) => b.addEventListener("click", () => togglePRActive(Number(b.closest("tr").dataset.id), b)));
  }

  async function togglePRActive(id, btn = null) {
    const r = prState.rules.find((x) => x.rule_id === id);
    if (!r) return;
    // Hard-block only the certain 409 (never previewed at all); otherwise let
    // the server judge preview freshness and surface its message.
    if (!r.active && !r.last_previewed_at && prState.previewed[id] !== r.updated_at) {
      toast("Preview required: run Preview on this rule (in its current form) before activating.", "error");
      return;
    }
    if (!r.active) {
      const ok = await confirmAction({ title: "Activate price rule?", message: `“${r.name}” starts changing live prices on the store websites within the hour. You previewed its impact - activate?`, actionLabel: "Activate" });
      if (!ok) return;
    } else {
      const storeCount = (r.stores || []).length;
      const where = storeCount ? `${storeCount} store${storeCount === 1 ? "" : "s"}` : "every store it targets";
      const ok = await confirmAction({ title: "Turn this rule off?", message: `“${r.name}” stops applying, and prices on ${where} go back to normal within the hour.`, actionLabel: "Turn it off" });
      if (!ok) return;
    }
    if (btn) btn.disabled = true;
    try {
      await api("/api/price-rules/toggle", { method: "PUT", body: { rule_id: id, active: !r.active } });
      toast(!r.active ? "Rule activated - prices change within the hour." : "Rule turned off - prices go back to normal within the hour.");
      loadPriceRules();
    } catch (e) { if (btn) btn.disabled = false; toast(e.payload?.message || e.message, "error"); }
  }

  async function deletePR(id, btn = null) {
    const r = prState.rules.find((x) => x.rule_id === id);
    const ok = await confirmAction({ title: "Delete price rule?", message: `Delete “${r?.name}”? ${r?.active ? "It is ON - prices go back to normal within the hour." : "This cannot be undone."}`, actionLabel: "Delete", danger: true });
    if (!ok) return;
    if (btn) btn.disabled = true;
    try { await api(`/api/price-rules?rule_id=${id}`, { method: "DELETE" }); toast("Rule deleted."); loadPriceRules(); }
    catch (e) { if (btn) btn.disabled = false; toast(e.message, "error"); }
  }

  function prChip(listName, value, wrap) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = listName === "stores" ? `${storeDisplayFor(value)} (${value})` : value;
    const x = document.createElement("button");
    x.type = "button"; x.className = "chip__remove"; x.textContent = "×"; x.setAttribute("aria-label", `Remove ${value}`);
    x.addEventListener("click", () => {
      prState.chips[listName] = prState.chips[listName].filter((v) => v !== value);
      chip.remove();
      prUpdateTargetSummary();
    });
    chip.append(x);
    wrap.append(chip);
  }

  function openPREditor(rule) {
    prState.editing = rule || null;
    $("#pr-dialog-title").textContent = rule ? `Edit: ${rule.name}` : "New price rule";
    $("#pr-name").value = rule?.name || "";
    $("#pr-priority").value = rule?.priority ?? 100;
    $("#pr-stackable").checked = Boolean(rule?.stackable);
    $("#pr-effect-type").value = rule?.effect_type || "percent";
    $("#pr-effect-value").value = rule?.effect_value ?? "";
    $("#pr-level-key").value = rule?.price_level_key || "";
    $("#pr-floor").value = rule?.floor_price ?? "";
    $("#pr-from").value = rule?.effective_from || "";
    $("#pr-until").value = rule?.effective_until || "";
    $("#pr-note").value = rule?.note || "";
    $("#pr-styles").value = (rule?.styles || []).join("\n");
    prState.chips.stores = [...(rule?.stores || [])];
    prState.chips.brands = [...(rule?.brands || [])];
    prState.chips.categories = [...(rule?.categories || [])];
    const storeWrap = $("#pr-store-chips"); storeWrap.replaceChildren();
    prState.chips.stores.forEach((s) => prChip("stores", s, storeWrap));
    const brandWrap = $("#pr-brand-chips"); brandWrap.replaceChildren();
    prState.chips.brands.forEach((b2) => prChip("brands", b2, brandWrap));
    const catWrap = $("#pr-cat-chips"); catWrap.replaceChildren();
    prState.chips.categories.forEach((c) => prChip("categories", c, catWrap));
    const tiers = $("#pr-tiers"); tiers.replaceChildren();
    (prState.dims?.tiers || []).forEach((t) => {
      const lab = document.createElement("label");
      lab.className = "chip";
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.value = t;
      cb.checked = (rule?.store_tiers || []).includes(t);
      cb.addEventListener("change", prUpdateTargetSummary);
      lab.append(cb, document.createTextNode(`tier ${t}`));
      tiers.append(lab);
    });
    attachStoreCombobox({ search: "#pr-store-search", hidden: "#pr-store-picked", options: "#pr-store-options",
      onPick: (code) => {
        if (code && !prState.chips.stores.includes(code)) { prState.chips.stores.push(code); prChip("stores", code, $("#pr-store-chips")); }
        const el = $("#pr-store-search"); el.value = "";
        prUpdateTargetSummary();
      } });
    attachChipPicker({ search: "#pr-brand-input", options: "#pr-brand-options", chips: "#pr-brand-chips",
      listName: "brands", items: () => prState.dims?.brands || [] });
    attachChipPicker({ search: "#pr-cat-input", options: "#pr-cat-options", chips: "#pr-cat-chips",
      listName: "categories", items: () => prState.dims?.categories || [] });
    prUpdateTargetSummary();
    prEffectTypeChanged();
    prEffectError("");
    $("#pr-dialog-status").textContent = rule?.active ? "Rule is ACTIVE - saving a material change deactivates it until re-previewed." : "";
    openDialog($("#pr-dialog"));
  }

  function prEffectError(msg) {
    const el = $("#pr-effect-error");
    if (el) { el.textContent = msg || ""; el.hidden = !msg; }
  }

  function prEffectTypeChanged() {
    const t = $("#pr-effect-type").value;
    $("#pr-level-wrap").hidden = t !== "price_level";
    $("#pr-value-wrap").hidden = t === "price_level";
    $("#pr-value-label").textContent = t === "percent" ? "Percent (±)" : t === "flat" ? "Amount (±$)" : t === "set_price" ? "Price ($)" : t === "margin_over_cost" ? "Multiplier (×)" : "Value";
    const val = $("#pr-effect-value");
    if (t === "set_price" || t === "margin_over_cost") { val.min = "0.0001"; val.max = "9999999"; }
    else if (t === "percent") { val.min = "-99.9999"; val.max = "1000"; }
    else { val.min = "-9999999"; val.max = "9999999"; }
  }

  async function savePR() {
    const btn = $("#pr-save");
    const body = {
      rule_id: prState.editing?.rule_id || null,
      name: $("#pr-name").value.trim(),
      active: Boolean(prState.editing?.active),
      priority: Number.parseInt($("#pr-priority").value || "100", 10),
      stackable: $("#pr-stackable").checked,
      stores: prState.chips.stores,
      store_tiers: $$("#pr-tiers input:checked").map((c) => c.value),
      styles: $("#pr-styles").value.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean),
      brands: prState.chips.brands,
      categories: prState.chips.categories,
      effect_type: $("#pr-effect-type").value,
      effect_value: $("#pr-effect-value").value === "" ? null : Number($("#pr-effect-value").value),
      price_level_key: $("#pr-level-key").value,
      floor_price: $("#pr-floor").value === "" ? null : Number($("#pr-floor").value),
      effective_from: $("#pr-from").value || "",
      effective_until: $("#pr-until").value || "",
      note: $("#pr-note").value.trim(),
    };
    prEffectError("");
    if (!body.name) { toast("Rule needs a name.", "error"); return; }
    if (body.effect_type === "price_level") {
      if (!body.price_level_key) { prEffectError("Choose a price level for this effect."); toast("Choose a price level for this effect.", "error"); return; }
    } else {
      const v = body.effect_value;
      let msg = "";
      if (v === null || Number.isNaN(v)) msg = "Enter an effect value.";
      else if (body.effect_type === "set_price" && v <= 0) msg = "Set-price must be greater than $0.";
      else if (body.effect_type === "margin_over_cost" && v <= 0) msg = "Margin multiplier must be greater than 0.";
      else if (body.effect_type === "percent" && v <= -100) msg = "Percent must be greater than −100 (that would zero the price).";
      else if (body.effect_type === "percent" && v > 1000) msg = "Percent above +1000 is not allowed.";
      else if (Math.abs(v) > 9999999) msg = "Effect value is out of range.";
      if (msg) { prEffectError(msg); toast(msg, "error"); return; }
    }
    if (body.floor_price !== null && (Number.isNaN(body.floor_price) || body.floor_price < 0 || body.floor_price > 9999999)) {
      prEffectError("Price floor must be between $0 and $9,999,999.");
      toast("Price floor must be between $0 and $9,999,999.", "error");
      return;
    }
    setBusy(btn, true, "Saving...");
    try {
      const resp = await api("/api/price-rules", { method: "PUT", body });
      if (resp?.deactivated) toast("Rule deactivated - preview again to re-activate.");
      else toast("Rule saved. Preview it to see impact; activation requires a fresh preview.");
      $("#pr-dialog").close();
      loadPriceRules();
    } catch (e) {
      const msg = e.payload?.message || e.message;
      prEffectError(msg);
      toast(msg, "error");
    } finally { setBusy(btn, false); }
  }

  async function previewPR(id, btn) {
    const box = $("#pr-preview");
    const allPreviewButtons = $$(".pr-preview");
    allPreviewButtons.forEach((b) => { b.disabled = true; });
    if (btn) setBusy(btn, true, "Previewing...");
    box.innerHTML = '<div class="grid-loading"><span class="spinner" aria-hidden="true"></span> Calculating exactly what will change (the same calculation the live sync uses)...</div>';
    try {
      const resp = await api("/api/price-rules/preview", { method: "POST", body: { rule_id: id, sample_limit: 200 } });
      const r = prState.rules.find((x) => x.rule_id === id);
      const recorded = resp.preview_recorded !== false;
      if (r && recorded) { prState.previewed[id] = r.updated_at; r.last_previewed_at = r.last_previewed_at || "just now"; }
      const s = resp.summary || {};
      $("#pr-preview-title").textContent = `Preview: ${r ? r.name : `rule ${id}`}`;
      const staleWarn = recorded ? "" : '<div class="notice notice--warning notice--tight"><span class="notice__icon">!</span><div><strong>Rule changed while previewing</strong> - this preview does not count; preview again before activating.</div></div>';
      const overWarn = Number(s.above_msrp) > 0 ? `<div class="notice notice--warning notice--tight"><span class="notice__icon">!</span><div><strong>${s.above_msrp} item${s.above_msrp === 1 ? "" : "s"} priced above the manufacturer's list price (MSRP)</strong> by this rule - allowed, but double-check it's intentional.</div></div>` : "";
      const trunc = s.truncated ? '<p class="muted">Too many products to preview them all - the counts show at least this many.</p>' : "";
      const perStore = resp.per_store || [];
      const moreStores = Number(resp.store_count || 0) - perStore.length;
      const fmtMoney = (v) => (v === null || v === undefined || v === "" ? "-" : `$${Number(v).toFixed(2)}`);
      const zeroNote = !(resp.sample || []).length
        ? '<div class="grid-empty">This rule currently matches no products - check its store and product targeting.</div>'
        : "";
      box.innerHTML = `
        <div class="result-summary">
          <div class="stat"><strong>${Number(s.affected ?? 0).toLocaleString()}</strong><small>items affected</small></div>
          <div class="stat"><strong>${s.stores ?? 0}</strong><small>stores</small></div>
          <div class="stat"><strong>${Number(s.changed ?? 0).toLocaleString()}</strong><small>prices changed</small></div>
          <div class="stat"><strong>${s.above_msrp ?? 0}</strong><small>above MSRP</small></div>
          <div class="stat"><strong>${fmtMoney(s.min_delta)} / ${fmtMoney(s.max_delta)}</strong><small>biggest drop / biggest increase</small></div>
        </div>
        ${staleWarn}${overWarn}${trunc}${zeroNote}
        ${perStore.length ? `<p class="muted">Per store: ${perStore.map((p) => `${escapeHtml(storeDisplayFor(p.fdm4_store))} (${p.affected})`).join(" · ")}${moreStores > 0 ? ` · +${moreStores} more store${moreStores === 1 ? "" : "s"}` : ""}</p>` : ""}
        ${(resp.sample || []).length ? `<table class="data-table"><thead><tr><th>Store</th><th>Style</th><th>SKU</th><th>Color / size</th><th>Base</th><th>New</th><th>MSRP</th></tr></thead>
        <tbody>${(resp.sample || []).map((row) => `<tr${row.over_msrp ? ' class="row--over-msrp"' : ""}>
          <td>${escapeHtml(storeDisplayFor(row.fdm4_store))}</td><td><code>${escapeHtml(row.style_code)}</code></td><td><code>${escapeHtml(row.sku)}</code></td>
          <td>${escapeHtml(text(row.color))} / ${escapeHtml(text(row.size))}</td>
          <td>$${row.before_price}</td><td><strong>$${row.after_price}</strong>${row.over_msrp ? " ⚠" : ""}</td><td>${row.msrp ? `$${row.msrp}` : "-"}</td></tr>`).join("")}</tbody></table>
        <p class="muted">Base = the price before any rules; New = the price this rule produces. Sample shows the ${resp.sample?.length ?? 0} largest price movements.${recorded ? " This rule can now be turned on from the list." : ""}</p>` : ""}`;
      if (recorded) renderPRList();
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (e) {
      $("#pr-preview-title").textContent = "Preview";
      renderErrorState(box, friendlyLoadError("the preview", e), () => previewPR(id, btn));
    }
    finally {
      if (btn) setBusy(btn, false);
      allPreviewButtons.forEach((b) => { b.disabled = false; });
    }
  }

  $("#pr-new").addEventListener("click", () => openPREditor(null));
  $("#pr-save").addEventListener("click", savePR);
  $("#pr-effect-type").addEventListener("change", prEffectTypeChanged);
  $("#pr-search").addEventListener("input", () => renderPRList());
  $("#pr-status-filter").addEventListener("change", () => renderPRList());

  // A real headless-browser regression harness needs to execute this exact
  // production closure without also booting the unrelated warehouse screens.
  // The seam is unavailable on http(s), requires browser automation, and
  // exposes only DOM elements plus immutable snapshots; tests still drive the
  // real event listeners, fetch calls, SSE parser, and render functions.
  const assistantTestHarness = window.__ARB_AGENT_UI_BROWSER_TEST__;
  if (
    assistantTestHarness?.assistantOnly === true
    && navigator.webdriver === true
    && ["about:", "file:"].includes(window.location.protocol)
  ) {
    const elements = initAssistant();
    if (!elements) throw new Error("Assistant test DOM is incomplete");
    const copyRecords = (records) => records.map((record) => (
      record && typeof record === "object" ? { ...record } : record
    ));
    Object.defineProperties(assistantTestHarness, {
      elements: { value: Object.freeze({ ...elements }) },
      snapshot: {
        value: () => Object.freeze({
          sessionId: assistantState.sessionId,
          generation: assistantState.generation,
          streaming: assistantState.streaming,
          sessionLoading: assistantState.sessionLoading,
          messageHistoryLoading: assistantState.messageHistoryLoading,
          messages: copyRecords(assistantState.messages),
          messagesTruncated: assistantState.messagesTruncated,
          messagesOldestCursor: assistantState.messagesOldestCursor
            ? { ...assistantState.messagesOldestCursor }
            : null,
          changeSet: assistantState.changeSet
            ? { ...assistantState.changeSet }
            : null,
          mappingJob: assistantState.mappingJob
            ? { ...assistantState.mappingJob }
            : null,
          reviewQueue: copyRecords(assistantState.reviewQueue),
          reviewTruncated: assistantState.reviewTruncated,
          reviewOldestCursor: assistantState.reviewOldestCursor
            ? { ...assistantState.reviewOldestCursor }
            : null,
          mappingQueue: copyRecords(assistantState.mappingQueue),
          mappingTruncated: assistantState.mappingTruncated,
          mappingOldestCursor: assistantState.mappingOldestCursor
            ? { ...assistantState.mappingOldestCursor }
            : null,
          operationCount: assistantState.operationControllers.size,
          requestGuard: { ...assistantRequestGuard },
        }),
      },
    });
    return;
  }

  $("#names-search").addEventListener("input", () => {
    clearTimeout(namesSearchTimer);
    namesSearchTimer = setTimeout(() => { namesState.q = $("#names-search").value; namesState.offset = 0; loadNames(); }, 250);
  });
  $("#names-filter").addEventListener("change", () => { namesState.filter = $("#names-filter").value; namesState.offset = 0; loadNames(); });
  $("#names-prev").addEventListener("click", () => { if (namesState.offset > 0) { namesState.offset = Math.max(0, namesState.offset - namesState.limit); loadNames(); } });
  $("#names-next").addEventListener("click", () => {
    // Clamp so a double-click can't advance past the last page into a dead
    // empty screen with no Previous button.
    const next = namesState.offset + namesState.limit;
    if (namesState.total && next >= namesState.total) return;
    namesState.offset = next;
    loadNames();
  });

  let colorSearchTimer = null;
  $("#color-search").addEventListener("input", () => {
    clearTimeout(colorSearchTimer);
    colorSearchTimer = setTimeout(() => { colorsState.offset = 0; loadColors(); }, 250);
  });
  $("#color-review-only").addEventListener("change", () => { colorsState.offset = 0; loadColors(); });
  $("#color-class-filter").addEventListener("change", () => { colorsState.offset = 0; loadColors(); });
  $$("#color-table thead th[data-sort]").forEach((th) => {
    th.setAttribute("role", "button");
    th.setAttribute("tabindex", "0");
    const toggle = () => {
      const field = th.dataset.sort;
      if (colorsState.sort === field) { colorsState.dir = colorsState.dir === "asc" ? "desc" : "asc"; }
      else { colorsState.sort = field; colorsState.dir = "asc"; }
      colorsState.offset = 0; loadColors();
    };
    th.addEventListener("click", toggle);
    th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  });
  $("#color-prev").addEventListener("click", () => { if (colorsState.offset > 0) { colorsState.offset = Math.max(0, colorsState.offset - colorsState.limit); loadColors(); } });
  $("#color-next").addEventListener("click", () => { colorsState.offset += colorsState.limit; loadColors(); });

  $$("[data-view]").forEach((el) => el.addEventListener("click", (e) => { e.preventDefault(); switchView(el.dataset.view); }));
  const navGroups = $$(".main-nav__group");
  const closeNavMenus = (except) => {
    navGroups.forEach((g) => {
      if (g === except) return;
      const menu = g.querySelector(".main-nav__menu");
      const trigger = g.querySelector(".main-nav__trigger");
      if (menu) menu.hidden = true;
      if (trigger) trigger.setAttribute("aria-expanded", "false");
    });
  };
  navGroups.forEach((g) => {
    const trigger = g.querySelector(".main-nav__trigger");
    const menu = g.querySelector(".main-nav__menu");
    if (!trigger || !menu) return;
    trigger.addEventListener("click", () => {
      const open = menu.hidden;
      closeNavMenus(g);
      menu.hidden = !open;
      trigger.setAttribute("aria-expanded", String(open));
    });
    menu.addEventListener("click", (e) => {
      if (e.target.closest(".main-nav__item")) {
        menu.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
      }
    });
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".main-nav__group")) closeNavMenus(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeNavMenus(); });
  attachStoreCombobox({ search: "#tier-store-search", hidden: "#tier-store-code", options: "#tier-store-options" });
  $("#tier-form").addEventListener("submit", saveTier);

  // ----- System health -----
  let healthTimer = null;

  function healthTimerSync(view) {
    if (view === "health") {
      if (!healthTimer) healthTimer = setInterval(() => {
        if (document.body.dataset.view === "health") loadHealth(true);
      }, 60000);
    } else if (healthTimer) {
      clearInterval(healthTimer);
      healthTimer = null;
    }
  }

  function healthAge(value) {
    if (!value) return "never";
    const then = new Date(value).getTime();
    if (Number.isNaN(then)) return "-";
    const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
    if (mins < 1) return "just now";
    if (mins < 90) return `${mins} min ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.floor(hours / 24)} d ago`;
  }

  function healthRunChip(status) {
    const s = text(status);
    const cls = s === "success" ? "chip health-chip--ok" : (s === "running" || s === "requested") ? "chip health-chip--pending" : "chip health-chip--bad";
    const label = s === "success" ? "ok" : (s === "running" ? "running" : (s === "requested" ? "queued" : s || "unknown"));
    return `<span class="${cls}">${escapeHtml(label)}</span>`;
  }

  function healthStat(label, value, sub, tone) {
    return `<div class="stat-card${tone ? ` stat-card--${tone}` : ""}">
      <span class="stat-card__label">${escapeHtml(label)}</span>
      <span class="stat-card__value">${escapeHtml(value)}</span>
      <span class="stat-card__sub">${escapeHtml(sub || "")}</span>
    </div>`;
  }

  function renderHealth(resp) {
    const runs = resp.pipeline?.runs || [];
    const latest = runs[0] || null;
    // A run in progress is normal (the pull is hourly) - judge OK/FAILED from
    // the most recent COMPLETED run so the tile doesn't scream during it.
    const lastDone = runs.find((r) => r.status !== "running" && r.status !== "requested") || null;
    const isRunning = latest && (latest.status === "running" || latest.status === "requested");
    const judged = lastDone;
    const judgedAge = judged ? healthAge(judged.finished_at || judged.started_at) : "never";
    const ageMins = judged ? Math.round((Date.now() - new Date(judged.finished_at || judged.started_at).getTime()) / 60000) : Infinity;
    let pipeTone = "ok", pipeValue = "OK";
    if (!judged || judged.status !== "success") { pipeTone = "bad"; pipeValue = judged ? "FAILED" : "NO RUNS"; }
    else if (ageMins > 180) { pipeTone = "bad"; pipeValue = "OVERDUE"; }
    else if (ageMins > 75) { pipeTone = "warn"; pipeValue = "RUNNING BEHIND"; }
    if (isRunning && pipeTone === "ok") { pipeValue = "RUNNING NOW"; }
    const okCount = Number(resp.pipeline?.ok_24h ?? 0);
    const failedCount = Number(resp.pipeline?.failed_24h ?? 0);
    const pipeSub = judged
      ? `last finished ${judgedAge} · ${okCount} successful pull${okCount === 1 ? "" : "s"} in 24h${failedCount ? ` · ${failedCount} failed` : ""}`
      : "no data pulls have run yet";
    const st = resp.state || {};
    const feats = resp.features || {};
    const pim = resp.pim || {};
    const feeds = resp.feeds || {};
    const stateAgeMins = st.latest_change ? Math.round((Date.now() - new Date(st.latest_change).getTime()) / 60000) : Infinity;
    // PIM freshness matters, not lifetime volume - a dead feed with a big
    // historical count should warn.
    const pimAgeH = pim.latest_event ? (Date.now() - new Date(pim.latest_event).getTime()) / 3600000 : Infinity;
    const pimOk = pimAgeH < 48;
    const mixCount = (feats.mix_stores || []).length;
    $("#health-stats").innerHTML = [
      healthStat("Data pull from FDM4", pipeValue, pipeSub, pipeTone),
      healthStat("Product data", st.latest_change ? `Updated ${healthAge(st.latest_change)}` : "No data", `${Number(st.active_rows || 0).toLocaleString()} live records · ${Number(st.changed_24h || 0).toLocaleString()} changed in 24h`, stateAgeMins > 26 * 60 ? "warn" : "ok"),
      healthStat("Price rules", String(feats.price_rules?.active ?? 0), feats.price_rules?.active ? "active - hourly update takes a bit longer" : "none active", null),
      healthStat("Sync freezes", String((feats.sync_blocks?.whole_store || 0) + (feats.sync_blocks?.styles || 0)), `${feats.sync_blocks?.whole_store || 0} whole-store · ${feats.sync_blocks?.styles || 0} styles`, null),
      healthStat("Custom product lineups", String(mixCount), mixCount ? `${mixCount} store${mixCount === 1 ? "" : "s"} with a custom list` : "none - all stores follow FDM4", null),
      healthStat("Product content feed", pim.latest_event ? (pimOk ? "Receiving updates" : "No recent updates") : "No updates yet", pim.latest_event ? `last update ${healthAge(pim.latest_event)} · ${Number(pim.products || 0).toLocaleString()} products` : "no updates received yet", pim.latest_event && pimOk ? "ok" : "warn"),
      healthStat("Connected systems", feeds.available ? String((feeds.consumers || []).length) : "-", feeds.available ? "systems reading our data" : "not set up yet", null),
    ].join("");

    const trend = $("#health-trend");
    trend.replaceChildren();
    const ordered = runs.slice().reverse();
    const maxDur = Math.max(1, ...ordered.map((r) => Number(r.duration_s || 0)));
    ordered.forEach((r) => {
      const d = Number(r.duration_s || 0);
      const bar = document.createElement("span");
      bar.className = "health-trend__bar";
      bar.style.height = d > 0 ? `${Math.max(8, Math.round((d / maxDur) * 100))}%` : "2%";
      const mins = Math.floor(d / 60);
      bar.title = `${mins}m ${String(d % 60).padStart(2, "0")}s - ${formatDate(r.started_at)}`;
      trend.appendChild(bar);
    });

    $("#health-runs").innerHTML = runs.length ? `<table class="data-table"><thead><tr>
      <th>Started</th><th>Status</th><th>Duration</th><th>Rows</th><th>Note</th>
    </tr></thead><tbody>${runs.map((r) => `<tr>
      <td>${escapeHtml(formatDate(r.started_at))}</td>
      <td>${healthRunChip(r.status)}</td>
      <td>${r.duration_s != null ? `${Math.floor(r.duration_s / 60)}m ${String(r.duration_s % 60).padStart(2, "0")}s` : "-"}</td>
      <td>${r.rows_loaded != null ? Number(r.rows_loaded).toLocaleString() : "-"}</td>
      <td class="health-note">${escapeHtml(r.error || r.note || "")}</td>
    </tr>`).join("")}</tbody></table>` : '<div class="grid-empty">No data pulls have run yet.</div>';

    const rules = feats.price_rules?.rules || [];
    $("#health-rules").innerHTML = rules.length
      ? `<ul class="health-list">${rules.map((r) => `<li>${escapeHtml(r.name || `Rule ${r.rule_id}`)}</li>`).join("")}</ul>`
      : '<div class="grid-empty">No active price rules.</div>';
    const blocks = feats.sync_blocks || {};
    $("#health-blocks").innerHTML = (blocks.whole_store || blocks.styles)
      ? `<ul class="health-list"><li>${escapeHtml(String(blocks.stores || 0))} store(s) affected</li><li>${escapeHtml(String(blocks.whole_store || 0))} whole-store freeze(s)</li><li>${escapeHtml(String(blocks.styles || 0))} style freeze(s)</li></ul>`
      : '<div class="grid-empty">No active sync freezes.</div>';
    const mixStores = feats.mix_stores || [];
    $("#health-mix").innerHTML = mixStores.length
      ? `<ul class="health-list">${mixStores.map((m) => `<li>${escapeHtml(storeDisplayFor(m.fdm4_store))} (${escapeHtml(m.fdm4_store)}) - <strong>${escapeHtml(m.mode === "all" ? "all products" : "curated list")}</strong></li>`).join("")}</ul>`
      : '<div class="grid-empty">No stores with a custom lineup.</div>';
    $("#health-pim").innerHTML = `<ul class="health-list">
      <li>${Number(pim.events || 0).toLocaleString()} update(s) received</li>
      <li>Latest: ${escapeHtml(pim.latest_event ? healthAge(pim.latest_event) : "never")}</li>
      <li>${Number(pim.products || 0).toLocaleString()} product(s) with extra content</li>
    </ul>`;
    const consumers = feeds.consumers || [];
    $("#health-feeds").innerHTML = !feeds.available
      ? '<div class="grid-empty">Not set up yet.</div>'
      : consumers.length
        ? `<ul class="health-list">${consumers.map((c) => `<li><strong>${escapeHtml(c.name)}</strong>${c.active ? "" : " (inactive)"} - checked in ${escapeHtml(healthAge(c.last_ping_at))}${c.last_ping_status ? ` (${escapeHtml(c.last_ping_status)})` : ""} · last downloaded data ${escapeHtml(healthAge(c.last_pull_at))}</li>`).join("")}</ul>`
        : '<div class="grid-empty">No systems connected yet.</div>';
    $("#health-updated").textContent = `Updated ${new Date().toLocaleTimeString()} - refreshes every minute while open.`;
  }

  const HEALTH_BOXES = ["#health-stats", "#health-runs", "#health-rules", "#health-blocks", "#health-mix", "#health-pim", "#health-feeds"];

  async function loadHealth(quiet = false) {
    if (!quiet) HEALTH_BOXES.forEach((sel) => { const el = $(sel); if (el) el.innerHTML = '<div class="grid-empty">Loading...</div>'; });
    try {
      const resp = await api("/api/health/overview");
      renderHealth(resp);
    } catch (e) {
      if (!quiet) {
        renderErrorState($("#health-stats"), "Couldn't load system status. Check your connection and press Try again.", () => loadHealth());
        HEALTH_BOXES.slice(1).forEach((sel) => { const el = $(sel); if (el) el.innerHTML = '<div class="grid-empty">Unavailable</div>'; });
        $("#health-updated").textContent = "";
      } else {
        const stamp = $("#health-updated");
        if (stamp && !stamp.textContent.includes("refresh failed")) {
          stamp.textContent += " (last refresh failed - showing older numbers, retrying)";
        }
      }
    }
  }

  wireEvents();
  initAssistant();
  loadStores();
  {
    const p = new URL(window.location.href).searchParams;
    switchView(p.get("view") || (p.get("store") ? "logo" : "dashboard"));
  }
})();

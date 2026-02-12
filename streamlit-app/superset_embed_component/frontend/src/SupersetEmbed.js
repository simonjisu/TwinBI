import { jsx as _jsx } from "react/jsx-runtime";
import { useEffect, useMemo, useRef } from "react";
import { Streamlit } from "streamlit-component-lib";
import { embedDashboard } from "@superset-ui/embedded-sdk";
console.log("SupersetEmbed BUILD:", new Date().toISOString());
function safeStringify(value) {
    try {
        return JSON.stringify(value);
    }
    catch {
        return String(value);
    }
}
function normalizeFilter(raw) {
    const col = raw.col ?? raw.subject;
    if (col === undefined || col === null || String(col).trim() === "")
        return null;
    const op = raw.op ?? raw.operator ?? "==";
    const val = raw.val ?? raw.comparator;
    return { col: String(col), op: String(op), val };
}
function readFilterArray(value) {
    if (!Array.isArray(value))
        return [];
    const out = [];
    for (const item of value) {
        if (!item || typeof item !== "object")
            continue;
        const normalized = normalizeFilter(item);
        if (normalized)
            out.push(normalized);
    }
    return out;
}
function readFilterMap(value) {
    if (!value || typeof value !== "object" || Array.isArray(value))
        return [];
    const out = [];
    for (const [col, rawVal] of Object.entries(value)) {
        if (!col || col.trim() === "")
            continue;
        const normalizedVal = Array.isArray(rawVal) &&
            rawVal.length === 1 &&
            Array.isArray(rawVal[0])
            ? rawVal[0]
            : rawVal;
        out.push({ col: String(col), op: "IN", val: normalizedVal });
    }
    return out;
}
function extractFiltersFromContainer(obj) {
    const formData = (obj.formData ?? obj.form_data);
    const payload = obj.payload;
    const payloadFormData = (payload?.formData ?? payload?.form_data);
    const extra = (obj.extra_form_data ??
        formData?.extra_form_data ??
        payload?.extra_form_data ??
        payloadFormData?.extra_form_data);
    const extraCamel = obj.extraFormData;
    const filterState = obj.filterState;
    const filterStateSnake = obj.filter_state;
    const filters = [];
    filters.push(...readFilterArray(obj.filters));
    filters.push(...readFilterArray(formData?.filters));
    filters.push(...readFilterArray(payload?.filters));
    filters.push(...readFilterArray(payloadFormData?.filters));
    filters.push(...readFilterArray(extra?.filters));
    filters.push(...readFilterArray(obj.adhoc_filters));
    filters.push(...readFilterArray(formData?.adhoc_filters));
    filters.push(...readFilterArray(payload?.adhoc_filters));
    filters.push(...readFilterArray(payloadFormData?.adhoc_filters));
    // Data-mask can carry cross-filter state in map shape:
    //   filterState.filters = { col_name: [...] }
    // or extraFormData.filters as a plain object.
    filters.push(...readFilterMap(filterState?.filters));
    filters.push(...readFilterMap(filterStateSnake?.filters));
    filters.push(...readFilterMap(extraCamel?.filters));
    filters.push(...readFilterMap(extra?.filters));
    return filters;
}
function extractFiltersFromMessage(msg) {
    if (!msg || typeof msg !== "object")
        return [];
    return extractFiltersFromContainer(msg);
}
function extractFiltersFromDataMask(mask) {
    if (!mask || typeof mask !== "object")
        return [];
    const queue = [mask];
    const seen = new Set();
    const collected = [];
    while (queue.length > 0 && seen.size < 400) {
        const node = queue.shift();
        if (!node || typeof node !== "object" || seen.has(node))
            continue;
        seen.add(node);
        const obj = node;
        collected.push(...extractFiltersFromContainer(obj));
        const nested = [obj.dataMask, obj.data_mask, obj.payload, obj.extraFormData];
        for (const value of nested) {
            if (value && typeof value === "object")
                queue.push(value);
        }
        for (const value of Object.values(obj)) {
            if (value && typeof value === "object")
                queue.push(value);
        }
    }
    const dedup = new Map();
    for (const filter of collected)
        dedup.set(filterKey(filter), filter);
    return [...dedup.values()];
}
function dedupeFilters(filters) {
    const dedup = new Map();
    for (const filter of filters)
        dedup.set(filterKey(filter), filter);
    return [...dedup.values()];
}
function extractGlobalFiltersFromNativeEntry(nativeId, entry) {
    // Native filter entries can contain display labels in filterState; those are
    // not stable column identifiers. Only trust structured filter payload fields.
    const extraFormData = (entry.extraFormData ??
        entry.extra_form_data);
    const formData = (entry.formData ?? entry.form_data);
    const candidates = [];
    candidates.push(...readFilterArray(entry.filters));
    candidates.push(...readFilterArray(extraFormData?.filters));
    candidates.push(...readFilterArray(formData?.filters));
    candidates.push(...readFilterArray(entry.adhoc_filters));
    candidates.push(...readFilterArray(formData?.adhoc_filters));
    candidates.push(...readFilterMap(extraFormData?.filters));
    candidates.push(...readFilterMap(formData?.filters));
    const filtered = candidates.filter((item) => {
        const col = String(item.col || "");
        // Drop obvious display-text artifacts like "2023-07-01, 2022-07-01".
        if (col.includes(",") && !col.includes("_"))
            return false;
        return true;
    });
    return dedupeFilters(filtered);
}
function extractScopedFiltersFromDataMask(mask) {
    if (!mask || typeof mask !== "object") {
        return { crossFilters: [], globalFilters: [], sourceSliceId: null };
    }
    const maskObj = mask;
    const crossFilters = [];
    const globalFilters = [];
    let sourceSliceId = null;
    for (const [key, value] of Object.entries(maskObj)) {
        if (!value || typeof value !== "object")
            continue;
        const entry = value;
        if (/^NATIVE_FILTER-/.test(key)) {
            globalFilters.push(...extractGlobalFiltersFromNativeEntry(key, entry));
            continue;
        }
        if (!/^\d+$/.test(key))
            continue;
        crossFilters.push(...extractFiltersFromContainer(entry));
        const dataMaskEntry = entry;
        const fsFilters = (dataMaskEntry.filterState?.filters ?? null);
        const efFilters = (dataMaskEntry.extraFormData?.filters ?? null);
        const hasFs = fsFilters &&
            typeof fsFilters === "object" &&
            Object.keys(fsFilters).length > 0;
        const hasEf = efFilters &&
            typeof efFilters === "object" &&
            Object.keys(efFilters).length > 0;
        if ((hasFs || hasEf) && sourceSliceId === null) {
            sourceSliceId = Number(key);
        }
    }
    // Fallback for unexpected mask shapes.
    if (crossFilters.length === 0 && globalFilters.length === 0) {
        return {
            crossFilters: extractFiltersFromDataMask(mask),
            globalFilters: [],
            sourceSliceId: inferSourceSliceId(mask),
        };
    }
    return {
        crossFilters: dedupeFilters(crossFilters),
        globalFilters: dedupeFilters(globalFilters),
        sourceSliceId,
    };
}
function normalizeValue(value) {
    if (Array.isArray(value)) {
        return [...value]
            .map((item) => normalizeValue(item))
            .sort((a, b) => safeStringify(a).localeCompare(safeStringify(b)));
    }
    if (value && typeof value === "object") {
        const obj = value;
        const keys = Object.keys(obj).sort();
        const out = {};
        for (const key of keys)
            out[key] = normalizeValue(obj[key]);
        return out;
    }
    return value;
}
function filterKey(filter) {
    return `${filter.col}|${filter.op}|${safeStringify(normalizeValue(filter.val))}`;
}
function hasExplicitFilterSignal(msg) {
    if (!msg || typeof msg !== "object")
        return false;
    const data = msg;
    const payload = data.payload;
    const candidates = [
        data,
        data.formData,
        data.form_data,
        payload,
        payload?.formData,
        payload?.form_data,
        data.extra_form_data,
        payload?.extra_form_data,
    ];
    return candidates.some((obj) => {
        if (!obj)
            return false;
        return ("filters" in obj ||
            "adhoc_filters" in obj ||
            "extra_form_data" in obj ||
            "dataMask" in obj ||
            "data_mask" in obj);
    });
}
function parseDashboardId(value) {
    if (typeof value === "number" && Number.isFinite(value))
        return Math.trunc(value);
    if (typeof value === "string") {
        const n = Number(value);
        if (Number.isFinite(n))
            return Math.trunc(n);
    }
    return null;
}
function inferSourceSliceId(mask) {
    if (!mask || typeof mask !== "object")
        return null;
    const entries = Object.entries(mask);
    for (const [key, value] of entries) {
        if (!/^\d+$/.test(key) || !value || typeof value !== "object")
            continue;
        const entry = value;
        const fsFilters = (entry.filterState?.filters ?? null);
        const efFilters = (entry.extraFormData?.filters ?? null);
        const hasFs = fsFilters &&
            typeof fsFilters === "object" &&
            Object.keys(fsFilters).length > 0;
        const hasEf = efFilters &&
            typeof efFilters === "object" &&
            Object.keys(efFilters).length > 0;
        if (hasFs || hasEf)
            return Number(key);
    }
    return null;
}
export default function SupersetEmbed(props) {
    const args = props.args;
    const mountRef = useRef(null);
    const dashboardId = args.dashboardId;
    const supersetDomain = args.supersetDomain;
    const guestToken = args.guestToken;
    const eventApiBase = args.eventApiBase;
    const sessionId = args.sessionId;
    const userId = args.userId;
    const dashboardIdNum = parseDashboardId(dashboardId);
    const dashboardIdRef = useRef(dashboardIdNum);
    const lastUiChartIdRef = useRef(null);
    const requestedHeight = args.height ?? 900;
    const effectiveHeight = Math.max(200, requestedHeight);
    const uiConfig = useMemo(() => ({
        hideTitle: false,
        hideChartControls: false,
        hideTab: false,
        emitDataMasks: true,
        filters: { expanded: true },
    }), []);
    useEffect(() => {
        Streamlit.setComponentReady();
    }, []);
    const prevCrossFiltersRef = useRef([]);
    const prevGlobalFiltersRef = useRef([]);
    const deviceIdRef = useRef("");
    useEffect(() => {
        try {
            const storageKey = "agent4olap_device_id";
            let id = window.localStorage.getItem(storageKey);
            if (!id) {
                id =
                    window.crypto && "randomUUID" in window.crypto
                        ? window.crypto.randomUUID()
                        : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
                window.localStorage.setItem(storageKey, id);
            }
            deviceIdRef.current = id;
        }
        catch {
            deviceIdRef.current = "";
        }
    }, []);
    const postEvent = async (eventType, payload) => {
        if (!eventApiBase || !sessionId)
            return;
        try {
            console.debug("[superset-embed] POST /events", {
                eventType,
                sessionId,
                userId,
                deviceId: deviceIdRef.current || null,
            });
            let fetchFn = window.fetch.bind(window);
            try {
                if (window.top && window.top !== window) {
                    const topFetch = window.top.fetch;
                    if (typeof topFetch === "function") {
                        fetchFn = topFetch.bind(window.top);
                    }
                }
            }
            catch {
                fetchFn = window.fetch.bind(window);
            }
            await fetchFn(`${eventApiBase.replace(/\/+$/, "")}/events`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    session_id: sessionId,
                    user_id: userId ?? null,
                    device_id: deviceIdRef.current || null,
                    event_type: eventType,
                    payload,
                }),
            });
        }
        catch (err) {
            console.warn("[superset-embed] Failed to post event", err, {
                eventType,
                eventApiBase,
                sessionId,
            });
        }
    };
    useEffect(() => {
        const mount = mountRef.current;
        if (!mount)
            return;
        if (!dashboardId || !supersetDomain || !guestToken)
            return;
        let dataMaskPollId = null;
        // Pre-size the mount point BEFORE embedding
        mount.style.width = "100%";
        mount.style.height = `${effectiveHeight}px`;
        mount.innerHTML = "";
        Streamlit.setFrameHeight(effectiveHeight);
        embedDashboard({
            id: dashboardId,
            supersetDomain,
            mountPoint: mount,
            fetchGuestToken: async () => guestToken,
            dashboardUiConfig: uiConfig,
        })
            .then((dashboard) => {
            postEvent("embed_loaded", {
                dashboard_id: dashboardIdNum ?? null,
                source: "superset_embed_component",
            });
            const debugWindow = window;
            debugWindow.__supersetEmbeddedDashboard = dashboard;
            debugWindow.__getSupersetDataMask = () => dashboard.getDataMask();
            const applyFilterDiff = (snapshot) => {
                const diffFilters = (previous, next) => {
                    const prevMap = new Map(previous.map((f) => [filterKey(f), f]));
                    const nextMap = new Map(next.map((f) => [filterKey(f), f]));
                    const added = [...nextMap.entries()]
                        .filter(([k]) => !prevMap.has(k))
                        .map(([, f]) => f);
                    const removed = [...prevMap.entries()]
                        .filter(([k]) => !nextMap.has(k))
                        .map(([, f]) => f);
                    return { added, removed };
                };
                const crossDiff = diffFilters(prevCrossFiltersRef.current, snapshot.crossFilters);
                const globalDiff = diffFilters(prevGlobalFiltersRef.current, snapshot.globalFilters);
                const effectiveSourceSliceId = snapshot.sourceSliceId ?? lastUiChartIdRef.current;
                const effectiveDashboardId = dashboardIdRef.current;
                for (const filter of crossDiff.added) {
                    postEvent("cross_filter_added", {
                        dashboard_id: effectiveDashboardId,
                        source_slice_id: effectiveSourceSliceId,
                        filter,
                    });
                }
                for (const filter of crossDiff.removed) {
                    postEvent("cross_filter_removed", {
                        dashboard_id: effectiveDashboardId,
                        source_slice_id: effectiveSourceSliceId,
                        filter,
                    });
                }
                for (const filter of globalDiff.added) {
                    postEvent("global_filter_added", {
                        dashboard_id: effectiveDashboardId,
                        source_slice_id: null,
                        filter,
                    });
                }
                for (const filter of globalDiff.removed) {
                    postEvent("global_filter_removed", {
                        dashboard_id: effectiveDashboardId,
                        source_slice_id: null,
                        filter,
                    });
                }
                prevCrossFiltersRef.current = snapshot.crossFilters;
                prevGlobalFiltersRef.current = snapshot.globalFilters;
            };
            const lastMaskRef = { current: null };
            // Cross-filter changes are emitted through data mask updates, not UI postMessage events.
            dashboard
                .getDataMask()
                .then((mask) => {
                lastMaskRef.current = mask;
                const snapshot = extractScopedFiltersFromDataMask(mask);
                prevCrossFiltersRef.current = snapshot.crossFilters;
                prevGlobalFiltersRef.current = snapshot.globalFilters;
            })
                .catch(() => {
                console.debug("[superset-embed] initial getDataMask unavailable");
                prevCrossFiltersRef.current = [];
                prevGlobalFiltersRef.current = [];
            });
            dashboard.observeDataMask((mask) => {
                lastMaskRef.current = mask;
                applyFilterDiff(extractScopedFiltersFromDataMask(mask));
            });
            // Fallback for environments where observeDataMask callbacks are not emitted reliably.
            dataMaskPollId = window.setInterval(() => {
                dashboard
                    .getDataMask()
                    .then((mask) => {
                    lastMaskRef.current = mask;
                    applyFilterDiff(extractScopedFiltersFromDataMask(mask));
                })
                    .catch((err) => {
                    console.debug("[superset-embed] poll getDataMask failed", err);
                });
            }, 1200);
            const applySize = () => {
                const iframe = mount.querySelector('iframe[title="Embedded Dashboard"]');
                if (iframe) {
                    iframe.style.width = "100%";
                    iframe.style.height = `${effectiveHeight}px`;
                    iframe.style.minHeight = `${effectiveHeight}px`;
                    iframe.style.display = "block";
                    // CRITICAL: Trigger resize event to force Superset to recalculate layouts
                    if (iframe.contentWindow) {
                        // Also try postMessage for Superset's event listener
                        iframe.contentWindow.postMessage({ type: 'resize', width: mount.offsetWidth, height: effectiveHeight }, supersetDomain);
                        // Bind parent so injected Superset JS can post active tab updates.
                        iframe.contentWindow.postMessage({ id: 'bind-parent' }, supersetDomain);
                    }
                    // Force parent window resize as fallback
                    window.dispatchEvent(new Event('resize'));
                    Streamlit.setFrameHeight(effectiveHeight);
                    return true;
                }
                return false;
            };
            // Try multiple times with delays to catch async rendering
            const attempts = [0, 100, 300, 500, 1000];
            attempts.forEach(delay => {
                setTimeout(() => {
                    applySize();
                }, delay);
            });
            // Also watch for late insertion
            const obs = new MutationObserver(() => {
                if (applySize()) {
                    // Keep observer alive to catch dashboard re-renders
                    setTimeout(() => applySize(), 200);
                }
            });
            obs.observe(mount, { childList: true, subtree: true });
            // Clean up observer after 5 seconds
            setTimeout(() => obs.disconnect(), 5000);
        })
            .catch((e) => {
            console.error("embedDashboard failed:", e);
            mount.innerHTML = `<div style="padding:12px;font-family:system-ui;color:#b91c1c;">Embed failed: ${String(e)}</div>`;
            Streamlit.setFrameHeight(220);
        });
        return () => {
            if (dataMaskPollId !== null) {
                window.clearInterval(dataMaskPollId);
            }
        };
    }, [dashboardId, supersetDomain, guestToken, effectiveHeight, uiConfig]);
    useEffect(() => {
        const configuredOrigin = (() => {
            try {
                return new URL(supersetDomain).origin;
            }
            catch {
                return "";
            }
        })();
        const allowSameProtoPortAnyHost = (() => {
            try {
                const u = new URL(supersetDomain);
                return (u.hostname === "localhost" ||
                    u.hostname === "127.0.0.1" ||
                    u.hostname === "0.0.0.0");
            }
            catch {
                return false;
            }
        })();
        const isAllowedOrigin = (origin) => {
            if (!origin)
                return false;
            if (origin === configuredOrigin)
                return true;
            if (!allowSameProtoPortAnyHost)
                return false;
            try {
                const eventUrl = new URL(origin);
                const cfg = new URL(supersetDomain);
                const cfgPort = cfg.port || (cfg.protocol === "https:" ? "443" : "80");
                const eventPort = eventUrl.port || (eventUrl.protocol === "https:" ? "443" : "80");
                return eventUrl.protocol === cfg.protocol && eventPort === cfgPort;
            }
            catch {
                return false;
            }
        };
        const handler = (event) => {
            if (!supersetDomain)
                return;
            if (!isAllowedOrigin(event.origin))
                return;
            if (!event.data)
                return;
            let message = event.data;
            if (typeof message === "string") {
                try {
                    message = JSON.parse(message);
                }
                catch {
                    // Keep raw string for tab parsing fallback.
                }
            }
            const data = message && typeof message === "object"
                ? message
                : {};
            if (window.top && window.top !== window) {
                try {
                    window.top.dispatchEvent(new MessageEvent("message", {
                        data: event.data,
                        origin: event.origin,
                    }));
                }
                catch {
                    // noop
                }
            }
            console.debug("[superset-embed] message from dashboard", {
                origin: event.origin,
                keys: Object.keys(data),
            });
            const uiPayload = data.payload;
            const eventDashboardId = parseDashboardId(data.dashboard_id) ||
                parseDashboardId(data.dashboardId) ||
                parseDashboardId(uiPayload?.dashboard_id) ||
                parseDashboardId(uiPayload?.dashboardId);
            if (eventDashboardId !== null) {
                dashboardIdRef.current = eventDashboardId;
            }
            const eventSliceId = parseDashboardId(data.chart_id) ||
                parseDashboardId(data.chartId) ||
                parseDashboardId(uiPayload?.chart_id) ||
                parseDashboardId(uiPayload?.chartId) ||
                parseDashboardId(uiPayload?.slice_id) ||
                parseDashboardId(uiPayload?.sliceId);
            if (eventSliceId !== null) {
                lastUiChartIdRef.current = eventSliceId;
            }
            if (data && data.id === 'superset-ui-event') {
                const eventType = String(data.event ||
                    data.event_type ||
                    uiPayload?.event ||
                    uiPayload?.event_type ||
                    "superset_ui_event");
                postEvent(eventType, {
                    dashboard_id: eventDashboardId ?? dashboardIdRef.current ?? dashboardIdNum ?? null,
                    slice_id: data.chart_id || data.chartId || uiPayload?.chart_id || uiPayload?.chartId || uiPayload?.slice_id || uiPayload?.sliceId || null,
                    viz_type: data.viz_type || uiPayload?.viz_type || null,
                    payload: data.payload,
                });
                return;
            }
            const hasStructuredUiSignal = data.event !== undefined ||
                data.event_type !== undefined ||
                data.chart_id !== undefined ||
                data.chartId !== undefined ||
                uiPayload?.chart_id !== undefined ||
                uiPayload?.chartId !== undefined ||
                uiPayload?.slice_id !== undefined ||
                uiPayload?.sliceId !== undefined;
            if (hasStructuredUiSignal) {
                const eventType = String(data.event ||
                    data.event_type ||
                    uiPayload?.event ||
                    uiPayload?.event_type ||
                    "superset_ui_event");
                postEvent(eventType, {
                    dashboard_id: eventDashboardId ?? dashboardIdRef.current ?? dashboardIdNum ?? null,
                    slice_id: data.chart_id ||
                        data.chartId ||
                        uiPayload?.chart_id ||
                        uiPayload?.chartId ||
                        uiPayload?.slice_id ||
                        uiPayload?.sliceId ||
                        null,
                    viz_type: data.viz_type || uiPayload?.viz_type || null,
                    payload: data.payload ?? data,
                });
                return;
            }
            const raw = typeof event.data === "string" ? event.data : safeStringify(event.data);
            if (!/tab/i.test(raw))
                return;
            const tabId = (data && (data.tabId || data.tab_id)) ||
                (data && (data.activeTabId || data.active_tab_id));
            const tabName = (data && (data.tabName || data.tab_name)) ||
                (data && (data.activeTabName || data.active_tab_name));
            postEvent("superset_tab_click", {
                dashboard_id: eventDashboardId ?? dashboardIdRef.current ?? dashboardIdNum ?? null,
                tab_id: tabId,
                tab_name: tabName,
                raw: event.data,
            });
        };
        window.addEventListener("message", handler);
        return () => {
            window.removeEventListener("message", handler);
        };
    }, [dashboardId, dashboardIdNum, supersetDomain, eventApiBase, sessionId, userId]);
    return (_jsx("div", { style: { width: "100%" }, children: _jsx("div", { ref: mountRef, style: {
                width: "100%",
                height: effectiveHeight,
                borderRadius: 12,
                overflow: "hidden",
                minWidth: "100%", // Ensure minimum width
            } }) }));
}

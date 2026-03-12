import React, { useEffect, useMemo, useRef } from "react";
import { Streamlit, ComponentProps } from "streamlit-component-lib";
import { embedDashboard } from "@superset-ui/embedded-sdk";

console.log("SupersetEmbed BUILD:", new Date().toISOString());

type Args = {
  dashboardId: string;
  supersetDomain: string;
  guestToken: string;
  eventApiBase?: string;
  sessionId?: string;
  userId?: string;
  height?: number;
};

type NormFilter = { col: string; op: string; val: unknown };
type DataMaskEntry = { id?: string; filterState?: Record<string, unknown>; extraFormData?: Record<string, unknown> };
type FilterScopeSnapshot = {
  crossFilters: NormFilter[];
  globalFilters: NormFilter[];
  sourceSliceId: number | null;
};

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function normalizeFilter(raw: Record<string, unknown>): NormFilter | null {
  const col = raw.col ?? raw.subject;
  if (col === undefined || col === null || String(col).trim() === "") return null;
  const op = raw.op ?? raw.operator ?? "==";
  const val = raw.val ?? raw.comparator;
  return { col: String(col), op: String(op), val };
}

function readFilterArray(value: unknown): NormFilter[] {
  if (!Array.isArray(value)) return [];
  const out: NormFilter[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const normalized = normalizeFilter(item as Record<string, unknown>);
    if (normalized) out.push(normalized);
  }
  return out;
}

function readFilterMap(value: unknown): NormFilter[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const out: NormFilter[] = [];
  for (const [col, rawVal] of Object.entries(value as Record<string, unknown>)) {
    if (!col || col.trim() === "") continue;
    const normalizedVal =
      Array.isArray(rawVal) &&
      rawVal.length === 1 &&
      Array.isArray(rawVal[0])
        ? rawVal[0]
        : rawVal;
    out.push({ col: String(col), op: "IN", val: normalizedVal });
  }
  return out;
}

function extractFiltersFromContainer(obj: Record<string, unknown>): NormFilter[] {
  const formData = (obj.formData ?? obj.form_data) as
    | Record<string, unknown>
    | undefined;
  const payload = obj.payload as Record<string, unknown> | undefined;
  const payloadFormData = (payload?.formData ?? payload?.form_data) as
    | Record<string, unknown>
    | undefined;
  const extra = (obj.extra_form_data ??
    formData?.extra_form_data ??
    payload?.extra_form_data ??
    payloadFormData?.extra_form_data) as Record<string, unknown> | undefined;
  const extraCamel = obj.extraFormData as Record<string, unknown> | undefined;
  const filterState = obj.filterState as Record<string, unknown> | undefined;
  const filterStateSnake = obj.filter_state as Record<string, unknown> | undefined;

  const filters: NormFilter[] = [];
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

function extractFiltersFromMessage(msg: unknown): NormFilter[] {
  if (!msg || typeof msg !== "object") return [];
  return extractFiltersFromContainer(msg as Record<string, unknown>);
}

function extractFiltersFromDataMask(mask: unknown): NormFilter[] {
  if (!mask || typeof mask !== "object") return [];
  const queue: unknown[] = [mask];
  const seen = new Set<unknown>();
  const collected: NormFilter[] = [];

  while (queue.length > 0 && seen.size < 400) {
    const node = queue.shift();
    if (!node || typeof node !== "object" || seen.has(node)) continue;
    seen.add(node);

    const obj = node as Record<string, unknown>;
    collected.push(...extractFiltersFromContainer(obj));

    const nested = [obj.dataMask, obj.data_mask, obj.payload, obj.extraFormData];
    for (const value of nested) {
      if (value && typeof value === "object") queue.push(value);
    }
    for (const value of Object.values(obj)) {
      if (value && typeof value === "object") queue.push(value);
    }
  }

  const dedup = new Map<string, NormFilter>();
  for (const filter of collected) dedup.set(filterKey(filter), filter);
  return [...dedup.values()];
}

function dedupeFilters(filters: NormFilter[]): NormFilter[] {
  const dedup = new Map<string, NormFilter>();
  for (const filter of filters) dedup.set(filterKey(filter), filter);
  return [...dedup.values()];
}

function extractGlobalFiltersFromNativeEntry(
  nativeId: string,
  entry: Record<string, unknown>
): NormFilter[] {
  // Native filter entries can contain display labels in filterState; those are
  // not stable column identifiers. Only trust structured filter payload fields.
  const extraFormData = (entry.extraFormData ??
    entry.extra_form_data) as Record<string, unknown> | undefined;
  const formData = (entry.formData ?? entry.form_data) as
    | Record<string, unknown>
    | undefined;

  const candidates: NormFilter[] = [];
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
    if (col.includes(",") && !col.includes("_")) return false;
    return true;
  });
  return dedupeFilters(filtered);
}

function extractScopedFiltersFromDataMask(mask: unknown): FilterScopeSnapshot {
  if (!mask || typeof mask !== "object") {
    return { crossFilters: [], globalFilters: [], sourceSliceId: null };
  }

  const maskObj = mask as Record<string, unknown>;
  const crossFilters: NormFilter[] = [];
  const globalFilters: NormFilter[] = [];
  let sourceSliceId: number | null = null;

  for (const [key, value] of Object.entries(maskObj)) {
    if (!value || typeof value !== "object") continue;
    const entry = value as Record<string, unknown>;

    if (/^NATIVE_FILTER-/.test(key)) {
      globalFilters.push(...extractGlobalFiltersFromNativeEntry(key, entry));
      continue;
    }

    if (!/^\d+$/.test(key)) continue;

    crossFilters.push(...extractFiltersFromContainer(entry));
    const dataMaskEntry = entry as DataMaskEntry;
    const fsFilters = (dataMaskEntry.filterState?.filters ?? null) as unknown;
    const efFilters = (dataMaskEntry.extraFormData?.filters ?? null) as unknown;
    const hasFs =
      fsFilters &&
      typeof fsFilters === "object" &&
      Object.keys(fsFilters as Record<string, unknown>).length > 0;
    const hasEf =
      efFilters &&
      typeof efFilters === "object" &&
      Object.keys(efFilters as Record<string, unknown>).length > 0;
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

function normalizeValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return [...value]
      .map((item) => normalizeValue(item))
      .sort((a, b) => safeStringify(a).localeCompare(safeStringify(b)));
  }
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj).sort();
    const out: Record<string, unknown> = {};
    for (const key of keys) out[key] = normalizeValue(obj[key]);
    return out;
  }
  return value;
}

function filterKey(filter: NormFilter): string {
  return `${filter.col}|${filter.op}|${safeStringify(normalizeValue(filter.val))}`;
}

function hasExplicitFilterSignal(msg: unknown): boolean {
  if (!msg || typeof msg !== "object") return false;
  const data = msg as Record<string, unknown>;
  const payload = data.payload as Record<string, unknown> | undefined;
  const candidates: Array<Record<string, unknown> | undefined> = [
    data,
    data.formData as Record<string, unknown> | undefined,
    data.form_data as Record<string, unknown> | undefined,
    payload,
    payload?.formData as Record<string, unknown> | undefined,
    payload?.form_data as Record<string, unknown> | undefined,
    data.extra_form_data as Record<string, unknown> | undefined,
    payload?.extra_form_data as Record<string, unknown> | undefined,
  ];
  return candidates.some((obj) => {
    if (!obj) return false;
    return (
      "filters" in obj ||
      "adhoc_filters" in obj ||
      "extra_form_data" in obj ||
      "dataMask" in obj ||
      "data_mask" in obj
    );
  });
}

function parseDashboardId(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "string") {
    const n = Number(value);
    if (Number.isFinite(n)) return Math.trunc(n);
  }
  return null;
}

function inferSourceSliceId(mask: unknown): number | null {
  if (!mask || typeof mask !== "object") return null;
  const entries = Object.entries(mask as Record<string, unknown>);
  for (const [key, value] of entries) {
    if (!/^\d+$/.test(key) || !value || typeof value !== "object") continue;
    const entry = value as DataMaskEntry;
    const fsFilters = (entry.filterState?.filters ?? null) as unknown;
    const efFilters = (entry.extraFormData?.filters ?? null) as unknown;
    const hasFs =
      fsFilters &&
      typeof fsFilters === "object" &&
      Object.keys(fsFilters as Record<string, unknown>).length > 0;
    const hasEf =
      efFilters &&
      typeof efFilters === "object" &&
      Object.keys(efFilters as Record<string, unknown>).length > 0;
    if (hasFs || hasEf) return Number(key);
  }
  return null;
}

export default function SupersetEmbed(props: ComponentProps) {
  const args = props.args as Args;
  const mountRef = useRef<HTMLDivElement | null>(null);

  const dashboardId = args.dashboardId;
  const supersetDomain = args.supersetDomain;
  const guestToken = args.guestToken;
  const eventApiBase = args.eventApiBase;
  const sessionId = args.sessionId;
  const userId = args.userId;
  const dashboardIdNum = parseDashboardId(dashboardId);
  const dashboardIdRef = useRef<number | null>(dashboardIdNum);
  const lastUiChartIdRef = useRef<number | null>(null);

  const requestedHeight = args.height ?? 900;
  const effectiveHeight = Math.max(
    200,
    requestedHeight
  );

  const uiConfig = useMemo(
    () => ({
      hideTitle: false,
      hideChartControls: false,
      hideTab: false,
      emitDataMasks: true,
      filters: { expanded: true },
    }),
    []
  );

  useEffect(() => {
    Streamlit.setComponentReady();
  }, []);

  const prevCrossFiltersRef = useRef<NormFilter[]>([]);
  const prevGlobalFiltersRef = useRef<NormFilter[]>([]);

  const postEvent = async (eventType: string, payload: Record<string, unknown>) => {
    if (!eventApiBase || !sessionId) return;
    try {
      await fetch(`${eventApiBase.replace(/\/+$/, "")}/events`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          user_id: userId ?? null,
          event_type: eventType,
          payload,
        }),
      });
    } catch (err) {
      console.warn("Failed to post event", err);
    }
  };

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;
    if (!dashboardId || !supersetDomain || !guestToken) return;
    let dataMaskPollId: number | null = null;

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
        const debugWindow = window as typeof window & {
          __supersetEmbeddedDashboard?: unknown;
          __getSupersetDataMask?: () => Promise<unknown>;
        };
        debugWindow.__supersetEmbeddedDashboard = dashboard;
        debugWindow.__getSupersetDataMask = () => dashboard.getDataMask();

        const applyFilterDiff = (snapshot: FilterScopeSnapshot) => {
          const diffFilters = (previous: NormFilter[], next: NormFilter[]) => {
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

          const crossDiff = diffFilters(
            prevCrossFiltersRef.current,
            snapshot.crossFilters
          );
          const globalDiff = diffFilters(
            prevGlobalFiltersRef.current,
            snapshot.globalFilters
          );

          const effectiveSourceSliceId =
            snapshot.sourceSliceId ?? lastUiChartIdRef.current;
          const effectiveDashboardId = dashboardIdRef.current;

          for (const filter of crossDiff.added) {
            postEvent("cross_filter_added", {
              dashboard_id: effectiveDashboardId,
              filter_type: "cross",
              source_slice_id: effectiveSourceSliceId,
              filter,
            });
          }
          for (const filter of crossDiff.removed) {
            postEvent("cross_filter_removed", {
              dashboard_id: effectiveDashboardId,
              filter_type: "cross",
              source_slice_id: effectiveSourceSliceId,
              filter,
            });
          }
          for (const filter of globalDiff.added) {
            postEvent("native_filter_added", {
              dashboard_id: effectiveDashboardId,
              filter_type: "native",
              source_slice_id: null,
              filter,
            });
          }
          for (const filter of globalDiff.removed) {
            postEvent("native_filter_removed", {
              dashboard_id: effectiveDashboardId,
              filter_type: "native",
              source_slice_id: null,
              filter,
            });
          }

          prevCrossFiltersRef.current = snapshot.crossFilters;
          prevGlobalFiltersRef.current = snapshot.globalFilters;
        };
        const lastMaskRef: { current: unknown } = { current: null };

        // Cross-filter changes are emitted through data mask updates, not UI postMessage events.
        dashboard
          .getDataMask()
          .then((mask: unknown) => {
            lastMaskRef.current = mask;
            const snapshot = extractScopedFiltersFromDataMask(mask);
            prevCrossFiltersRef.current = snapshot.crossFilters;
            prevGlobalFiltersRef.current = snapshot.globalFilters;
          })
          .catch(() => {
            prevCrossFiltersRef.current = [];
            prevGlobalFiltersRef.current = [];
          });

        dashboard.observeDataMask((mask: unknown) => {
          lastMaskRef.current = mask;
          applyFilterDiff(extractScopedFiltersFromDataMask(mask));
        });

        // Fallback for environments where observeDataMask callbacks are not emitted reliably.
        dataMaskPollId = window.setInterval(() => {
          dashboard
            .getDataMask()
            .then((mask: unknown) => {
              lastMaskRef.current = mask;
              applyFilterDiff(extractScopedFiltersFromDataMask(mask));
            })
            .catch(() => {});
        }, 1200);

        const applySize = () => {
          const iframe = mount.querySelector('iframe[title="Embedded Dashboard"]') as HTMLIFrameElement | null;
          if (iframe) {
            iframe.style.width = "100%";
            iframe.style.height = `${effectiveHeight}px`;
            iframe.style.minHeight = `${effectiveHeight}px`;
            iframe.style.display = "block";
            
            // CRITICAL: Trigger resize event to force Superset to recalculate layouts
            if (iframe.contentWindow) {
              // Also try postMessage for Superset's event listener
              iframe.contentWindow.postMessage(
                { type: 'resize', width: mount.offsetWidth, height: effectiveHeight },
                supersetDomain
              );

              // Bind parent so injected Superset JS can post active tab updates.
              iframe.contentWindow.postMessage(
                { id: 'bind-parent' },
                supersetDomain
              );
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
    const resolveLegendEvent = (
      rawType: string,
      rawData: Record<string, unknown>,
      rawPayload: Record<string, unknown> | undefined,
    ) => {
      if (rawType !== "legend_toggle") {
        return {
          eventType: rawType,
          legendName: null as string | null,
          legendActive: null as boolean | null,
          selected: null as Record<string, unknown> | null,
        };
      }
      const clickedRaw =
        rawData.clicked_name ??
        rawData.clickedName ??
        rawPayload?.clicked_name ??
        rawPayload?.clickedName ??
        rawPayload?.name ??
        rawData.name;
      const legendName =
        typeof clickedRaw === "string" && clickedRaw.trim()
          ? clickedRaw.trim()
          : null;
      const selected =
        rawPayload?.selected && typeof rawPayload.selected === "object"
          ? (rawPayload.selected as Record<string, unknown>)
          : rawData.selected && typeof rawData.selected === "object"
            ? (rawData.selected as Record<string, unknown>)
            : null;
      let legendActive: boolean | null = null;
      if (
        legendName &&
        selected &&
        Object.prototype.hasOwnProperty.call(selected, legendName)
      ) {
        const rawValue = selected[legendName];
        if (typeof rawValue === "boolean") legendActive = rawValue;
        else if (rawValue === 0 || rawValue === 1) legendActive = Boolean(rawValue);
      }
      const mappedType =
        legendActive === true
          ? "legend_toggle_activate"
          : legendActive === false
            ? "legend_toggle_deactivate"
            : "legend_toggle";
      return { eventType: mappedType, legendName, legendActive, selected };
    };

    const handler = (event: MessageEvent) => {
      if (!supersetDomain) return;
      const origin = new URL(supersetDomain).origin;
      if (event.origin !== origin) return;
      if (!event.data) return;

      let message: unknown = event.data;
      if (typeof message === "string") {
        try {
          message = JSON.parse(message);
        } catch {
          // Keep raw string for tab parsing fallback.
        }
      }
      const data =
        message && typeof message === "object"
          ? (message as Record<string, unknown>)
          : ({} as Record<string, unknown>);
      const uiPayload = data.payload as Record<string, unknown> | undefined;
      const eventDashboardId =
        parseDashboardId(data.dashboard_id) ||
        parseDashboardId(data.dashboardId) ||
        parseDashboardId(uiPayload?.dashboard_id) ||
        parseDashboardId(uiPayload?.dashboardId);
      if (eventDashboardId !== null) {
        dashboardIdRef.current = eventDashboardId;
      }
      const eventSliceId =
        parseDashboardId(data.chart_id) ||
        parseDashboardId(data.chartId) ||
        parseDashboardId(uiPayload?.chart_id) ||
        parseDashboardId(uiPayload?.chartId) ||
        parseDashboardId(uiPayload?.slice_id) ||
        parseDashboardId(uiPayload?.sliceId);
      if (eventSliceId !== null) {
        lastUiChartIdRef.current = eventSliceId;
      }

      if (data && data.id === 'superset-ui-event') {
        const baseEventType = String(
          data.event ||
            data.event_type ||
            uiPayload?.event ||
            uiPayload?.event_type ||
            "superset_ui_event"
        );
        const legendResolved = resolveLegendEvent(baseEventType, data, uiPayload);
        postEvent(legendResolved.eventType, {
          dashboard_id:
            eventDashboardId ?? dashboardIdRef.current ?? dashboardIdNum ?? null,
          slice_id: data.chart_id || data.chartId || uiPayload?.chart_id || uiPayload?.chartId || uiPayload?.slice_id || uiPayload?.sliceId || null,
          viz_type: data.viz_type || uiPayload?.viz_type || null,
          legend_name: legendResolved.legendName,
          legend_active: legendResolved.legendActive,
          selected: legendResolved.selected,
          payload: data.payload,
        });
        return;
      }

      const raw =
        typeof event.data === "string" ? event.data : safeStringify(event.data);
      if (!/tab/i.test(raw)) return;

      const tabId =
        (data && (data.tabId || data.tab_id)) ||
        (data && (data.activeTabId || data.active_tab_id));
      const tabName =
        (data && (data.tabName || data.tab_name)) ||
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

  return (
    <div style={{ width: "100%" }}>
      <div
        ref={mountRef}
        style={{
          width: "100%",
          height: effectiveHeight,
          borderRadius: 12,
          overflow: "hidden",
          minWidth: "100%", // Ensure minimum width
        }}
      />
    </div>
  );
}

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

export default function SupersetEmbed(props: ComponentProps) {
  const args = props.args as Args;
  const mountRef = useRef<HTMLDivElement | null>(null);

  const dashboardId = args.dashboardId;
  const supersetDomain = args.supersetDomain;
  const guestToken = args.guestToken;
  const eventApiBase = args.eventApiBase;
  const sessionId = args.sessionId;
  const userId = args.userId;

  const requestedHeight = args.height ?? 900;
  const effectiveHeight = Math.max(
    1000,
    Math.min(requestedHeight, window.innerHeight - 120)
  );

  const uiConfig = useMemo(
    () => ({
      hideTitle: false,
      hideChartControls: false,
      hideTab: false,
      filters: { expanded: true },
    }),
    []
  );

  useEffect(() => {
    Streamlit.setComponentReady();
  }, []);

  const lastTabEventRef = useRef<string | null>(null);

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
      .then(() => {
        const applySize = () => {
          const iframe = mount.querySelector('iframe[title="Embedded Dashboard"]') as HTMLIFrameElement | null;
          if (iframe) {
            iframe.style.width = "100%";
            iframe.style.height = `${effectiveHeight}px`;
            iframe.style.minHeight = `${effectiveHeight}px`;
            iframe.style.display = "block";
            
            // CRITICAL: Trigger resize event to force Superset to recalculate layouts
            if (iframe.contentWindow) {
              // Dispatch resize event to the iframe's window
              iframe.contentWindow.dispatchEvent(new Event('resize'));
              
              // Also try postMessage for Superset's event listener
              iframe.contentWindow.postMessage(
                { type: 'resize', width: mount.offsetWidth, height: effectiveHeight },
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
  }, [dashboardId, supersetDomain, guestToken, effectiveHeight, uiConfig]);

  useEffect(() => {
    const handler = (event: MessageEvent) => {
      if (!supersetDomain) return;
      const origin = new URL(supersetDomain).origin;
      if (event.origin !== origin) return;
      if (!event.data) return;

      const raw =
        typeof event.data === "string" ? event.data : JSON.stringify(event.data);
      if (!/tab/i.test(raw)) return;

      const data = event.data as Record<string, unknown>;
      const tabId =
        (data && (data.tabId || data.tab_id)) ||
        (data && (data.activeTabId || data.active_tab_id));
      const tabName =
        (data && (data.tabName || data.tab_name)) ||
        (data && (data.activeTabName || data.active_tab_name));

      const token = raw.slice(0, 200);
      if (lastTabEventRef.current === token) return;
      lastTabEventRef.current = token;

      postEvent("superset_tab_click", {
        dashboard_id: dashboardId,
        tab_id: tabId,
        tab_name: tabName,
        raw: event.data,
      });
    };

    window.addEventListener("message", handler);
    return () => {
      window.removeEventListener("message", handler);
    };
  }, [dashboardId, supersetDomain, eventApiBase, sessionId, userId]);

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

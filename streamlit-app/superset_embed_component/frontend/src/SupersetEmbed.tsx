import React, { useEffect, useMemo, useRef } from "react";
import { Streamlit, ComponentProps } from "streamlit-component-lib";
import { embedDashboard } from "@superset-ui/embedded-sdk";

type Args = {
  dashboardId: string;
  supersetDomain: string;
  guestToken: string;
  height?: number;
};

export default function SupersetEmbed(props: ComponentProps) {
  const args = props.args as Args;
  const mountRef = useRef<HTMLDivElement | null>(null);

  const dashboardId = args.dashboardId;
  const supersetDomain = args.supersetDomain;
  const guestToken = args.guestToken;

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

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;
    if (!dashboardId || !supersetDomain || !guestToken) return;

    mount.style.width = "100%";
    mount.style.height = `${effectiveHeight}px`;
    mount.innerHTML = "";

    Streamlit.setFrameHeight(effectiveHeight);
    const t1 = window.setTimeout(() => Streamlit.setFrameHeight(effectiveHeight), 200);
    const t2 = window.setTimeout(() => Streamlit.setFrameHeight(effectiveHeight), 800);

    embedDashboard({
      id: dashboardId,
      supersetDomain,
      mountPoint: mount,
      fetchGuestToken: async () => guestToken,
      dashboardUiConfig: uiConfig,
    })
      .then(() => {
        const iframe = mount.querySelector("iframe") as HTMLIFrameElement | null;
        if (iframe) {
          iframe.style.width = "100%";
          iframe.style.height = `${effectiveHeight}px`;
          iframe.style.minHeight = `${effectiveHeight}px`;
          iframe.setAttribute("height", String(effectiveHeight));
        }
        Streamlit.setFrameHeight(effectiveHeight);
      })
      .catch((e) => {
        console.error("embedDashboard failed:", e);
        mount.innerHTML = `<div style="padding:12px;font-family:system-ui;color:#b91c1c;">Embed failed: ${String(
          e
        )}</div>`;
        Streamlit.setFrameHeight(220);
      });

    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
    };
  }, [dashboardId, supersetDomain, guestToken, effectiveHeight, uiConfig]);

  return (
    <div style={{ width: "100%" }}>
      <div
        ref={mountRef}
        style={{
          width: "100%",
          height: effectiveHeight,
          borderRadius: 12,
          overflow: "hidden",
        }}
      />
    </div>
  );
}

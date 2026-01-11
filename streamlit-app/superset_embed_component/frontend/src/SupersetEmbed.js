import { jsx as _jsx } from "react/jsx-runtime";
import { useEffect, useMemo, useRef } from "react";
import { Streamlit } from "streamlit-component-lib";
import { embedDashboard } from "@superset-ui/embedded-sdk";
export default function SupersetEmbed(props) {
    const args = props.args;
    const mountRef = useRef(null);
    const { dashboardId, supersetDomain, guestToken } = args;
    const requestedHeight = args.height ?? 900;
    const effectiveHeight = Math.max(600, Math.min(requestedHeight, window.innerHeight - 120));
    const uiConfig = useMemo(() => args.uiConfig ?? {
        hideTitle: false,
        hideChartControls: false,
        hideTab: false,
        filters: { expanded: true }
    }, [args.uiConfig]);
    useEffect(() => {
        Streamlit.setComponentReady();
    }, []);
    useEffect(() => {
        const mount = mountRef.current;
        if (!mount)
            return;
        if (!dashboardId || !supersetDomain || !guestToken) {
            mount.innerHTML =
                `<div style="padding:12px;font-family:system-ui;color:#b45309;">
          Missing args: dashboardId / supersetDomain / guestToken
        </div>`;
            Streamlit.setFrameHeight(140);
            return;
        }
        mount.style.width = "100%";
        mount.style.height = `${effectiveHeight}px`;
        mount.innerHTML = "";
        // Streamlit iframe 높이도 선반영
        Streamlit.setFrameHeight(effectiveHeight);
        let cancelled = false;
        (async () => {
            try {
                await embedDashboard({
                    id: dashboardId,
                    supersetDomain,
                    mountPoint: mount,
                    fetchGuestToken: async () => guestToken,
                    dashboardUiConfig: uiConfig
                });
                if (cancelled)
                    return;
                // embedDashboard가 iframe을 만들기 때문에, 우리는 iframe 스타일만 "외부에서" 조정
                const iframe = mount.querySelector('iframe[title="Embedded Dashboard"]');
                if (iframe) {
                    iframe.style.width = "100%";
                    iframe.style.height = `${effectiveHeight}px`;
                    iframe.style.display = "block";
                    iframe.style.border = "0";
                }
                Streamlit.setFrameHeight(effectiveHeight);
            }
            catch (e) {
                console.error("embedDashboard failed:", e);
                mount.innerHTML = `<div style="padding:12px;font-family:system-ui;color:#b91c1c;">
          Embed failed: ${String(e?.message ?? e)}
        </div>`;
                Streamlit.setFrameHeight(220);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [dashboardId, supersetDomain, guestToken, effectiveHeight, uiConfig]);
    return (_jsx("div", { style: { width: "100%" }, children: _jsx("div", { ref: mountRef, style: {
                width: "100%",
                height: effectiveHeight,
                borderRadius: 12,
                overflow: "hidden"
            } }) }));
}

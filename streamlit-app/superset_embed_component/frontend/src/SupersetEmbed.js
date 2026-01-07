import { jsx as _jsx } from "react/jsx-runtime";
import { useEffect, useMemo, useRef } from "react";
import { Streamlit } from "streamlit-component-lib";
import { embedDashboard } from "@superset-ui/embedded-sdk";
console.log("SupersetEmbed BUILD:", new Date().toISOString());
export default function SupersetEmbed(props) {
    const args = props.args;
    const mountRef = useRef(null);
    const dashboardId = args.dashboardId;
    const supersetDomain = args.supersetDomain;
    const guestToken = args.guestToken;
    const requestedHeight = args.height ?? 900;
    const effectiveHeight = Math.max(1000, Math.min(requestedHeight, window.innerHeight - 120));
    const uiConfig = useMemo(() => ({
        hideTitle: false,
        hideChartControls: false,
        hideTab: false,
        filters: { expanded: true },
    }), []);
    useEffect(() => {
        Streamlit.setComponentReady();
    }, []);
    useEffect(() => {
        const mount = mountRef.current;
        if (!mount)
            return;
        if (!dashboardId || !supersetDomain || !guestToken)
            return;
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
                const iframe = mount.querySelector('iframe[title="Embedded Dashboard"]');
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
                        iframe.contentWindow.postMessage({ type: 'resize', width: mount.offsetWidth, height: effectiveHeight }, supersetDomain);
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
    return (_jsx("div", { style: { width: "100%" }, children: _jsx("div", { ref: mountRef, style: {
                width: "100%",
                height: effectiveHeight,
                borderRadius: 12,
                overflow: "hidden",
                minWidth: "100%", // Ensure minimum width
            } }) }));
}

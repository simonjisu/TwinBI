import { jsx as _jsx } from "react/jsx-runtime";
import { useEffect, useMemo, useRef } from "react";
import { Streamlit } from "streamlit-component-lib";
import { embedDashboard } from "@superset-ui/embedded-sdk";
console.log("NEW BUILD XYZ");
export default function SupersetEmbed(props) {
    const args = props.args;
    const mountRef = useRef(null);
    const dashboardId = args.dashboardId;
    const supersetDomain = args.supersetDomain;
    const guestToken = args.guestToken;
    const height = args.height ?? 900;
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
        if (!mountRef.current)
            return;
        if (!dashboardId || !supersetDomain || !guestToken)
            return;
        mountRef.current.innerHTML = "";
        embedDashboard({
            id: dashboardId,
            supersetDomain,
            mountPoint: mountRef.current,
            fetchGuestToken: async () => guestToken,
            dashboardUiConfig: uiConfig,
        })
            .then(() => {
            Streamlit.setFrameHeight(height);
        })
            .catch((e) => {
            console.error("embedDashboard failed:", e);
            if (mountRef.current) {
                mountRef.current.innerHTML =
                    `<div style="padding:12px;font-family:system-ui;color:#b91c1c;">Embed failed: ${String(e)}</div>`;
            }
            Streamlit.setFrameHeight(200);
        });
    }, [dashboardId, supersetDomain, guestToken, height, uiConfig]);
    return (_jsx("div", { style: { width: "100%" }, children: _jsx("div", { ref: mountRef, style: {
                width: "100%",
                height: height,
                borderRadius: 12,
                overflow: "hidden",
            } }) }));
}

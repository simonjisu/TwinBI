import { jsx as _jsx } from "react/jsx-runtime";
import React from "react";
import ReactDOM from "react-dom/client";
import { withStreamlitConnection } from "streamlit-component-lib";
import SupersetEmbed from "./SupersetEmbed";
const Connected = withStreamlitConnection(SupersetEmbed);
ReactDOM.createRoot(document.getElementById("root")).render(_jsx(React.StrictMode, { children: _jsx(Connected, {}) }));

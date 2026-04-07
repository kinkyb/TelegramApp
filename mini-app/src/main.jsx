/**
 * main.jsx — Vite entry point.
 *
 * Mounts the React app into #root. The Telegram WebApp SDK is already
 * loaded via the <script> tag in index.html before this module runs.
 */

import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

// Global reset — keep it minimal, Telegram handles most of the chrome
const style = document.createElement("style");
style.textContent = `
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #121212; color: #fff; }
`;
document.head.appendChild(style);

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

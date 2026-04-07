/**
 * App.jsx — Root component.
 *
 * Initialises the Telegram WebApp SDK, then renders the Feed.
 */

import React, { useEffect } from "react";
import Feed from "./components/Feed";

export default function App() {
  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    if (tg) {
      tg.ready();    // signal that the app is ready
      tg.expand();   // request full-height mode
    }
  }, []);

  return <Feed />;
}

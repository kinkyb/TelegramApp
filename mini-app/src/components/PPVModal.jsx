/**
 * PPVModal.jsx — Overlay shown when a user taps a locked PPV item.
 *
 * Instructs the user to unlock via the bot in the channel.
 * After payment succeeds there (handled by bot.py), they can return here
 * and tap "I've paid" to have the app re-fetch the unlocked URL.
 */

import React, { useState } from "react";
import { fetchContent } from "../api";

/**
 * @param {{ item: Object, onClose: Function, onUnlocked: Function }} props
 * @param {Object}   props.item       - The locked content row from the API.
 * @param {Function} props.onClose    - Called when the modal is dismissed.
 * @param {Function} props.onUnlocked - Called with the unlocked file_url.
 */
export default function PPVModal({ item, onClose, onUnlocked }) {
  const [checking, setChecking] = useState(false);
  const [error, setError]       = useState("");

  async function handleCheck() {
    setChecking(true);
    setError("");
    try {
      const fresh = await fetchContent(item.id);
      if (!fresh.locked && fresh.file_url) {
        onUnlocked(fresh.file_url);
      } else {
        setError("Payment not confirmed yet. Please try again in a moment.");
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setChecking(false);
    }
  }

  return (
    <div style={styles.overlay} onClick={onClose}>
      <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
        <p style={styles.lock}>🔒</p>
        <p style={styles.title}>Exclusive Content</p>
        <p style={styles.desc}>
          This item costs <strong>{item.ppv_price_stars} Stars</strong>.
          <br />
          Tap <em>Unlock</em> in the channel post to pay, then come back
          and tap the button below.
        </p>

        {error && <p style={styles.error}>{error}</p>}

        <button
          onClick={handleCheck}
          disabled={checking}
          style={styles.primaryBtn}
        >
          {checking ? "Checking…" : "I've paid — show content"}
        </button>

        <button onClick={onClose} style={styles.secondaryBtn}>
          Cancel
        </button>
      </div>
    </div>
  );
}

const styles = {
  overlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.7)",
    display: "flex",
    alignItems: "flex-end",
    justifyContent: "center",
    zIndex: 999,
  },
  modal: {
    background: "#1e1e1e",
    borderRadius: "16px 16px 0 0",
    padding: "24px 20px 32px",
    width: "100%",
    maxWidth: 480,
    textAlign: "center",
  },
  lock:  { fontSize: 40, margin: "0 0 8px" },
  title: { fontSize: 18, fontWeight: 700, color: "#fff", margin: "0 0 8px" },
  desc:  { fontSize: 14, color: "#aaa", lineHeight: 1.5, margin: "0 0 16px" },
  error: { fontSize: 13, color: "#ff5555", margin: "0 0 12px" },
  primaryBtn: {
    display: "block",
    width: "100%",
    background: "#0088cc",
    color: "#fff",
    border: "none",
    borderRadius: 10,
    padding: "12px 0",
    fontSize: 15,
    fontWeight: 600,
    cursor: "pointer",
    marginBottom: 10,
  },
  secondaryBtn: {
    display: "block",
    width: "100%",
    background: "transparent",
    color: "#888",
    border: "1px solid #444",
    borderRadius: 10,
    padding: "10px 0",
    fontSize: 14,
    cursor: "pointer",
  },
};

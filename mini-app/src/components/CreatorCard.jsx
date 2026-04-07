/**
 * CreatorCard.jsx — Displays a promoted creator with their promo GIF,
 * bio, and a "Visit OnlyFans" button that opens via Telegram's openLink.
 */

import React from "react";

/**
 * @param {{ creator: Object }} props
 * @param {string} props.creator.name
 * @param {string} props.creator.bio
 * @param {string} props.creator.gif_url
 * @param {string} props.creator.onlyfans_url
 */
export default function CreatorCard({ creator }) {
  const { name, bio, gif_url, onlyfans_url } = creator;

  function handleVisit() {
    // Use Telegram's built-in link opener when available
    if (window.Telegram?.WebApp?.openLink) {
      window.Telegram.WebApp.openLink(onlyfans_url);
    } else {
      window.open(onlyfans_url, "_blank", "noopener,noreferrer");
    }
  }

  return (
    <div style={styles.card}>
      {gif_url && (
        <img
          src={gif_url}
          alt={`${name} promo`}
          style={styles.gif}
          loading="lazy"
        />
      )}
      <div style={styles.body}>
        <p style={styles.name}>{name}</p>
        {bio && <p style={styles.bio}>{bio}</p>}
        <button onClick={handleVisit} style={styles.button}>
          Visit OnlyFans
        </button>
      </div>
    </div>
  );
}

const styles = {
  card: {
    background: "#1e1e1e",
    borderRadius: 12,
    overflow: "hidden",
    marginBottom: 16,
  },
  gif: {
    width: "100%",
    display: "block",
    objectFit: "cover",
    maxHeight: 280,
  },
  body: {
    padding: "12px 14px 14px",
  },
  name: {
    margin: "0 0 4px",
    fontWeight: 700,
    fontSize: 16,
    color: "#fff",
  },
  bio: {
    margin: "0 0 10px",
    fontSize: 13,
    color: "#aaa",
    lineHeight: 1.4,
  },
  button: {
    background: "#0088cc",
    color: "#fff",
    border: "none",
    borderRadius: 8,
    padding: "8px 16px",
    fontWeight: 600,
    fontSize: 14,
    cursor: "pointer",
    width: "100%",
  },
};

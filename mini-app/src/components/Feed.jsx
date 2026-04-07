/**
 * Feed.jsx — Scrollable content feed that interleaves own content with
 * creator promo cards (every 5th item is a promo card).
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { fetchCreators, fetchFeed } from "../api";
import CreatorCard from "./CreatorCard";
import PPVModal from "./PPVModal";

const PROMO_INTERVAL = 5; // Insert a promo every N content items

export default function Feed() {
  const [items,    setItems]    = useState([]);       // content rows
  const [creators, setCreators] = useState([]);       // promoted creators
  const [page,     setPage]     = useState(1);
  const [loading,  setLoading]  = useState(false);
  const [hasMore,  setHasMore]  = useState(true);
  const [ppvItem,  setPpvItem]  = useState(null);     // item shown in modal
  const [unlockedUrls, setUnlockedUrls] = useState({}); // contentId → url
  const [error,    setError]    = useState("");

  const loaderRef = useRef(null);   // sentinel div for infinite scroll

  // Initial data load
  useEffect(() => {
    fetchCreators()
      .then((data) => setCreators(data.creators ?? []))
      .catch(console.error);

    loadPage(1);
  }, []);

  async function loadPage(targetPage) {
    if (loading) return;
    setLoading(true);
    try {
      const data = await fetchFeed(targetPage, 20);
      const newItems = data.items ?? [];
      setItems((prev) => (targetPage === 1 ? newItems : [...prev, ...newItems]));
      setHasMore(newItems.length === 20);
      setPage(targetPage);
    } catch (e) {
      console.error("Feed load error:", e);
      setHasMore(false);   // stop the observer from retrying indefinitely
      if (targetPage === 1) setError("Could not load feed. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }

  // Infinite scroll via IntersectionObserver
  const observerCb = useCallback(
    (entries) => {
      if (entries[0].isIntersecting && hasMore && !loading) {
        loadPage(page + 1);
      }
    },
    [hasMore, loading, page]
  );

  useEffect(() => {
    const el = loaderRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(observerCb, { threshold: 0.1 });
    observer.observe(el);
    return () => observer.disconnect();
  }, [observerCb]);

  // Build interleaved list: content + creator promos
  function buildFeedRows() {
    const rows = [];
    let promoIdx = 0;

    items.forEach((item, i) => {
      rows.push({ type: "content", data: item });

      // Insert a promo card after every PROMO_INTERVAL content items
      if ((i + 1) % PROMO_INTERVAL === 0 && creators.length > 0) {
        rows.push({ type: "promo", data: creators[promoIdx % creators.length] });
        promoIdx++;
      }
    });

    return rows;
  }

  function handleUnlocked(contentId, fileUrl) {
    setUnlockedUrls((prev) => ({ ...prev, [contentId]: fileUrl }));
    setPpvItem(null);
  }

  return (
    <div style={styles.container}>
      {error && <p style={styles.errorText}>{error}</p>}

      {!loading && !error && items.length === 0 && (
        <p style={styles.emptyText}>No content yet. Upload something to get started.</p>
      )}

      {buildFeedRows().map((row, idx) => {
        if (row.type === "promo") {
          return <CreatorCard key={`promo-${row.data.id}-${idx}`} creator={row.data} />;
        }

        const item     = row.data;
        const isLocked = item.is_ppv && !unlockedUrls[item.id];
        const fileUrl  = unlockedUrls[item.id] ?? item.file_url;

        return (
          <div key={`content-${item.id}`} style={styles.card}>
            <MediaPreview
              item={item}
              fileUrl={fileUrl}
              isLocked={isLocked}
              onTapLocked={() => setPpvItem(item)}
            />
            {item.caption && <p style={styles.caption}>{item.caption}</p>}
          </div>
        );
      })}

      {/* Infinite scroll sentinel */}
      <div ref={loaderRef} style={{ height: 40 }}>
        {loading && <p style={styles.loadingText}>Loading…</p>}
      </div>

      {ppvItem && (
        <PPVModal
          item={ppvItem}
          onClose={() => setPpvItem(null)}
          onUnlocked={(url) => handleUnlocked(ppvItem.id, url)}
        />
      )}
    </div>
  );
}

/** Renders the appropriate media element or a locked placeholder. */
function MediaPreview({ item, fileUrl, isLocked, onTapLocked }) {
  if (isLocked) {
    return (
      <button onClick={onTapLocked} style={styles.lockedBtn}>
        <span style={styles.lockIcon}>🔒</span>
        <span style={styles.lockLabel}>
          Unlock for {item.ppv_price_stars} Stars
        </span>
      </button>
    );
  }

  const type = item.file_type;

  if (type === "image") {
    return (
      <img src={fileUrl} alt={item.caption ?? ""} style={styles.media} loading="lazy" />
    );
  }
  if (type === "video") {
    return (
      <video src={fileUrl} controls style={styles.media} preload="metadata" />
    );
  }
  if (type === "gif") {
    return (
      <img src={fileUrl} alt={item.caption ?? ""} style={styles.media} loading="lazy" />
    );
  }
  return <a href={fileUrl} style={styles.fallbackLink}>View file</a>;
}

const styles = {
  container: {
    maxWidth: 480,
    margin: "0 auto",
    padding: "8px 12px 80px",
    fontFamily: "system-ui, sans-serif",
    background: "#121212",
    minHeight: "100vh",
    color: "#fff",
  },
  card: {
    marginBottom: 16,
    borderRadius: 12,
    overflow: "hidden",
    background: "#1e1e1e",
  },
  media: {
    width: "100%",
    display: "block",
    maxHeight: 400,
    objectFit: "cover",
  },
  caption: {
    margin: 0,
    padding: "8px 12px 12px",
    fontSize: 13,
    color: "#ccc",
    lineHeight: 1.4,
  },
  lockedBtn: {
    width: "100%",
    background: "#2a2a2a",
    border: "none",
    cursor: "pointer",
    padding: "48px 16px",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: 8,
  },
  lockIcon:  { fontSize: 36 },
  lockLabel: { color: "#aaa", fontSize: 14, fontWeight: 600 },
  fallbackLink: { display: "block", padding: 12, color: "#0088cc" },
  loadingText: { textAlign: "center", color: "#666", fontSize: 13 },
  errorText:   { textAlign: "center", color: "#ff5555", fontSize: 14, padding: "32px 0" },
  emptyText:   { textAlign: "center", color: "#666",   fontSize: 14, padding: "32px 0" },
};

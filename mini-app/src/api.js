/**
 * api.js — Thin fetch wrapper for the Flask backend.
 *
 * Every request attaches the Telegram initData as a Bearer-style
 * Authorization header so the backend can verify the caller's identity.
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

/** Return the raw Telegram initData string (empty string outside Telegram). */
function getInitData() {
  return window.Telegram?.WebApp?.initData ?? "";
}

/** Shared fetch helper that injects the Authorization header. */
async function apiFetch(path, options = {}) {
  const initData = getInitData();
  const headers = {
    "Content-Type": "application/json",
    ...(initData ? { Authorization: `tma ${initData}` } : {}),
    ...(options.headers ?? {}),
  };

  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error ?? "API error");
  }
  return res.json();
}

/**
 * Fetch a page of free content items.
 * @param {number} page - 1-based page number.
 * @param {number} perPage - Items per page.
 * @returns {Promise<{items: Array, page: number, per_page: number}>}
 */
export function fetchFeed(page = 1, perPage = 20) {
  return apiFetch(`/api/feed?page=${page}&per_page=${perPage}`);
}

/**
 * Fetch all active promoted creators.
 * @returns {Promise<{creators: Array}>}
 */
export function fetchCreators() {
  return apiFetch("/api/creators");
}

/**
 * Fetch a single content item (may be locked if PPV and not purchased).
 * @param {number} id - Content id.
 * @returns {Promise<Object>}
 */
export function fetchContent(id) {
  return apiFetch(`/api/content/${id}`);
}

/**
 * Verify a completed Stars payment and retrieve the full content URL.
 * @param {number} contentId
 * @param {number} starsPaid
 * @returns {Promise<{file_url: string}>}
 */
export function verifyPurchase(contentId, starsPaid) {
  return apiFetch("/api/purchase/verify", {
    method: "POST",
    body: JSON.stringify({ content_id: contentId, stars_paid: starsPaid }),
  });
}

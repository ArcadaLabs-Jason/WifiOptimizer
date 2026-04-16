// Format a Unix-seconds timestamp as a relative-time string for display in
// the panel header ("just now", "5 min ago", "2 hr ago", "3d ago"). Returns
// "never" for the sentinel value 0 (settings not yet applied).
export function timeAgo(ts: number): string {
  if (!ts) return "never";
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

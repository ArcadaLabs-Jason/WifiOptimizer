// Format a Unix-seconds timestamp as a relative-time string for display in
// the panel ("just now", "5 min ago", "2 hr ago", "1 day ago", "3 days ago").
// Returns "never" for the sentinel value 0 (settings not yet applied).
export function timeAgo(ts: number): string {
  if (!ts) return "never";
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  const days = Math.floor(diff / 86400);
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

// A backend call that never answers used to leave every control greyed out
// until the panel was reloaded. The slowest changes take about a minute.
export const CALL_TIMEOUT_MS = 120 * 1000;

export class NoAnswer extends Error {}

// The call's own result, or NoAnswer once ms have passed without one.
export function withTimeout<T>(call: Promise<T>, ms: number = CALL_TIMEOUT_MS): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const id = setTimeout(() => reject(new NoAnswer()), ms);
    call.then(
      (value) => {
        clearTimeout(id);
        resolve(value);
      },
      (err) => {
        clearTimeout(id);
        reject(err);
      },
    );
  });
}

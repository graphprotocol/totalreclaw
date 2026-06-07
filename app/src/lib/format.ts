/** "1 fact" / "2 facts" — irregular plural via the 3rd arg. */
export function count(n: number, singular: string, plural = `${singular}s`): string {
  return `${n} ${n === 1 ? singular : plural}`;
}

/** Human relative date for session headers ("Today", "3 days ago", "Mar 4, 2026"). */
export function relativeDate(d: Date): string {
  const now = new Date();
  const ms = now.getTime() - d.getTime();
  const day = 86_400_000;
  const sameDay =
    now.getFullYear() === d.getFullYear() &&
    now.getMonth() === d.getMonth() &&
    now.getDate() === d.getDate();
  if (sameDay) return "Today";
  const days = Math.floor(ms / day);
  if (days <= 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  if (days < 30) {
    const w = Math.floor(days / 7);
    return `${w} week${w > 1 ? "s" : ""} ago`;
  }
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

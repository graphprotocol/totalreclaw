/** "1 fact" / "2 facts" / "1 entity" / "2 entities" — irregular plural via the 3rd arg. */
export function count(n: number, singular: string, plural = `${singular}s`): string {
  return `${n} ${n === 1 ? singular : plural}`;
}

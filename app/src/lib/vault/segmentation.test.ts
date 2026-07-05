import { describe, it, expect } from "vitest";
import {
  segmentByTimeGap,
  groupDurationMs,
  isCollapsedRun,
  DEFAULT_SESSION_GAP_MS,
  Timestamped,
} from "./segmentation";

const MIN = 60 * 1000;

interface Item extends Timestamped {
  id: string;
  session?: string;
}

/** Build an item at `minutes` past a fixed epoch anchor. */
function at(minutes: number, id: string, session?: string): Item {
  const base = Date.UTC(2026, 0, 1, 0, 0, 0); // 2026-01-01T00:00:00Z
  return { id, session, createdAt: new Date(base + minutes * MIN) };
}

describe("segmentByTimeGap — basics", () => {
  it("returns [] for an empty list", () => {
    expect(segmentByTimeGap([])).toEqual([]);
  });

  it("groups a single item into one group", () => {
    const groups = segmentByTimeGap([at(0, "a")]);
    expect(groups).toHaveLength(1);
    expect(groups[0]!.items.map((i) => i.id)).toEqual(["a"]);
    expect(groups[0]!.sessionId).toBeNull();
  });

  it("keeps items within the gap in one group", () => {
    // three items 10 min apart, gap 40 min → all one conversation
    const groups = segmentByTimeGap([at(0, "a"), at(10, "b"), at(20, "c")]);
    expect(groups).toHaveLength(1);
    expect(groups[0]!.items.map((i) => i.id)).toEqual(["a", "b", "c"]);
  });

  it("splits into separate groups across a large gap", () => {
    // morning cluster, then a 3h gap, then afternoon cluster
    const groups = segmentByTimeGap([
      at(0, "m1"),
      at(15, "m2"),
      at(195, "a1"), // +180 min after m2 → new group
      at(205, "a2"),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups[0]!.items.map((i) => i.id)).toEqual(["m1", "m2"]);
    expect(groups[1]!.items.map((i) => i.id)).toEqual(["a1", "a2"]);
  });

  it("splits the dominant collapse case: three conversations in one stream", () => {
    // investing (morning) → local-LLMs (afternoon) → self-hosting (evening)
    const groups = segmentByTimeGap([
      at(0, "inv1"),
      at(5, "inv2"),
      at(300, "llm1"), // +5h
      at(308, "llm2"),
      at(600, "host1"), // +~5h
      at(602, "host2"),
      at(605, "host3"),
    ]);
    expect(groups).toHaveLength(3);
    expect(groups.map((g) => g.items.length)).toEqual([2, 2, 3]);
  });
});

describe("segmentByTimeGap — ordering", () => {
  it("sorts unsorted input by createdAt within a group", () => {
    const groups = segmentByTimeGap([at(20, "c"), at(0, "a"), at(10, "b")]);
    expect(groups).toHaveLength(1);
    expect(groups[0]!.items.map((i) => i.id)).toEqual(["a", "b", "c"]);
  });

  it("emits groups ordered by start time ascending", () => {
    const groups = segmentByTimeGap([
      at(600, "late"),
      at(0, "early"),
      at(300, "mid"),
    ]);
    // Each item is >40min from the others → three groups, oldest first
    expect(groups).toHaveLength(3);
    expect(groups.map((g) => g.items[0]!.id)).toEqual(["early", "mid", "late"]);
  });

  it("boundary: a gap exactly equal to the threshold stays in one group", () => {
    // delta === gapMs is NOT > gapMs → same group
    const groups = segmentByTimeGap([at(0, "a"), at(40, "b")], {
      gapMs: 40 * MIN,
    });
    expect(groups).toHaveLength(1);
  });

  it("boundary: one ms over the threshold splits", () => {
    const gapMs = 40 * MIN;
    const base = Date.UTC(2026, 0, 1, 0, 0, 0);
    const a: Item = { id: "a", createdAt: new Date(base) };
    const b: Item = { id: "b", createdAt: new Date(base + gapMs + 1) };
    const groups = segmentByTimeGap([a, b], { gapMs });
    expect(groups).toHaveLength(2);
  });
});

describe("segmentByTimeGap — custom gap", () => {
  it("honours a custom gapMs", () => {
    // items 20 min apart; with a 15-min gap they split
    const groups = segmentByTimeGap([at(0, "a"), at(20, "b")], {
      gapMs: 15 * MIN,
    });
    expect(groups).toHaveLength(2);
  });

  it("uses DEFAULT_SESSION_GAP_MS when gapMs is omitted", () => {
    // 39 min apart < 40 min default → one group; 41 min → two
    expect(segmentByTimeGap([at(0, "a"), at(39, "b")])).toHaveLength(1);
    expect(segmentByTimeGap([at(0, "a"), at(41, "b")])).toHaveLength(2);
    expect(DEFAULT_SESSION_GAP_MS).toBe(40 * MIN);
  });
});

describe("segmentByTimeGap — session-aware partitioning", () => {
  it("never merges items from two different raw sessions, even when interleaved in time", () => {
    // s1 and s2 are interleaved in time but must not mix
    const items = [
      at(0, "s1-a", "s1"),
      at(2, "s2-a", "s2"),
      at(4, "s1-b", "s1"),
      at(6, "s2-b", "s2"),
    ];
    const groups = segmentByTimeGap(items, {
      sessionKeyOf: (i) => i.session,
    });
    // Each session is contiguous within the default gap → one group each.
    expect(groups).toHaveLength(2);
    const s1 = groups.find((g) => g.sessionId === "s1")!;
    const s2 = groups.find((g) => g.sessionId === "s2")!;
    expect(s1.items.map((i) => i.id)).toEqual(["s1-a", "s1-b"]);
    expect(s2.items.map((i) => i.id)).toEqual(["s2-a", "s2-b"]);
  });

  it("sub-splits a single collapsed session by time gap and preserves its id", () => {
    // One raw session with a morning and evening sitting.
    const items = [
      at(0, "a", "collapsed"),
      at(10, "b", "collapsed"),
      at(500, "c", "collapsed"), // big gap
      at(505, "d", "collapsed"),
    ];
    const groups = segmentByTimeGap(items, {
      sessionKeyOf: (i) => i.session,
    });
    expect(groups).toHaveLength(2);
    // Original session id preserved on both sub-groups.
    expect(groups.every((g) => g.sessionId === "collapsed")).toBe(true);
    // Keys are distinct (start timestamp disambiguates).
    expect(groups[0]!.key).not.toBe(groups[1]!.key);
    expect(groups[0]!.items.map((i) => i.id)).toEqual(["a", "b"]);
    expect(groups[1]!.items.map((i) => i.id)).toEqual(["c", "d"]);
  });

  it("treats null/undefined session ids as one anonymous bucket", () => {
    const items = [
      at(0, "a", undefined),
      at(5, "b", undefined),
    ];
    const groups = segmentByTimeGap(items, {
      sessionKeyOf: (i) => i.session,
    });
    expect(groups).toHaveLength(1);
    expect(groups[0]!.sessionId).toBeNull();
  });
});

describe("segmentByTimeGap — group metadata", () => {
  it("reports start and end timestamps", () => {
    const groups = segmentByTimeGap([at(0, "a"), at(10, "b"), at(30, "c")]);
    expect(groups).toHaveLength(1);
    const g = groups[0]!;
    expect(g.start.getTime()).toBe(at(0, "a").createdAt.getTime());
    expect(g.end.getTime()).toBe(at(30, "c").createdAt.getTime());
    expect(groupDurationMs(g)).toBe(30 * MIN);
  });

  it("group key is stable for the same input", () => {
    const input = [at(0, "a"), at(5, "b")];
    const k1 = segmentByTimeGap(input)[0]!.key;
    const k2 = segmentByTimeGap(input)[0]!.key;
    expect(k1).toBe(k2);
  });

  it("keys carry the session prefix when session-aware", () => {
    const groups = segmentByTimeGap([at(0, "a", "sess-x")], {
      sessionKeyOf: (i) => i.session,
    });
    expect(groups[0]!.key.startsWith("sess-x::")).toBe(true);
  });
});

describe("segmentByTimeGap — invalid timestamps", () => {
  it("pushes NaN-timestamped items into a trailing group without corrupting good runs", () => {
    const bad: Item = { id: "bad", createdAt: new Date(NaN) };
    const groups = segmentByTimeGap([at(0, "a"), at(10, "b"), bad]);
    // Good run stays intact; bad item is isolated.
    const good = groups.find((g) => g.items.some((i) => i.id === "a"))!;
    expect(good.items.map((i) => i.id)).toEqual(["a", "b"]);
    const badGroup = groups.find((g) => g.items.some((i) => i.id === "bad"))!;
    expect(badGroup.items.map((i) => i.id)).toEqual(["bad"]);
  });
});

describe("isCollapsedRun", () => {
  it("flags a run with too many items", () => {
    expect(isCollapsedRun(31, 0)).toBe(true);
    expect(isCollapsedRun(30, 0)).toBe(false);
  });

  it("flags a run spanning too long", () => {
    const sevenHours = 7 * 60 * MIN;
    expect(isCollapsedRun(2, sevenHours)).toBe(true);
    const fiveHours = 5 * 60 * MIN;
    expect(isCollapsedRun(2, fiveHours)).toBe(false);
  });

  it("respects custom thresholds", () => {
    expect(isCollapsedRun(5, 0, { maxItems: 4 })).toBe(true);
    expect(isCollapsedRun(2, 2 * 60 * MIN, { maxSpanMs: 60 * MIN })).toBe(true);
  });
});

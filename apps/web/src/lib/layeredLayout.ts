// Layered layout for a directed graph: each node goes in a column by the length of the longest
// dependency chain below it, so arrows read left to right from dependents to what they depend on.
// Cycles are broken by ignoring edges that close a loop. Order within a column follows the
// average position of each node's neighbours, which keeps most edges short and uncrossed.

export interface LayoutEdge {
  source: string;
  target: string;
}

export interface Placement {
  column: number;
  row: number;
}

export interface Layout {
  placements: Map<string, Placement>;
  columns: string[][];
}

const ORDERING_SWEEPS = 6;

export function layeredLayout(nodeIds: string[], edges: LayoutEdge[]): Layout {
  const known = new Set(nodeIds);
  const outgoing = new Map<string, string[]>(nodeIds.map((id) => [id, []]));
  const neighbours = new Map<string, string[]>(nodeIds.map((id) => [id, []]));
  for (const { source, target } of edges) {
    if (source === target || !known.has(source) || !known.has(target)) continue;
    outgoing.get(source)!.push(target);
    neighbours.get(source)!.push(target);
    neighbours.get(target)!.push(source);
  }

  // Longest path to a node with no dependencies, ignoring edges back onto the DFS stack.
  const rank = new Map<string, number>();
  const onStack = new Set<string>();
  const visit = (id: string): number => {
    const done = rank.get(id);
    if (done !== undefined) return done;
    onStack.add(id);
    let best = 0;
    for (const next of outgoing.get(id)!) {
      if (onStack.has(next)) continue;
      best = Math.max(best, visit(next) + 1);
    }
    onStack.delete(id);
    rank.set(id, best);
    return best;
  };
  const sorted = [...nodeIds].sort();
  sorted.forEach(visit);

  const maxRank = Math.max(0, ...rank.values());
  const columns: string[][] = Array.from({ length: maxRank + 1 }, () => []);
  for (const id of sorted) columns[maxRank - rank.get(id)!].push(id);

  const position = new Map<string, number>();
  const remember = () =>
    columns.forEach((column) =>
      column.forEach((id, row) => position.set(id, (row + 0.5) / column.length)),
    );
  remember();
  for (let sweep = 0; sweep < ORDERING_SWEEPS; sweep += 1) {
    for (const column of columns) {
      const weight = new Map<string, number>();
      for (const id of column) {
        const around = neighbours.get(id)!;
        weight.set(
          id,
          around.length
            ? around.reduce((sum, other) => sum + position.get(other)!, 0) / around.length
            : position.get(id)!,
        );
      }
      column.sort((a, b) => weight.get(a)! - weight.get(b)! || a.localeCompare(b));
      column.forEach((id, row) => position.set(id, (row + 0.5) / column.length));
    }
  }
  remember();

  const placements = new Map<string, Placement>();
  columns.forEach((column, index) =>
    column.forEach((id, row) => placements.set(id, { column: index, row })),
  );
  return { placements, columns };
}

import type {
  Graph,
  GraphNode,
  NodeStyle,
  NodeStyles,
  RawGraph,
  TypeGroup,
  TypeSubgroup,
} from './types'

// Adapt a Neo4j/APOC graph export into the internal model used by the renderer.
export function adaptNeo4j(data: RawGraph): Graph {
  const nodes = (data.nodes ?? []).map((n) => {
    const props = n.properties ?? {}
    const { name, ...rest } = props as { name?: unknown } & Record<string, unknown>
    const labels = n.labels ?? []
    return {
      id: n.id,
      type: labels[0] ?? '',
      sub: labels[1] ?? null,
      label: (name as string) || n.id,
      props: rest,
    }
  })
  const links = (data.relationships ?? []).map((r) => ({
    source: r.startNode,
    target: r.endNode,
    rel: r.type,
  }))
  return { nodes, links }
}

// Deterministic hue (0–359) from a string, so a given type always maps to the
// same colour across reloads without needing a hand-authored entry.
function hashHue(str: string): number {
  let h = 0
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) | 0
  return Math.abs(h) % 360
}

// PascalCase/camelCase/snake/kebab -> spaced upper-case label,
// e.g. "FlightPhase" -> "FLIGHT PHASE".
export function humanizeType(type: string): string {
  if (!type) return 'UNKNOWN'
  return type
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .toUpperCase()
}

// Resolve a node type to a complete style. Hand-authored entries win; anything
// missing falls back to procedurally generated styling so unknown types still
// render with a stable, distinct colour. A cache keeps a given type identical.
export function createStyleResolver(styles: NodeStyles) {
  const cache = new Map<string, NodeStyle>()
  return function styleFor(type: string): NodeStyle {
    const key = type || ''
    const cached = cache.get(key)
    if (cached) return cached

    const hue = hashHue(key)
    const generated: NodeStyle = {
      color: `hsl(${hue}, 65%, 60%)`,
      dimColor: `hsl(${hue}, 55%, 18%)`,
      label: humanizeType(key),
      size: 8,
    }
    const style = { ...generated, ...(styles[type] ?? {}) }
    cache.set(key, style)
    return style
  }
}

export type StyleResolver = ReturnType<typeof createStyleResolver>

// Node types present in the data: configured types first (for a stable legend
// order) then any unconfigured extras, alphabetically.
export function presentTypes(graph: Graph, styles: NodeStyles): string[] {
  const present = new Set(
    graph.nodes.map((n) => n.type).filter((t): t is string => t != null && t !== ''),
  )
  const configured = Object.keys(styles).filter((t) => present.has(t))
  const extras = [...present].filter((t) => !(t in styles)).sort()
  return [...configured, ...extras]
}

// Stable key identifying the filter leaf a node belongs to: the `type::sub`
// pair when the node has a subtype, otherwise the bare top-level type. This is
// the granularity the renderer and sidebar filter on.
export function nodeFilterKey(n: Pick<GraphNode, 'type' | 'sub'>): string {
  return n.sub ? `${n.type}::${n.sub}` : n.type
}

// Build the two-level filter tree: each present top-level type, with the
// subtypes found beneath it nested as expandable leaves. Types whose nodes have
// no subtype are leaves themselves (no children). Top-level order follows
// presentTypes; subgroups are sorted by label.
export function buildTypeTree(graph: Graph, styles: NodeStyles): TypeGroup[] {
  const styleFor = createStyleResolver(styles)
  const subCounts = new Map<string, Map<string | null, number>>()
  for (const n of graph.nodes) {
    if (!n.type) continue
    if (!subCounts.has(n.type)) subCounts.set(n.type, new Map())
    const subs = subCounts.get(n.type)!
    subs.set(n.sub, (subs.get(n.sub) ?? 0) + 1)
  }

  return presentTypes(graph, styles).map((type) => {
    const subs = subCounts.get(type) ?? new Map<string | null, number>()
    let count = 0
    for (const c of subs.values()) count += c
    const subgroups: TypeSubgroup[] = [...subs.entries()]
      .filter((e): e is [string, number] => e[0] != null && e[0] !== '')
      .map(([sub, c]) => ({ key: `${type}::${sub}`, label: humanizeType(sub), count: c }))
      .sort((a, b) => a.label.localeCompare(b.label))
    return { type, key: type, label: styleFor(type).label, count, subgroups }
  })
}

// Internal model used by the renderer, plus the raw Neo4j/APOC shapes we adapt
// from. Styling is intentionally kept out of the graph JSON so that file stays a
// pure Neo4j export artifact.

export interface NodeStyle {
  color: string
  dimColor: string
  label: string
  size: number
}

export type NodeStyles = Record<string, NodeStyle>

// Graph layout strategies offered by the renderer.
export type LayoutMode = 'force' | 'radial'

// ── Raw Neo4j / APOC export shapes ──────────────────────────────────────────
export interface RawNode {
  id: string
  labels?: string[]
  properties?: Record<string, unknown>
}

export interface RawRelationship {
  id?: string
  type: string
  startNode: string
  endNode: string
  properties?: Record<string, unknown>
}

export interface RawGraph {
  nodes?: RawNode[]
  relationships?: RawRelationship[]
}

// ── Internal model ──────────────────────────────────────────────────────────
export interface GraphNode {
  id: string
  type: string
  sub: string | null
  label: string
  props: Record<string, unknown>
}

export interface GraphLink {
  source: string
  target: string
  rel: string
}

export interface Graph {
  nodes: GraphNode[]
  links: GraphLink[]
}

// ── Sidebar filter tree ──────────────────────────────────────────────────────
// A top-level type and the subtypes present beneath it. Leaves are addressed by
// a stable filter key (see nodeFilterKey): the subtype key for nodes that have a
// second label, or the bare type for nodes that don't.
export interface TypeSubgroup {
  key: string
  label: string
  count: number
}

export interface TypeGroup {
  type: string
  key: string
  label: string
  count: number
  subgroups: TypeSubgroup[]
}

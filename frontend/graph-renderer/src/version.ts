import type { Graph, GraphNode } from './types'

export type GraphViewMode = { kind: 'current' } | { kind: 'as-of'; date: string }

export interface VersionInfo {
  logicalId: string
  version: number
  validFrom: string
  validTo: string | null
  current: boolean
}

export function getVersionInfo(node: GraphNode): VersionInfo | null {
  const { logicalId, version, validFrom, validTo, current } = node.props
  if (typeof current !== 'boolean') return null
  if (typeof logicalId !== 'string') return null
  if (typeof version !== 'number') return null
  if (typeof validFrom !== 'string') return null
  if (validTo !== null && validTo !== undefined && typeof validTo !== 'string') return null

  return {
    logicalId,
    version,
    validFrom,
    validTo: validTo ?? null,
    current,
  }
}

export function isVersionedNode(node: GraphNode): boolean {
  return typeof node.props.current === 'boolean'
}

// Event-dated nodes (e.g. Flights) are not *versioned* — they are immutable events that
// happened on a date. They carry an ISO `date` but no version properties. Under an as-of
// snapshot they are included only if they had already occurred, so the view reflects "the
// graph as it existed on that date" rather than treating the event as a version.
export function isEventDatedNode(node: GraphNode): boolean {
  return !isVersionedNode(node) && typeof node.props.date === 'string'
}

function isNodeVisible(node: GraphNode, mode: GraphViewMode): boolean {
  if (isEventDatedNode(node)) {
    if (mode.kind === 'current') return true
    return (node.props.date as string) <= mode.date
  }
  if (!isVersionedNode(node)) return true
  if (mode.kind === 'current') return node.props.current === true

  const version = getVersionInfo(node)
  if (!version) return false
  return version.validFrom <= mode.date && (version.validTo === null || mode.date < version.validTo)
}

// The graph is rooted at the aircraft: the operational subgraph (flights, flight phases,
// aerodromes, runways, ATC, crew) only connects back to the aircraft *through* flights. Once
// an as-of snapshot hides the flights that haven't occurred yet, the aerodrome/runway/ATC
// islands those flights linked in detach from the aircraft but would otherwise linger as
// floating, disconnected nodes (and partial details of flights that haven't happened). After
// the temporal filter we therefore keep only the component still reachable from the aircraft.
// In the current view everything is connected, so this is a no-op.
const ROOT_TYPE = 'Aircraft'

function pruneToRootComponent(graph: Graph): Graph {
  const roots = graph.nodes.filter((node) => node.type === ROOT_TYPE).map((node) => node.id)
  if (roots.length === 0) return graph

  const adjacency = new Map<string, string[]>()
  const connect = (from: string, to: string): void => {
    const neighbours = adjacency.get(from)
    if (neighbours) neighbours.push(to)
    else adjacency.set(from, [to])
  }
  for (const link of graph.links) {
    connect(link.source, link.target)
    connect(link.target, link.source)
  }

  const reachable = new Set<string>(roots)
  const queue = [...roots]
  while (queue.length > 0) {
    const current = queue.pop()!
    for (const neighbour of adjacency.get(current) ?? []) {
      if (!reachable.has(neighbour)) {
        reachable.add(neighbour)
        queue.push(neighbour)
      }
    }
  }

  const nodes = graph.nodes.filter((node) => reachable.has(node.id))
  const links = graph.links.filter(
    (link) => reachable.has(link.source) && reachable.has(link.target),
  )
  return { nodes, links }
}

export function filterGraphByVersion(graph: Graph, mode: GraphViewMode): Graph {
  const nodes = graph.nodes.filter((node) => isNodeVisible(node, mode))
  const visibleIds = new Set(nodes.map((node) => node.id))
  const links = graph.links.filter(
    (link) => visibleIds.has(link.source) && visibleIds.has(link.target),
  )
  return pruneToRootComponent({ nodes, links })
}

export function versionHistory(graph: Graph, logicalId: string): GraphNode[] {
  return graph.nodes
    .filter((node) => getVersionInfo(node)?.logicalId === logicalId)
    .sort((a, b) => (getVersionInfo(b)?.version ?? 0) - (getVersionInfo(a)?.version ?? 0))
}

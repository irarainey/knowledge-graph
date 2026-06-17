<script setup lang="ts">
import { ref } from 'vue'
import type { TypeGroup } from '../types'
import type { StyleResolver } from '../graph'

interface DomainSection {
  key: string
  label: string
  color: string
  leafKeys: string[]
  groups: TypeGroup[]
}

const props = defineProps<{
  sections: DomainSection[]
  activeKeys: Set<string>
  highlightKeys: Set<string>
  styleFor: StyleResolver
}>()

defineEmits<{
  toggle: [key: string]
  'toggle-group': [type: string]
  'toggle-domain': [domain: string]
  'select-all': []
  'deselect-all': []
}>()

// Which top-level groups are currently expanded. Collapsed by default to keep
// the list compact; only groups that actually have subtypes can expand.
const expanded = ref<Set<string>>(new Set())

function toggleExpand(type: string): void {
  const next = new Set(expanded.value)
  if (next.has(type)) next.delete(type)
  else next.add(type)
  expanded.value = next
}

// The leaf keys that make up a group: its subtypes, or the bare type when it has
// none. Used to derive the group's on/off/mixed state from activeKeys.
function leafKeys(group: TypeGroup): string[] {
  return group.subgroups.length ? group.subgroups.map((s) => s.key) : [group.key]
}

function groupState(group: TypeGroup): 'on' | 'off' | 'mixed' {
  const keys = leafKeys(group)
  const on = keys.filter((k) => props.activeKeys.has(k)).length
  if (on === 0) return 'off'
  if (on === keys.length) return 'on'
  return 'mixed'
}

function domainState(section: DomainSection): 'on' | 'off' | 'mixed' {
  const on = section.leafKeys.filter((k) => props.activeKeys.has(k)).length
  if (on === 0) return 'off'
  if (on === section.leafKeys.length) return 'on'
  return 'mixed'
}

// Whether a leaf/group/domain takes part in the current selection's focus, used
// to draw an accent so the sidebar reflects what the selected node connects to.
function groupHighlighted(group: TypeGroup): boolean {
  return leafKeys(group).some((k) => props.highlightKeys.has(k))
}

function domainHighlighted(section: DomainSection): boolean {
  return section.leafKeys.some((k) => props.highlightKeys.has(k))
}
</script>

<template>
  <div id="sidebar">
    <div class="bulk-actions">
      <button class="bulk-btn" @click="$emit('select-all')">Select all</button>
      <button class="bulk-btn" @click="$emit('deselect-all')">Deselect all</button>
    </div>

    <div v-for="section in sections" :key="section.key" class="section">
      <button
        class="domain-heading"
        :class="[domainState(section), { highlight: domainHighlighted(section) }]"
        @click="$emit('toggle-domain', section.key)"
      >
        <span class="dot" :style="{ '--dot': section.color }"></span>
        <span class="label">{{ section.label }}</span>
      </button>

      <div v-for="group in section.groups" :key="group.type" class="group">
        <div class="group-row" :class="groupState(group)">
          <button
            class="expand"
            :class="{ open: expanded.has(group.type) }"
            :disabled="group.subgroups.length === 0"
            :aria-label="expanded.has(group.type) ? 'Collapse' : 'Expand'"
            @click="toggleExpand(group.type)"
          >
            <svg
              v-if="group.subgroups.length"
              viewBox="0 0 16 16"
              width="12"
              height="12"
              aria-hidden="true"
            >
              <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" />
            </svg>
          </button>

          <button
            class="filter-btn"
            :class="{ highlight: groupHighlighted(group) }"
            @click="$emit('toggle-group', group.type)"
          >
            <span class="dot" :style="{ '--dot': styleFor(group.type).color }"></span>
            <span class="label">{{ group.label }}</span>
            <span class="count">{{ group.count }}</span>
          </button>
        </div>

        <div v-if="expanded.has(group.type) && group.subgroups.length" class="subgroups">
          <button
            v-for="sub in group.subgroups"
            :key="sub.key"
            class="filter-btn sub"
            :class="{ active: activeKeys.has(sub.key), highlight: highlightKeys.has(sub.key) }"
            @click="$emit('toggle', sub.key)"
          >
            <span class="dot" :style="{ '--dot': styleFor(group.type).color }"></span>
            <span class="label">{{ sub.label }}</span>
            <span class="count">{{ sub.count }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
#sidebar {
  position: fixed;
  left: 0;
  top: 56px;
  bottom: 0;
  width: 280px;
  z-index: 50;
  background: var(--panel);
  border-right: 1px solid var(--border);
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
}

.section {
  display: flex;
  flex-direction: column;
}
.section + .section {
  margin-top: 12px;
}

/* The domain heading doubles as the whole-domain toggle: a prominent section
   label whose dot reflects the domain's on/off/mixed state. */
.domain-heading {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--border);
  color: var(--text-dim);
  font-family: inherit;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 6px 6px 8px;
  margin-bottom: 6px;
  cursor: pointer;
  transition: color 0.15s;
}
.domain-heading:hover {
  color: var(--text);
}
.domain-heading .dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
  background: transparent;
  box-shadow: inset 0 0 0 1.5px var(--text-dim);
}
.domain-heading.on,
.domain-heading.mixed {
  color: var(--text);
}
.domain-heading.on .dot {
  background: var(--dot);
  box-shadow: none;
}
.domain-heading.mixed .dot {
  background: linear-gradient(90deg, var(--dot) 0 50%, transparent 50% 100%);
  box-shadow: inset 0 0 0 1.5px var(--dot);
}

.bulk-actions {
  display: flex;
  gap: 6px;
  margin: 0 6px 10px;
}
.bulk-btn {
  flex: 1;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-dim);
  font-family: inherit;
  font-size: 12px;
  padding: 6px 8px;
  cursor: pointer;
  transition: all 0.15s;
}
.bulk-btn:hover {
  background: var(--panel-2);
  color: var(--text);
}

.group {
  display: flex;
  flex-direction: column;
}

.group-row {
  display: flex;
  align-items: center;
}

.expand {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 32px;
  flex-shrink: 0;
  background: transparent;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  padding: 0;
}
.expand:disabled {
  cursor: default;
  visibility: hidden;
}
.expand svg {
  transition: transform 0.15s;
}
.expand.open svg {
  transform: rotate(90deg);
}

.filter-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--text-dim);
  font-family: inherit;
  font-size: 13px;
  padding: 8px 10px;
  cursor: pointer;
  transition: all 0.15s;
  flex: 1;
  min-width: 0;
  text-align: left;
}
.filter-btn .dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
  /* Hidden state: drain the colour to a hollow ring so the row reads as "off". */
  background: transparent;
  box-shadow: inset 0 0 0 1.5px var(--text-dim);
  opacity: 0.7;
}
.filter-btn .label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.filter-btn:hover {
  background: rgba(255, 255, 255, 0.04);
  color: var(--text);
}
.filter-btn .count {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-dim);
}

/* Shown state: full-colour dot, bright text and a clear filled row. The group
   row is driven by its on/off/mixed state, leaf rows by their own active flag. */
.group-row.on .filter-btn,
.group-row.mixed .filter-btn,
.filter-btn.sub.active {
  background: var(--panel-2);
  border-color: var(--border);
  color: var(--text);
}
.group-row.on .filter-btn .dot,
.filter-btn.sub.active .dot {
  background: var(--dot);
  box-shadow: none;
  opacity: 1;
}
.group-row.on .filter-btn .count,
.filter-btn.sub.active .count {
  color: var(--text);
}
/* Mixed: some subtypes on. Show a half-filled dot so it's distinct from on/off. */
.group-row.mixed .filter-btn .dot {
  background: linear-gradient(90deg, var(--dot) 0 50%, transparent 50% 100%);
  box-shadow: inset 0 0 0 1.5px var(--dot);
  opacity: 1;
}

.subgroups {
  display: flex;
  flex-direction: column;
  margin-left: 20px;
  padding-left: 6px;
  border-left: 1px solid var(--border);
}
.filter-btn.sub {
  font-size: 12.5px;
  padding: 6px 10px;
}

/* Selection highlight: an amber accent marking the types that take part in the
   currently selected node's focus lineage. Deliberately distinct from the
   on/off "active" state so the two readings don't collide. */
.filter-btn.highlight {
  box-shadow: inset 2px 0 0 var(--accent);
  background: var(--accent-weak);
}
.filter-btn.highlight .label {
  color: var(--accent);
}
.domain-heading.highlight {
  color: var(--accent);
}
</style>

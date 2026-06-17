import type { ProjectionBasisEntry } from '../types'

export interface NodeInspectorFact {
  label: string
  value: string
}

export interface NodeInspectorSurfaceModel {
  title: string
  role: string
  state: string
  token: string
  caption: string
  readingLine: string
  localEvidence: NodeInspectorFact[]
  transitionNote: string
  nextDependency: string
  basisMarkers: Array<{
    label: string
    value: string
    ref: string
  }>
}

function compactWhitespace(value: string) {
  return value.replace(/\s+/g, ' ').trim()
}

function sentenceFragments(value: string) {
  return compactWhitespace(value)
    .split(/[.;]/)
    .map((item) => compactWhitespace(item))
    .filter(Boolean)
}

function compactLead(value: string) {
  const fragments = sentenceFragments(value)
  if (fragments.length === 0) {
    return ''
  }

  return fragments[0]
}

export function buildNodeInspectorSurfaceModel(input: {
  title: string
  role: string
  state: string
  token: string
  caption: string
  facts: NodeInspectorFact[]
  detail: string
  transition: string
  nextTitle?: string
  sources: ProjectionBasisEntry[]
}): NodeInspectorSurfaceModel {
  const { title, role, state, token, caption, facts, detail, transition, nextTitle, sources } =
    input

  return {
    title: compactWhitespace(title),
    role: compactWhitespace(role),
    state: compactWhitespace(state),
    token: compactWhitespace(token),
    caption: compactLead(caption) || compactLead(detail),
    readingLine: compactLead(detail),
    localEvidence: facts.slice(0, 3).map((fact) => ({
      label: compactWhitespace(fact.label),
      value: compactWhitespace(fact.value),
    })),
    transitionNote: compactLead(transition),
    nextDependency: compactWhitespace(nextTitle || 'operator boundary'),
    basisMarkers: sources.slice(0, 4).map((source) => ({
      label: compactWhitespace(source.label),
      value: compactWhitespace(source.value),
      ref: `${source.section}.${source.field}`,
    })),
  }
}

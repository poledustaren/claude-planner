export type BoardItem = {
  id: string
  title: string
  tier: string
  status: string
  wave: number
}

export type BoardJob = {
  id: string
  item: string
  kind: string
  tier: string
  model: string
  status: string
  sec: number
  steps: number
  ctxK: number
  cost: number
  last: string
  isRunning: boolean
}

export type BoardPlan = {
  root: string
  project: string
  title: string
  status: string
  done: number
  total: number
  hold: string
  issue: string
  isActive: boolean
  items: BoardItem[]
  jobs: BoardJob[]
  next: string
}

export type Board = { plans: BoardPlan[] }

declare module 'claude-code' {
  interface PluginState {
    'plan-board': {
      board: Shaped<Board>
      roots: string[]
      isHidden: boolean
    }
  }
}

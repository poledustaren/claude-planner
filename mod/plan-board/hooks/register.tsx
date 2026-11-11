import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Board, BoardJob, BoardPlan } from '../types'

const PANE = 'plan-board'
const POLL_MS = 2000
const IDLE_EVERY = 15 // без активного плана опрашиваем раз в 30 с
const MARK: Record<string, string> = {
  done: '✔',
  todo: '·',
  in_progress: '▶',
  failed: '✘',
  skipped: '–',
}

const board = atom({ plugin: 'plan-board', key: 'board' } as const, { plans: [] }, { shape: 'board-1' })
const roots = atom({ plugin: 'plan-board', key: 'roots' } as const, [])
const isHidden = atom({ plugin: 'plan-board', key: 'isHidden' } as const, false)

const clip = (text: string, width: number): string =>
  text.length <= width ? text : text.slice(0, Math.max(1, width - 1)) + '…'

const headline = (plan: BoardPlan): string => {
  const running = plan.jobs.filter(job => job.isRunning).length
  const parts = [`${plan.project}: ${plan.done}/${plan.total}`, plan.status]
  if (running > 0) parts.push(`воркеров ${running}`)
  if (plan.hold) parts.push(`пауза: ${plan.hold}`)
  if (plan.issue) parts.push(plan.issue)

  return parts.join(' · ')
}

const jobLine = (job: BoardJob): string => {
  const what = job.item ? `п.${job.item}` : job.kind
  const cost = job.cost > 0 ? ` $${job.cost.toFixed(3)}` : ''

  return `${what} ${job.tier} ${job.status.toLowerCase()} ${job.sec}с шаг ${job.steps} ctx ${job.ctxK}k${cost}  ${job.last}`
}

export const register: Register = on => {
  let lastRaw = ''
  let isLive = false
  let isPolling = false

  on('session.start', async ($, e, next) => {
    if (!e.isInteractive) {
      return next(e)
    }

    const poll = async (): Promise<void> => {
      if (isPolling) return
      isPolling = true

      try {
        const dirs = await read($, roots)
        const ran = await $.process.run(
          ['/bin/sh', '-c', 'exec "$HOME/bin/cplan" json "$@"', 'cplan', ...dirs],
          { timeoutMs: 8000 },
        )

        if (ran.exitCode !== 0 || ran.stdout === lastRaw) return

        const fresh: Board = JSON.parse(ran.stdout)
        lastRaw = ran.stdout
        isLive = fresh.plans.some(plan => plan.isActive)
        await update($, board, () => fresh)
        const live = fresh.plans.filter(plan => plan.isActive)
        $.ui.status(live.length > 0 ? `план ${live.map(headline).join(' | ')}` : undefined)
      } catch {
        // cplan не установлен или вернул не JSON: панель просто молчит
      } finally {
        isPolling = false
      }
    }

    await $.command.register({
      name: 'plan-board',
      description: 'Панель прогресса плана и воркеров claude-planner (повторный вызов — скрыть или показать полосу)',
    })
    await update($, roots, list => (list.includes(e.cwd) ? list : [...list, e.cwd]))
    void poll()

    let tick = 0
    $.clock.every(POLL_MS, () => {
      tick += 1

      if (isLive || tick % IDLE_EVERY === 0) {
        void poll()
      }
    })

    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const input = e as unknown as Record<string, unknown>
    const dir = input.project_dir
    const isPlanner = e.tool.startsWith('mcp__planner__') && typeof dir === 'string'

    if (isPlanner) {
      await update($, roots, list => (list.includes(dir) ? list : [...list, dir]))
      isLive = true // опрос оживает сразу, не дожидаясь редкого такта
    }

    return next(e)
  })

  on('command.run', { command: 'plan-board' }, async $ => {
    const opened = await $.ui.open({ id: PANE, title: 'План' })

    if (opened.isPlaced) {
      await update($, isHidden, () => false)

      return { text: 'Панель плана открыта. Полоса над вводом показана.' }
    }

    const hidden = !(await read($, isHidden))
    await update($, isHidden, () => hidden)

    return { text: hidden ? 'Полоса плана скрыта.' : 'Полоса плана показана.' }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const state = await read($, board)
    const live = state.plans.filter(plan => plan.isActive)

    if (e.props.hasSurvey || live.length === 0 || (await read($, isHidden))) {
      return next(e)
    }

    const { Box, Text } = $.ui.resolve(e)
    const width = Math.max(20, e.props.bodyColumns)
    const room = Math.max(1, Math.min(6, e.props.maxRows - 1))
    const rows: { text: string; isHead: boolean; isBad: boolean }[] = []

    for (const plan of live) {
      rows.push({ text: `План ${headline(plan)} — ${plan.title}`, isHead: true, isBad: false })

      for (const job of plan.jobs.filter(one => one.isRunning)) {
        rows.push({ text: `  ${jobLine(job)}`, isHead: false, isBad: false })
      }

      for (const item of plan.items.filter(one => one.status === 'failed')) {
        rows.push({ text: `  ✘ п.${item.id} ${item.title}`, isHead: false, isBad: true })
      }
    }

    return (
      <Box flexDirection="column">
        {rows.slice(0, room).map(row => (
          <Text bold={row.isHead} dimColor={!row.isHead && !row.isBad} color={row.isBad ? 'error' : undefined}>
            {clip(row.text, width)}
          </Text>
        ))}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const state = await read($, board)
    const width = Math.max(20, e.props.bodyColumns)

    if (state.plans.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>Планов нет. План появится здесь после plan_init.</Text>
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {state.plans.map(plan => (
          <Box flexDirection="column">
            <Text bold>{clip(plan.title, width)}</Text>
            <Text dimColor>{clip(headline(plan), width)}</Text>
            {plan.items.map(item => (
              <Text
                dimColor={item.status === 'done' || item.status === 'skipped'}
                color={item.status === 'failed' ? 'error' : item.status === 'in_progress' ? 'suggestion' : undefined}
              >
                {clip(`${MARK[item.status] ?? '?'} ${item.id} [${item.tier}, в${item.wave}] ${item.title}`, width)}
              </Text>
            ))}
            {plan.jobs.length > 0 && <Text bold>Воркеры</Text>}
            {plan.jobs.map(job => (
              <Text dimColor={!job.isRunning}>{clip(jobLine(job), width)}</Text>
            ))}
            <Text dimColor>{clip(plan.next, width * 2)}</Text>
          </Box>
        ))}
      </Box>
    )
  })
}

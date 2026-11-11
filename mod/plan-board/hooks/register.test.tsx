import { expect, mock, test } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'

const PLAN = {
  root: '/work/shop',
  project: 'shop',
  title: 'Пагинация заказов',
  status: 'active',
  done: 1,
  total: 3,
  hold: '',
  issue: 'SHOP-7',
  isActive: true,
  items: [
    { id: '1', title: 'Модель', tier: 'code-low', status: 'done', wave: 1 },
    { id: '2', title: 'Обработчик', tier: 'code', status: 'in_progress', wave: 2 },
    { id: '3', title: 'Миграция', tier: 'main', status: 'failed', wave: 2 },
  ],
  jobs: [
    {
      id: 'i2-1',
      item: '2',
      kind: 'item',
      tier: 'code',
      model: 'sonnet',
      status: 'RUNNING',
      sec: 14,
      steps: 5,
      ctxK: 7,
      cost: 0,
      last: 'Edit src/api/orders.py',
      isRunning: true,
    },
  ],
  next: 'Дальше: работают воркеры (1): plan_run() дождётся ближайшего',
}

const BAND = {
  plugin: 'plan-board',
  component: 'AbovePrompt',
  requestId: 'band',
  props: {
    hasSurvey: false,
    isCollapsed: false,
    isWorking: false,
    maxRows: 8,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 7 },
    view: {},
  },
} as const

const PANE = {
  plugin: 'plan-board',
  component: 'Pane',
  requestId: 'plan-board',
  props: {
    title: 'План',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 20 },
    view: {},
  },
} as const

const world = (on: Parameters<TestBody>[1], plans: unknown[]) => {
  const ran: string[][] = []
  const statuses: (string | undefined)[] = []
  on('command.register', () => ({ value: { command: 'plan-board' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('tool.call', () => ({ result: {} }))
  on('ui.status', (_, e) => {
    statuses.push(e as unknown as string | undefined)

    return { value: undefined }
  })
  on('process.run', (_, e) => {
    ran.push([...(e as unknown as { argv: string[] }).argv])

    return {
      value: {
        exitCode: 0,
        stdout: JSON.stringify({ plans }),
        stderr: '',
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }
  })

  return { ran, statuses }
}

test('the band shows the plan, its running worker and the failed item', async ($, on) => {
  const clock = mock.clock(on)
  const { ran } = world(on, [PLAN])

  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  await clock.advance(0)
  expect(ran[0]?.slice(-1)).toEqual(['/work/shop'])

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    const head = await ui.find({ type: 'Text', text: /План shop/ })
    expect(head?.text).toBe('План shop: 1/3 · active · воркеров 1 · SHOP-7 — Пагинация заказов')
    const job = await ui.find({ type: 'Text', text: /п\.2 code/ })
    expect(job?.text).toContain('шаг 5 ctx 7k  Edit src/api/orders.py')
    expect((await ui.find({ type: 'Text', text: /Миграция/ }))?.text).toBe('  ✘ п.3 Миграция')
    await ui.unmount()
  }
})

test('a planner tool call adds its project to the polled folders', async ($, on) => {
  const clock = mock.clock(on)
  const { ran } = world(on, [PLAN])

  await $.session.start({ cwd: '/home/me', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'mcp__planner__plan_run', project_dir: '/work/shop' })
  await clock.advance(2000)
  expect(ran.at(-1)?.slice(-2)).toEqual(['/home/me', '/work/shop'])
})

test('no active plan: the band is left to the engine and nothing polls fast', async ($, on) => {
  const clock = mock.clock(on)
  const { ran } = world(on, [{ ...PLAN, status: 'finished', isActive: false, jobs: [] }])
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text>engine</Text>
  })

  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  await clock.advance(0)
  const polls = ran.length
  await clock.advance(10000)
  expect(ran.length).toBe(polls)
  await clock.advance(20000)
  expect(ran.length).toBe(polls + 1)

  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await ui.find({ type: 'Text' }))?.text).toBe('engine')
  await ui.unmount()
})

test('the pane lists every item with its mark, on every surface', async ($, on) => {
  const clock = mock.clock(on)
  world(on, [PLAN])

  await $.session.start({ cwd: '/work/shop', surface: 'desktop', isInteractive: true })
  await clock.advance(0)

  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /Модель/ }))?.text).toBe('✔ 1 [code-low, в1] Модель')
    expect((await ui.find({ type: 'Text', text: /Обработчик/ }))?.text).toBe('▶ 2 [code, в2] Обработчик')
    await ui.unmount()
  }
})

test('a headless session starts no polling', async ($, on) => {
  const clock = mock.clock(on)
  const { ran } = world(on, [PLAN])

  await $.session.start({ cwd: '/work/shop', surface: null, isInteractive: false })
  await clock.advance(60000)
  expect(ran.length).toBe(0)
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import { App } from './App'

const responses: Record<string, unknown> = {
  '/api/users?limit=100&offset=0': {
    items: [{ user_id: 1, segment: 'vip', city: 'HCMC' }],
    total: 1,
    limit: 100,
    offset: 0,
  },
  '/api/products?limit=24&offset=0': {
    items: [
      {
        product_id: 101,
        product_name: 'Demo Product',
        category_id: 3,
        category_code: 'cat-3',
        brand_id: 4,
        brand_name: 'Demo Brand',
        current_price: 19.99,
        price_bucket: 2,
      },
    ],
    total: 1,
    limit: 24,
    offset: 0,
  },
}

beforeEach(() => {
  window.localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url
      if (path === '/api/events') {
        return new Response(
          JSON.stringify({
            event_id: 'event-1',
            correlation_id: 'request-1',
            status: 'accepted',
            duplicate: false,
            event_timestamp: new Date().toISOString(),
          }),
          { status: 202, headers: { 'Content-Type': 'application/json' } },
        )
      }
      if (path === '/api/recommendations') {
        expect(init?.method).toBe('POST')
        return new Response(
          JSON.stringify({
            request_id: 'recommendation-1',
            user_id: 1,
            model_version: 'stable-001',
            ab_variant: 'control',
            items: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }
      if (path.includes('/status')) {
        return new Response(
          JSON.stringify({
            event_id: 'event-1',
            status: 'feature_store_updated',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        )
      }
      return new Response(JSON.stringify(responses[path]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('loads live catalog and requests recommendations for the selected user', async () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  )

  expect(await screen.findByText('Demo Product')).toBeInTheDocument()
  await userEvent.selectOptions(screen.getByLabelText('Active user'), '1')
  await userEvent.click(
    screen.getByRole('button', { name: 'Get recommendations' }),
  )
  expect(await screen.findByText('Recommended now')).toBeInTheDocument()
  expect(screen.getByText('MODEL stable-001')).toBeInTheDocument()
})

test('sends a cart event and updates cart state', async () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  )

  await screen.findByText('Demo Product')
  await userEvent.selectOptions(screen.getByLabelText('Active user'), '1')
  await userEvent.click(screen.getByRole('button', { name: 'Add to cart' }))
  expect(await screen.findByText('Cart 1')).toBeInTheDocument()
  expect(screen.getByText('CART · Demo Product')).toBeInTheDocument()
})

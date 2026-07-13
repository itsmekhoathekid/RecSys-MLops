import type { components } from './schema'

export type User = components['schemas']['User']
export type Product = components['schemas']['Product']
export type UserPage = components['schemas']['UserPage']
export type ProductPage = components['schemas']['ProductPage']
export type EventAccepted = components['schemas']['EventAccepted']
export type EventStatus = components['schemas']['EventStatus']
export type RecommendationResponse =
  components['schemas']['RecommendationResponse']
export type Action = components['schemas']['EventRequest']['action']

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: string
    } | null
    throw new Error(
      body?.detail ?? `Request failed with status ${response.status}`,
    )
  }
  return (await response.json()) as T
}

export const api = {
  users: () => request<UserPage>('/api/users?limit=100&offset=0'),
  products: (offset = 0) =>
    request<ProductPage>(`/api/products?limit=24&offset=${offset}`),
  event: (
    payload: {
      user_id: number
      product_id: number
      action: Action
      session_id: string
      request_id?: string
      impression_id?: string
      quantity?: number
    },
    idempotencyKey: string,
  ) =>
    request<EventAccepted>('/api/events', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(payload),
    }),
  eventStatus: (eventId: string) =>
    request<EventStatus>(`/api/events/${eventId}/status`),
  recommendations: (userId: number, sessionId: string, topK = 10) =>
    request<RecommendationResponse>('/api/recommendations', {
      method: 'POST',
      body: JSON.stringify({
        user_id: userId,
        session_id: sessionId,
        top_k: topK,
      }),
    }),
}

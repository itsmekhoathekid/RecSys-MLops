import { useMutation, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import {
  api,
  type Action,
  type Product,
  type RecommendationResponse,
} from './api/client'
import styles from './App.module.css'

type TimelineEvent = {
  id: string
  label: string
  status: 'accepted' | 'feature_store_updated' | 'waiting' | 'failed'
}

function sessionId(): string {
  const key = 'recsys-demo-session-id'
  const existing = window.localStorage.getItem(key)
  if (existing) return existing
  const created = `web-session-${crypto.randomUUID()}`
  window.localStorage.setItem(key, created)
  return created
}

export function App() {
  const [selectedUser, setSelectedUser] = useState<number | null>(null)
  const [cart, setCart] = useState<Record<number, number>>({})
  const [timeline, setTimeline] = useState<TimelineEvent[]>([])
  const [recommendations, setRecommendations] =
    useState<RecommendationResponse | null>(null)
  const currentSession = useMemo(sessionId, [])

  const users = useQuery({ queryKey: ['users'], queryFn: api.users })
  const products = useQuery({
    queryKey: ['products'],
    queryFn: () => api.products(0),
  })

  const recommendationMutation = useMutation({
    mutationFn: () => {
      if (selectedUser === null) throw new Error('Select a user first')
      return api.recommendations(selectedUser, currentSession)
    },
    onSuccess: setRecommendations,
  })

  const updateTimeline = (id: string, status: TimelineEvent['status']) => {
    setTimeline((current) =>
      current.map((event) => (event.id === id ? { ...event, status } : event)),
    )
  }

  const pollStatus = async (eventId: string) => {
    const deadline = Date.now() + 60_000
    while (Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 1_500))
      const result = await api.eventStatus(eventId)
      if (result.status === 'feature_store_updated') {
        updateTimeline(eventId, 'feature_store_updated')
        return
      }
    }
    updateTimeline(eventId, 'waiting')
  }

  const sendAction = async (
    product: Product,
    action: Action,
    correlation?: { requestId: string; impressionId: string },
  ) => {
    if (selectedUser === null) {
      setTimeline((current) => [
        {
          id: crypto.randomUUID(),
          label: 'Select a user before sending an event',
          status: 'failed',
        },
        ...current,
      ])
      return
    }
    const temporaryId = crypto.randomUUID()
    setTimeline((current) => [
      {
        id: temporaryId,
        label: `${action.toUpperCase()} · ${product.product_name}`,
        status: 'waiting',
      },
      ...current,
    ])
    try {
      const accepted = await api.event(
        {
          user_id: selectedUser,
          product_id: product.product_id,
          action,
          session_id: currentSession,
          request_id: correlation?.requestId,
          impression_id: correlation?.impressionId,
          quantity: action === 'purchase' ? (cart[product.product_id] ?? 1) : 1,
        },
        `web-${temporaryId}`,
      )
      setTimeline((current) =>
        current.map((event) =>
          event.id === temporaryId
            ? { ...event, id: accepted.event_id, status: 'accepted' }
            : event,
        ),
      )
      if (action === 'cart') {
        setCart((current) => ({
          ...current,
          [product.product_id]: (current[product.product_id] ?? 0) + 1,
        }))
      }
      if (action === 'purchase') {
        setCart((current) => {
          const next = { ...current }
          delete next[product.product_id]
          return next
        })
      }
      void pollStatus(accepted.event_id).catch(() =>
        updateTimeline(accepted.event_id, 'waiting'),
      )
    } catch {
      updateTimeline(temporaryId, 'failed')
    }
  }

  const cartCount = Object.values(cart).reduce(
    (sum, quantity) => sum + quantity,
    0,
  )

  return (
    <main className={styles.shell}>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>LIVE PERSONALIZATION LAB</p>
          <h1>RecSys Store</h1>
          <p className={styles.subtitle}>
            Every interaction becomes a realtime feature.
          </p>
        </div>
        <div className={styles.controls}>
          <label>
            Active user
            <select
              aria-label="Active user"
              value={selectedUser ?? ''}
              onChange={(event) => setSelectedUser(Number(event.target.value))}
            >
              <option value="" disabled>
                Choose a user
              </option>
              {users.data?.items.map((user) => (
                <option key={user.user_id} value={user.user_id}>
                  User {user.user_id} · {user.segment ?? 'active'}
                </option>
              ))}
            </select>
          </label>
          <button
            className={styles.recommendButton}
            disabled={selectedUser === null || recommendationMutation.isPending}
            onClick={() => recommendationMutation.mutate()}
          >
            {recommendationMutation.isPending
              ? 'Ranking…'
              : 'Get recommendations'}
          </button>
          <div className={styles.cart}>Cart {cartCount}</div>
        </div>
      </header>

      {recommendationMutation.error && (
        <p className={styles.error}>{recommendationMutation.error.message}</p>
      )}

      {recommendations && (
        <section
          className={styles.recommendations}
          aria-labelledby="recommendation-title"
        >
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>
                MODEL {recommendations.model_version}
              </p>
              <h2 id="recommendation-title">Recommended now</h2>
            </div>
            <span>{recommendations.ab_variant ?? 'default'} route</span>
          </div>
          <div className={styles.horizontalCards}>
            {recommendations.items.map((item, index) => (
              <article
                className={styles.recommendationCard}
                key={item.impression_id}
              >
                <span className={styles.rank}>#{index + 1}</span>
                <h3>
                  {item.product?.product_name ?? `Product ${item.item_id}`}
                </h3>
                <p>{item.product?.brand_name ?? 'Live candidate'}</p>
                <strong>
                  {item.product
                    ? `$${item.product.current_price.toFixed(2)}`
                    : item.score.toFixed(4)}
                </strong>
                {item.product && (
                  <button
                    onClick={() =>
                      void sendAction(item.product!, 'view', {
                        requestId: recommendations.request_id,
                        impressionId: item.impression_id,
                      })
                    }
                  >
                    View recommendation
                  </button>
                )}
              </article>
            ))}
          </div>
        </section>
      )}

      <div className={styles.contentGrid}>
        <section aria-labelledby="catalog-title">
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>LIVE POSTGRES CATALOG</p>
              <h2 id="catalog-title">Popular products</h2>
            </div>
            <span>{products.data?.total ?? 0} active</span>
          </div>
          {products.isLoading && <p>Loading live catalog…</p>}
          {products.error && (
            <p className={styles.error}>{products.error.message}</p>
          )}
          <div className={styles.productGrid}>
            {products.data?.items.map((product) => (
              <article className={styles.productCard} key={product.product_id}>
                <div className={styles.productVisual} aria-hidden="true">
                  <span>
                    {product.category_code ?? `CAT ${product.category_id}`}
                  </span>
                </div>
                <div className={styles.productBody}>
                  <p>{product.brand_name ?? `Brand ${product.brand_id}`}</p>
                  <h3>{product.product_name}</h3>
                  <strong>${product.current_price.toFixed(2)}</strong>
                  <div className={styles.actions}>
                    <button onClick={() => void sendAction(product, 'view')}>
                      View
                    </button>
                    <button onClick={() => void sendAction(product, 'cart')}>
                      Add to cart
                    </button>
                    <button
                      className={styles.purchase}
                      onClick={() => void sendAction(product, 'purchase')}
                    >
                      Purchase
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>

        <aside className={styles.timeline} aria-labelledby="timeline-title">
          <p className={styles.eyebrow}>POSTGRES → CDC → FLINK → FEAST</p>
          <h2 id="timeline-title">Event stream</h2>
          {timeline.length === 0 && (
            <p>Interact with a product to watch the stream.</p>
          )}
          <ol>
            {timeline.map((event) => (
              <li key={event.id}>
                <span
                  className={`${styles.statusDot} ${styles[event.status]}`}
                />
                <div>
                  <strong>{event.label}</strong>
                  <small>{event.status.replaceAll('_', ' ')}</small>
                </div>
              </li>
            ))}
          </ol>
        </aside>
      </div>
    </main>
  )
}

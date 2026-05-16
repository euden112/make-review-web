import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import Navbar from './Navbar'

const API_BASE = 'http://localhost:8000'

const CATEGORY_LABELS = {
  graphics: '그래픽',
  controls: '조작감',
  optimization: '최적화',
  content: '콘텐츠 양',
  price_value: '가성비',
}

const SENTIMENT_CONFIG = {
  positive: { label: '긍정적', cls: 'bg-green-50 text-green-600 dark:bg-green-900/20 dark:text-green-400' },
  negative: { label: '부정적', cls: 'bg-red-50 text-red-600 dark:bg-red-900/20 dark:text-red-400' },
  mixed:    { label: '중립',   cls: 'bg-gray-50 text-gray-600 dark:bg-gray-700 dark:text-gray-300' },
}

function SentimentBadge({ value }) {
  const cfg = SENTIMENT_CONFIG[value] || SENTIMENT_CONFIG.mixed
  return (
    <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${cfg.cls}`}>
      {cfg.label}
    </span>
  )
}

const BUCKET_COLORS = {
  early: { bar: '#6366f1', light: 'rgba(99,102,241,0.15)' },
  mid:   { bar: '#f59e0b', light: 'rgba(245,158,11,0.15)' },
  late:  { bar: '#10b981', light: 'rgba(16,185,129,0.15)' },
}

function PlaytimeBarChart({ buckets, isDark }) {
  const keys = ['early', 'mid', 'late']
  const available = keys.filter(k => buckets?.[k]?.data_available)
  if (available.length === 0) return null

  const W = 420, H = 160, PAD = { top: 12, right: 16, bottom: 40, left: 44 }
  const inner = { w: W - PAD.left - PAD.right, h: H - PAD.top - PAD.bottom }
  const barW = Math.floor(inner.w / keys.length * 0.45)
  const gap = inner.w / keys.length
  const stroke = isDark ? '#3a3a5e' : '#e5e7eb'
  const axisColor = isDark ? '#6b7280' : '#9ca3af'

  const gridLines = [0, 25, 50, 75, 100]

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-md" style={{ maxHeight: 160 }}>
      <rect x={0} y={0} width={W} height={H} fill="transparent" />
      {gridLines.map(v => {
        const y = PAD.top + inner.h - (v / 100) * inner.h
        return (
          <g key={v}>
            <line x1={PAD.left} y1={y} x2={PAD.left + inner.w} y2={y} stroke={stroke} strokeWidth={1} strokeDasharray={v === 0 ? '0' : '3,3'} />
            <text x={PAD.left - 6} y={y + 4} textAnchor="end" fontSize={9} fill={axisColor}>{v}%</text>
          </g>
        )
      })}

      {keys.map((k, i) => {
        const d = buckets?.[k]
        const score = d?.data_available ? (d.sentiment_score ?? 0) : 0
        const cx = PAD.left + gap * i + gap / 2
        const barH = (score / 100) * inner.h
        const barY = PAD.top + inner.h - barH
        const col = BUCKET_COLORS[k]
        const sentiment = d?.sentiment_overall
        const barColor = sentiment === 'positive' ? '#22c55e' : sentiment === 'negative' ? '#ef4444' : col.bar

        return (
          <g key={k}>
            <rect
              x={cx - barW / 2} y={PAD.top + inner.h}
              width={barW} height={0}
              fill={col.light} rx={3}
            />
            {d?.data_available && (
              <>
                <rect x={cx - barW / 2} y={barY} width={barW} height={barH} fill={barColor} rx={3} opacity={0.85} />
                <text x={cx} y={barY - 4} textAnchor="middle" fontSize={10} fontWeight="bold" fill={barColor}>
                  {score.toFixed(0)}%
                </text>
              </>
            )}
            {!d?.data_available && (
              <text x={cx} y={PAD.top + inner.h / 2} textAnchor="middle" fontSize={9} fill={axisColor}>데이터 없음</text>
            )}
            <text x={cx} y={PAD.top + inner.h + 14} textAnchor="middle" fontSize={9} fill={axisColor}>
              {d?.label?.split(' ')[0] || k}
            </text>
            <text x={cx} y={PAD.top + inner.h + 26} textAnchor="middle" fontSize={8} fill={axisColor}>
              {d?.label?.replace(/^[^\s]+\s/, '') || ''}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

function PlaytimeBucketCard({ bucket, data }) {
  const sentimentColor =
    data?.sentiment_overall === 'positive' ? '#22c55e' :
    data?.sentiment_overall === 'negative' ? '#ef4444' : '#f5a623'

  return (
    <div className="flex-1 bg-gray-50 dark:bg-[#2a2a3e] rounded-xl p-5 border border-gray-200 dark:border-[#3a3a5e] flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-bold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
          {data?.label || bucket}
        </span>
        {data?.data_available && <SentimentBadge value={data.sentiment_overall} />}
      </div>

      {!data?.data_available ? (
        <p className="text-xs text-gray-400 dark:text-gray-500">
          데이터 부족
        </p>
      ) : (
        <>
          {data.sentiment_score !== null && (
            <div className="flex items-center gap-2">
              <div className="flex-1 h-1.5 rounded-full bg-gray-200 dark:bg-[#1e1e2e] overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${data.sentiment_score}%`, background: sentimentColor }}
                />
              </div>
              <span className="text-xs font-bold" style={{ color: sentimentColor }}>
                {data.sentiment_score.toFixed(0)}%
              </span>
            </div>
          )}
          {data.summary && (
            <p className="text-xs leading-relaxed text-gray-600 dark:text-[#aaaaaa]">
              {data.summary}
            </p>
          )}
          {data.pros?.length > 0 && (
            <ul className="flex flex-col gap-1">
              {data.pros.map((p, i) => (
                <li key={i} className="text-xs text-gray-600 dark:text-[#aaaaaa] flex gap-1">
                  <span className="text-green-500 shrink-0">+</span> {p}
                </li>
              ))}
            </ul>
          )}
          {data.cons?.length > 0 && (
            <ul className="flex flex-col gap-1">
              {data.cons.map((c, i) => (
                <li key={i} className="text-xs text-gray-600 dark:text-[#aaaaaa] flex gap-1">
                  <span className="text-red-400 shrink-0">−</span> {c}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}

function ReviewCard({ review, translation, translating }) {
  const [showOriginal, setShowOriginal] = useState(false)

  const quote = typeof review === 'string' ? review : review?.quote || review?.summary || ''
  const reason = typeof review === 'string' ? '' : review?.reason || ''
  const source = typeof review === 'string' ? '' : review?.source || ''
  const reviewId = typeof review === 'string' ? '' : review?.review_id

  const displayText = showOriginal ? quote : (translation || quote)
  const hasTranslation = !!translation && translation !== quote

  return (
    <div className="rounded-lg border border-gray-200 dark:border-[#3a3a5e] bg-gray-50 dark:bg-[#2a2a3e] p-4">
      <div className="flex flex-wrap items-center gap-2 mb-2">
        {source && (
          <span className="text-[11px] font-bold px-2 py-0.5 rounded-full bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-300 border border-blue-200 dark:border-blue-700">
            {source}
          </span>
        )}
        {reviewId !== undefined && reviewId !== null && (
          <span className="text-[11px] text-gray-400 dark:text-gray-500">
            #{reviewId}
          </span>
        )}
        <div className="ml-auto">
          {translating && !translation && (
            <span className="text-[11px] text-gray-400 dark:text-gray-500 animate-pulse">번역 중...</span>
          )}
          {hasTranslation && (
            <button
              onClick={() => setShowOriginal(p => !p)}
              className="text-[11px] text-blue-500 dark:text-blue-400 hover:underline bg-transparent border-none cursor-pointer"
            >
              {showOriginal ? '번역 보기' : '원문 보기'}
            </button>
          )}
        </div>
      </div>

      {quote && (
        <p className="text-sm leading-relaxed text-gray-700 dark:text-[#cccccc]">
          {displayText}
        </p>
      )}

      {!showOriginal && translation && translation !== quote && (
        <p className="text-[11px] text-gray-400 dark:text-gray-500 mt-1">AI 번역</p>
      )}

      {reason && (
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">
          {reason}
        </p>
      )}
    </div>
  )
}

function RepresentativeReviewSection({ title, reviews, translations, translating, emptyMessage }) {
  return (
    <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
      <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">{title}</h2>
      {reviews && reviews.length > 0 ? (
        <div className="grid gap-3">
          {reviews.map((review, idx) => (
            <ReviewCard
              key={idx}
              review={review}
              translation={translations?.[idx]}
              translating={translating}
            />
          ))}
        </div>
      ) : (
        <p className="text-sm text-gray-400 dark:text-gray-500">{emptyMessage}</p>
      )}
    </div>
  )
}

function groupRepresentativeReviews(reviews) {
  const grouped = { steam: [], metacritic: [], other: [] }

  ;(reviews || []).forEach((review) => {
    const source = typeof review === 'string' ? '' : String(review?.source || '').toLowerCase()
    if (source.includes('steam')) {
      grouped.steam.push(review)
    } else if (source.includes('metacritic') || source.includes('critic')) {
      grouped.metacritic.push(review)
    } else {
      grouped.other.push(review)
    }
  })

  return grouped
}

function SentimentTrendChart({ data, isDark }) {
  const [tooltip, setTooltip] = useState(null)
  if (!data?.monthly?.length) return null

  const W = 800, H = 200, PAD = { top: 16, right: 20, bottom: 36, left: 48 }
  const inner = { w: W - PAD.left - PAD.right, h: H - PAD.top - PAD.bottom }

  const monthly = data.monthly
  const inflectionMap = {}
  ;(data.inflections || []).forEach(inf => { inflectionMap[inf.date] = inf })

  const ratios = monthly.map(m => m.neg_ratio)
  const minR = Math.min(...ratios)
  const maxR = Math.max(...ratios)
  const span = maxR - minR || 0.01

  const xStep = inner.w / Math.max(monthly.length - 1, 1)
  const toX = i => PAD.left + i * xStep
  const toY = r => PAD.top + inner.h - ((r - minR) / span) * inner.h

  const points = monthly.map((m, i) => `${toX(i).toFixed(1)},${toY(m.neg_ratio).toFixed(1)}`).join(' ')
  const fillPoints = [
    `${toX(0).toFixed(1)},${(PAD.top + inner.h).toFixed(1)}`,
    ...monthly.map((m, i) => `${toX(i).toFixed(1)},${toY(m.neg_ratio).toFixed(1)}`),
    `${toX(monthly.length - 1).toFixed(1)},${(PAD.top + inner.h).toFixed(1)}`,
  ].join(' ')

  const tickIndices = monthly.length <= 12
    ? monthly.map((_, i) => i)
    : monthly.reduce((acc, _, i) => {
        if (i === 0 || i === monthly.length - 1 || i % Math.ceil(monthly.length / 10) === 0) acc.push(i)
        return acc
      }, [])

  const gridLines = 4
  const stroke = isDark ? '#3a3a5e' : '#e5e7eb'
  const axisColor = isDark ? '#6b7280' : '#9ca3af'
  const lineColor = '#6366f1'
  const fillColor = isDark ? 'rgba(99,102,241,0.12)' : 'rgba(99,102,241,0.08)'

  return (
    <div className="relative w-full overflow-x-auto">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full"
        style={{ minWidth: 480 }}
        onMouseLeave={() => setTooltip(null)}
      >
        {/* grid */}
        {Array.from({ length: gridLines + 1 }, (_, gi) => {
          const y = PAD.top + (inner.h / gridLines) * gi
          const val = maxR - (span / gridLines) * gi
          return (
            <g key={gi}>
              <line x1={PAD.left} y1={y} x2={PAD.left + inner.w} y2={y} stroke={stroke} strokeWidth={1} />
              <text x={PAD.left - 6} y={y + 4} textAnchor="end" fontSize={10} fill={axisColor}>
                {(val * 100).toFixed(0)}%
              </text>
            </g>
          )
        })}

        {/* fill */}
        <polygon points={fillPoints} fill={fillColor} />

        {/* line */}
        <polyline points={points} fill="none" stroke={lineColor} strokeWidth={2} strokeLinejoin="round" />

        {/* x-axis ticks */}
        {tickIndices.map(i => (
          <text key={i} x={toX(i)} y={PAD.top + inner.h + 16} textAnchor="middle" fontSize={9} fill={axisColor}>
            {monthly[i].date.slice(0, 7)}
          </text>
        ))}

        {/* inflection markers */}
        {monthly.map((m, i) => {
          const inf = inflectionMap[m.date]
          if (!inf) return null
          const cx = toX(i), cy = toY(m.neg_ratio)
          const isSpike = inf.direction === 'negative_spike'
          return (
            <g key={i}>
              <circle
                cx={cx} cy={cy} r={6}
                fill={isSpike ? '#ef4444' : '#22c55e'}
                stroke={isDark ? '#1e1e2e' : '#fff'}
                strokeWidth={2}
                style={{ cursor: 'pointer' }}
                onMouseEnter={() => setTooltip({ x: cx, y: cy, inf, m })}
              />
            </g>
          )
        })}

        {/* hover dots */}
        {monthly.map((m, i) => {
          const inf = inflectionMap[m.date]
          if (inf) return null
          return (
            <circle
              key={i}
              cx={toX(i)} cy={toY(m.neg_ratio)} r={3}
              fill="transparent"
              style={{ cursor: 'crosshair' }}
              onMouseEnter={() => setTooltip({ x: toX(i), y: toY(m.neg_ratio), m })}
            />
          )
        })}

        {/* tooltip */}
        {tooltip && (() => {
          const { x, y, inf, m } = tooltip
          const tw = 160, th = inf ? 72 : 44
          const tx = Math.min(x + 10, W - tw - 4)
          const ty = Math.max(y - th - 10, PAD.top)
          const bg = isDark ? '#1e1e2e' : '#fff'
          const border = isDark ? '#3a3a5e' : '#e5e7eb'
          const fg = isDark ? '#e0e0e0' : '#111827'
          const sub = isDark ? '#9ca3af' : '#6b7280'
          return (
            <g>
              <rect x={tx} y={ty} width={tw} height={th} rx={6} fill={bg} stroke={border} strokeWidth={1} />
              <text x={tx + 8} y={ty + 16} fontSize={10} fontWeight="bold" fill={fg}>{m.date.slice(0, 7)}</text>
              <text x={tx + 8} y={ty + 30} fontSize={10} fill={sub}>
                부정비율 {(m.neg_ratio * 100).toFixed(1)}%  총 {m.total.toLocaleString()}건
              </text>
              {inf && (
                <>
                  <text x={tx + 8} y={ty + 46} fontSize={10} fill={inf.direction === 'negative_spike' ? '#ef4444' : '#22c55e'}>
                    {inf.direction === 'negative_spike' ? '↑ 부정 급증' : '↓ 긍정 회복'} {(Math.abs(inf.delta) * 100).toFixed(0)}%p
                  </text>
                  {inf.patch_title && (
                    <text x={tx + 8} y={ty + 60} fontSize={9} fill={sub}>
                      {inf.patch_title.slice(0, 22)}{inf.patch_title.length > 22 ? '…' : ''}
                    </text>
                  )}
                </>
              )}
            </g>
          )
        })()}
      </svg>

      {/* legend */}
      <div className="flex gap-4 mt-2 text-xs text-gray-500 dark:text-gray-400">
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 h-3 rounded-full bg-red-500" /> 부정 급증
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 h-3 rounded-full bg-green-500" /> 긍정 회복
        </span>
      </div>
    </div>
  )
}

function GameDetailPage({ isDark, toggleDark }) {
  const { id } = useParams()
  const navigate = useNavigate()

  const [game, setGame] = useState(null)
  const [summary, setSummary] = useState(null)
  const [playtimeAnalysis, setPlaytimeAnalysis] = useState(null)
  const [criticSummary, setCriticSummary] = useState(null)
  const [buySignal, setBuySignal] = useState(null)
  const [highlights, setHighlights] = useState(null)
  const [sentimentTrend, setSentimentTrend] = useState(null)
  const [reviewTranslations, setReviewTranslations] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    window.scrollTo(0, 0)
  }, [])

  useEffect(() => {
    if (!id) return

    const fetchData = async () => {
      setLoading(true)
      setError(null)

      try {
        const [gamesRes, summaryRes, playtimeRes, criticRes, trendRes, buySignalRes, highlightsRes] = await Promise.all([
          fetch(`${API_BASE}/api/v1/games/`),
          fetch(`${API_BASE}/api/v1/games/${id}/summary`),
          fetch(`${API_BASE}/api/v1/games/${id}/playtime-analysis`),
          fetch(`${API_BASE}/api/v1/games/${id}/critic-summary`),
          fetch(`${API_BASE}/api/v1/games/${id}/sentiment-trend`).catch(() => null),
          fetch(`${API_BASE}/api/v1/games/${id}/buy-signal`).catch(() => null),
          fetch(`${API_BASE}/api/v1/games/${id}/highlights?limit=5`).catch(() => null),
        ])

        if (gamesRes.ok) {
          const gamesData = await gamesRes.json()
          const found = gamesData.find(g => g.id === parseInt(id))
          setGame(found || null)
        }

        if (summaryRes.ok) {
          setSummary(await summaryRes.json())
        } else if (summaryRes.status === 404) {
          setError('아직 AI 요약본이 없습니다.')
        }

        if (playtimeRes.ok) {
          setPlaytimeAnalysis(await playtimeRes.json())
        }

        if (criticRes.ok) {
          setCriticSummary(await criticRes.json())
        }

        if (trendRes?.ok) {
          setSentimentTrend(await trendRes.json())
        }

        if (buySignalRes?.ok) {
          setBuySignal(await buySignalRes.json())
        }

        if (highlightsRes?.ok) {
          setHighlights(await highlightsRes.json())
        }
      } catch {
        setError('서버에 연결할 수 없습니다.')
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, [id])

  useEffect(() => {
    if (!summary?.representative_reviews?.length) return

    const grouped = groupRepresentativeReviews(summary.representative_reviews)
    const steamQuotes = grouped.steam.slice(0, 3).map(r => (typeof r === 'string' ? r : r?.quote || r?.summary || ''))
    const criticQuotes = grouped.metacritic.slice(0, 3).map(r => (typeof r === 'string' ? r : r?.quote || r?.summary || ''))
    const allQuotes = [...steamQuotes, ...criticQuotes].filter(Boolean)

    if (!allQuotes.length) return

    fetch(`${API_BASE}/api/v1/translate/batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts: allQuotes }),
    })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        const steamTr = data?.translations?.slice(0, steamQuotes.length) ?? []
        const criticTr = data?.translations?.slice(steamQuotes.length) ?? []
        setReviewTranslations({ steam: steamTr, metacritic: criticTr })
      })
      .catch(() => setReviewTranslations({ steam: [], metacritic: [] }))
  }, [summary])

  const translating = !!summary?.representative_reviews?.length && reviewTranslations === null

  if (!game && !loading) return <div className="p-10">게임을 찾을 수 없습니다.</div>

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-[#0f0f1a]">
      <Navbar isDark={isDark} toggleDark={toggleDark} />

      {/* 게임 배너 */}
      <section className="relative h-[440px] overflow-hidden flex gap-10 items-center px-12"
        style={{ background: 'linear-gradient(135deg, #0d2d63 0%, #1a1a2e 100%)' }}
      >
        {game?.hero_image && (
          <img
            src={game.hero_image}
            alt=""
            className="absolute top-0 left-0 w-full h-full object-cover object-top opacity-50 z-0"
          />
        )}

        <div className="absolute top-4 right-6 z-10 flex items-center gap-4">
          <button
            onClick={() => navigate('/', { state: { compareId: parseInt(id) } })}
            className="text-white/70 text-xs cursor-pointer hover:text-white transition-colors bg-transparent border border-white/20 hover:border-white/50 rounded-lg px-3 py-1.5"
          >
            ⇄ 다른 게임과 비교
          </button>
          <span
            onClick={() => navigate('/')}
            className="text-white/70 text-xs cursor-pointer hover:text-white transition-colors"
          >
            ← 목록으로 돌아가기
          </span>
        </div>

        <div className="relative z-10 w-40 min-w-[160px] h-56 bg-[#2a2a3e] rounded-lg flex items-center justify-center shrink-0">
          {game?.cover_image
            ? <img src={game.cover_image} alt={game.canonical_title} className="w-full h-full object-cover rounded-lg" />
            : <span className="text-gray-500 text-xs">No Image</span>
          }
        </div>

        <div className="relative z-10">
          <h1 className="text-white text-4xl font-extrabold mb-3">
            {game?.canonical_title || ''}
          </h1>

          {buySignal?.is_good_timing && (
            <div className="flex items-center gap-2 mb-3 flex-wrap">
              <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-black"
                style={{ background: 'rgba(34,197,94,0.2)', color: '#22c55e', border: '1px solid rgba(34,197,94,0.4)' }}>
                ✦ 지금이 적기
              </span>
              {buySignal.discount_percent > 0 && (
                <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-black"
                  style={{ background: '#ef4444', color: '#fff' }}>
                  -{buySignal.discount_percent}%
                </span>
              )}
              {buySignal.reasons?.slice(0, 2).map((r, i) => (
                <span key={i} className="text-xs text-white/70">{r}</span>
              ))}
            </div>
          )}

          {game?.rating != null && (
            <div className="flex items-center gap-2 mb-4">
              <span className="text-xl font-extrabold" style={{ color: '#ffb020' }}>
                {game.rating.toFixed(1)}
              </span>
              <span className="text-white text-base">/ 5.0</span>
              <div className="flex gap-0.5">
                {[1, 2, 3, 4, 5].map((star) => (
                  <span key={star} className="text-xl"
                    style={{ color: star <= Math.round(game.rating) ? '#ffb020' : 'rgba(255,255,255,0.3)' }}
                  >★</span>
                ))}
              </div>
            </div>
          )}

          {summary && (
            <p className="text-sm leading-relaxed max-w-xl" style={{ color: '#e6edf8' }}>
              {summary.summary_text?.split('\n')[0]?.replace(/\*\*/g, '')}
            </p>
          )}
        </div>
      </section>

      {/* 리뷰 요약 블록들 */}
      <div className="px-12 py-8 flex flex-col gap-6">

        {loading && (
          <div className="text-center py-10 text-gray-400">AI 요약 불러오는 중...</div>
        )}

        {!loading && error && (
          <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm text-center text-gray-400">
            {error}
          </div>
        )}

        {!loading && summary && (
          <>
            {/* 전체 요약 */}
            <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
              <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">AI 종합 요약</h2>
              <p className="text-sm leading-relaxed text-gray-700 dark:text-[#cccccc]">
                {summary.summary_text?.replace(/\*\*/g, '')}
              </p>
              {summary.keywords && summary.keywords.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-4">
                  {summary.keywords.map((kw, i) => (
                    <span key={i} className="text-xs px-2 py-1 rounded-full bg-blue-50 dark:bg-[#1a2a4a] text-blue-600 dark:text-blue-300 border border-blue-200 dark:border-blue-700">
                      {kw}
                    </span>
                  ))}
                </div>
              )}
            </div>

            {/* 플랫폼별 대표 리뷰 */}
            <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
              <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">플랫폼별 대표 리뷰</h2>
              <div className="grid grid-cols-2 gap-6">
                {(() => {
                  const groupedReviews = groupRepresentativeReviews(summary.representative_reviews || [])
                  return (
                    <>
                      <RepresentativeReviewSection
                        title="Steam 대표 리뷰"
                        reviews={groupedReviews.steam.slice(0, 3)}
                        translations={reviewTranslations?.steam}
                        translating={translating}
                        emptyMessage="Steam 대표 리뷰가 없습니다."
                      />
                      <RepresentativeReviewSection
                        title="Metacritic 대표 리뷰"
                        reviews={groupedReviews.metacritic.slice(0, 3)}
                        translations={reviewTranslations?.metacritic}
                        translating={translating}
                        emptyMessage="Metacritic 대표 리뷰가 없습니다."
                      />
                    </>
                  )
                })()}
              </div>
            </div>

            {/* 장점 / 단점 */}
            <div className="grid grid-cols-2 gap-6">
              <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
                <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">👍 장점</h2>
                {summary.pros && summary.pros.length > 0 ? (
                  <ul className="flex flex-col gap-2">
                    {summary.pros.map((pro, i) => (
                      <li key={i} className="text-sm text-gray-700 dark:text-[#cccccc] flex gap-2">
                        <span className="text-green-500 shrink-0">•</span> {pro}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="h-20 bg-gray-50 dark:bg-[#2a2a3e] rounded-lg" />
                )}
              </div>
              <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
                <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">👎 단점</h2>
                {summary.cons && summary.cons.length > 0 ? (
                  <ul className="flex flex-col gap-2">
                    {summary.cons.map((con, i) => (
                      <li key={i} className="text-sm text-gray-700 dark:text-[#cccccc] flex gap-2">
                        <span className="text-red-500 shrink-0">•</span> {con}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="h-20 bg-gray-50 dark:bg-[#2a2a3e] rounded-lg" />
                )}
              </div>
            </div>

            {/* 감성 분석 */}
            {summary.sentiment_overall && (
              <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
                <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">감성 분석</h2>
                <div className="flex items-center gap-4">
                  <SentimentBadge value={summary.sentiment_overall} />
                  {summary.sentiment_score !== null && (
                    <span className="text-sm text-gray-500 dark:text-gray-400">
                      점수: {(summary.sentiment_score).toFixed(0)}%
                    </span>
                  )}
                </div>
              </div>
            )}

            {/* 카테고리별 세부 분석 */}
            {summary.aspect_sentiment && Object.keys(summary.aspect_sentiment).length > 0 && (
              <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
                <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-4">카테고리별 분석</h2>
                <div className="grid grid-cols-2 gap-3">
                  {Object.entries(summary.aspect_sentiment).map(([key, value], i) => (
                    <div key={i} className="flex items-center justify-between bg-gray-50 dark:bg-[#2a2a3e] rounded-lg px-4 py-2">
                      <span className="text-sm text-gray-700 dark:text-[#e0e0e0] font-bold">
                        {CATEGORY_LABELS[key] || key}
                      </span>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-gray-500 dark:text-gray-400">{value.label}</span>
                        <span className="text-sm font-bold" style={{ color: value.score >= 7 ? '#22c55e' : value.score >= 5 ? '#f5a623' : '#ef4444' }}>
                          {value.score?.toFixed(1)}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        {/* ── Sprint 4: 플레이타임별 여론 ── */}
        {!loading && (
          <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
            <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-1">
              플레이타임별 여론
            </h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 mb-4">
              초반 구간 기준: {playtimeAnalysis?.bucket_thresholds?.early_max != null
                ? `~${playtimeAnalysis.bucket_thresholds.early_max}시간`
                : '데이터 없음'}
            </p>

            {!playtimeAnalysis ? (
              <p className="text-sm text-gray-400 dark:text-gray-500">플레이타임 분석 데이터가 없습니다.</p>
            ) : (
              <div className="flex flex-col gap-6">
                <div className="flex items-end gap-8">
                  <PlaytimeBarChart buckets={playtimeAnalysis.buckets} isDark={isDark} />
                  <div className="flex flex-col gap-1 text-xs text-gray-400 dark:text-gray-500 pb-8">
                    <span className="flex items-center gap-1.5"><span className="inline-block w-2.5 h-2.5 rounded-sm bg-green-500" /> 긍정적</span>
                    <span className="flex items-center gap-1.5"><span className="inline-block w-2.5 h-2.5 rounded-sm bg-red-500" /> 부정적</span>
                    <span className="flex items-center gap-1.5"><span className="inline-block w-2.5 h-2.5 rounded-sm bg-amber-400" /> 중립</span>
                  </div>
                </div>
                <div className="flex gap-4">
                  {['early', 'mid', 'late'].map(bucket => (
                    <PlaytimeBucketCard
                      key={bucket}
                      bucket={bucket}
                      data={playtimeAnalysis.buckets?.[bucket]}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── Sprint 4: 비평가 반응 ── */}
        {!loading && (
          <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
            <div className="flex items-center justify-between mb-1">
              <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0]">비평가 반응</h2>
              {criticSummary && (
                <span className="text-xs text-gray-400 dark:text-gray-500">출시 당시 전문가 반응</span>
              )}
            </div>

            {!criticSummary ? (
              <p className="text-sm text-gray-400 dark:text-gray-500">비평가 리뷰 데이터가 없습니다.</p>
            ) : (
              <div className="flex flex-col gap-4 mt-3">
                <div className="flex items-center gap-3">
                  <SentimentBadge value={criticSummary.sentiment_overall} />
                  {criticSummary.sentiment_score !== null && (
                    <span className="text-sm text-gray-500 dark:text-gray-400">
                      점수: {criticSummary.sentiment_score.toFixed(0)}%
                    </span>
                  )}
                </div>

                {criticSummary.summary && (
                  <p className="text-sm leading-relaxed text-gray-700 dark:text-[#cccccc]">
                    {criticSummary.summary}
                  </p>
                )}

                <div className="grid grid-cols-2 gap-4">
                  {criticSummary.pros?.length > 0 && (
                    <div>
                      <p className="text-xs font-bold text-green-600 dark:text-green-400 mb-1">장점</p>
                      <ul className="flex flex-col gap-1">
                        {criticSummary.pros.map((p, i) => (
                          <li key={i} className="text-xs text-gray-600 dark:text-[#aaaaaa] flex gap-1">
                            <span className="text-green-500 shrink-0">•</span> {p}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {criticSummary.cons?.length > 0 && (
                    <div>
                      <p className="text-xs font-bold text-red-500 dark:text-red-400 mb-1">단점</p>
                      <ul className="flex flex-col gap-1">
                        {criticSummary.cons.map((c, i) => (
                          <li key={i} className="text-xs text-gray-600 dark:text-[#aaaaaa] flex gap-1">
                            <span className="text-red-400 shrink-0">•</span> {c}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>

                {criticSummary.keywords?.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {criticSummary.keywords.map((kw, i) => (
                      <span key={i} className="text-xs px-2 py-0.5 rounded-full bg-purple-50 dark:bg-purple-900/20 text-purple-600 dark:text-purple-300 border border-purple-200 dark:border-purple-700">
                        {kw}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
        {/* ── Sprint 5: 감성 트렌드 차트 ── */}
        {!loading && (
          <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
            <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-1">감성 트렌드</h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 mb-4">
              Steam 리뷰 월별 부정 비율 추이 — 마커는 여론 변곡점
            </p>
            {sentimentTrend
              ? <SentimentTrendChart data={sentimentTrend} isDark={isDark} />
              : <p className="text-sm text-gray-400 dark:text-gray-500">감성 트렌드 데이터가 없습니다.</p>
            }
          </div>
        )}

        {/* ── 구매 타이밍 시그널 ── */}
        {!loading && buySignal && (
          <div className={`rounded-xl p-7 border shadow-sm ${
            buySignal.is_good_timing
              ? 'bg-green-50 dark:bg-[#0d2a1a] border-green-200 dark:border-green-800'
              : 'bg-white dark:bg-[#1e1e2e] border-gray-200 dark:border-[#2a2a3e]'
          }`}>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] flex items-center gap-2">
                구매 타이밍
                {buySignal.is_good_timing && (
                  <span className="text-xs px-2 py-0.5 rounded-full font-black"
                    style={{ background: 'rgba(34,197,94,0.15)', color: '#22c55e', border: '1px solid rgba(34,197,94,0.35)' }}>
                    ✦ 지금이 적기
                  </span>
                )}
              </h2>
              {buySignal.discount_percent > 0 && (
                <span className="text-sm font-black px-3 py-1 rounded-full"
                  style={{ background: '#ef4444', color: '#fff' }}>
                  -{buySignal.discount_percent}% 할인 중
                </span>
              )}
            </div>

            {buySignal.original_price != null && buySignal.original_price > 0 && (
              <div className="flex items-baseline gap-2 mb-3">
                <span className="text-2xl font-black text-green-600 dark:text-green-400">
                  ₩{(buySignal.final_price ?? buySignal.original_price).toLocaleString()}
                </span>
                {buySignal.discount_percent > 0 && (
                  <span className="text-sm text-gray-400 line-through">
                    ₩{buySignal.original_price.toLocaleString()}
                  </span>
                )}
              </div>
            )}

            <ul className="flex flex-col gap-1">
              {buySignal.reasons?.map((reason, i) => (
                <li key={i} className="text-sm text-gray-700 dark:text-[#cccccc] flex items-center gap-2">
                  <span className={buySignal.is_good_timing ? 'text-green-500' : 'text-gray-400'}>•</span>
                  {reason}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* ── 이 게임의 명장면 ── */}
        {!loading && highlights?.highlights?.length > 0 && (
          <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
            <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-1">이 게임의 명장면</h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 mb-4">
              가장 많은 공감을 받은 감동 리뷰 — 플레이어가 실제로 느낀 순간
            </p>
            <div className="flex gap-4 overflow-x-auto pb-2" style={{ scrollSnapType: 'x mandatory' }}>
              {highlights.highlights.map((h, i) => (
                <div key={h.review_id ?? i}
                  className="flex-none w-72 bg-gray-50 dark:bg-[#2a2a3e] rounded-xl p-5 border border-gray-200 dark:border-[#3a3a5e] flex flex-col gap-3"
                  style={{ scrollSnapAlign: 'start' }}>
                  <p className="text-sm leading-relaxed text-gray-800 dark:text-[#e0e0e0] italic line-clamp-5">
                    "{h.text}"
                  </p>
                  <div className="flex items-center gap-3 mt-auto pt-2 border-t border-gray-200 dark:border-[#3a3a5e]">
                    {h.playtime_hours != null && (
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        {Math.round(h.playtime_hours)}h 플레이
                      </span>
                    )}
                    {h.helpful_count > 0 && (
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        공감 {h.helpful_count}
                      </span>
                    )}
                    {h.linked_aspect && (
                      <span className="ml-auto text-xs px-2 py-0.5 rounded-full bg-blue-50 dark:bg-[#1a2a4a] text-blue-600 dark:text-blue-300 border border-blue-200 dark:border-blue-700">
                        {h.linked_aspect}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export default GameDetailPage

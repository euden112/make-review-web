import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import Navbar from './Navbar'

const API_BASE = import.meta.env.VITE_API_BASE || ''

const CATEGORY_LABELS = {
  graphics: '그래픽',
  controls: '조작감',
  optimization: '최적화',
  content: '콘텐츠 양',
  price_value: '가성비',
  sound: '음향',
  gameplay: '재미',
  difficulty: '난이도',
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

// 카테고리별 점수를 다각형(레이더) 차트로 — 어느 축이 돌출/함몰됐는지 한눈에.
// 절대 점수가 아니라 "이 게임 안에서" 강·약점 프로파일을 읽도록 한다.
function AspectRadarChart({ aspects, isDark }) {
  const n = aspects.length
  if (n < 3) return null

  const W = 340, H = 300, cx = 170, cy = 148, R = 92, MAX = 10
  const labelR = R + 20
  const stroke = isDark ? '#3a3a5e' : '#e5e7eb'
  const labelColor = isDark ? '#e0e0e0' : '#374151'
  const accent = '#6366f1'
  // 절대 점수가 아니라 "이 게임 평균 대비" 상대 강약으로 색을 정한다.
  // 평균 위 = 강점(녹), 평균 아래 = 약점(적), 평균 근처 = 보통(회색).
  const mean = aspects.reduce((s, a) => s + a.score, 0) / n
  const NEUTRAL = isDark ? '#9ca3af' : '#9ca3af'
  const relColor = (s) => { const d = s - mean; return d >= 0.3 ? '#22c55e' : d <= -0.3 ? '#ef4444' : NEUTRAL }

  const angleFor = (i) => (-90 + (360 / n) * i) * (Math.PI / 180)
  const pt = (i, r) => [cx + r * Math.cos(angleFor(i)), cy + r * Math.sin(angleFor(i))]
  const polyPath = (r) =>
    aspects.map((_, i) => { const p = pt(i, r); return `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}` }).join(' ') + ' Z'

  const dataPath =
    aspects.map((a, i) => { const p = pt(i, (Math.max(0, Math.min(MAX, a.score)) / MAX) * R); return `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}` }).join(' ') + ' Z'

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 300 }}>
      {[0.25, 0.5, 0.75, 1].map((rr, ri) => (
        <path key={ri} d={polyPath(rr * R)} fill="none" stroke={stroke} strokeWidth={1} />
      ))}
      {aspects.map((_, i) => { const p = pt(i, R); return (
        <line key={i} x1={cx} y1={cy} x2={p[0]} y2={p[1]} stroke={stroke} strokeWidth={1} />
      ) })}

      <path d={dataPath} fill={accent} fillOpacity={0.18} stroke={accent} strokeWidth={2} strokeLinejoin="round" />

      {aspects.map((a, i) => {
        const p = pt(i, (Math.max(0, Math.min(MAX, a.score)) / MAX) * R)
        return <circle key={i} cx={p[0]} cy={p[1]} r={4} fill={relColor(a.score)} />
      })}

      {aspects.map((a, i) => {
        const lp = pt(i, labelR)
        const cos = Math.cos(angleFor(i))
        const anchor = Math.abs(cos) < 0.3 ? 'middle' : cos > 0 ? 'start' : 'end'
        const label = CATEGORY_LABELS[a.key] || a.label || a.key
        const c = relColor(a.score)
        // 절대 수치는 기준이 불명확해 표기하지 않는다. 색(강/약)과 형상으로만 비교.
        return (
          <text key={i} x={lp[0]} y={lp[1] + 3} textAnchor={anchor} fontSize={11} fontWeight="bold"
            fill={c === NEUTRAL ? labelColor : c}>{label}</text>
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
  const [expanded, setExpanded] = useState(false)

  const quote = typeof review === 'string' ? review : review?.quote || review?.summary || ''
  const reason = typeof review === 'string' ? '' : review?.reason || ''
  const source = typeof review === 'string' ? '' : review?.source || ''
  const reviewId = typeof review === 'string' ? '' : review?.review_id

  const displayText = showOriginal ? quote : (translation || quote)
  const hasTranslation = !!translation && translation !== quote
  const isLong = displayText.length > 120

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
        <p className={`text-sm leading-relaxed text-gray-700 dark:text-[#cccccc] ${!expanded && isLong ? 'line-clamp-6' : ''}`}>
          {displayText}
        </p>
      )}

      {isLong && (
        <button
          onClick={() => setExpanded(p => !p)}
          className="mt-2 text-[11px] text-blue-500 dark:text-blue-400 hover:underline bg-transparent border-none cursor-pointer"
        >
          {expanded ? '접기' : '전체 보기'}
        </button>
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

function PointList({ title, items, color }) {
  if (!items?.length) return null
  return (
    <div className="mt-4">
      <p className="text-xs font-bold text-gray-500 dark:text-gray-400 mb-2">{title}</p>
      <ul className="flex flex-col gap-1.5">
        {items.slice(0, 4).map((item, idx) => (
          <li key={idx} className="text-xs leading-relaxed text-gray-600 dark:text-[#aaaaaa] flex gap-1.5">
            <span className="shrink-0" style={{ color }}>{color === '#22c55e' ? '+' : '-'}</span>
            {item}
          </li>
        ))}
      </ul>
    </div>
  )
}

function SummaryCard({ title, data, emptyMessage }) {
  return (
    <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
      <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">{title}</h2>
      {data?.summary ? (
        <>
          <div className="flex items-center gap-2 mb-3">
            <SentimentBadge value={data.sentiment_overall} />
            {data.sentiment_score != null && (
              <span className="text-xs text-gray-500 dark:text-gray-400">
                점수: {data.sentiment_score.toFixed(0)}%
              </span>
            )}
          </div>
          <p className="text-sm leading-relaxed text-gray-700 dark:text-[#cccccc]">
            {data.summary}
          </p>
          <PointList title="주요 호평" items={data.pros} color="#22c55e" />
          <PointList title="주의할 점" items={data.cons} color="#ef4444" />
        </>
      ) : (
        <p className="text-sm text-gray-400 dark:text-gray-500">{emptyMessage}</p>
      )}
    </div>
  )
}

function RecommendationTargetsSection({ recommendations }) {
  if (!recommendations?.length) return null

  return (
    <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
      <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-1">이런 사람에게 추천</h2>
      <p className="text-xs text-gray-400 dark:text-gray-500 mb-4">
        리뷰에서 반복된 긍정 근거를 바탕으로 정리한 추천 대상
      </p>
      <div className="grid grid-cols-2 gap-3">
        {recommendations.map((item, idx) => (
          <div key={`${item.category}-${idx}`} className="rounded-lg bg-gray-50 dark:bg-[#2a2a3e] border border-gray-200 dark:border-[#3a3a5e] p-4">
            <div className="flex items-center justify-between gap-2 mb-2">
              <h3 className="text-sm font-bold text-gray-800 dark:text-[#e0e0e0]">{item.label}</h3>
              {item.category && (
                <span className="text-[11px] px-2 py-0.5 rounded-full bg-blue-50 dark:bg-[#1a2a4a] text-blue-600 dark:text-blue-300 border border-blue-200 dark:border-blue-700">
                  {CATEGORY_LABELS[item.category] || item.category}
                </span>
              )}
            </div>
            <p className="text-xs leading-relaxed text-gray-600 dark:text-[#aaaaaa]">
              {item.summary}
            </p>
            {item.evidence_count > 0 && (
              <p className="text-[11px] text-gray-400 dark:text-gray-500 mt-3">
                긍정 근거 {item.evidence_count}건
              </p>
            )}
          </div>
        ))}
      </div>
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

function formatWon(value) {
  if (value == null || Number(value) <= 0) return '정보 없음'
  return `₩${Number(value).toLocaleString()}`
}

function formatDisplayItemValue(item) {
  if (!item) return ''
  if (item.text) return item.text
  if (item.unit === 'KRW') return formatWon(item.value)
  if (item.unit === 'percent') return item.value > 0 ? `${item.value}% 할인 중` : '현재 할인 없음'
  if (item.unit === 'ratio' && item.value != null) return `${(item.value * 100).toFixed(0)}%`
  return item.value ?? ''
}

function buySignalDisplayItems(signal) {
  if (!signal) return []
  if (Array.isArray(signal.display_items) && signal.display_items.length > 0) {
    return signal.display_items
  }

  const items = [
    {
      type: 'current_price',
      label: '현재 가격',
      value: signal.final_price ?? signal.original_price,
      unit: 'KRW',
      always_show: true,
    },
    {
      type: 'discount',
      label: '할인 정보',
      value: signal.discount_percent ?? 0,
      unit: 'percent',
      always_show: true,
    },
  ]
  if (signal.show_positive_ratio && signal.positive_ratio != null) {
    items.push({
      type: 'positive_ratio',
      label: '긍정 비율',
      value: signal.positive_ratio,
      delta: signal.positive_delta,
      unit: 'ratio',
      text: signal.positive_delta != null
        ? `최근 긍정 비율 ${(signal.positive_ratio * 100).toFixed(0)}% (+${(signal.positive_delta * 100).toFixed(0)}%p)`
        : `최근 긍정 비율 ${(signal.positive_ratio * 100).toFixed(0)}%`,
      always_show: false,
    })
  }
  return items
}

function GameDetailPage({ isDark, toggleDark }) {
  const { id } = useParams()
  const navigate = useNavigate()

  const [game, setGame] = useState(null)
  const [summary, setSummary] = useState(null)
  const [playtimeAnalysis, setPlaytimeAnalysis] = useState(null)
  const [criticSummary, setCriticSummary] = useState(null)
  const [userSummary, setUserSummary] = useState(null)
  const [buySignal, setBuySignal] = useState(null)
  const [recommendationTargets, setRecommendationTargets] = useState(null)
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
        const [gamesRes, summaryRes, playtimeRes, criticRes, userRes, buySignalRes, recommendationRes] = await Promise.all([
          fetch(`${API_BASE}/api/v1/games/`),
          fetch(`${API_BASE}/api/v1/games/${id}/summary`),
          fetch(`${API_BASE}/api/v1/games/${id}/playtime-analysis`),
          fetch(`${API_BASE}/api/v1/games/${id}/critic-summary`),
          fetch(`${API_BASE}/api/v1/games/${id}/user-summary`).catch(() => null),
          fetch(`${API_BASE}/api/v1/games/${id}/buy-signal`).catch(() => null),
          fetch(`${API_BASE}/api/v1/games/${id}/recommendation-targets?limit=4`).catch(() => null),
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

        if (userRes?.ok) {
          setUserSummary(await userRes.json())
        }


        if (buySignalRes?.ok) {
          setBuySignal(await buySignalRes.json())
        }

        if (recommendationRes?.ok) {
          setRecommendationTargets(await recommendationRes.json())
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

          {summary && (summary.one_liner || summary.summary_text) && (
            <p className="text-sm leading-relaxed max-w-xl" style={{ color: '#e6edf8' }}>
              {summary.one_liner ?? summary.summary_text?.split('\n')[0]?.replace(/\*\*/g, '')}
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
            {/* 키워드 (태그) 별도 섹션 — AI 종합 요약은 유저 리뷰 요약과 중복이라 제거 */}
            {summary.keywords && summary.keywords.length > 0 && (
              <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
                <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">키워드</h2>
                <div className="flex flex-wrap gap-2">
                  {summary.keywords.map((kw, i) => (
                    <span key={i} className="text-xs px-2 py-1 rounded-full bg-blue-50 dark:bg-[#1a2a4a] text-blue-600 dark:text-blue-300 border border-blue-200 dark:border-blue-700">
                      {kw}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* ── B안: user/critic 분리 2단 블록 (unified 본문 폐지) ── */}
            {(userSummary?.summary || criticSummary?.summary) && (
              <div className="grid grid-cols-2 gap-6">
                <SummaryCard
                  title="유저 리뷰 요약"
                  data={userSummary}
                  emptyMessage="유저 리뷰 데이터가 없습니다."
                />
                <SummaryCard
                  title="비평가 리뷰 요약"
                  data={criticSummary}
                  emptyMessage="비평가 리뷰 데이터가 없습니다."
                />
              </div>
            )}

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

            {/* 카테고리별 분석 — 게임 내 상대 강·약점 프로파일 (다각형 차트) */}
            {summary.aspect_sentiment && Object.keys(summary.aspect_sentiment).length > 0 && (() => {
              const aspects = Object.entries(summary.aspect_sentiment).map(([key, v]) => ({
                key,
                label: v.label,
                score: typeof v.score === 'number' ? v.score : (Number(v.score) || 0),
              }))
              const sorted = [...aspects].sort((a, b) => b.score - a.score)
              const strength = sorted[0]
              const weakness = sorted[sorted.length - 1]
              const labelOf = (a) => CATEGORY_LABELS[a.key] || a.label || a.key
              return (
                <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
                  <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-1">카테고리별 분석</h2>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mb-4">이 게임 안에서의 상대적 강점과 약점</p>
                  {aspects.length >= 3 ? (
                    <div className="flex flex-col items-center">
                      <AspectRadarChart aspects={aspects} isDark={isDark} />
                      <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                        강점 <span className="font-bold" style={{ color: '#22c55e' }}>{labelOf(strength)}</span>
                        <span className="mx-1.5 text-gray-300 dark:text-gray-600">·</span>
                        약점 <span className="font-bold" style={{ color: '#ef4444' }}>{labelOf(weakness)}</span>
                      </p>
                    </div>
                  ) : (
                    <div className="grid grid-cols-2 gap-3">
                      {aspects.map((a, i) => (
                        <div key={i} className="flex items-center justify-between bg-gray-50 dark:bg-[#2a2a3e] rounded-lg px-4 py-2">
                          <span className="text-sm text-gray-700 dark:text-[#e0e0e0] font-bold">{labelOf(a)}</span>
                          <div className="flex items-center gap-2">
                            <span className="text-xs text-gray-500 dark:text-gray-400">{a.label}</span>
                            <span className="text-sm font-bold" style={{ color: a.score >= 7 ? '#22c55e' : a.score >= 5 ? '#f5a623' : '#ef4444' }}>
                              {a.score?.toFixed(1)}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })()}
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

            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {buySignalDisplayItems(buySignal).map((item) => (
                <div
                  key={item.type}
                  className="rounded-lg bg-white/70 dark:bg-[#2a2a3e] border border-gray-200 dark:border-[#3a3a5e] px-4 py-3"
                >
                  <p className="text-[11px] font-bold text-gray-400 dark:text-gray-500 mb-1">
                    {item.label}
                  </p>
                  <p className={`text-sm font-black ${
                    item.type === 'positive_ratio' || (item.type === 'discount' && item.value > 0)
                      ? 'text-green-600 dark:text-green-400'
                      : 'text-gray-900 dark:text-[#e0e0e0]'
                  }`}>
                    {formatDisplayItemValue(item)}
                  </p>
                  {item.type === 'current_price'
                    && buySignal.original_price != null
                    && buySignal.discount_percent > 0
                    && buySignal.original_price !== (buySignal.final_price ?? buySignal.original_price) && (
                    <p className="text-[11px] text-gray-400 line-through mt-1">
                      {formatWon(buySignal.original_price)}
                    </p>
                  )}
                </div>
              ))}
            </div>

            {/* BUG-3: 세일 카운트다운 미제공 → 스냅샷 시각 + 스토어 확인 헤지 */}
            <div className="mt-4 pt-3 border-t border-gray-200 dark:border-[#2a2a3e] flex flex-wrap items-center justify-between gap-2">
              {buySignal.price_as_of && (
                <span className="text-xs text-gray-400">
                  가격 기준 {new Date(buySignal.price_as_of).toLocaleString('ko-KR')}
                  {buySignal.price_is_stale && ' · 최신이 아닐 수 있음'}
                </span>
              )}
              {buySignal.store_url && (
                <a href={buySignal.store_url} target="_blank" rel="noopener noreferrer"
                  className="text-xs font-bold text-blue-600 dark:text-blue-400 hover:underline">
                  최종 가격은 Steam에서 확인 →
                </a>
              )}
            </div>
          </div>
        )}

        {!loading && (
          <RecommendationTargetsSection
            recommendations={recommendationTargets?.recommendations}
          />
        )}
      </div>
    </div>
  )
}

export default GameDetailPage

import { useEffect, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import Navbar from './Navbar'

const API_BASE = import.meta.env.VITE_API_BASE || ''

const GAME_META = {
  1: { rating: 4 },
  2: { rating: 5 },
  3: { rating: 3 },
  4: { rating: 5 },
  5: { rating: 3 },
}

const BUCKET_LABELS = { early: '초반', mid: '중반', late: '후반' }
const SENTIMENT_COLORS = { positive: '#22c55e', negative: '#ef4444', mixed: '#f59e0b' }
const SENTIMENT_LABELS = { positive: '긍정적', negative: '부정적', mixed: '중립' }

function ScoreBar({ score, color }) {
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 rounded-full bg-gray-200 dark:bg-[#2a2a3e] overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${score ?? 0}%`, background: color }}
        />
      </div>
      <span className="text-xs font-bold w-8 text-right" style={{ color }}>
        {score != null ? `${score.toFixed(0)}%` : '—'}
      </span>
    </div>
  )
}

function SentimentChip({ value }) {
  const color = SENTIMENT_COLORS[value] || '#6b7280'
  const label = SENTIMENT_LABELS[value] || value || '—'
  return (
    <span
      className="text-[11px] font-bold px-2 py-0.5 rounded-full border"
      style={{ color, borderColor: color, background: `${color}18` }}
    >
      {label}
    </span>
  )
}

function CompareRow({ label, left, right }) {
  return (
    <div className="grid grid-cols-[1fr_auto_1fr] gap-4 items-start py-3 border-b border-gray-100 dark:border-[#2a2a3e] last:border-0">
      <div>{left}</div>
      <div className="text-xs text-gray-400 dark:text-gray-500 font-bold self-center whitespace-nowrap px-2">{label}</div>
      <div className="text-right">{right}</div>
    </div>
  )
}

function TagList({ items, color }) {
  if (!items?.length) return <span className="text-xs text-gray-400">—</span>
  return (
    <ul className="flex flex-col gap-0.5">
      {items.slice(0, 4).map((item, i) => (
        <li key={i} className="text-xs text-gray-600 dark:text-[#aaaaaa] flex gap-1">
          <span style={{ color }} className="shrink-0">{color === '#22c55e' ? '+' : '−'}</span> {item}
        </li>
      ))}
    </ul>
  )
}

function RadarChart({ games, isDark }) {
  const axes = [
    { key: 'sentiment_score', label: '전체 감성' },
    { key: 'early', label: '초반 여론' },
    { key: 'mid', label: '중반 여론' },
    { key: 'late', label: '후반 여론' },
    { key: 'critic_score', label: '비평가' },
  ]

  const W = 280, H = 260, cx = W / 2, cy = H / 2 + 10, R = 90
  const n = axes.length
  const angleStep = (2 * Math.PI) / n
  const startAngle = -Math.PI / 2

  const axisPoints = axes.map((_, i) => {
    const angle = startAngle + i * angleStep
    return { x: cx + R * Math.cos(angle), y: cy + R * Math.sin(angle) }
  })

  const labelPoints = axes.map((_, i) => {
    const angle = startAngle + i * angleStep
    const r = R + 20
    return { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) }
  })

  const toPolygon = (values) =>
    values.map((v, i) => {
      const ratio = (v ?? 0) / 100
      const angle = startAngle + i * angleStep
      return `${(cx + R * ratio * Math.cos(angle)).toFixed(1)},${(cy + R * ratio * Math.sin(angle)).toFixed(1)}`
    }).join(' ')

  const gridLevels = [0.25, 0.5, 0.75, 1]
  const stroke = isDark ? '#3a3a5e' : '#e5e7eb'
  const axisStroke = isDark ? '#4a4a6e' : '#d1d5db'
  const textFill = isDark ? '#9ca3af' : '#6b7280'

  const COLORS = ['#6366f1', '#f59e0b']

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-[280px]">
      {gridLevels.map(level => (
        <polygon
          key={level}
          points={axisPoints.map(p => {
            const dx = p.x - cx, dy = p.y - cy
            return `${(cx + dx * level).toFixed(1)},${(cy + dy * level).toFixed(1)}`
          }).join(' ')}
          fill="none"
          stroke={stroke}
          strokeWidth={1}
        />
      ))}

      {axisPoints.map((p, i) => (
        <line key={i} x1={cx} y1={cy} x2={p.x} y2={p.y} stroke={axisStroke} strokeWidth={1} />
      ))}

      {games.map((g, gi) => {
        const values = axes.map(a => {
          if (a.key === 'early') return g.playtime?.buckets?.early?.sentiment_score ?? null
          if (a.key === 'mid')   return g.playtime?.buckets?.mid?.sentiment_score ?? null
          if (a.key === 'late')  return g.playtime?.buckets?.late?.sentiment_score ?? null
          if (a.key === 'critic_score') return g.critic?.sentiment_score ?? null
          return g.summary?.[a.key] ?? null
        })
        const col = COLORS[gi]
        return (
          <polygon
            key={gi}
            points={toPolygon(values)}
            fill={`${col}22`}
            stroke={col}
            strokeWidth={2}
          />
        )
      })}

      {labelPoints.map((p, i) => (
        <text
          key={i}
          x={p.x} y={p.y}
          textAnchor="middle"
          dominantBaseline="middle"
          fontSize={9}
          fill={textFill}
        >
          {axes[i].label}
        </text>
      ))}
    </svg>
  )
}

async function fetchGameData(id) {
  const [gamesRes, summaryRes, playtimeRes, criticRes] = await Promise.all([
    fetch(`${API_BASE}/api/v1/games/`),
    fetch(`${API_BASE}/api/v1/games/${id}/summary`).catch(() => null),
    fetch(`${API_BASE}/api/v1/games/${id}/playtime-analysis`).catch(() => null),
    fetch(`${API_BASE}/api/v1/games/${id}/critic-summary`).catch(() => null),
  ])

  const games = gamesRes.ok ? await gamesRes.json() : []
  const game = games.find(g => g.id === parseInt(id)) || null
  const summary = summaryRes?.ok ? await summaryRes.json() : null
  const playtime = playtimeRes?.ok ? await playtimeRes.json() : null
  const critic = criticRes?.ok ? await criticRes.json() : null

  return { game, summary, playtime, critic }
}

export default function GameComparePage({ isDark, toggleDark }) {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const ids = (searchParams.get('ids') || '').split(',').filter(Boolean).slice(0, 2)

  const [data, setData] = useState([null, null])
  const [loading, setLoading] = useState(true)
  const id0 = ids[0]
  const id1 = ids[1]

  useEffect(() => {
    if (!id0 || !id1) return
    let active = true
    Promise.all([id0, id1].map(fetchGameData)).then(results => {
      if (active) {
        setData(results)
        setLoading(false)
      }
    })
    return () => {
      active = false
      setLoading(true)
    }
  }, [id0, id1])

  if (ids.length < 2) {
    return (
      <div className="min-h-screen bg-gray-100 dark:bg-[#0f0f1a]">
        <Navbar isDark={isDark} toggleDark={toggleDark} />
        <div className="flex flex-col items-center justify-center h-[60vh] gap-4">
          <p className="text-gray-400 dark:text-gray-500">비교할 게임을 2개 선택해주세요.</p>
          <button
            onClick={() => navigate('/')}
            className="text-sm text-blue-500 hover:underline bg-transparent border-none cursor-pointer"
          >
            목록으로 →
          </button>
        </div>
      </div>
    )
  }

  const [A, B] = data

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-[#0f0f1a]">
      <Navbar isDark={isDark} toggleDark={toggleDark} />

      <div className="px-8 py-6 max-w-5xl mx-auto flex flex-col gap-6">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate('/')}
            className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 bg-transparent border-none cursor-pointer"
          >
            ← 목록으로
          </button>
          <h1 className="text-lg font-bold text-gray-900 dark:text-[#e0e0e0]">게임 비교</h1>
        </div>

        {loading ? (
          <div className="text-center py-20 text-gray-400">불러오는 중...</div>
        ) : (
          <>
            {/* 게임 헤더 */}
            <div className="grid grid-cols-[1fr_auto_1fr] gap-4">
              {[A, B].map((d, i) => (
                <div
                  key={i}
                  className={`bg-white dark:bg-[#1e1e2e] rounded-xl p-5 border border-gray-200 dark:border-[#2a2a3e] shadow-sm flex items-center gap-4 cursor-pointer hover:border-blue-400 transition-colors ${i === 2 ? 'text-right' : ''}`}
                  onClick={() => navigate(`/games/${ids[i]}`)}
                >
                  <div className="w-14 h-20 bg-gray-100 dark:bg-[#2a2a3e] rounded-lg overflow-hidden shrink-0">
                    {d?.game?.cover_image
                      ? <img src={d.game.cover_image} alt="" className="w-full h-full object-cover" />
                      : <div className="w-full h-full" />
                    }
                  </div>
                  <div>
                    <p className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] leading-tight">
                      {d?.game?.canonical_title || `게임 ${ids[i]}`}
                    </p>
                    <div className="flex gap-0.5 mt-1">
                      {[1,2,3,4,5].map(s => (
                        <span key={s} className="text-sm" style={{ color: s <= (GAME_META[parseInt(ids[i])]?.rating || 3) ? '#ffb020' : '#d1d5db' }}>★</span>
                      ))}
                    </div>
                    {d?.summary?.sentiment_overall && <SentimentChip value={d.summary.sentiment_overall} />}
                  </div>
                </div>
              ))}
              <div className="flex items-center justify-center text-2xl font-black text-gray-300 dark:text-[#3a3a5e]">VS</div>
            </div>

            {/* 레이더 차트 */}
            <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-6 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
              <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-4">종합 비교</h2>
              <div className="flex items-center gap-8">
                <RadarChart games={[A, B].map(d => d || {})} isDark={isDark} />
                <div className="flex flex-col gap-2">
                  {[A, B].map((d, i) => (
                    <span key={i} className="flex items-center gap-2 text-xs text-gray-600 dark:text-[#aaaaaa]">
                      <span className="inline-block w-3 h-1.5 rounded-full" style={{ background: ['#6366f1','#f59e0b'][i] }} />
                      {d?.game?.canonical_title || `게임 ${ids[i]}`}
                    </span>
                  ))}
                </div>
              </div>
            </div>

            {/* 감성 점수 비교 */}
            <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-6 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
              <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-4">감성 점수</h2>
              <CompareRow
                label="전체 감성"
                left={<ScoreBar score={A?.summary?.sentiment_score} color="#6366f1" />}
                right={<ScoreBar score={B?.summary?.sentiment_score} color="#f59e0b" />}
              />
              <CompareRow
                label="비평가"
                left={<ScoreBar score={A?.critic?.sentiment_score} color="#6366f1" />}
                right={<ScoreBar score={B?.critic?.sentiment_score} color="#f59e0b" />}
              />
              {['early', 'mid', 'late'].map(bucket => (
                <CompareRow
                  key={bucket}
                  label={BUCKET_LABELS[bucket]}
                  left={<ScoreBar score={A?.playtime?.buckets?.[bucket]?.sentiment_score} color="#6366f1" />}
                  right={<ScoreBar score={B?.playtime?.buckets?.[bucket]?.sentiment_score} color="#f59e0b" />}
                />
              ))}
            </div>

            {/* 장단점 비교 */}
            <div className="grid grid-cols-2 gap-4">
              {[A, B].map((d, i) => (
                <div key={i} className="bg-white dark:bg-[#1e1e2e] rounded-xl p-6 border border-gray-200 dark:border-[#2a2a3e] shadow-sm flex flex-col gap-4">
                  <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0]">
                    {d?.game?.canonical_title || `게임 ${ids[i]}`}
                  </h2>
                  <div>
                    <p className="text-xs font-bold text-green-600 dark:text-green-400 mb-1">장점</p>
                    <TagList items={d?.summary?.pros} color="#22c55e" />
                  </div>
                  <div>
                    <p className="text-xs font-bold text-red-500 dark:text-red-400 mb-1">단점</p>
                    <TagList items={d?.summary?.cons} color="#ef4444" />
                  </div>
                  {d?.summary?.keywords?.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 mt-1">
                      {d.summary.keywords.map((kw, j) => (
                        <span key={j} className="text-[11px] px-2 py-0.5 rounded-full bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-300 border border-blue-200 dark:border-blue-700">
                          {kw}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* 비평가 반응 비교 */}
            <div className="grid grid-cols-2 gap-4">
              {[A, B].map((d, i) => (
                <div key={i} className="bg-white dark:bg-[#1e1e2e] rounded-xl p-6 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
                  <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">비평가 반응</h2>
                  {!d?.critic ? (
                    <p className="text-xs text-gray-400">데이터 없음</p>
                  ) : (
                    <div className="flex flex-col gap-2">
                      <SentimentChip value={d.critic.sentiment_overall} />
                      {d.critic.summary && (
                        <p className="text-xs leading-relaxed text-gray-600 dark:text-[#aaaaaa]">
                          {d.critic.summary}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

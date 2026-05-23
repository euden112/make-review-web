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

function RepresentativeReviewSection({ title, reviews, emptyMessage }) {
  return (
    <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
      <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">{title}</h2>
      {reviews && reviews.length > 0 ? (
        <div className="grid gap-3">
          {reviews.map((review, idx) => {
            const quote = typeof review === 'string' ? review : review?.quote || review?.summary || ''
            const reason = typeof review === 'string' ? '' : review?.reason || ''
            const source = typeof review === 'string' ? '' : review?.source || ''
            const reviewId = typeof review === 'string' ? '' : review?.review_id

            return (
              <div key={idx} className="rounded-lg border border-gray-200 dark:border-[#3a3a5e] bg-gray-50 dark:bg-[#2a2a3e] p-4">
                <div className="flex flex-wrap items-center gap-2 mb-2">
                  {source && (
                    <span className="text-[11px] font-bold px-2 py-0.5 rounded-full bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-300 border border-blue-200 dark:border-blue-700">
                      {source}
                    </span>
                  )}
                  {reviewId !== undefined && reviewId !== null && (
                    <span className="text-[11px] text-gray-400 dark:text-gray-500">
                      review_id: {reviewId}
                    </span>
                  )}
                </div>
                {quote && (
                  <p className="text-sm leading-relaxed text-gray-700 dark:text-[#cccccc]">
                    {quote}
                  </p>
                )}
                {reason && (
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">
                    {reason}
                  </p>
                )}
              </div>
            )
          })}
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

function GameDetailPage({ isDark, toggleDark }) {
  const { id } = useParams()
  const navigate = useNavigate()

  const [game, setGame] = useState(null)
  const [summary, setSummary] = useState(null)
  const [playtimeAnalysis, setPlaytimeAnalysis] = useState(null)
  const [criticSummary, setCriticSummary] = useState(null)
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
        const gamesRes = await fetch(`${API_BASE}/api/v1/games/`)
        if (gamesRes.ok) {
          const gamesData = await gamesRes.json()
          const found = gamesData.find(g => g.id === parseInt(id))
          setGame(found || null)
        }

        const summaryRes = await fetch(`${API_BASE}/api/v1/games/${id}/summary`)
        if (summaryRes.ok) {
          setSummary(await summaryRes.json())
        } else if (summaryRes.status === 404) {
          setError('아직 AI 요약본이 없습니다.')
        }

        const playtimeRes = await fetch(`${API_BASE}/api/v1/games/${id}/playtime-analysis`)
        if (playtimeRes.ok) {
          setPlaytimeAnalysis(await playtimeRes.json())
        }

        const criticRes = await fetch(`${API_BASE}/api/v1/games/${id}/critic-summary`)
        if (criticRes.ok) {
          setCriticSummary(await criticRes.json())
        }
      } catch {
        setError('서버에 연결할 수 없습니다.')
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, [id])

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

        <div className="absolute top-4 right-6 z-10">
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
                        emptyMessage="Steam 대표 리뷰가 없습니다."
                      />
                      <RepresentativeReviewSection
                        title="Metacritic 대표 리뷰"
                        reviews={groupedReviews.metacritic.slice(0, 3)}
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
              <div className="flex gap-4">
                {['early', 'mid', 'late'].map(bucket => (
                  <PlaytimeBucketCard
                    key={bucket}
                    bucket={bucket}
                    data={playtimeAnalysis.buckets?.[bucket]}
                  />
                ))}
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
      </div>
    </div>
  )
}

export default GameDetailPage

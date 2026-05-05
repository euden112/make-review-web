import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import Navbar from './Navbar'

const API_BASE = 'http://localhost:8000'

const GAME_META = {
  1: { rating: 4 },
  2: { rating: 5 },
  3: { rating: 3 },
  4: { rating: 5 },
  5: { rating: 3 },
}

const CATEGORY_LABELS = {
  graphics: '그래픽',
  controls: '조작감',
  optimization: '최적화',
  content: '콘텐츠 양',
  price_value: '가성비',
}

function GameDetailPage({ isDark, toggleDark }) {
  const { id } = useParams()
  const navigate = useNavigate()

  const [game, setGame] = useState(null)
  const [summary, setSummary] = useState(null)
  const [perspectives, setPerspectives] = useState([])
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
        // 게임 목록에서 해당 게임 정보 가져오기
        const gamesRes = await fetch(`${API_BASE}/api/v1/games/`)
        if (gamesRes.ok) {
          const gamesData = await gamesRes.json()
          const found = gamesData.find(g => g.id === parseInt(id))
          setGame(found || null)
        }

        // AI 요약 조회
        const summaryRes = await fetch(`${API_BASE}/api/v1/games/${id}/summary`)
        if (summaryRes.ok) {
          const summaryData = await summaryRes.json()
          setSummary(summaryData)
        } else if (summaryRes.status === 404) {
          setError('아직 AI 요약본이 없습니다.')
        }

        // 언어권별 요약 조회
        const perspRes = await fetch(`${API_BASE}/api/v1/games/${id}/perspectives`)
        if (perspRes.ok) {
          const perspData = await perspRes.json()
          setPerspectives(perspData)
        }
      } catch (e) {
        setError('서버에 연결할 수 없습니다.')
      } finally {
        setLoading(false)
      }
    }

    fetchData()
  }, [id])

  const meta = GAME_META[parseInt(id)] || { rating: 3 }

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

          <div className="flex items-center gap-2 mb-4">
            <span className="text-xl font-extrabold" style={{ color: '#ffb020' }}>
              {meta.rating}.0
            </span>
            <span className="text-white text-base">/ 5.0</span>
            <div className="flex gap-0.5">
              {[1, 2, 3, 4, 5].map((star) => (
                <span key={star} className="text-xl"
                  style={{ color: star <= meta.rating ? '#ffb020' : 'rgba(255,255,255,0.3)' }}
                >★</span>
              ))}
            </div>
          </div>

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
                  <span className={`text-sm font-bold px-3 py-1 rounded-full ${
                    summary.sentiment_overall === 'positive'
                      ? 'bg-green-50 text-green-600 dark:bg-green-900/20 dark:text-green-400'
                      : summary.sentiment_overall === 'negative'
                      ? 'bg-red-50 text-red-600 dark:bg-red-900/20 dark:text-red-400'
                      : 'bg-gray-50 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
                  }`}>
                    {summary.sentiment_overall === 'positive' ? '긍정적' : summary.sentiment_overall === 'negative' ? '부정적' : '중립'}
                  </span>
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

            {/* 언어권별 시각 */}
            {perspectives.length > 0 && (
              <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
                <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-4">언어권별 시각</h2>
                <div className="flex flex-col gap-4">
                  {perspectives.map((p, i) => (
                    <div key={i} className="border-l-2 border-blue-400 pl-4">
                      <span className="text-xs font-bold text-blue-500 uppercase">{p.review_language || p.language_code}</span>
                      <p className="text-sm text-gray-700 dark:text-[#cccccc] mt-1">{p.summary_text?.replace(/\*\*/g, '')}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

export default GameDetailPage
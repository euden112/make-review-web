import { useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import Navbar from './Navbar'

const MOCK_GAMES = [
  {
    id: 1,
    canonical_title: '엘든 링',
    cover_image: 'https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/library_600x900.jpg',
    hero_image: 'https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/header.jpg',
    description: 'AI 한줄 요약',
    rating: 5,
  },
  ...Array.from({ length: 17 }, (_, i) => ({
    id: i + 2,
    canonical_title: `게임 제목 ${i + 2}`,
    cover_image: null,
    hero_image: null,
    description: '게임에 대한 간단한 AI 요약 문구가 들어갈 자리입니다.',
    rating: 5,
  }))
]

function GameDetailPage({ isDark, toggleDark }) {
  const { id } = useParams()
  const navigate = useNavigate()
  const game = MOCK_GAMES.find((g) => g.id === parseInt(id))

  useEffect(() => {
    window.scrollTo(0, 0)
  }, [])

  if (!game) return <div className="p-10">게임을 찾을 수 없습니다.</div>

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-[#0f0f1a]">
      <Navbar isDark={isDark} toggleDark={toggleDark} />

      {/* 게임 배너 */}
      <section className="relative h-[440px] overflow-hidden flex gap-10 items-center px-12"
        style={{ background: 'linear-gradient(135deg, #0d2d63 0%, #1a1a2e 100%)' }}
      >
        {game.hero_image && (
          <img
            src={game.hero_image}
            alt=""
            className="absolute top-0 left-0 w-full h-full object-cover object-top opacity-50 z-0"
          />
        )}

        {/* 뒤로가기 */}
        <div className="absolute top-4 right-6 z-10">
          <span
            onClick={() => navigate('/')}
            className="text-white/70 text-xs cursor-pointer hover:text-white transition-colors"
          >
            ← 목록으로 돌아가기
          </span>
        </div>

        {/* 게임 커버 이미지 */}
        <div className="relative z-10 w-40 min-w-[160px] h-56 bg-[#2a2a3e] rounded-lg flex items-center justify-center shrink-0">
          {game.cover_image
            ? <img src={game.cover_image} alt={game.canonical_title} className="w-full h-full object-cover rounded-lg" />
            : <span className="text-gray-500 text-xs">No Image</span>
          }
        </div>

        {/* 게임 정보 */}
        <div className="relative z-10">
          <h1 className="text-white text-4xl font-extrabold mb-3">
            {game.canonical_title}
          </h1>

          <div className="flex items-center gap-2 mb-4">
            <span className="text-xl font-extrabold" style={{ color: '#ffb020' }}>
              {game.rating ? `${game.rating}.0` : '-'}
            </span>
            <span className="text-white text-base">/ 5.0</span>
            <div className="flex gap-0.5">
              {[1, 2, 3, 4, 5].map((star) => (
                <span key={star} className="text-xl"
                  style={{ color: game.rating && star <= game.rating ? '#ffb020' : 'rgba(255,255,255,0.3)' }}
                >★</span>
              ))}
            </div>
          </div>

          <p className="text-sm leading-relaxed mb-6 max-w-xl" style={{ color: '#e6edf8' }}>
            {game.description}
          </p>
        </div>
      </section>

      {/* 리뷰 요약 블록들 */}
      <div className="px-12 py-8 flex flex-col gap-6">

        {/* 블록 1 */}
        <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
          <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">예시 블록</h2>
          <div className="h-20 bg-gray-50 dark:bg-[#2a2a3e] rounded-lg" />
        </div>

        {/* 블록 2 */}
        <div className="grid grid-cols-2 gap-6">
          <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
            <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">예시 블록</h2>
            <div className="h-20 bg-gray-50 dark:bg-[#2a2a3e] rounded-lg" />
          </div>
          <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
            <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">예시 블록</h2>
            <div className="h-20 bg-gray-50 dark:bg-[#2a2a3e] rounded-lg" />
          </div>
        </div>

        {/* 블록 3 */}
        <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
          <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">예시 블록</h2>
          <div className="h-20 bg-gray-50 dark:bg-[#2a2a3e] rounded-lg" />
        </div>

        {/* 블록 4 */}
        <div className="bg-white dark:bg-[#1e1e2e] rounded-xl p-7 border border-gray-200 dark:border-[#2a2a3e] shadow-sm">
          <h2 className="text-sm font-bold text-gray-900 dark:text-[#e0e0e0] mb-3">예시 블록</h2>
          <div className="h-16 bg-gray-50 dark:bg-[#2a2a3e] rounded-lg" />
        </div>

      </div>
    </div>
  )
}

export default GameDetailPage
import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
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

const BANNERS = MOCK_GAMES.slice(0, 5)

function HeroBanner() {
  const [current, setCurrent] = useState(0)
  const navigate = useNavigate()

  useEffect(() => {
    const timer = setInterval(() => {
      setCurrent((prev) => (prev + 1) % BANNERS.length)
    }, 4000)
    return () => clearInterval(timer)
  }, [current])

  const banner = BANNERS[current]

  return (
    <section className="relative w-full h-[440px] overflow-hidden flex flex-col justify-start pt-10"
      style={{ background: 'linear-gradient(135deg, #0d2d63 0%, #1a1a2e 100%)' }}
    >
      {banner.hero_image && (
        <img
          key={banner.id}
          src={banner.hero_image}
          alt=""
          className="absolute top-0 left-0 w-full h-full object-cover object-top opacity-50 z-0"
        />
      )}

      {/* 배너 내용 */}
      <div className="px-12 max-w-2xl relative z-10">
        <div className="inline-block rounded-full px-3 py-1 text-xs font-bold mb-4"
          style={{ background: 'rgba(255,176,32,0.15)', color: '#ffb020', border: '1px solid rgba(255,176,32,0.35)' }}
        >
          추천 게임
        </div>

        <h1 className="text-5xl font-extrabold leading-tight tracking-tight m-0"
          style={{ color: '#e0e0e0' }}
        >
          {banner.canonical_title}
        </h1>

        <p className="mt-4 mb-5 text-base leading-relaxed" style={{ color: '#e6edf8' }}>
          {banner.description}
        </p>

        <div className="flex items-center gap-2 mt-28 mb-4">
          <span className="text-xl font-extrabold" style={{ color: '#ffb020' }}>
            {banner.rating ? `${banner.rating}.0` : '-'}
          </span>
          <span className="text-base text-white">/ 5.0</span>
          <div className="flex gap-0.5">
            {[1, 2, 3, 4, 5].map((star) => (
              <span key={star} className="text-xl"
                style={{ color: banner.rating && star <= banner.rating ? '#ffb020' : 'rgba(255,255,255,0.3)' }}
              >★</span>
            ))}
          </div>
        </div>

        <button
          onClick={() => navigate(`/games/${banner.id}`)}
          className="bg-blue-700 hover:bg-blue-800 text-white border-none rounded-lg px-5 py-3 text-sm font-bold cursor-pointer"
          style={{ color: '#e0e0e0' }}
        >
          AI 리뷰 요약 보기
        </button>
      </div>

      {/* 인디케이터 */}
      <div className="absolute bottom-5 left-1/2 -translate-x-1/2 flex gap-2 z-10">
        {BANNERS.map((_, i) => (
          <div
            key={i}
            onClick={() => setCurrent(i)}
            className="h-2 rounded-full cursor-pointer transition-all duration-300"
            style={{
              width: i === current ? '24px' : '8px',
              background: i === current ? '#e0e0e0' : 'rgba(255,255,255,0.35)',
            }}
          />
        ))}
      </div>
    </section>
  )
}

function StarRating({ rating }) {
  return (
    <div className="flex gap-0.5">
      {[1, 2, 3, 4, 5].map((star) => (
        <span key={star} className="text-sm"
          style={{ color: rating && star <= rating ? '#f5a623' : '#d9d9d9' }}
        >★</span>
      ))}
    </div>
  )
}

function GameCard({ game, onClick }) {
  const [hovered, setHovered] = useState(false)

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className={`bg-white dark:bg-[#1e1e2e] rounded-lg overflow-hidden border border-gray-200 dark:border-[#2a2a3e] flex flex-row h-36 cursor-pointer transition-all duration-200 ${hovered ? 'shadow-lg -translate-y-0.5' : 'shadow-sm'}`}
    >
      {/* 커버 이미지 */}
      <div className="w-24 min-w-[95px] bg-gray-100 dark:bg-[#2a2a3e] flex items-center justify-center">
        {game.cover_image ? (
          <img src={game.cover_image} alt={game.canonical_title} className="w-full h-full object-cover" />
        ) : (
          <span className="text-gray-400 text-xs">No Image</span>
        )}
      </div>

      {/* 카드 내용 */}
      <div className="p-3 flex flex-col justify-between flex-1 overflow-hidden">
        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between items-start gap-2">
            <h2 className="text-gray-900 dark:text-[#e0e0e0] text-sm font-bold m-0 truncate flex-1">
              {game.canonical_title}
            </h2>
            <div className="bg-[#f5a623] text-[#eeeeee] text-xs font-bold rounded px-1.5 py-0.5 min-w-[38px] text-center">
              {game.rating ? `${game.rating}.0` : '-'}
            </div>
          </div>

          <StarRating rating={game.rating} />

          <p className="text-xs leading-snug m-0 line-clamp-2 text-gray-500 dark:text-[#cccccc]">
            {game.description}
          </p>
        </div>

        <button
          onClick={() => onClick(game)}
          className={`text-[#e0e0e0] border-none rounded px-2.5 py-1.5 text-xs font-bold cursor-pointer transition-colors duration-200 self-start ${hovered ? 'bg-blue-700' : 'bg-blue-600'}`}
        >
          → AI 리뷰 요약 보기
        </button>
      </div>
    </div>
  )
}

function GameListPage({ isDark, toggleDark }) {
  const [games] = useState(MOCK_GAMES)
  const navigate = useNavigate()

  const handleCardClick = (game) => {
    navigate(`/games/${game.id}`)
  }

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-[#0f0f1a]">
      <Navbar isDark={isDark} toggleDark={toggleDark} />
      <HeroBanner />

      <div className="px-12 py-10">
        <h2 className="text-gray-900 dark:text-[#e0e0e0] text-2xl font-extrabold mb-6">
          전체 게임 리뷰
        </h2>

        <div className="grid grid-cols-3 gap-5">
          {games.map((game) => (
            <GameCard key={game.id} game={game} onClick={handleCardClick} />
          ))}
        </div>
      </div>
    </div>
  )
}

export default GameListPage
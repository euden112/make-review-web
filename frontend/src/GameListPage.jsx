import { useState, useEffect } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import Navbar from './Navbar'

const API_BASE = 'http://localhost:8000'

function HeroBanner({ games }) {
  const [current, setCurrent] = useState(0)
  const navigate = useNavigate()

  const banners = games.slice(0, 5)

  useEffect(() => {
    if (banners.length === 0) return
    const timer = setInterval(() => {
      setCurrent((prev) => (prev + 1) % banners.length)
    }, 4000)
    return () => clearInterval(timer)
  }, [banners.length])

  if (banners.length === 0) return (
    <section className="relative w-full h-[440px]"
      style={{ background: 'linear-gradient(135deg, #0d2d63 0%, #1a1a2e 100%)' }}
    />
  )

  const banner = banners[current]

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

      <div className="px-12 max-w-2xl relative z-10">
        <div className="inline-block rounded-full px-3 py-1 text-xs font-bold mb-4"
          style={{ background: 'rgba(255,176,32,0.15)', color: '#ffb020', border: '1px solid rgba(255,176,32,0.35)' }}
        >
          인기 게임
        </div>

        <h1 className="text-5xl font-extrabold leading-tight tracking-tight m-0"
          style={{ color: '#e0e0e0' }}
        >
          {banner.canonical_title}
        </h1>

        {banner.rating != null && (
          <div className="flex items-center gap-2 mt-28 mb-4">
            <span className="text-xl font-extrabold" style={{ color: '#ffb020' }}>
              {banner.rating.toFixed(1)}
            </span>
            <span className="text-base text-white">/ 5.0</span>
            <div className="flex gap-0.5">
              {[1, 2, 3, 4, 5].map((star) => (
                <span key={star} className="text-xl"
                  style={{ color: star <= Math.round(banner.rating) ? '#ffb020' : 'rgba(255,255,255,0.3)' }}
                >★</span>
              ))}
            </div>
          </div>
        )}

        <button
          onClick={() => navigate(`/games/${banner.id}`)}
          className="bg-blue-700 hover:bg-blue-800 text-white border-none rounded-lg px-5 py-3 text-sm font-bold cursor-pointer"
          style={{ color: '#e0e0e0' }}
        >
          AI 리뷰 요약 보기
        </button>
      </div>

      <div className="absolute bottom-5 left-1/2 -translate-x-1/2 flex gap-2 z-10">
        {banners.map((_, i) => (
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

function SearchBar({ searchText, setSearchText, selectedGenre, setSelectedGenre, sortOrder, setSortOrder, allGenres }) {
  const [genreOpen, setGenreOpen] = useState(false)
  const [sortOpen, setSortOpen] = useState(false)

  const sortLabels = { none: '정렬', high: '평점 높은 순', low: '평점 낮은 순' }

  const hasFilter = searchText || selectedGenre || sortOrder !== 'none'

  const resetAll = () => {
    setSearchText('')
    setSelectedGenre('')
    setSortOrder('none')
  }

  return (
    <div className="px-12 py-4 bg-white dark:bg-[#13131f] border-b border-gray-200 dark:border-[#2a2a3e]">
      <div className="flex items-center gap-3">
        <div className="flex-1 flex items-center gap-2 bg-gray-100 dark:bg-[#1e1e2e] border border-gray-200 dark:border-[#2a2a3e] rounded-lg px-4 py-2">
          <span className="text-gray-400 text-sm">🔍</span>
          <input
            type="text"
            placeholder="게임 제목 검색..."
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
            className="flex-1 bg-transparent outline-none text-sm text-gray-900 dark:text-[#e0e0e0] placeholder-gray-400"
          />
          {searchText && (
            <button onClick={() => setSearchText('')} className="text-gray-400 hover:text-gray-600 text-sm cursor-pointer border-none bg-transparent">✕</button>
          )}
        </div>

        <div className="relative">
          <button
            onClick={() => { setGenreOpen(p => !p); setSortOpen(false) }}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-bold cursor-pointer transition-colors ${
              selectedGenre
                ? 'bg-blue-600 text-white border-blue-600'
                : 'bg-white dark:bg-[#1e1e2e] text-gray-700 dark:text-[#e0e0e0] border-gray-200 dark:border-[#2a2a3e]'
            }`}
          >
            장르{selectedGenre ? `: ${selectedGenre}` : ''} <span className="text-xs">▼</span>
          </button>
          {genreOpen && (
            <div className="absolute top-full mt-1 left-0 bg-white dark:bg-[#1e1e2e] border border-gray-200 dark:border-[#2a2a3e] rounded-lg shadow-lg z-20 min-w-[120px] py-1">
              <button
                onClick={() => { setSelectedGenre(''); setGenreOpen(false) }}
                className="w-full text-left px-4 py-2 text-sm text-gray-700 dark:text-[#e0e0e0] hover:bg-gray-100 dark:hover:bg-[#2a2a3e] cursor-pointer border-none bg-transparent"
              >
                전체
              </button>
              {allGenres.map(genre => (
                <button
                  key={genre}
                  onClick={() => { setSelectedGenre(genre); setGenreOpen(false) }}
                  className={`w-full text-left px-4 py-2 text-sm cursor-pointer border-none bg-transparent ${
                    selectedGenre === genre
                      ? 'text-blue-600 font-bold'
                      : 'text-gray-700 dark:text-[#e0e0e0] hover:bg-gray-100 dark:hover:bg-[#2a2a3e]'
                  }`}
                >
                  {genre}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="relative">
          <button
            onClick={() => { setSortOpen(p => !p); setGenreOpen(false) }}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-bold cursor-pointer transition-colors ${
              sortOrder !== 'none'
                ? 'bg-blue-600 text-white border-blue-600'
                : 'bg-white dark:bg-[#1e1e2e] text-gray-700 dark:text-[#e0e0e0] border-gray-200 dark:border-[#2a2a3e]'
            }`}
          >
            {sortLabels[sortOrder]} <span className="text-xs">▼</span>
          </button>
          {sortOpen && (
            <div className="absolute top-full mt-1 right-0 bg-white dark:bg-[#1e1e2e] border border-gray-200 dark:border-[#2a2a3e] rounded-lg shadow-lg z-20 min-w-[140px] py-1">
              {Object.entries(sortLabels).map(([val, label]) => (
                <button
                  key={val}
                  onClick={() => { setSortOrder(val); setSortOpen(false) }}
                  className={`w-full text-left px-4 py-2 text-sm cursor-pointer border-none bg-transparent ${
                    sortOrder === val
                      ? 'text-blue-600 font-bold'
                      : 'text-gray-700 dark:text-[#e0e0e0] hover:bg-gray-100 dark:hover:bg-[#2a2a3e]'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 mt-3 flex-wrap min-h-[28px]">
        {hasFilter && (
          <>
            {searchText && (
              <span className="flex items-center gap-1 bg-blue-50 dark:bg-[#1a2a4a] text-blue-700 dark:text-blue-300 text-xs font-bold px-3 py-1 rounded-full border border-blue-200 dark:border-blue-700">
                "{searchText}"
                <button onClick={() => setSearchText('')} className="cursor-pointer border-none bg-transparent text-blue-400 hover:text-blue-600 ml-0.5">✕</button>
              </span>
            )}
            {selectedGenre && (
              <span className="flex items-center gap-1 bg-blue-50 dark:bg-[#1a2a4a] text-blue-700 dark:text-blue-300 text-xs font-bold px-3 py-1 rounded-full border border-blue-200 dark:border-blue-700">
                {selectedGenre}
                <button onClick={() => setSelectedGenre('')} className="cursor-pointer border-none bg-transparent text-blue-400 hover:text-blue-600 ml-0.5">✕</button>
              </span>
            )}
            {sortOrder !== 'none' && (
              <span className="flex items-center gap-1 bg-blue-50 dark:bg-[#1a2a4a] text-blue-700 dark:text-blue-300 text-xs font-bold px-3 py-1 rounded-full border border-blue-200 dark:border-blue-700">
                {sortOrder === 'high' ? '평점 높은 순' : '평점 낮은 순'}
                <button onClick={() => setSortOrder('none')} className="cursor-pointer border-none bg-transparent text-blue-400 hover:text-blue-600 ml-0.5">✕</button>
              </span>
            )}
            <button
              onClick={resetAll}
              className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 cursor-pointer border-none bg-transparent flex items-center gap-1"
            >
              🔄 필터 초기화
            </button>
          </>
        )}
      </div>
    </div>
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

function GameCard({ game, onClick, isSelected, onToggleCompare, compareDisabled, buySignal }) {
  const [hovered, setHovered] = useState(false)

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className={`bg-white dark:bg-[#1e1e2e] rounded-lg overflow-hidden border flex flex-row h-36 cursor-pointer transition-all duration-200 ${
        isSelected
          ? 'border-blue-500 shadow-md shadow-blue-500/20'
          : hovered ? 'border-gray-300 dark:border-[#3a3a5e] shadow-lg -translate-y-0.5' : 'border-gray-200 dark:border-[#2a2a3e] shadow-sm'
      }`}
    >
      <div className="w-24 min-w-[95px] bg-gray-100 dark:bg-[#2a2a3e] flex items-center justify-center relative">
        {game.cover_image ? (
          <img src={game.cover_image} alt={game.canonical_title} className="w-full h-full object-cover" />
        ) : (
          <span className="text-gray-400 text-xs">No Image</span>
        )}
        <button
          onClick={e => { e.stopPropagation(); onToggleCompare(game.id) }}
          disabled={compareDisabled && !isSelected}
          title={isSelected ? '비교에서 제거' : '비교에 추가'}
          className={`absolute top-1.5 right-1.5 w-5 h-5 rounded-full border-2 flex items-center justify-center text-[10px] font-black transition-colors cursor-pointer ${
            isSelected
              ? 'bg-blue-500 border-blue-500 text-white'
              : compareDisabled
                ? 'bg-gray-200 dark:bg-[#2a2a3e] border-gray-300 dark:border-[#3a3a5e] text-gray-400 cursor-not-allowed'
                : 'bg-white dark:bg-[#1e1e2e] border-gray-300 dark:border-[#3a3a5e] text-gray-400 hover:border-blue-400 hover:text-blue-400'
          }`}
        >
          {isSelected ? '✓' : '+'}
        </button>
      </div>

      <div className="p-3 flex flex-col justify-between flex-1 overflow-hidden">
        <div className="flex flex-col gap-1.5">
          <div className="flex justify-between items-start gap-2">
            <h2 className="text-gray-900 dark:text-[#e0e0e0] text-sm font-bold m-0 truncate flex-1">
              {game.canonical_title}
            </h2>
            <div className="flex items-center gap-1.5">
              {buySignal?.is_good_timing && (
                <span className="text-[10px] font-black px-1.5 py-0.5 rounded-full whitespace-nowrap"
                  style={{ background: 'rgba(34,197,94,0.15)', color: '#22c55e', border: '1px solid rgba(34,197,94,0.4)' }}>
                  ✦ 적기{buySignal.discount_percent > 0 ? ` -${buySignal.discount_percent}%` : ''}
                </span>
              )}
              {game.rating != null && (
                <div className="bg-[#f5a623] text-[#eeeeee] text-xs font-bold rounded px-1.5 py-0.5 min-w-[38px] text-center">
                  {game.rating.toFixed(1)}
                </div>
              )}
            </div>
          </div>

          <StarRating rating={game.rating != null ? Math.round(game.rating) : null} />

          <p className="text-xs leading-snug m-0 line-clamp-2 text-gray-500 dark:text-[#cccccc]">
            AI 리뷰 요약 보기
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
  const [games, setGames] = useState([])
  const [loading, setLoading] = useState(true)
  const [buySignals, setBuySignals] = useState({})
  const navigate = useNavigate()
  const location = useLocation()

  const [searchText, setSearchText] = useState('')
  const [selectedGenre, setSelectedGenre] = useState('')
  const [sortOrder, setSortOrder] = useState('none')
  const [compareIds, setCompareIds] = useState(
    location.state?.compareId ? [location.state.compareId] : []
  )

  const toggleCompare = (id) => {
    setCompareIds(prev =>
      prev.includes(id) ? prev.filter(x => x !== id) : prev.length < 2 ? [...prev, id] : prev
    )
  }

  useEffect(() => {
    fetch(`${API_BASE}/api/v1/games/`)
      .then(res => res.json())
      .then(data => {
        setGames(data)
        setLoading(false)
        // buy-signal 비동기 개별 조회 (non-blocking)
        data.forEach(game => {
          fetch(`${API_BASE}/api/v1/games/${game.id}/buy-signal`)
            .then(r => r.ok ? r.json() : null)
            .then(signal => {
              if (signal) setBuySignals(prev => ({ ...prev, [game.id]: signal }))
            })
            .catch(() => null)
        })
      })
      .catch(() => setLoading(false))
  }, [])

  const allGenres = [...new Set(
    games.flatMap(g => g.tags || [])
  )]

  const filteredGames = games
    .filter(g => g.canonical_title.toLowerCase().includes(searchText.toLowerCase()))
    .filter(g => !selectedGenre || (g.tags || []).includes(selectedGenre))
    .sort((a, b) => {
      const rA = a.rating ?? 0
      const rB = b.rating ?? 0
      if (sortOrder === 'high') return rB - rA
      if (sortOrder === 'low') return rA - rB
      return 0
    })

  const handleCardClick = (game) => navigate(`/games/${game.id}`)

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-[#0f0f1a]">
      <Navbar isDark={isDark} toggleDark={toggleDark} />
      <HeroBanner games={games} />
      <SearchBar
        searchText={searchText} setSearchText={setSearchText}
        selectedGenre={selectedGenre} setSelectedGenre={setSelectedGenre}
        sortOrder={sortOrder} setSortOrder={setSortOrder}
        allGenres={allGenres}
      />

      <div className="px-12 py-10 min-h-screen">
        <h2 className="text-gray-900 dark:text-[#e0e0e0] text-2xl font-extrabold mb-6">
          전체 게임 리뷰
          <span className="text-base font-normal text-gray-400 ml-3">{filteredGames.length}개</span>
        </h2>

        {loading ? (
          <div className="text-center py-20 text-gray-400">게임 목록 불러오는 중...</div>
        ) : (
          <div className="grid grid-cols-3 gap-5">
            {filteredGames.length > 0 ? (
              filteredGames.map((game) => (
                <GameCard
                  key={game.id}
                  game={game}
                  onClick={handleCardClick}
                  isSelected={compareIds.includes(game.id)}
                  onToggleCompare={toggleCompare}
                  compareDisabled={compareIds.length >= 2}
                  buySignal={buySignals[game.id]}
                />
              ))
            ) : (
              <div className="col-span-3 text-center py-20 text-gray-400">
                검색 결과가 없습니다.
              </div>
            )}
          </div>
        )}

        {/* 비교 플로팅 바 */}
        {compareIds.length > 0 && (
          <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-4 bg-white dark:bg-[#1e1e2e] border border-gray-200 dark:border-[#3a3a5e] shadow-xl rounded-2xl px-6 py-3">
            <span className="text-sm text-gray-600 dark:text-gray-300">
              {compareIds.length === 1
                ? '비교할 게임을 1개 더 선택하세요'
                : '2개 게임이 선택됨'}
            </span>
            <button
              onClick={() => setCompareIds([])}
              className="text-xs text-gray-400 hover:text-gray-600 bg-transparent border-none cursor-pointer"
            >
              취소
            </button>
            {compareIds.length === 2 && (
              <button
                onClick={() => navigate(`/compare?ids=${compareIds.join(',')}`)}
                className="bg-blue-600 hover:bg-blue-700 text-white text-sm font-bold px-4 py-1.5 rounded-lg border-none cursor-pointer transition-colors"
              >
                비교하기 →
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default GameListPage
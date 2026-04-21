import { useState, useEffect} from 'react'
import { useNavigate } from 'react-router-dom'
import Navbar from './Navbar'

// 임시 게임 데이터
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

// 메인 배너
const BANNERS = MOCK_GAMES.slice(0, 5)

function HeroBanner() {
  const [current, setCurrent] = useState(0)
  const navigate = useNavigate()

  // 4초마다 자동으로 다음 배너로
  useEffect(() => {
    const timer = setInterval(() => {
      setCurrent((prev) => (prev + 1) % BANNERS.length)
    }, 4000)
    return () => clearInterval(timer)
  }, [current])

  const banner = BANNERS[current]

  return (
    <section
      style={{
        position: 'relative',
        width: '100%',
        height: '440px',
        background: 'linear-gradient(135deg, #0d2d63 0%, #1a1a2e 100%)',
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'flex-start',
        paddingTop: '40px',
      }}
    >
      {banner.hero_image && (
        <img
          key={banner.id}
          src={banner.hero_image}
          alt=""
          style={{
            position: 'absolute',
            top: 0,
            left: 0,
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            objectPosition: 'top',
            opacity: 0.5,
            zIndex: 0,
          }}
        />
      )}

      {/* 배너 내용 */}
      <div style={{ padding: '0 48px', maxWidth: '700px', position: 'relative', zIndex: 1 }}>
        <div
          style={{
            display: 'inline-block',
            background: 'rgba(255, 176, 32, 0.15)',
            color: '#ffb020',
            border: '1px solid rgba(255, 176, 32, 0.35)',
            borderRadius: '20px',
            padding: '6px 12px',
            fontSize: '12px',
            fontWeight: '700',
            marginBottom: '18px',
          }}
        >
          추천 게임
        </div>

        <h1
          style={{
            margin: 0,
            color: '#e0e0e0',
            fontSize: '52px',
            fontWeight: '800',
            lineHeight: 1.1,
            letterSpacing: '-1px',
          }}
        >
          {banner.canonical_title}
        </h1>

        <p
          style={{
            marginTop: '16px',
            marginBottom: '22px',
            color: '#e6edf8',
            fontSize: '16px',
            lineHeight: 1.6,
          }}
        >
          {banner.description}
        </p>

        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginTop: '110px', marginBottom: '16px' }}>
        <span style={{ color: '#ffb020', fontSize: '20px', fontWeight: '800' }}>
        {banner.rating ? `${banner.rating}.0` : '-'}
        </span>
        <span style={{ color: '#ffffff', fontSize: '16px' }}>/ 5.0</span>
        <div style={{ display: 'flex', gap: '2px' }}>
        {[1, 2, 3, 4, 5].map((star) => (
        <span
        key={star}
        style={{
          fontSize: '20px',
          color: banner.rating && star <= banner.rating ? '#ffb020' : 'rgba(255,255,255,0.3)',
        }}
      >
        ★
      </span>
    ))}
  </div>
</div>

        <button
          onClick={() => navigate(`/games/${banner.id}`)}
          style={{
            background: '#1565d8',
            color: '#e0e0e0',
            border: 'none',
            borderRadius: '8px',
            padding: '12px 20px',
            fontSize: '14px',
            fontWeight: '700',
            cursor: 'pointer',
          }}
        >
          AI 리뷰 요약 보기
        </button>
      </div>

      {/* 하단 점 인디케이터 */}
      <div
        style={{
          position: 'absolute',
          bottom: '20px',
          left: '50%',
          transform: 'translateX(-50%)',
          display: 'flex',
          gap: '8px',
          zIndex: 1,
        }}
      >
        {BANNERS.map((_, i) => (
          <div
            key={i}
            onClick={() => setCurrent(i)}
            style={{
              width: i === current ? '24px' : '8px',
              height: '8px',
              borderRadius: '4px',
              background: i === current ? '#e0e0e0' : 'rgba(255,255,255,0.35)',
              cursor: 'pointer',
              transition: 'all 0.3s ease',
            }}
          />
        ))}
      </div>
    </section>
  )
}

// 별점
function StarRating({ rating }) {
  return (
    <div style={{ display: 'flex', gap: '2px' }}>
      {[1, 2, 3, 4, 5].map((star) => (
        <span
          key={star}
          style={{
            fontSize: '15px',
            color: rating && star <= rating ? '#f5a623' : '#d9d9d9',
          }}
        >
          ★
        </span>
      ))}
    </div>
  )
}

// 게임 카드
function GameCard({ game, onClick, isDark }) {
  const [hovered, setHovered] = useState(false)

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        background: isDark ? '#1e1e2e' : '#ffffff',
        borderRadius: '8px',
        overflow: 'hidden',
        border: isDark ? '1px solid #2a2a3e' : '1px solid #e5e7eb',
        boxShadow: hovered
          ? '0 8px 20px rgba(0,0,0,0.10)'
          : '0 2px 8px rgba(0,0,0,0.06)',
        transition: 'all 0.2s ease',
        transform: hovered ? 'translateY(-2px)' : 'translateY(0)',
        display: 'flex',
        flexDirection: 'row',
        height: '140px',
      }}
    >
      <div
        style={{
          width: '95px',
          minWidth: '95px',
          background: isDark ? '#2a2a3e' : '#f3f4f6',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        {game.cover_image ? (
          <img
            src={game.cover_image}
            alt={game.canonical_title}
            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
          />
        ) : (
          <span style={{ color: '#9ca3af', fontSize: '11px' }}>No Image</span>
        )}
      </div>

      <div
        style={{
          padding: '12px',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
          flexGrow: 1,
          overflow: 'hidden',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'flex-start',
              gap: '8px',
            }}
          >
            <h2
              style={{
                color: isDark ? '#e0e0e0' : '#111827',
                fontSize: '15px',
                fontWeight: '700',
                margin: 0,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                flex: 1,
              }}
            >
              {game.canonical_title}
            </h2>

            <div
              style={{
                background: '#f5a623',
                color: '#eeeeee',
                fontSize: '12px',
                fontWeight: '700',
                borderRadius: '4px',
                padding: '3px 7px',
                minWidth: '38px',
                textAlign: 'center',
              }}
            >
              {game.rating ? `${game.rating}.0` : '-'}
            </div>
          </div>

          <StarRating rating={game.rating} />

          <p
            style={{
              color: isDark ? '#cccccc' : '#4b5563',
              fontSize: '11px',
              lineHeight: '1.4',
              margin: 0,
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            }}
          >
            {game.description}
          </p>
        </div>

        <button
          onClick={() => onClick(game)}
          style={{
            background: hovered ? '#0b5ed7' : '#1565d8',
            color: '#e0e0e0',
            border: 'none',
            borderRadius: '4px',
            padding: '6px 10px',
            fontSize: '11px',
            fontWeight: '700',
            cursor: 'pointer',
            transition: 'background 0.2s ease',
            alignSelf: 'flex-start',
          }}
        >
          → AI 리뷰 요약 보기
        </button>
      </div>
    </div>
  )
}

// 메인 페이지
function GameListPage({ isDark, toggleDark }) {
  const [games] = useState(MOCK_GAMES)
  const navigate = useNavigate()

  const handleCardClick = (game) => {
    navigate(`/games/${game.id}`)
  }

  return (
    <div style={{ minHeight: '100vh', background: isDark ? '#0f0f1a' : '#f5f6f8' }}>
      <Navbar isDark={isDark} toggleDark={toggleDark} />
      <HeroBanner />

      <div style={{ padding: '40px 48px' }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: '24px',
          }}
        >
          <h2
            style={{
              color: isDark ? '#e0e0e0' : '#111111',
              fontSize: '28px',
              fontWeight: '800',
              margin: 0,
            }}
          >
            전체 게임 리뷰
          </h2>

        </div>

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
            gap: '18px',
          }}
        >
          {games.map((game) => (
            <GameCard key={game.id} game={game} onClick={handleCardClick} isDark={isDark} />
          ))}
        </div>
      </div>
    </div>
  )
}

export default GameListPage
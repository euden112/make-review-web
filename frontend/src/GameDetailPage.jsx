import { useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
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

function GameDetailPage({ isDark, toggleDark }) {
  const { id } = useParams()
  const navigate = useNavigate()
  const game = MOCK_GAMES.find((g) => g.id === parseInt(id))

  useEffect(() => {
    window.scrollTo(0, 0)
  }, [])

  if (!game) return <div style={{ padding: '40px' }}>게임을 찾을 수 없습니다.</div>

  return (
    <div style={{ minHeight: '100vh', background: isDark ? '#0f0f1a' : '#f5f6f8' }}>

       <Navbar isDark={isDark} toggleDark={toggleDark} />

      {/* 게임 배너 */}
      <section style={{
        position: 'relative',
        height: '440px',
        background: 'linear-gradient(135deg, #0d2d63 0%, #1a1a2e 100%)',
        overflow: 'hidden',
        padding: '48px',
        display: 'flex',
        gap: '40px',
        alignItems: 'center',
      }}>
        {game.hero_image && (
          <img
            src={game.hero_image}
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
        
        {/* 뒤로가기 */}
        <div style={{ position: 'absolute', top: '16px', right: '24px' }}>
          <span
            onClick={() => navigate('/')}
            style={{ color: 'rgba(255,255,255,0.7)', fontSize: '13px', cursor: 'pointer' }}
          >
            ← 목록으로 돌아가기
          </span>
        </div>

        {/* 게임 커버 이미지 */}
        <div style={{
          position: 'relative',
          zIndex: 1,
          width: '160px',
          minWidth: '160px',
          height: '220px',
          background: '#2a2a3e',
          borderRadius: '8px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}>
          {game.cover_image
            ? <img src={game.cover_image} alt={game.canonical_title} style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: '8px' }} />
            : <span style={{ color: '#555', fontSize: '12px' }}>No Image</span>
          }
        </div>

        {/* 게임 정보 */}
        <div style={{ position: 'relative', zIndex: 1 }}>
          <h1 style={{ color: '#ffffff', fontSize: '42px', fontWeight: '800', margin: '0 0 12px' }}>
            {game.canonical_title}
          </h1>

          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '16px' }}>
            <span style={{ color: '#ffb020', fontSize: '22px', fontWeight: '800' }}>
              {game.rating ? `${game.rating}.0` : '-'}
            </span>
            <span style={{ color: '#ffffff', fontSize: '16px' }}>/ 5.0</span>
            <div style={{ display: 'flex', gap: '2px' }}>
              {[1, 2, 3, 4, 5].map((star) => (
                <span key={star} style={{
                  fontSize: '22px',
                  color: game.rating && star <= game.rating ? '#ffb020' : 'rgba(255,255,255,0.3)',
                }}>★</span>
              ))}
            </div>
          </div>

          <p style={{ color: '#e6edf8', fontSize: '15px', lineHeight: 1.6, marginBottom: '24px', maxWidth: '600px' }}>
            {game.description}
          </p>

        </div>
      </section>

      {/* 리뷰 요약 블록들 */}
      <div style={{ padding: '32px 48px', display: 'flex', flexDirection: 'column', gap: '24px' }}>

        {/* 블록 1 */}
        <div style={{
          background: isDark ? '#1e1e2e' : '#ffffff',
          borderRadius: '12px',
          padding: '28px',
          border: isDark ? '1px solid #2a2a3e' : '1px solid #e5e7eb',
          boxShadow: '0 2px 8px rgba(0,0,0,0.06)',
        }}>
          <h2 style={{ fontSize: '16px', fontWeight: '700', color: isDark ? '#e0e0e0' : '#111827', marginBottom: '12px' }}>
            예시 블록
          </h2>
          <div style={{ height: '80px', background: isDark ? '#2a2a3e' : '#f9fafb', borderRadius: '8px' }} />
        </div>

        {/* 블록 2 */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '24px' }}>
          <div style={{
            background: isDark ? '#1e1e2e' : '#ffffff',
            borderRadius: '12px',
            padding: '28px',
            border: isDark ? '1px solid #2a2a3e' : '1px solid #e5e7eb',
            boxShadow: '0 2px 8px rgba(0,0,0,0.06)',
          }}>
            <h2 style={{ fontSize: '16px', fontWeight: '700', color: isDark ? '#e0e0e0' : '#111827', marginBottom: '12px' }}>
              예시 블록
            </h2>
            <div style={{ height: '80px', background: isDark ? '#2a2a3e' : '#f9fafb', borderRadius: '8px' }} />
          </div>

          <div style={{
            background: isDark ? '#1e1e2e' : '#ffffff',
            borderRadius: '12px',
            padding: '28px',
            border: isDark ? '1px solid #2a2a3e' : '1px solid #e5e7eb',
            boxShadow: '0 2px 8px rgba(0,0,0,0.06)',
          }}>
            <h2 style={{ fontSize: '16px', fontWeight: '700', color: isDark ? '#e0e0e0' : '#111827', marginBottom: '12px' }}>
              예시 블록
            </h2>
            <div style={{ height: '80px', background: isDark ? '#2a2a3e' : '#f9fafb', borderRadius: '8px' }} />
          </div>
        </div>

        {/* 블록 3 */}
        <div style={{
          background: isDark ? '#1e1e2e' : '#ffffff',
          borderRadius: '12px',
          padding: '28px',
          border: isDark ? '1px solid #2a2a3e' : '1px solid #e5e7eb',
          boxShadow: '0 2px 8px rgba(0,0,0,0.06)',
        }}>
          <h2 style={{ fontSize: '16px', fontWeight: '700', color: isDark ? '#e0e0e0' : '#111827', marginBottom: '12px' }}>
            예시 블록
          </h2>
          <div style={{ height: '80px', background: isDark ? '#2a2a3e' : '#f9fafb', borderRadius: '8px' }} />
        </div>

        {/* 블록 4 */}
        <div style={{
          background: isDark ? '#1e1e2e' : '#ffffff',
          borderRadius: '12px',
          padding: '28px',
          border: isDark ? '1px solid #2a2a3e' : '1px solid #e5e7eb',
          boxShadow: '0 2px 8px rgba(0,0,0,0.06)',
        }}>
          <h2 style={{ fontSize: '16px', fontWeight: '700', color: isDark ? '#e0e0e0' : '#111827', marginBottom: '12px' }}>
            예시 블록
          </h2>
          <div style={{ height: '60px', background: isDark ? '#2a2a3e' : '#f9fafb', borderRadius: '8px' }} />
        </div>

      </div>
    </div>
  )
}

export default GameDetailPage
import { useNavigate } from 'react-router-dom'

function Navbar({ isDark, toggleDark }) {
  const navigate = useNavigate()

  return (
    <nav
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 100,
        background: isDark ? '#1a1a2e' : '#ffffff',
        borderBottom: isDark ? '1px solid #2a2a3e' : '1px solid #e5e7eb',
        padding: '0 48px',
        height: '64px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}
    >
      <div
        onClick={() => navigate('/')}
        style={{
          color: isDark ? '#e0e0e0' : '#111827',
          fontSize: '24px',
          fontWeight: '700',
          letterSpacing: '-0.5px',
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          gap: '10px',
        }}
      >
        <span>🎮</span>
        <span>게임 리뷰</span>
      </div>

      {/* 다크모드 토글 버튼 */}
      <button
        onClick={toggleDark}
        style={{
          background: isDark ? '#2a2a3e' : '#f3f4f6',
          border: 'none',
          borderRadius: '8px',
          padding: '8px 12px',
          cursor: 'pointer',
          fontSize: '18px',
        }}
      >
        {isDark ? '☀️' : '🌙'}
      </button>
    </nav>
  )
}

export default Navbar
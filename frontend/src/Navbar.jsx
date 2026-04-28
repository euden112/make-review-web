import { useNavigate } from 'react-router-dom'

function Navbar({ isDark, toggleDark }) {
  const navigate = useNavigate()

  return (
    <nav className="sticky top-0 z-50 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 px-12 h-16 flex items-center justify-between">
      <div
        onClick={() => navigate('/')}
        className="text-gray-900 dark:text-gray-100 text-2xl font-bold cursor-pointer flex items-center gap-2"
      >
        <span></span>
        <span>게임 리뷰</span>
      </div>

      <button
        onClick={toggleDark}
        className="bg-gray-100 dark:bg-gray-700 border-none rounded-lg px-3 py-2 cursor-pointer text-lg"
      >
        {isDark ? '☀️' : '🌙'}
      </button>
    </nav>
  )
}

export default Navbar
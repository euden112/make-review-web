import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import GameListPage from './GameListPage'
import GameDetailPage from './GameDetailPage'

function App() {
  const [isDark, setIsDark] = useState(
    localStorage.getItem('darkMode') === 'true'
  )

  useEffect(() => {
    if (isDark) {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }, [isDark])

  const toggleDark = () => {
    const next = !isDark
    setIsDark(next)
    localStorage.setItem('darkMode', next)
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<GameListPage isDark={isDark} toggleDark={toggleDark} />} />
        <Route path="/games/:id" element={<GameDetailPage isDark={isDark} toggleDark={toggleDark} />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
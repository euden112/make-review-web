import { BrowserRouter, Routes, Route } from 'react-router-dom'
import GameListPage from './GameListPage'
import GameDetailPage from './GameDetailPage'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<GameListPage />} />
        <Route path="/games/:id" element={<GameDetailPage />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
import { useState, useRef, useEffect } from 'react'

const API_BASE = 'http://localhost:8000'

const WELCOME_MESSAGE = {
  role: 'assistant',
  content: '안녕하세요! 게임 추천 챗봇입니다 🎮\n\n좋아하는 게임과 싫어하는 게임을 알려주시면 데이터베이스에 있는 게임 중에서 딱 맞는 게임을 추천해 드릴게요!\n\n예시: "저는 다크소울을 좋아하고 리듬게임은 싫어해요"',
}

export default function ChatBot({ isDark }) {
  const [isOpen, setIsOpen] = useState(false)
  const [messages, setMessages] = useState([WELCOME_MESSAGE])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef(null)

  useEffect(() => {
    if (isOpen) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, isOpen])

  const sendMessage = async () => {
    const text = input.trim()
    if (!text || loading) return

    const userMessage = { role: 'user', content: text }
    const nextMessages = [...messages, userMessage]
    setMessages(nextMessages)
    setInput('')
    setLoading(true)

    try {
      const res = await fetch(`${API_BASE}/api/v1/chat/recommend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: nextMessages
            .filter((m) => m !== WELCOME_MESSAGE)
            .map((m) => ({ role: m.role, content: m.content })),
        }),
      })

      if (!res.ok) {
        throw new Error(`서버 오류: ${res.status}`)
      }

      const data = await res.json()
      setMessages((prev) => [...prev, { role: 'assistant', content: data.reply }])
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `오류가 발생했습니다: ${err.message}` },
      ])
    } finally {
      setLoading(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  const resetChat = () => {
    setMessages([WELCOME_MESSAGE])
    setInput('')
  }

  const bg = isDark ? 'bg-gray-900 border-gray-700' : 'bg-white border-gray-200'
  const headerBg = isDark ? 'bg-gray-800' : 'bg-blue-600'
  const msgBg = isDark ? 'bg-gray-800' : 'bg-gray-50'
  const inputBg = isDark ? 'bg-gray-800 border-gray-600 text-white placeholder-gray-400' : 'bg-white border-gray-300 text-gray-900 placeholder-gray-500'
  const assistantBubble = isDark ? 'bg-gray-700 text-gray-100' : 'bg-blue-50 text-gray-800'
  const userBubble = 'bg-blue-600 text-white'

  return (
    <>
      {/* 플로팅 버튼 */}
      <button
        onClick={() => setIsOpen((v) => !v)}
        className="fixed bottom-6 right-6 z-50 w-14 h-14 rounded-full bg-blue-600 hover:bg-blue-700 text-white shadow-lg flex items-center justify-center text-2xl transition-transform hover:scale-110"
        title="게임 추천 챗봇"
      >
        {isOpen ? '✕' : '🎮'}
      </button>

      {/* 채팅창 */}
      {isOpen && (
        <div
          className={`fixed bottom-24 right-6 z-50 w-80 sm:w-96 rounded-2xl shadow-2xl border flex flex-col overflow-hidden ${bg}`}
          style={{ height: '520px' }}
        >
          {/* 헤더 */}
          <div className={`${headerBg} px-4 py-3 flex items-center justify-between`}>
            <div className="flex items-center gap-2">
              <span className="text-lg">🎮</span>
              <span className="text-white font-semibold text-sm">게임 추천 챗봇</span>
            </div>
            <button
              onClick={resetChat}
              className="text-white/70 hover:text-white text-xs px-2 py-1 rounded hover:bg-white/10 transition-colors"
              title="대화 초기화"
            >
              초기화
            </button>
          </div>

          {/* 메시지 영역 */}
          <div className={`flex-1 overflow-y-auto p-3 space-y-3 ${msgBg}`}>
            {messages.map((msg, i) => (
              <div
                key={i}
                className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap leading-relaxed ${
                    msg.role === 'user' ? userBubble : assistantBubble
                  } ${msg.role === 'assistant' ? 'rounded-tl-sm' : 'rounded-tr-sm'}`}
                >
                  {msg.content}
                </div>
              </div>
            ))}
            {loading && (
              <div className="flex justify-start">
                <div className={`${assistantBubble} rounded-2xl rounded-tl-sm px-4 py-2 text-sm`}>
                  <span className="animate-pulse">추천 중...</span>
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* 입력 영역 */}
          <div className={`p-3 border-t ${isDark ? 'border-gray-700' : 'border-gray-200'}`}>
            <div className="flex gap-2">
              <textarea
                rows={2}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="좋아하는/싫어하는 게임을 입력하세요..."
                className={`flex-1 resize-none rounded-xl border px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500 ${inputBg}`}
                disabled={loading}
              />
              <button
                onClick={sendMessage}
                disabled={loading || !input.trim()}
                className="self-end px-3 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-white rounded-xl text-sm font-medium transition-colors"
              >
                전송
              </button>
            </div>
            <p className={`text-xs mt-1 ${isDark ? 'text-gray-500' : 'text-gray-400'}`}>
              Enter로 전송 · Shift+Enter로 줄바꿈
            </p>
          </div>
        </div>
      )}
    </>
  )
}

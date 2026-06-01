const STAR_GAP = 2
// 좌우 대칭(중심 x=12, viewBox 24) SVG 별. 50% 클리핑 시 정확히 시각적 중앙에서 잘린다.
// 안쪽 반지름 비율 0.382(황금비)로 꼭지가 뾰족한 전형적인 5각 별.
const STAR_PATH = "M12 1 L14.47 8.6 L22.46 8.6 L16 13.3 L18.47 20.9 L12 16.2 L5.53 20.9 L8 13.3 L1.54 8.6 L9.53 8.6 Z"

// 별을 하나씩 개별 클리핑한다. 각 별은 자기 너비 기준으로
// (rating - index)만큼만 칠해진다. 4.2 → 4개 꽉 + 5번째 20%, 4.5 → 5번째 정확히 절반.
function StarBar({ rating, size = 14, fillColor = '#f5a623', emptyColor = '#d9d9d9' }) {
  return (
    <div style={{ display: 'inline-flex', gap: `${STAR_GAP}px` }}>
      {[0, 1, 2, 3, 4].map(i => {
        const frac = rating == null ? 0 : Math.max(0, Math.min(1, rating - i))
        return (
          <span key={i} style={{
            position: 'relative', display: 'inline-block',
            width: `${size}px`, height: `${size}px`, lineHeight: 0,
          }}>
            <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: 'block' }}>
              <path d={STAR_PATH} fill={emptyColor} />
            </svg>
            <span style={{
              position: 'absolute', top: 0, left: 0,
              width: `${frac * 100}%`, height: '100%', overflow: 'hidden',
            }}>
              <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: 'block' }}>
                <path d={STAR_PATH} fill={fillColor} />
              </svg>
            </span>
          </span>
        )
      })}
    </div>
  )
}

export default StarBar

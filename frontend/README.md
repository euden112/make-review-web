# 게임 리뷰 - Frontend

React + Vite 기반 프론트엔드입니다. nginx 정적 서빙 환경에서는 `/api`를 백엔드로 프록시하고, 로컬 개발 서버에서는 `VITE_API_BASE`로 백엔드 주소를 지정할 수 있습니다.

## 구현 사항

- 게임 목록 페이지
  - 히어로 배너, 게임 카드 그리드, Steam 인기 태그 기반 장르 필터
  - 평점 정렬과 검색
  - 게임별 구매 타이밍 시그널 요약 표시
- 게임 상세 페이지
  - 유저 요약, 평론가 요약, 플레이타임 구간별 요약
  - 공통 5축 카테고리 레이더: 콘텐츠/볼륨, 재미, 그래픽, 조작감, 최적화
    - 면적은 0~10 항목 점수, 색·라벨은 점수 기반 9밴드
    - 대표 강점/약점 캡션은 리뷰 언급량·긍정률 기반 `relative_label`
  - 눈에 띄는 반응: 스토리/캐릭터, 난이도, 음향, 가성비 중 근거가 충분하고 강·약점 또는 언급 비중이 두드러진 항목만 표시
  - 장단점, 리뷰 토픽, 대표 리뷰, 추천 대상, 구매 시그널
- 게임 비교 페이지
- 챗봇 추천 UI
- 다크모드 지원(localStorage 저장)
- 공통 Navbar 컴포넌트

## 기술 스택

- React + Vite
- Tailwind CSS v3
- nginx 정적 서빙(Docker 배포)

## 로컬 실행

```bash
cd frontend
npm install
npm run dev
```

기본 개발 서버는 `http://localhost:5173`입니다.

백엔드가 같은 origin이 아니면 `.env` 또는 실행 환경에 다음 값을 지정합니다.

```env
VITE_API_BASE=http://localhost:8000
```

Docker Compose로 전체 스택을 실행할 때는 `VITE_API_BASE`를 비워 두고 nginx의 `/api` 프록시를 사용합니다.

## 배포

Docker 배포는 루트의 `docker-compose.yml`에 포함된 `frontend` 서비스가 담당합니다.

```bash
docker compose up -d --build frontend
```

Vercel에 단독 배포할 때는 Root Directory를 `frontend`로 지정하고, 백엔드 주소를 `VITE_API_BASE`에 설정합니다.

현재 배포 URL:

```text
https://make-review-web.vercel.app/
```

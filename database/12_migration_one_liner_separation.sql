-- Migration 12: game_review_summaries.one_liner 컬럼 추가
-- 배경: 기존엔 summary_text 안에 "**한줄평**\n\n본문" 형태로 합쳐 저장 →
--       UI가 split('\n')[0]로 재추출하는 헐거운 의존이 있었고, 동일 한줄평이
--       본문 머리에 echo되면 시각적 중복이 발생.
-- 조치: one_liner를 별도 NULL 허용 컬럼으로 분리. summary_text는 본문만 보관.
--       기존 행은 one_liner=NULL → API/프론트는 split fallback으로 호환 유지.

ALTER TABLE game_review_summaries
    ADD COLUMN IF NOT EXISTS one_liner TEXT;

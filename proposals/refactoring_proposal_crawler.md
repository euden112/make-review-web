# Crawler 파트 리팩터링 제안서

## 문서 목적
아키텍처 리뷰 결과를 바탕으로 Crawler 파트에서 우선 반영이 필요한 개선사항을 정리합니다.
본 문서는 왜 수정이 필요한지(리스크)와 어떻게 수정해야 하는지(권장 조치)를 빠르게 전달하기 위한 실행 문서입니다.

## 1) [공통 연계] 파일명 및 연동 인터페이스 표준화

### 리스크
- 산출물 파일명이 일관되지 않으면 Backend 적재 파이프라인에서 파일 탐지 누락/오탐이 발생할 수 있습니다.
- 운영 장애 발생 시 어떤 실행(run)의 결과물인지 추적이 어려워집니다.

### 권장 조치 (Crawler)
- 출력 파일명을 아래 규칙으로 통일합니다.
  - `{platform_code}_reviews_raw_{timestamp}.json`
  - 예: `steam_reviews_raw_20260414T103000Z.json`
- timestamp는 UTC `YYYYMMDDTHHMMSSZ` 포맷으로 고정합니다.
- 파일 생성 시 메타데이터를 함께 포함합니다.
  - `platform_code`, `collected_at`, `schema_version`, `record_count`

## 2) [협업] 무거운 NLP 모델 책임 분리

### 리스크
- `steam_crawler.py` 내 `SentenceTransformer` 추론이 크롤링 I/O와 결합되어 수집 시간 증가 및 장애 전파가 발생합니다.
- 모델 다운로드/초기화 이슈가 전체 크롤링 실패로 이어질 수 있습니다.

### 권장 조치 (Crawler)
- Crawler는 1단계 규칙 기반 필터만 수행합니다.
- `SentenceTransformer` 기반 분류 책임은 Backend 비동기 워커로 이관합니다.
- Crawler 산출물은 Raw 중심으로 전달하고, NLP 후처리는 Backend 파이프라인에서 수행합니다.

## 3) [Crawler 핵심] Steam API Rate Limit(429) 대응 견고화

### 리스크
- 고정 대기(`time.sleep(1.0)`)는 혼잡 구간에서 실패를 반복할 수 있고, 비혼잡 구간에서는 처리량을 과도하게 제한합니다.

### 권장 조치 (Crawler)
- Exponential Backoff + Jitter를 적용합니다.
- 기본 정책 예시:
  - 최대 재시도 5회
  - 대기: `base * 2^attempt + random_jitter`
  - 상한(cap): 30초
- `Retry-After` 헤더가 있으면 우선 반영합니다.

## 4) [Crawler 핵심] 리뷰 필터링 과차단 완화

### 4-1. [심각] 원문 보존 원칙 적용

리스크:
- `preprocess_body`에서 500자 초과 본문을 절단하면 필터링 전 문맥이 훼손되어 정상 장문 리뷰가 유실될 수 있습니다.

권장 조치:
- 필터링 단계에서는 원문 전체를 유지합니다.
- 길이 제한은 저장 직전(DB 정책) 또는 노출 직전(프론트 정책)으로 후행 분리합니다.

제거 영향 평가:
- 데이터 품질: 개선 효과가 큽니다. 장문 리뷰의 핵심 문맥 보존으로 오탐/누락을 줄일 수 있습니다.
- 크롤링 속도: 소폭 저하 가능성이 있습니다. 특히 크롤러 내부에서 언어 감지/임베딩 분류를 계속 수행할 경우 텍스트 길이에 비례해 CPU 사용량이 증가합니다.
- 토큰 비용: 크롤러 자체의 직접 토큰 비용 증가는 거의 없지만, 후속 AI 요약 파이프라인 입력 길이가 늘어 전체 토큰 사용량이 증가할 수 있습니다.
- 저장/운영 비용: JSON 파일 크기와 저장소 사용량이 증가할 수 있습니다.

운영 권장안(균형 전략):
1. 크롤링 단계에서는 500자 절단을 제거하고 원문을 보존합니다.
2. 비정상 초장문 방어를 위해 안전 상한(예: 8000~12000자)을 별도 적용합니다.
3. AI 입력 전처리 단계에서 문장 단위 압축/발췌를 적용해 토큰 비용을 제어합니다.
4. 변경 전후 지표를 비교합니다.
  - 평균 본문 길이, 필터 탈락률, 수집 건수, AI 입력 토큰량, 요약 품질 샘플 점검 결과

간단 수도 코드:
```python
cleaned = normalize_text(raw_text)
if len(cleaned) < MIN_BODY_LENGTH:
    reject("too_short")
# 긴 본문도 필터 단계에서는 절단하지 않음
```

### 4-2. [높음] UNIQUE_RATIO 조건 완화

리스크:
- `UNIQUE_RATIO < 0.4`를 장문에도 동일 적용하면 감정 강조 반복이 있는 정상 리뷰 오탐 가능성이 큽니다.

권장 조치:
- 본문이 특정 길이(예: 400자) 초과 시 UNIQUE_RATIO 검사를 우회합니다.

간단 수도 코드:
```python
if len(text) <= 400:
    if unique_ratio < 0.4:
        reject("word_repetition")
else:
    log_info("unique_ratio_bypassed", text_len=len(text))
```

### 4-3. [보통] General 카테고리 도입

리스크:
- 카테고리 미매칭 리뷰를 즉시 폐기하면 유효 리뷰(새 이슈, 복합 의견)가 손실됩니다.

권장 조치:
- 1, 2단계를 통과한 리뷰는 미매칭 시에도 `General`로 보존합니다.

간단 수도 코드:
```python
matched = match_categories(text)
if not matched:
    matched = ["General"]
return pass_result(categories=matched)
```

## 완료 기준(DoD)
- 출력 파일명 규칙 준수율 100%
- 429 재시도 정책 반영 및 실패율 모니터링 가능
- 필터링 단계에서 원문 절단 제거
- 장문 UNIQUE_RATIO 예외 적용
- 카테고리 미매칭 리뷰의 `General` 보존 적용
# Crawler 파트 리팩터링 제안서

## 문서 목적
아키텍처 리뷰 결과를 바탕으로 Crawler 파트에서 우선 반영이 필요한 개선사항을 정리합니다.
본 문서는 왜 수정이 필요한지(리스크)와 어떻게 수정해야 하는지(권장 조치)를 빠르게 전달하기 위한 실행 문서입니다.

## 1) [공통 연계] 파일명 및 연동 인터페이스 표준화

### 리스크
- 산출물 파일명이 일관되지 않으면 Backend 적재 파이프라인에서 파일 탐지 누락/오탐이 발생할 수 있습니다.
- 운영 장애 발생 시 어떤 실행(run)의 결과물인지 추적이 어려워집니다.

### 권장 조치 (Crawler)
- 출력 파일명을 아래 규칙으로 통일합니다.
  - `{platform_code}_reviews_raw_{timestamp}.json`
  - 예: `steam_reviews_raw_20260414T103000Z.json`
- timestamp는 UTC `YYYYMMDDTHHMMSSZ` 포맷으로 고정합니다.
- 파일 생성 시 메타데이터를 함께 포함합니다.
  - `platform_code`, `collected_at`, `schema_version`, `record_count`

## 2) [협업] 무거운 NLP 모델 책임 분리

### 리스크
- `steam_crawler.py` 내 `SentenceTransformer` 추론이 크롤링 I/O와 결합되어 수집 시간 증가 및 장애 전파가 발생합니다.
- 모델 다운로드/초기화 이슈가 전체 크롤링 실패로 이어질 수 있습니다.

### 권장 조치 (Crawler)
- Crawler는 1단계 규칙 기반 필터만 수행합니다.
- `SentenceTransformer` 기반 분류 책임은 Backend 비동기 워커로 이관합니다.
- Crawler 산출물은 Raw 중심으로 전달하고, NLP 후처리는 Backend 파이프라인에서 수행합니다.

## 3) [Crawler 핵심] Steam API Rate Limit(429) 대응 견고화

### 리스크
- 고정 대기(`time.sleep(1.0)`)는 혼잡 구간에서 실패를 반복할 수 있고, 비혼잡 구간에서는 처리량을 과도하게 제한합니다.

### 권장 조치 (Crawler)
- Exponential Backoff + Jitter를 적용합니다.
- 기본 정책 예시:
  - 최대 재시도 5회
  - 대기: `base * 2^attempt + random_jitter`
  - 상한(cap): 30초
- `Retry-After` 헤더가 있으면 우선 반영합니다.

## 4) [Crawler 핵심] 리뷰 필터링 과차단 완화

### 4-1. [심각] 원문 보존 원칙 적용

리스크:
- `preprocess_body`에서 500자 초과 본문을 절단하면 필터링 전 문맥이 훼손되어 정상 장문 리뷰가 유실될 수 있습니다.

권장 조치:
- 필터링 단계에서는 원문 전체를 유지합니다.
- 길이 제한은 저장 직전(DB 정책) 또는 노출 직전(프론트 정책)으로 후행 분리합니다.

제거 영향 평가:
- 데이터 품질: 개선 효과가 큽니다. 장문 리뷰의 핵심 문맥 보존으로 오탐/누락을 줄일 수 있습니다.
- 크롤링 속도: 소폭 저하 가능성이 있습니다. 특히 크롤러 내부에서 언어 감지/임베딩 분류를 계속 수행할 경우 텍스트 길이에 비례해 CPU 사용량이 증가합니다.
- 토큰 비용: 크롤러 자체의 직접 토큰 비용 증가는 거의 없지만, 후속 AI 요약 파이프라인 입력 길이가 늘어 전체 토큰 사용량이 증가할 수 있습니다.
- 저장/운영 비용: JSON 파일 크기와 저장소 사용량이 증가할 수 있습니다.

운영 권장안(균형 전략):
1. 크롤링 단계에서는 500자 절단을 제거하고 원문을 보존합니다.
2. 비정상 초장문 방어를 위해 안전 상한(예: 8000~12000자)을 별도 적용합니다.
3. AI 입력 전처리 단계에서 문장 단위 압축/발췌를 적용해 토큰 비용을 제어합니다.
4. 변경 전후 지표를 비교합니다.
  - 평균 본문 길이, 필터 탈락률, 수집 건수, AI 입력 토큰량, 요약 품질 샘플 점검 결과

간단 수도 코드:
```python
cleaned = normalize_text(raw_text)
if len(cleaned) < MIN_BODY_LENGTH:
    reject("too_short")
# 긴 본문도 필터 단계에서는 절단하지 않음
```

### 4-2. [높음] UNIQUE_RATIO 조건 완화

리스크:
- `UNIQUE_RATIO < 0.4`를 장문에도 동일 적용하면 감정 강조 반복이 있는 정상 리뷰 오탐 가능성이 큽니다.

권장 조치:
- 본문이 특정 길이(예: 400자) 초과 시 UNIQUE_RATIO 검사를 우회합니다.

간단 수도 코드:
```python
if len(text) <= 400:
    if unique_ratio < 0.4:
        reject("word_repetition")
else:
    log_info("unique_ratio_bypassed", text_len=len(text))
```

### 4-3. [보통] General 카테고리 도입

리스크:
- 카테고리 미매칭 리뷰를 즉시 폐기하면 유효 리뷰(새 이슈, 복합 의견)가 손실됩니다.

권장 조치:
- 1, 2단계를 통과한 리뷰는 미매칭 시에도 `General`로 보존합니다.

간단 수도 코드:
```python
matched = match_categories(text)
if not matched:
    matched = ["General"]
return pass_result(categories=matched)
```

## 완료 기준(DoD)
- 출력 파일명 규칙 준수율 100%
- 429 재시도 정책 반영 및 실패율 모니터링 가능
- 필터링 단계에서 원문 절단 제거
- 장문 UNIQUE_RATIO 예외 적용
- 카테고리 미매칭 리뷰의 `General` 보존 적용

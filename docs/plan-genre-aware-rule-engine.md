# 장르 확장형 리뷰 정제 룰 엔진 보완 계획

## 목적

현재 리뷰 요약 품질 보완은 `reduce_api.py` 안의 산발적 조건문을 `SummaryRule` 기반 순차 룰로 옮긴 1차 상태다. 이 구조는 이전보다 낫지만, 장르가 늘어날수록 코드 안에 게임별·장르별 표현이 다시 쌓일 위험이 있다.

이번 보완의 목적은 다음과 같다.

- 후보 정제 흐름을 `accept / ambiguous / reject`로 유지한다.
- 명확한 후보는 휴리스틱으로 빠르게 처리한다.
- 애매한 후보만 경량 분류기 또는 LLM으로 재판정할 수 있게 한다.
- 룰은 코드 내부 if문이 아니라 우선순위·조건·템플릿 기반 데이터로 관리한다.
- 특정 게임명 조건을 피하고, 장르와 aspect에 확장 가능한 일반 조건으로 정리한다.

## 현재 문제

1. 룰이 코드에 직접 선언되어 있다.
   - 함수 내부 if 체인은 줄었지만, 룰 데이터가 여전히 `reduce_api.py` 안에 있다.
   - 룰 추가가 코드 수정과 동일해져 장르 확장 시 회귀 위험이 커진다.

2. 일부 룰이 특정 장르 경험에 치우쳐 있다.
   - 전투, 길 찾기, 진행 장벽, 후속작 대기 같은 액션·오픈월드 중심 표현이 많다.
   - 퍼즐, 스포츠, 시뮬레이션, 전략, 카드/덱빌딩, 비주얼 노벨 등에서는 충분한 문장화가 어렵다.

3. 애매한 후보 재판정 훅은 있으나 아직 실제 분류기/LLM 연결 전 단계다.
   - 현재는 비용 없는 deterministic classifier만 사용한다.
   - 구조상 연결 지점은 확보됐지만, 품질 개선 효과는 제한적이다.

## 목표 구조

```text
Map local LLM
  -> evidence_items 생성
  -> schema/grounding repair
  -> Reduce API 요약
  -> public output 후보 생성
  -> 후보 품질 판정
       accept: 룰 적용 또는 fallback 문장화
       reject: 룰로 복구 가능한 경우만 복구, 아니면 폐기
       ambiguous: lightweight classifier 또는 LLM hook
  -> review_id grounding gate
```

Map 단계의 로컬 LLM 역할은 유지한다. 로컬 LLM은 raw review와 deterministic candidate를 보고 Reduce가 읽기 좋은 evidence token을 만드는 핵심 단계다. 룰 엔진은 Map LLM을 대체하지 않고, Reduce 이후 공개 문장 정합성을 보정한다.

## 구현 단계

### 1단계: 룰 데이터 외부화

- `ai-pipeline/ai_module/map_reduce/rules/summary_rules.json` 추가
- 각 룰은 다음 필드를 가진다.
  - `name`
  - `priority`
  - `polarity`
  - `template`
  - `any_terms`
  - `all_terms`
  - `none_terms`
  - `regex_patterns`
  - `genres`
  - `aspects`
- `reduce_api.py`는 JSON을 로드해 `SummaryRule` 객체로 변환한다.
- JSON 로드 실패 시 조용히 잘못된 요약을 만들지 않도록 예외를 발생시킨다.

### 2단계: 공통 룰과 장르 룰 분리

- 공통 룰:
  - 버그, 크래시, 저장/로드 실패
  - 최적화, 성능
  - 가격/구매 추천
  - 볼륨, 반복성
  - 조작감, 접근성
  - 사운드, 스토리
- 장르 룰:
  - action_rpg: 전투, 보스, 빌드, 진행 장벽
  - open_world: 탐험, 길 찾기, 활동 밀도
  - strategy: 밸런스, AI, 턴 흐름
  - puzzle: 문제 설계, 힌트, 난이도 곡선
  - sports/racing: 조작감, 물리, 온라인 매칭
  - simulation: 관리 루프, 경제, 반복 작업

### 3단계: 장르 컨텍스트 연결

- 현재 1차 구현에서는 장르 메타데이터만 룰에 둔다.
- 후속으로 게임 메타데이터 또는 Map evidence aspect 분포에서 `active_genres`를 추론해 `_apply_summary_rules()`에 전달한다.
- 장르 컨텍스트가 없으면 공통 룰과 기존 호환 룰을 모두 사용할 수 있게 한다.

### 4단계: ambiguous 후보 재판정

- 기본값은 deterministic classifier로 유지한다.
- 별도 옵션이 켜졌을 때만 경량 분류기 또는 로컬 LLM을 호출한다.
- Reduce 단계 Groq API 토큰 사용량 절감 목표를 해치지 않도록 원격 Reduce API에는 애매한 후보 재판정을 맡기지 않는다.

### 5단계: 검증

- 단위 테스트:
  - JSON 룰 로드
  - 우선순위 적용
  - 특정 게임명 직접 매칭 회피
  - reject 후보의 룰 기반 복구
  - unknown negative 후보 폐기
- dry-run:
  - 기존 1~2게임 gate 유지
  - 추가 게임 확보 후 서로 다른 장르 5게임 이상 검증

## 이번 작업 범위

이번 작업에서는 1단계와 2단계의 기반만 반영한다.

- 룰을 JSON 파일로 분리한다.
- `SummaryRule`에 `genres`, `aspects` 메타데이터를 추가한다.
- 현재 테스트를 유지하면서 룰 로드/우선순위 테스트를 추가한다.
- 실제 장르 컨텍스트 추론과 LLM 재판정은 후속 작업으로 남긴다.

## 완료 기준

- `reduce_api.py`에 긴 룰 목록이 직접 남아 있지 않다.
- 룰 추가가 코드 수정이 아니라 JSON 데이터 수정으로 가능하다.
- 기존 품질 테스트가 통과한다.
- 서비스 코드에는 특정 게임명 전용 조건이 추가되지 않는다.

## 후속 기획: 자동 학습형 룰 개선

자동 학습형 룰 엔진은 active rule을 모델이 직접 수정하는 방식으로 만들지 않는다. 목표는 dry-run과 실제 요약 검증에서 실패 샘플을 모으고, 반복 패턴을 rule candidate로 제안한 뒤, shadow 검증과 사람 승인을 거쳐 active rule로 승격하는 구조다.

### 목표

- 여러 게임과 장르 테스트를 반복할수록 실패 샘플이 축적된다.
- 반복적으로 나타나는 실패 유형을 자동 분류한다.
- 특정 게임명에 의존하지 않는 일반화된 룰 후보를 생성한다.
- 후보 룰은 즉시 서비스 출력에 반영하지 않고 검증 단계에 둔다.
- 검증을 통과한 룰만 사람이 승인해 active rule로 승격한다.

### 전체 흐름

```text
dry-run / 실제 요약 실행
  -> 품질 gate 결과 수집
  -> 실패/애매한 후보 JSONL 저장
  -> 실패 유형 자동 분류
  -> candidate rule 생성
  -> shadow rule로 매칭 영향 관찰
  -> 회귀 테스트와 장르 fixture 검증
  -> 사람 승인 후 active rule 승격
```

### 저장할 실패 샘플

실패 샘플은 rule mining의 근거가 되므로 review grounding 정보를 함께 저장한다.

- `game_id`
- `title`
- `genres`
- `review_id`
- `source`
- `polarity`
- `public_detail`
- `generated_sentence`
- `decision`: `accept | ambiguous | reject`
- `matched_rule`
- `failure_type`
- `gate_failures`

### 실패 유형

초기 분류는 휴리스틱으로 시작한다.

- `noise`: 욕설, 밈, 의미 없는 말투
- `too_vague`: 실제 리뷰 detail 없이 일반론만 출력
- `missing_common_rule`: 버그, 최적화, 가격, 볼륨 등 공통 룰 누락
- `missing_genre_rule`: 장르별 조작감, 밸런스, 퍼즐 설계 등 룰 누락
- `wrong_rule_match`: 기존 룰이 다른 의미의 후보에 잘못 매칭
- `over_specific_rule`: 특정 게임명이나 고유 표현에 과의존
- `grounding_mismatch`: review_id 근거와 출력 문장 의미 불일치

### 룰 상태

룰은 세 단계로 관리한다.

- `candidate`: 자동 생성된 후보. 실제 출력에는 사용하지 않는다.
- `shadow`: 실제 요약 중 매칭 여부만 기록한다. 출력에는 영향이 없다.
- `active`: 검증과 승인 후 실제 문장 생성에 사용한다.

### LLM 사용 범위

LLM은 룰을 확정하지 않고 후보 제안에만 사용한다.

허용:

- 실패 샘플 묶음의 공통 패턴 요약
- 장르와 aspect 후보 추정
- template 초안 생성
- 특정 게임명 조건 제거 제안

금지:

- active rule 직접 수정
- 단일 리뷰만 근거로 한 룰 생성
- support count 없는 룰 승격
- review_id 근거 없는 template 생성
- 특정 게임명 조건 생성

### 파일 구조 초안

```text
ai-pipeline/ai_module/map_reduce/rules/
  summary_rules.json
  candidate_rules.json
  shadow_rules.json
  rule_learning_config.json

ai-pipeline/ai_module/map_reduce/
  rule_learning.py
  rule_validation.py
```

### 승격 기준

candidate rule은 다음 조건을 만족해야 shadow 또는 active로 이동할 수 있다.

- 최소 support count 충족
- 가능하면 여러 리뷰, 여러 게임에서 반복 확인
- 특정 게임명 직접 조건 없음
- 기존 품질 테스트 통과
- dry-run gate 통과
- review_id grounding 유지
- 기존 active rule보다 오탐을 늘리지 않음

### 난이도와 현실적 범위

자동 학습형 전체 구현 난이도는 중상이다. 다만 관측형부터 시작하면 실현 가능성이 높다.

1. 관측형: 실패 샘플 저장, matched rule 기록, ambiguous/reject 후보 저장
2. 제안형: 반복 실패 패턴을 candidate rule로 생성
3. 반자동형: shadow 검증 후 사람 승인으로 active rule 승격

현재 단계에서는 이 기획을 문서로만 남기고, 실제 구현은 진행하지 않는다.

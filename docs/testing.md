---
title: 검증 범위와 재현 가능한 테스트
description: 로컬 contract, HTTP 및 SSE chaos matrix와 Azure 실환경 결과의 구분
ms.date: 2026-10-19
---

## 테스트 계층

| 계층 | 검증 대상 | 실제 Azure 호출 |
|------|-----------|-----------------|
| Unit/contract | config, headers, retry budget, deadline, concurrency, circuit와 복구 | 없음 |
| SDK integration | 실제 AsyncOpenAI + httpx MockTransport, deployment name, store=false, SSE | 없음 |
| Chaos matrix | 4개 모델 × same/mixed × 10조건, 요청별 실제 선택 경로 기록 | 없음 |
| Virtual subscriptions | 가상 두 구독 × N개 지역, 구독별 quota 가정과 14개 장애 조건 | 없음 |
| ARM preflight | 실제 기존 resource와 model deployment의 관리 정보 | GET만 사용 |
| Live smoke | 기존 모델 endpoint 소량 추론, upstream TTFT | 명시적 승인 후 과금 가능 |
| Global load/Agent E2E | 국가별 실제 사용자 지연과 부하, tool 및 검색 포함 | 현재 미수행 |

기본 chaos matrix는 현재 사용자 범위인 단일 구독입니다.
`simulate --subscriptions 2 --regions N`으로 가상 두 구독의 routing·quota pool 격리를 검증합니다.
실제 cross-subscription quota나 capacity isolation을 검증하지 않습니다.

## 10개 chaos 조건

| 조건 | Same 모드 기대 | Mixed 모드 기대 |
|------|----------------|-----------------|
| healthy | 동일 모델 성공 | 요청 모델 성공 |
| region_429 | 다른 지역의 동일 모델 | 동일 모델 우선 |
| same_model_429 | bounded failure | 승인 fallback 모델 성공·degraded 기록 |
| same_model_503 | bounded failure | 요청 모델 전체 장애 후 승인 모델 성공 |
| fallback_model_429 | bounded failure | 요청 모델과 첫 fallback 모두 장애이면 다음 승인 모델 |
| correlated_429 | bounded failure | 전체 모델이 막히면 bounded failure |
| primary_503 | 동일 모델 지역 전환 | 동일 모델 우선 |
| primary_timeout | 동일 모델 지역 전환 | 동일 모델 우선 |
| auth_401 | 한 번 시도 후 fail-fast | 타 모델·지역으로 권한 오류 은폐 금지 |
| headroom | 한 번 성공 후 공유 quota pool 선제 제한 | 요청 모델 pool 제한 후 승인 후보 |

HTTP와 SSE 모드를 각각 실행합니다.
`passed=true`는 이 기대 결과에 부합했다는 뜻이며 모든 요청 성공을 뜻하지 않습니다.
각 row에는 성공·실패 수, 실제 mock HTTP attempt 수, 최대 attempt, degraded 수와 model invariant가 있습니다.
`samples`에는 논리 요청마다 성공 여부, 선택 모델·지역 별칭, `output_verified`,
실패 원인과 실제 HTTP 시도 순서가 포함됩니다.
첫 텍스트 이후 stream disconnect는 replay 없이 호출자에게 전파되는지도 별도 검사합니다.

두 가상 구독에서는 `subscription_429`, `subscription_503`,
`subscription_request_headroom`, `subscription_token_headroom` 조건을 추가합니다.
한 구독의 headroom 소진이 같은 구독의 다른 지역을 차단하면서
다른 구독의 동일 모델은 차단하지 않는지 직접 예산 객체와 실제 SDK 호출을 함께 검증합니다.
결과의 `topology`에는 구독·지역 별칭, backend 수, quota pool 수와 가정을 기록합니다.
`samples`의 선택 구독과 각 HTTP attempt의 구독도 포함합니다.
현재 공유 JSON에는 단일·두 구독 모두 topology 및 시도별 구독 metadata를 포함합니다.

```powershell
uv run python -m pytest -q
uv run python -m foundry_resiliency simulate --count 10 --output artifacts\resilience-http.json
uv run python -m foundry_resiliency simulate --stream --count 10 --output artifacts\resilience-sse.json
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
az bicep build --file infra\main.bicep --outfile artifacts\main.json
```

```powershell
uv run python -m foundry_resiliency simulate --subscriptions 2 --regions 4 --count 10 `
  --output artifacts\two-subscriptions-http.json
uv run python -m foundry_resiliency simulate --subscriptions 2 --regions 4 --stream --count 10 `
  --output artifacts\two-subscriptions-sse.json
```

N은 각 구독에 배치한 지역 개수이며, 1~16 범위입니다.
N=4 테스트는 32개 backend와 subscription/model/version 기준 8개 quota pool을 사용합니다.
두 protocol 합계 2,240개 요청에서 1,680개 성공, 560개 기대된 실패를 관측했습니다.
구독 단위 429/503/요청·토큰 headroom의 네 조건에서는 640/640 성공,
모든 후보의 상관된 429 조건에서는 0/160 성공을 확인했습니다.
원시 결과는 [두 구독 HTTP](results/two-subscriptions-http.json)와
[두 구독 SSE](results/two-subscriptions-sse.json)에 있습니다.
모든 성공은 텍스트·stream 완료까지 검증하며 실제 Azure 호출은 없습니다.

시도 상한은 최대 20회를 유지합니다.
정상 후보까지 필요한 시도가 상한을 넘는 큰 N에서는 기대 결과에 `attempt_budget` 실패가 포함됩니다.
각 요청은 독립적인 시도 예산을 가지며 후속 요청은 같은 router의 cooldown 상태를 재사용합니다.
이 연속 요청 테스트는 처리량 또는 동시 부하 성능 시험이 아닙니다.

## 실제 검증과 결과 저장

로컬 실측 결과는 [verification.json](verification.json)에 기록합니다.
모의 TTFT는 SDK·로컬 코드의 시간이며 모델·네트워크 성능 증거가 아닙니다.
공유 가능한 모의 장애 결과는 [HTTP JSON](results/resilience-http.json)과
[SSE JSON](results/resilience-sse.json)에 저장했습니다.
각각 80개 조건/800개 요청에서 484개 성공, 316개 기대된 실패를 관측했습니다.
README의 집계 표와 저장된 JSON이 일치하는지 회귀 테스트로 확인합니다.
다시 실행한 결과나 실환경 상세 JSON은 `artifacts`에 저장하고 고객 데이터나 키를 넣지 않습니다.
공유 결과는 실제 endpoint·국가·Azure location 없이 Region01~Region04만 포함합니다.
실험 정책은 결과의 `policy`에 포함되며, 60초 backoff와 고정 jitter 샘플로 반복 호출 수를 일정하게 합니다.
실험은 15초 deadline·10초 attempt/첫 텍스트 제한을 사용합니다.
`timing_mode=virtual_policy_clock`으로 정책 시계를 분리하여 SDK 초기화·호스트 스케줄링 지연이
모의 remaining/reset·cooldown·deadline 판정을 바꾸지 않도록 합니다.
가상 시계는 모의 대기에서만 진행하며, 즉시 응답하는 모의 요청의 `first_token_ms=0`은
실제 Azure TTFT가 0이라는 의미가 아닙니다. `elapsed_ms`는 호스트의 실제 실험 경과 시간입니다.
실제 라우터는 그대로 monotonic clock과 asyncio timeout을 사용하며,
실제 timeout·deadline과 동시성·취소 동작은 별도 회귀 테스트로 검증합니다.
모의 결과로 latency 목표 합격이나 부하 시험을 주장하지 않습니다.
pytest의 fake clock 테스트는 긴 Retry-After를 실제로 기다리지 않고 규칙을 검증합니다.
SSE 테스트는 HTTP chunk를 텍스트 토큰으로 잘못 계산하지 않습니다.

SVG는 XML·접근성 검사와 별도로 브라우저에서 실제 글자 경계를 측정합니다.
전체 viewBox뿐 아니라 각 라벨이 속한 가장 작은 박스에 대해 SVG 좌표 기준 좌우 12, 상하 8의 여백을 확인합니다.
Segoe UI, Arial, sans-serif 각각에서 `document.fonts.ready`를 기다린 후
박스 밖 글자와 같은 박스 내 텍스트 겹침도 검사합니다.
이 브라우저 치수 검증은 pytest의 정적 SVG 검사와 구분하며, 결과는 검증 기록의 `svg_render`에 남깁니다.

`smoke`는 `--confirm-paid` 없이는 호출하지 않습니다.
실제 endpoint가 없다면 live test는 `not_run`으로 남깁니다.
preflight는 quota Scope·사용 가능 capacity·전체 Agent 기능을 검증하지 않습니다.

## 실환경 합격 기준 수립

실제 SLO는 업무별로 합의합니다. 예를 들어 국가별 p95 TTFT 상한, deadline 내 성공률,
429 비율, failover 추가 지연, degraded 응답 품질·비용, 복구 시 재진입 비율을 따로 정의합니다.
성공 요청만 모아 latency를 계산하지 말고 timeout과 실패율을 함께 보고합니다.

실제 장애 테스트는 전용 test deployment, 비용 상한과 중단 조건 아래 수행합니다.
HTTP 429는 로컬 fault injection, 제한된 test proxy, 실제 service 관측 중 무엇인지 구분합니다.
실제 shared resource pool을 고갈시키는 시험은 수행하지 않습니다.
모든 모델의 실제 context·tools·안전성·언어 품질 평가는 별도 검증 대상입니다.

## 개발 환경 제한 처리

초기 dependency restore에서 `files.pythonhosted.org` TLS handshake가 실패할 수 있습니다.
인증서 검증을 끄거나 `trusted-host`로 우회하지 마세요.
이 경우 네트워크를 수정한 뒤 `uv sync`를 재실행합니다.
이미 승인된 Python 환경을 사용할 때는 버전과 validation 방법을 결과에 명시합니다.
의존성 설치 실패를 테스트 성공으로 기록하지 않습니다.

이 작업에서는 시스템의 기존 승인 package feed를 사용해 프로젝트 `.venv`에 의존성을 복원했습니다.
공유 Python 설치나 인증서 검증 설정은 변경하지 않았습니다.
현재 workspace의 검증 환경을 그대로 재사용하려면 자동 dependency sync 없이 실행할 수 있습니다.

```powershell
uv run --no-sync python -m pytest -q
uv run --no-sync python -m foundry_resiliency simulate --stream --output artifacts\streaming.json
```

기본 public index의 `uv sync` 성공과 위 복원 경로의 성공은 구분합니다.
외부 배포 가능한 dependency lock은 생성하지 않았으며, 실제 검증 버전은 결과 JSON에 기록합니다.
